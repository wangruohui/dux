from __future__ import annotations

import fnmatch
import os
import queue
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import db
from .model import NodeRecord
from .scanner import ScanResult, scan_subtree_to_db


LIVE_CHILD_LIMIT = 200


@dataclass(slots=True)
class IndexResult:
    root: NodeRecord
    scan: ScanResult


@dataclass(slots=True)
class FilterResult:
    paths: list[str]
    scanned_dirs: int
    elapsed_seconds: float
    indexed_matches: int
    live_only_matches: int
    stale_index_matches: int


class DeleteCancelled(RuntimeError):
    def __init__(self, completed_targets: list[str]) -> None:
        super().__init__("delete cancelled after synchronizing the index")
        self.completed_targets = completed_targets


class FilterCancelled(RuntimeError):
    pass


class DuxService:
    def __init__(
        self,
        db_path: str | Path | None = None,
        max_workers: int = 256,
        delete_slots: threading.BoundedSemaphore | None = None,
    ) -> None:
        self.db_path = Path(db_path or db.DEFAULT_DB_PATH).expanduser()
        self.conn = db.connect(self.db_path)
        self.max_workers = max_workers
        self.delete_slots = delete_slots or threading.BoundedSemaphore(256)

    def close(self) -> None:
        self.conn.close()

    def canonical(self, path: str | os.PathLike[str]) -> str:
        return str(Path(path).expanduser().resolve())

    def filter_paths(
        self,
        path: str,
        keyword: str,
        exclude: str = "",
        progress: Callable[[int, int, str], None] | None = None,
        progress_interval: int = 100,
        cancel_event: threading.Event | None = None,
    ) -> FilterResult:
        if not keyword:
            raise ValueError("filter keyword must not be empty")

        root = self.canonical(path)
        if not Path(root).is_dir():
            raise NotADirectoryError(root)

        def cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        if cancelled():
            raise FilterCancelled("filter cancelled")

        started_at = time.monotonic()
        filter_conn = db.connect(self.db_path)
        filter_conn.set_progress_handler(lambda: int(cancelled()), 1000)
        try:
            try:
                indexed_rows = db.fetch_filter_candidates(filter_conn, root, keyword)
            except sqlite3.OperationalError as exc:
                if cancelled():
                    raise FilterCancelled("filter cancelled") from exc
                raise
        finally:
            filter_conn.close()
        if cancelled():
            raise FilterCancelled("filter cancelled")
        indexed_candidates: list[tuple[str, bool]] = []
        for row in indexed_rows:
            if cancelled():
                raise FilterCancelled("filter cancelled")
            candidate = str(row["path"])
            if exclude and exclude in os.path.relpath(candidate, root):
                continue
            if fnmatch.fnmatchcase(str(row["name"]), keyword):
                indexed_candidates.append((candidate, bool(row["is_dir"])))

        indexed_live: dict[str, bool] = {}
        stale_index_matches = 0
        matched_index_dirs: list[str] = []
        for candidate, is_dir in sorted(
            indexed_candidates,
            key=lambda item: (len(Path(item[0]).parts), item[0]),
        ):
            if cancelled():
                raise FilterCancelled("filter cancelled")
            if any(
                candidate.startswith(matched_dir.rstrip("/") + "/")
                for matched_dir in matched_index_dirs
            ):
                continue
            if is_dir:
                matched_index_dirs.append(candidate)
            if not os.path.lexists(candidate):
                stale_index_matches += 1
                continue
            indexed_live[candidate] = is_dir

        work: queue.Queue[str | None] = queue.Queue()
        work.put(root)
        matches: dict[str, bool] = {}
        matches_lock = threading.Lock()
        progress_lock = threading.Lock()
        stop_event = threading.Event()
        errors: list[BaseException] = []
        error_lock = threading.Lock()
        scanned_dirs = 0

        def record_error(exc: BaseException) -> None:
            with error_lock:
                if not errors:
                    errors.append(exc)
            stop_event.set()

        def handle_dir(directory: str) -> None:
            nonlocal scanned_dirs
            with os.scandir(directory) as entries:
                for entry in entries:
                    if stop_event.is_set() or cancelled():
                        stop_event.set()
                        return
                    if exclude and exclude in os.path.relpath(entry.path, root):
                        continue
                    is_dir = entry.is_dir(follow_symlinks=False)
                    if fnmatch.fnmatchcase(entry.name, keyword):
                        with matches_lock:
                            matches[entry.path] = is_dir
                        continue
                    if is_dir:
                        work.put(entry.path)

            if cancelled():
                stop_event.set()
                return
            with progress_lock:
                scanned_dirs += 1
                current_dirs = scanned_dirs
            with matches_lock:
                current_matches = len(set(indexed_live).union(matches))
            if progress is not None and progress_interval > 0 and current_dirs % progress_interval == 0:
                progress(current_dirs, current_matches, directory)

        def worker() -> None:
            while True:
                directory = work.get()
                try:
                    if directory is None:
                        return
                    if stop_event.is_set() or cancelled():
                        stop_event.set()
                        continue
                    try:
                        handle_dir(directory)
                    except FileNotFoundError:
                        pass
                    except BaseException as exc:
                        record_error(exc)
                finally:
                    work.task_done()

        with ThreadPoolExecutor(max_workers=max(1, self.max_workers)) as pool:
            futures = [pool.submit(worker) for _ in range(max(1, self.max_workers))]
            work.join()
            for _ in futures:
                work.put(None)
            work.join()
            for future in futures:
                future.result()

        if errors:
            raise errors[0]
        if cancelled():
            raise FilterCancelled("filter cancelled")
        combined = set(indexed_live).union(matches)
        final_paths: list[str] = []
        matched_dirs: list[str] = []
        for candidate in sorted(combined, key=lambda item: (len(Path(item).parts), item)):
            if cancelled():
                raise FilterCancelled("filter cancelled")
            if any(
                candidate.startswith(matched_dir.rstrip("/") + "/")
                for matched_dir in matched_dirs
            ):
                continue
            final_paths.append(candidate)
            if matches.get(candidate, indexed_live.get(candidate, False)):
                matched_dirs.append(candidate)
        final_path_set = set(final_paths)
        return FilterResult(
            paths=sorted(final_paths),
            scanned_dirs=scanned_dirs,
            elapsed_seconds=time.monotonic() - started_at,
            indexed_matches=len(final_path_set.intersection(indexed_live)),
            live_only_matches=len(final_path_set.intersection(matches).difference(indexed_live)),
            stale_index_matches=stale_index_matches,
        )

    def index_path(
        self,
        path: str,
        progress: Callable[[int, str], None] | None = None,
        progress_interval: int = 10000,
        lock_status: Callable[[str], None] | None = None,
    ) -> IndexResult:
        root = self.canonical(path)
        staging_path = self._create_staging_db_path()
        try:
            staging_conn = db.connect(staging_path)
            try:
                with staging_conn:
                    scan = scan_subtree_to_db(
                        staging_conn,
                        root,
                        max_workers=self.max_workers,
                        progress=progress,
                        progress_interval=progress_interval,
                    )
                    db.aggregate_subtree(staging_conn, root)
                    new_root = db.fetch_node(staging_conn, root)
                    if new_root is None:
                        raise FileNotFoundError(root)
                staging_conn.execute("PRAGMA wal_checkpoint(FULL)")
            finally:
                staging_conn.close()
            self._swap_indexed_subtree(root, staging_path, new_root, lock_status=lock_status)
        finally:
            self._remove_staging_db(staging_path)

        root_record = NodeRecord(
            path=new_root["path"],
            parent_path=new_root["parent_path"],
            name=new_root["name"],
            is_dir=bool(new_root["is_dir"]),
            indexed=bool(new_root["indexed"]),
            depth=int(new_root["depth"]),
            size_bytes=int(new_root["size_bytes"]),
            file_count=int(new_root["file_count"]),
            dir_count=int(new_root["dir_count"]),
        )
        return IndexResult(root=root_record, scan=scan)

    def _swap_indexed_subtree(
        self,
        root: str,
        staging_path: Path,
        new_root: sqlite3.Row,
        lock_status: Callable[[str], None] | None = None,
    ) -> None:
        alias = "staging_index"
        attached = False
        db.attach_database(self.conn, staging_path, alias)
        attached = True
        try:
            new_size = int(new_root["size_bytes"])
            new_files = int(new_root["file_count"])
            new_dirs = int(new_root["dir_count"])

            with db.writer_transaction(
                self.conn,
                "index-merge",
                root,
                on_wait=lock_status,
            ):
                old_root = db.fetch_node(self.conn, root)
                old_size = int(old_root["size_bytes"]) if old_root else 0
                old_files = int(old_root["file_count"]) if old_root else 0
                old_dirs = int(old_root["dir_count"]) if old_root else 0
                db.ensure_ancestor_placeholders(self.conn, root)
                db.delete_subtree_rows(self.conn, root)
                db.insert_subtree_from_attached(self.conn, alias, root)
                size_delta = new_size - old_size
                file_delta = new_files - old_files
                dir_delta = new_dirs - old_dirs
                if size_delta or file_delta or dir_delta:
                    db.apply_delta_to_ancestors(self.conn, root, size_delta, file_delta, dir_delta)
        finally:
            if attached:
                db.detach_database(self.conn, alias)

    def _create_staging_db_path(self) -> Path:
        handle = tempfile.NamedTemporaryFile(prefix="dux-index-", suffix=".db", delete=False)
        handle.close()
        return Path(handle.name)

    def _remove_staging_db(self, path: Path) -> None:
        for suffix in ("", "-wal", "-shm", ".writer.lock"):
            try:
                path.with_name(path.name + suffix).unlink()
            except FileNotFoundError:
                pass

    def delete_path(
        self,
        path: str,
        *,
        permanent: bool = False,
        trash: bool = False,
        progress: Callable[[int, str], None] | None = None,
        progress_interval: int = 1000,
        unlink_workers: int = 8,
        cancel_event: threading.Event | None = None,
        status: Callable[[str, str], None] | None = None,
    ) -> str:
        def report(_target: str, count: int, current_path: str) -> None:
            if progress is not None:
                progress(count, current_path)

        destinations = self.delete_paths(
            [path],
            permanent=permanent,
            trash=trash,
            progress=report,
            progress_interval=progress_interval,
            workers=1,
            unlink_workers=unlink_workers,
            cancel_event=cancel_event,
            status=status,
        )
        return destinations[0]

    def delete_paths(
        self,
        paths: list[str],
        *,
        permanent: bool = False,
        trash: bool = False,
        progress: Callable[[str, int, str], None] | None = None,
        status: Callable[[str, str], None] | None = None,
        progress_interval: int = 1000,
        workers: int = 2,
        unlink_workers: int = 8,
        cancel_event: threading.Event | None = None,
    ) -> list[str]:
        if permanent == trash:
            raise ValueError("choose exactly one of permanent or trash")

        operation_cancel = cancel_event or threading.Event()
        targets: list[tuple[str, bool]] = []
        for path in paths:
            target = self.canonical(path)
            row = db.fetch_node(self.conn, target)
            if row is None and not Path(target).exists():
                raise FileNotFoundError(target)
            targets.append((target, row is not None))

        deleted_queue: queue.Queue[tuple[str, bool] | None] = queue.Queue(maxsize=65536)
        writer_errors: list[BaseException] = []
        target_summary = ", ".join(target for target, _indexed in targets[:3])
        if len(targets) > 3:
            target_summary += f", ... and {len(targets) - 3} more"

        def report_lock_wait(owner: str) -> None:
            if status is not None:
                status(target_summary, f"waiting-lock:{owner}")

        def write_deleted(path: str, is_dir: bool) -> None:
            while True:
                if writer_errors:
                    raise writer_errors[0]
                try:
                    deleted_queue.put((path, is_dir), timeout=0.1)
                    return
                except queue.Full:
                    continue

        def index_writer() -> None:
            batch: list[tuple[str, bool]] = []
            last_flush = time.monotonic()

            def flush() -> None:
                nonlocal last_flush
                if batch:
                    db.delete_nodes_incremental(
                        self.conn,
                        batch,
                        target_summary,
                        on_wait=report_lock_wait,
                    )
                    batch.clear()
                last_flush = time.monotonic()

            try:
                while True:
                    timeout = max(0.0, 0.2 - (time.monotonic() - last_flush))
                    try:
                        item = deleted_queue.get(timeout=timeout)
                    except queue.Empty:
                        flush()
                        continue
                    if item is None:
                        flush()
                        return
                    batch.append(item)
                    if len(batch) >= 5000 or time.monotonic() - last_flush >= 0.2:
                        flush()
            except BaseException as exc:
                writer_errors.append(exc)
                operation_cancel.set()

        def remove_target(target: str) -> tuple[str, bool]:
            if trash:
                if operation_cancel.is_set():
                    return "", False
                return self._move_to_trash(target), True

            def report(count: int, current_path: str) -> None:
                if progress is not None:
                    progress(target, count, current_path)

            completed = self._remove_from_fs(
                target,
                progress=report,
                progress_interval=progress_interval,
                unlink_workers=unlink_workers,
                cancel_event=operation_cancel,
                deleted=write_deleted,
            )
            return "", completed

        destinations: list[str] = []
        completed_targets: list[str] = []
        was_cancelled = False
        delete_errors: list[BaseException] = []
        writer_thread: threading.Thread | None = None
        if permanent:
            writer_thread = threading.Thread(target=index_writer, name="dux-delete-index-writer")
            writer_thread.start()
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(remove_target, target): (target, indexed) for target, indexed in targets}
            for future in as_completed(futures):
                target, indexed = futures[future]
                try:
                    destination, completed = future.result()
                except BaseException as exc:
                    operation_cancel.set()
                    delete_errors.append(exc)
                    continue
                destinations.append(destination)
                if completed:
                    completed_targets.append(target)
                else:
                    was_cancelled = True

        if writer_thread is not None:
            if status is not None:
                for target, _indexed in targets:
                    status(target, "flushing-index")
            while writer_thread.is_alive() and not writer_errors:
                try:
                    deleted_queue.put(None, timeout=0.1)
                    break
                except queue.Full:
                    continue
            writer_thread.join()
        if writer_errors:
            raise writer_errors[0]

        indexed_targets = dict(targets)
        for target in completed_targets:
            if indexed_targets[target]:
                self._delete_subtree_from_index(target, lock_status=report_lock_wait)
            if status is not None:
                status(target, "index-synced")

        if delete_errors:
            raise delete_errors[0]
        if was_cancelled:
            raise DeleteCancelled(completed_targets)
        return destinations

    def list_children(self, path: str, sort_by: str = "size", reverse: bool = True) -> list[sqlite3.Row]:
        root = self.canonical(path)
        rows = db.fetch_children(self.conn, root)
        if sort_by == "name":
            return sorted(rows, key=lambda row: row["name"], reverse=reverse)
        if sort_by == "count":
            return sorted(rows, key=lambda row: (row["file_count"], row["name"]), reverse=reverse)
        if sort_by == "dircount":
            return sorted(rows, key=lambda row: (row["dir_count"], row["name"]), reverse=reverse)
        return sorted(rows, key=lambda row: (row["size_bytes"], row["name"]), reverse=reverse)

    def has_node(self, path: str) -> bool:
        return db.fetch_node(self.conn, self.canonical(path)) is not None

    def get_node(self, path: str) -> sqlite3.Row | None:
        return db.fetch_node(self.conn, self.canonical(path))

    def ensure_navigation_path(self, path: str) -> None:
        root = self.canonical(path)
        with db.writer_transaction(self.conn, "ensure-navigation", root):
            db.ensure_ancestor_placeholders(self.conn, root)
            db.refresh_placeholder_ancestor_aggregates(self.conn, root)

    def list_visible_children(
        self,
        path: str,
        sort_by: str = "size",
        reverse: bool = True,
        live_limit: int = LIVE_CHILD_LIMIT,
    ) -> tuple[list[dict[str, object]], bool]:
        root = self.canonical(path)
        by_path: dict[str, dict[str, object]] = {}
        for row in db.fetch_children(self.conn, root):
            by_path[row["path"]] = {
                "path": row["path"],
                "name": row["name"],
                "is_dir": bool(row["is_dir"]),
                "indexed": bool(row["indexed"]),
                "live_only": False,
                "size_bytes": int(row["size_bytes"]),
                "file_count": int(row["file_count"]),
                "dir_count": int(row["dir_count"]),
                "mtime": self._safe_mtime(row["path"]),
            }

        truncated = False
        seen_live = 0
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if seen_live >= live_limit:
                        truncated = True
                        break
                    seen_live += 1
                    child_path = entry.path
                    if child_path in by_path:
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    by_path[child_path] = {
                        "path": child_path,
                        "name": entry.name,
                        "is_dir": stat.S_ISDIR(st.st_mode),
                        "indexed": False,
                        "live_only": True,
                        "size_bytes": None,
                        "file_count": None,
                        "dir_count": None,
                        "mtime": float(st.st_mtime),
                    }
        except OSError:
            pass

        rows = list(by_path.values())
        if sort_by == "name":
            rows.sort(key=lambda row: str(row["name"]), reverse=reverse)
        elif sort_by == "count":
            rows.sort(
                key=lambda row: (
                    row["file_count"] is not None,
                    int(row["file_count"] or 0),
                    str(row["name"]),
                ),
                reverse=reverse,
            )
        elif sort_by == "mtime":
            rows.sort(key=lambda row: (float(row["mtime"] or 0.0), str(row["name"])), reverse=reverse)
        else:
            rows.sort(
                key=lambda row: (
                    row["size_bytes"] is not None,
                    int(row["size_bytes"] or 0),
                    str(row["name"]),
                ),
                reverse=reverse,
            )
        rows.sort(key=lambda row: bool(row["indexed"]), reverse=True)
        return rows, truncated

    def stat_visible_children(self, path: str) -> dict[str, float]:
        result: dict[str, float] = {}
        for row in db.fetch_children(self.conn, self.canonical(path)):
            result[row["path"]] = self._safe_mtime(row["path"])
        return result

    def _safe_mtime(self, path: str) -> float:
        try:
            return Path(path).lstat().st_mtime
        except OSError:
            return 0.0

    def _replace_subtree(self, root: str, nodes: dict[str, NodeRecord]) -> None:
        old_root = db.fetch_node(self.conn, root)
        old_size = int(old_root["size_bytes"]) if old_root else 0
        old_files = int(old_root["file_count"]) if old_root else 0
        old_dirs = int(old_root["dir_count"]) if old_root else 0

        new_root = nodes[root]
        size_delta = new_root.size_bytes - old_size
        file_delta = new_root.file_count - old_files
        dir_delta = new_root.dir_count - old_dirs

        with db.writer_transaction(self.conn, "replace-subtree", root):
            db.delete_subtree_rows(self.conn, root)
            db.upsert_nodes(self.conn, nodes)
            if size_delta or file_delta or dir_delta:
                db.apply_delta_to_ancestors(self.conn, root, size_delta, file_delta, dir_delta)

    def _delete_subtree_from_index(
        self,
        root: str,
        lock_status: Callable[[str], None] | None = None,
    ) -> None:
        row = db.fetch_node(self.conn, root)
        if row is None:
            return
        size_delta = -int(row["size_bytes"])
        file_delta = -int(row["file_count"])
        dir_delta = -int(row["dir_count"])
        if bool(row["is_dir"]):
            dir_delta -= 1

        with db.writer_transaction(
            self.conn,
            "delete-subtree",
            root,
            on_wait=lock_status,
        ):
            db.delete_subtree_rows(self.conn, root)
            db.apply_delta_to_ancestors(self.conn, root, size_delta, file_delta, dir_delta)

    def _move_to_trash(self, path: str) -> str:
        home = Path.home().resolve()
        source = Path(path)
        trash_root = home / "trash"
        try:
            rel = source.relative_to(home)
            destination = trash_root / rel
        except ValueError:
            destination = trash_root / source.relative_to("/")
        if destination.exists():
            suffix = time.strftime("%Y%m%d_%H%M%S")
            destination = destination.with_name(f"{destination.name}.{suffix}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return str(destination)

    def _remove_from_fs(
        self,
        path: str,
        *,
        progress: Callable[[int, str], None] | None = None,
        progress_interval: int = 1000,
        unlink_workers: int = 8,
        cancel_event: threading.Event | None = None,
        deleted: Callable[[str, bool], None] | None = None,
    ) -> bool:
        target = Path(path)
        if target.is_dir() and not target.is_symlink():
            count, completed = self._remove_dir_parallel(
                target,
                progress=progress,
                progress_interval=progress_interval,
                workers=unlink_workers,
                cancel_event=cancel_event,
                deleted=deleted,
            )
            if progress:
                progress(count, str(target))
            return completed
        if cancel_event is not None and cancel_event.is_set():
            return False
        else:
            with self.delete_slots:
                if cancel_event is not None and cancel_event.is_set():
                    return False
                target.unlink()
            if deleted is not None:
                deleted(str(target), False)
            if progress:
                progress(1, str(target))
            return True

    @staticmethod
    def _unlink_path(path: Path) -> str:
        path.unlink()
        return str(path)

    def _remove_dir_parallel(
        self,
        target: Path,
        *,
        progress: Callable[[int, str], None] | None,
        progress_interval: int,
        workers: int,
        cancel_event: threading.Event | None,
        deleted: Callable[[str, bool], None] | None,
    ) -> tuple[int, bool]:
        worker_count = max(1, workers)
        dir_queue: queue.Queue[Path | None] = queue.Queue()
        unlink_queue: queue.Queue[str | None] = queue.Queue(maxsize=worker_count * 16)
        dir_queue.put(target)
        dirs: list[Path] = []
        dirs_lock = threading.Lock()
        count_lock = threading.Lock()
        errors: list[BaseException] = []
        error_lock = threading.Lock()
        stop_event = threading.Event()
        count = 0

        def cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        def should_stop() -> bool:
            return stop_event.is_set() or cancelled()

        def bump(current_path: str) -> None:
            nonlocal count
            with count_lock:
                count += 1
                current = count
            if progress and progress_interval > 0 and current % progress_interval == 0:
                progress(current, current_path)

        def record_error(exc: BaseException) -> None:
            with error_lock:
                if not errors:
                    errors.append(exc)
            stop_event.set()

        def queue_unlink(path: str) -> None:
            while not should_stop():
                try:
                    unlink_queue.put(path, timeout=0.1)
                    return
                except queue.Full:
                    continue

        def scan_worker() -> None:
            while True:
                current_dir = dir_queue.get()
                try:
                    if current_dir is None:
                        return
                    if should_stop():
                        continue
                    with dirs_lock:
                        dirs.append(current_dir)
                    try:
                        child_dirs: list[Path] = []
                        child_files: list[str] = []
                        with self.delete_slots:
                            if should_stop():
                                continue
                            with os.scandir(current_dir) as entries:
                                for entry in entries:
                                    if should_stop():
                                        break
                                    if entry.is_dir(follow_symlinks=False):
                                        child_dirs.append(Path(entry.path))
                                    else:
                                        child_files.append(entry.path)
                        for child_dir in child_dirs:
                            dir_queue.put(child_dir)
                        for child_file in child_files:
                            queue_unlink(child_file)
                    except BaseException as exc:
                        record_error(exc)
                finally:
                    dir_queue.task_done()

        def unlink_worker() -> None:
            while True:
                path = unlink_queue.get()
                try:
                    if path is None:
                        return
                    if should_stop():
                        continue
                    try:
                        with self.delete_slots:
                            if should_stop():
                                continue
                            self._unlink_path(Path(path))
                        if deleted is not None:
                            deleted(path, False)
                        bump(path)
                    except BaseException as exc:
                        record_error(exc)
                finally:
                    unlink_queue.task_done()

        if worker_count == 1:
            scan_worker_count = 1
            unlink_worker_count = 1
        else:
            scan_worker_count = min(32, max(1, worker_count // 8))
            unlink_worker_count = worker_count - scan_worker_count

        pool_worker_count = scan_worker_count + unlink_worker_count
        with ThreadPoolExecutor(max_workers=pool_worker_count) as pool:
            scan_futures = [pool.submit(scan_worker) for _ in range(scan_worker_count)]
            unlink_futures = [pool.submit(unlink_worker) for _ in range(unlink_worker_count)]
            dir_queue.join()
            for _ in scan_futures:
                dir_queue.put(None)
            dir_queue.join()
            unlink_queue.join()
            for _ in unlink_futures:
                unlink_queue.put(None)
            unlink_queue.join()
            for future in scan_futures:
                future.result()
            for future in unlink_futures:
                future.result()

        if errors:
            raise errors[0]

        if cancelled():
            return count, not target.exists()

        dirs_by_depth: dict[int, list[Path]] = defaultdict(list)
        for dir_path in dirs:
            dirs_by_depth[len(dir_path.parts)].append(dir_path)

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            for depth in sorted(dirs_by_depth, reverse=True):
                if cancelled():
                    break
                future_to_dir = {
                    pool.submit(self._rmdir_with_slot, dir_path, cancel_event): dir_path
                    for dir_path in dirs_by_depth[depth]
                }
                for future in as_completed(future_to_dir):
                    dir_path = future_to_dir[future]
                    if future.result():
                        if deleted is not None and dir_path != target:
                            deleted(str(dir_path), True)
                        bump(str(dir_path))

        return count, not target.exists()

    def _rmdir_with_slot(self, path: Path, cancel_event: threading.Event | None) -> bool:
        with self.delete_slots:
            if cancel_event is not None and cancel_event.is_set():
                return False
            path.rmdir()
        return True

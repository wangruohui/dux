from __future__ import annotations

import os
import queue
import shutil
import sqlite3
import stat
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


class DuvizService:
    def __init__(self, db_path: str | Path | None = None, max_workers: int = 8) -> None:
        self.conn = db.connect(db_path)
        self.max_workers = max_workers

    def close(self) -> None:
        self.conn.close()

    def canonical(self, path: str | os.PathLike[str]) -> str:
        return str(Path(path).expanduser().resolve())

    def index_path(
        self,
        path: str,
        progress: Callable[[int, str], None] | None = None,
        progress_interval: int = 10000,
    ) -> IndexResult:
        root = self.canonical(path)
        old_root = db.fetch_node(self.conn, root)
        old_size = int(old_root["size_bytes"]) if old_root else 0
        old_files = int(old_root["file_count"]) if old_root else 0
        old_dirs = int(old_root["dir_count"]) if old_root else 0

        with self.conn:
            db.ensure_ancestor_placeholders(self.conn, root)
            db.delete_subtree_rows(self.conn, root)
            scan = scan_subtree_to_db(
                self.conn,
                root,
                max_workers=self.max_workers,
                progress=progress,
                progress_interval=progress_interval,
            )
            db.aggregate_subtree(self.conn, root)
            new_root = db.fetch_node(self.conn, root)
            if new_root is None:
                raise FileNotFoundError(root)
            size_delta = int(new_root["size_bytes"]) - old_size
            file_delta = int(new_root["file_count"]) - old_files
            dir_delta = int(new_root["dir_count"]) - old_dirs
            if size_delta or file_delta or dir_delta:
                db.apply_delta_to_ancestors(self.conn, root, size_delta, file_delta, dir_delta)

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

    def delete_path(
        self,
        path: str,
        *,
        permanent: bool = False,
        trash: bool = False,
        progress: Callable[[int, str], None] | None = None,
        progress_interval: int = 1000,
        unlink_workers: int = 8,
    ) -> str:
        if permanent == trash:
            raise ValueError("choose exactly one of permanent or trash")

        target = self.canonical(path)
        row = db.fetch_node(self.conn, target)
        if row is None and not Path(target).exists():
            raise FileNotFoundError(target)

        destination = ""
        if trash:
            destination = self._move_to_trash(target)
        elif permanent:
            self._remove_from_fs(
                target,
                progress=progress,
                progress_interval=progress_interval,
                unlink_workers=unlink_workers,
            )

        if row is not None:
            self._delete_subtree_from_index(target)
        return destination

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
    ) -> list[str]:
        if permanent == trash:
            raise ValueError("choose exactly one of permanent or trash")

        targets: list[tuple[str, bool]] = []
        for path in paths:
            target = self.canonical(path)
            row = db.fetch_node(self.conn, target)
            if row is None and not Path(target).exists():
                raise FileNotFoundError(target)
            targets.append((target, row is not None))

        def remove_target(target: str) -> str:
            if trash:
                return self._move_to_trash(target)

            def report(count: int, current_path: str) -> None:
                if progress is not None:
                    progress(target, count, current_path)

            self._remove_from_fs(
                target,
                progress=report,
                progress_interval=progress_interval,
                unlink_workers=unlink_workers,
            )
            return ""

        destinations: list[str] = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(remove_target, target): (target, indexed) for target, indexed in targets}
            for future in as_completed(futures):
                target, indexed = futures[future]
                destination = future.result()
                destinations.append(destination)
                if indexed:
                    if status is not None:
                        status(target, "updating-index")
                    self._delete_subtree_from_index(target)
                    if status is not None:
                        status(target, "index-updated")
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
        with self.conn:
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

        with self.conn:
            db.delete_subtree_rows(self.conn, root)
            db.upsert_nodes(self.conn, nodes)
            if size_delta or file_delta or dir_delta:
                db.apply_delta_to_ancestors(self.conn, root, size_delta, file_delta, dir_delta)

    def _delete_subtree_from_index(self, root: str) -> None:
        row = db.fetch_node(self.conn, root)
        if row is None:
            return
        size_delta = -int(row["size_bytes"])
        file_delta = -int(row["file_count"])
        dir_delta = -int(row["dir_count"])
        if bool(row["is_dir"]):
            dir_delta -= 1

        with self.conn:
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
    ) -> None:
        target = Path(path)
        if target.is_dir() and not target.is_symlink():
            count = self._remove_dir_parallel(
                target,
                progress=progress,
                progress_interval=progress_interval,
                workers=unlink_workers,
            )
            if progress:
                progress(count, str(target))
        else:
            target.unlink()
            if progress:
                progress(1, str(target))

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
    ) -> int:
        worker_count = max(1, workers)
        dir_queue: queue.Queue[Path | None] = queue.Queue()
        dir_queue.put(target)
        dirs: list[Path] = []
        dirs_lock = threading.Lock()
        count_lock = threading.Lock()
        errors: list[BaseException] = []
        error_lock = threading.Lock()
        stop_event = threading.Event()
        count = 0

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

        def scan_worker() -> None:
            while True:
                current_dir = dir_queue.get()
                try:
                    if current_dir is None:
                        return
                    if stop_event.is_set():
                        continue
                    with dirs_lock:
                        dirs.append(current_dir)
                    try:
                        with os.scandir(current_dir) as entries:
                            for entry in entries:
                                if stop_event.is_set():
                                    break
                                try:
                                    if entry.is_dir(follow_symlinks=False):
                                        dir_queue.put(Path(entry.path))
                                    else:
                                        os.unlink(entry.path)
                                        bump(entry.path)
                                except BaseException as exc:
                                    record_error(exc)
                                    break
                    except BaseException as exc:
                        record_error(exc)
                finally:
                    dir_queue.task_done()

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [pool.submit(scan_worker) for _ in range(worker_count)]
            dir_queue.join()
            for _ in futures:
                dir_queue.put(None)
            dir_queue.join()
            for future in futures:
                future.result()

        if errors:
            raise errors[0]

        dirs_by_depth: dict[int, list[Path]] = defaultdict(list)
        for dir_path in dirs:
            dirs_by_depth[len(dir_path.parts)].append(dir_path)

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            for depth in sorted(dirs_by_depth, reverse=True):
                future_to_dir = {pool.submit(Path.rmdir, dir_path): dir_path for dir_path in dirs_by_depth[depth]}
                for future in as_completed(future_to_dir):
                    dir_path = future_to_dir[future]
                    future.result()
                    bump(str(dir_path))

        return count

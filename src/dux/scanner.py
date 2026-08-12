from __future__ import annotations

import os
import queue
import sqlite3
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import db
from .model import NodeRecord

ProgressCallback = Callable[[int, str], None]


@dataclass(slots=True)
class ScanResult:
    elapsed_seconds: float
    scanned_files: int
    scanned_dirs: int


def _canonical(path: str | os.PathLike[str]) -> str:
    return str(Path(path).expanduser().resolve())


def scan_subtree_to_db(
    conn: sqlite3.Connection,
    root_path: str,
    max_workers: int = 8,
    progress: ProgressCallback | None = None,
    progress_interval: int = 10000,
    batch_size: int = 5000,
) -> ScanResult:
    started_at = time.monotonic()
    root = _canonical(root_path)
    root_path_obj = Path(root)
    root_name = root_path_obj.name or root
    root_parent = str(root_path_obj.parent) if root != "/" else None
    write_queue: queue.Queue[list[NodeRecord] | None] = queue.Queue(
        maxsize=max(4, min(max_workers, 32))
    )
    work: queue.Queue[tuple[str, int] | None] = queue.Queue()
    root_depth = len(root_path_obj.parts)
    work.put((root, root_depth))
    progress_lock = threading.Lock()
    scanned_files = 0
    scanned_dirs = 1

    def write_records(records: list[NodeRecord]) -> None:
        if records:
            write_queue.put(records)

    def writer() -> None:
        batch: list[NodeRecord] = []
        while True:
            item = write_queue.get()
            try:
                if item is None:
                    if batch:
                        db.upsert_node_batch(conn, batch)
                    return
                batch.extend(item)
                while len(batch) >= batch_size:
                    db.upsert_node_batch(conn, batch[:batch_size])
                    del batch[:batch_size]
            finally:
                write_queue.task_done()

    writer_thread = threading.Thread(target=writer, name="dux-sqlite-writer")
    writer_thread.start()
    write_records(
        [
            NodeRecord(
                path=root,
                parent_path=root_parent,
                name=root_name,
                is_dir=True,
                indexed=True,
                depth=root_depth,
                size_bytes=0,
                file_count=0,
                dir_count=0,
            )
        ]
    )

    def record_progress(file_count: int, dir_count: int, path: str) -> None:
        nonlocal scanned_files, scanned_dirs
        reports: list[int] = []
        with progress_lock:
            previous = scanned_files
            scanned_files += file_count
            scanned_dirs += dir_count
            if progress is not None and progress_interval > 0:
                report_at = ((previous // progress_interval) + 1) * progress_interval
                while report_at <= scanned_files:
                    reports.append(report_at)
                    report_at += progress_interval
        if progress is not None:
            for count in reports:
                progress(count, path)

    def handle_dir(dir_path: str, dir_depth: int) -> None:
        records: list[NodeRecord] = []
        found_files = 0
        found_dirs = 0
        current_path = dir_path
        try:
            with os.scandir(dir_path) as it:
                for entry in it:
                    child_path = entry.path
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue

                    is_dir = stat.S_ISDIR(st.st_mode)
                    records.append(
                        NodeRecord(
                            path=child_path,
                            parent_path=dir_path,
                            name=entry.name,
                            is_dir=is_dir,
                            indexed=True,
                            depth=dir_depth + 1,
                            size_bytes=0 if is_dir else int(st.st_size),
                            file_count=0 if is_dir else 1,
                            dir_count=0,
                        )
                    )
                    if len(records) >= batch_size:
                        write_records(records)
                        records = []

                    if is_dir:
                        found_dirs += 1
                        work.put((child_path, dir_depth + 1))
                    else:
                        found_files += 1
                        current_path = child_path
        except OSError:
            pass
        write_records(records)
        record_progress(found_files, found_dirs, current_path)

    def worker() -> None:
        while True:
            item = work.get()
            try:
                if item is None:
                    return
                dir_path, dir_depth = item
                handle_dir(dir_path, dir_depth)
            finally:
                work.task_done()

    worker_count = max(1, max_workers)
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(worker) for _ in range(worker_count)]
        work.join()
        for _ in futures:
            work.put(None)
        work.join()
        for future in futures:
            future.result()

    write_queue.put(None)
    write_queue.join()
    writer_thread.join()

    return ScanResult(
        elapsed_seconds=time.monotonic() - started_at,
        scanned_files=scanned_files,
        scanned_dirs=scanned_dirs,
    )

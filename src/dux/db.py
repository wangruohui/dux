from __future__ import annotations

import errno
import fcntl
import json
import os
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .model import NodeRecord


DEFAULT_DB_PATH = Path("~/.cache/dux/dux.db").expanduser()
LOCK_REPORT_INTERVAL = 1.0
_CONNECTION_PATHS: dict[int, Path] = {}
_CONNECTION_PATHS_LOCK = threading.Lock()


def is_storage_full_error(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, OSError) and current.errno in {errno.ENOSPC, errno.EDQUOT}:
            return True
        if isinstance(current, sqlite3.Error):
            code = getattr(current, "sqlite_errorcode", None)
            if code is not None and code & 0xFF == sqlite3.SQLITE_FULL:
                return True
        message = str(current).lower()
        if any(
            text in message
            for text in ("database or disk is full", "no space left on device", "disk quota exceeded")
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS nodes (
    path TEXT PRIMARY KEY,
    parent_path TEXT,
    name TEXT NOT NULL,
    is_dir INTEGER NOT NULL,
    indexed INTEGER NOT NULL DEFAULT 1,
    depth INTEGER NOT NULL DEFAULT 0,
    size_bytes INTEGER NOT NULL,
    file_count INTEGER NOT NULL,
    dir_count INTEGER NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_parent_path ON nodes(parent_path);
CREATE INDEX IF NOT EXISTS idx_nodes_path_prefix ON nodes(path);
CREATE INDEX IF NOT EXISTS idx_nodes_depth ON nodes(depth);
"""


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or DEFAULT_DB_PATH).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    initialize = not path.exists() or path.stat().st_size == 0
    conn = sqlite3.connect(path, check_same_thread=False, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA synchronous=NORMAL")
    with _CONNECTION_PATHS_LOCK:
        _CONNECTION_PATHS[id(conn)] = path.resolve()
    if initialize:
        with writer_lock(conn, "initialize-schema", str(path)):
            conn.executescript(SCHEMA)
    else:
        _migrate(conn)
    return conn


def connect_readonly(
    db_path: str | Path | None = None,
    *,
    immutable: bool = False,
) -> sqlite3.Connection:
    path = Path(db_path or DEFAULT_DB_PATH).expanduser().resolve()
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    conn = sqlite3.connect(
        f"{path.as_uri()}?{query}",
        uri=True,
        check_same_thread=False,
        timeout=60.0,
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA schema_version").fetchone()
    except BaseException:
        conn.close()
        raise
    with _CONNECTION_PATHS_LOCK:
        _CONNECTION_PATHS[id(conn)] = path
    return conn


def connection_path(conn: sqlite3.Connection) -> Path:
    with _CONNECTION_PATHS_LOCK:
        return _CONNECTION_PATHS[id(conn)]


def _writer_description(metadata: dict[str, object] | None) -> str:
    if not metadata:
        return "unknown writer"
    started_at = float(metadata.get("started_at", time.time()))
    held = max(0.0, time.time() - started_at)
    return (
        f"pid={metadata.get('pid', '?')} operation={metadata.get('operation', '?')} "
        f"target={metadata.get('target', '?')} held={held:.1f}s "
        f"command={metadata.get('command', '?')}"
    )


def _read_lock_metadata(lock_file) -> dict[str, object] | None:
    try:
        lock_file.seek(0)
        content = lock_file.read()
        return json.loads(content) if content else None
    except (OSError, ValueError, TypeError):
        return None


def _open_process_candidates(db_path: Path) -> str:
    candidates: list[str] = []
    db_paths = {str(db_path), f"{db_path}-wal", f"{db_path}-shm"}
    proc_root = Path("/proc")
    for process_dir in proc_root.iterdir():
        if not process_dir.name.isdigit():
            continue
        try:
            open_paths: set[str] = set()
            for fd in (process_dir / "fd").iterdir():
                try:
                    open_paths.add(os.readlink(fd))
                except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                    continue
            if not open_paths.intersection(db_paths):
                continue
            command = (process_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                errors="replace"
            ).strip()
            candidates.append(f"pid={process_dir.name} command={command or '?'}")
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
    return "; ".join(candidates) if candidates else "no visible process candidates"


@contextmanager
def writer_lock(
    conn: sqlite3.Connection,
    operation: str,
    target: str,
    on_wait: Callable[[str], None] | None = None,
) -> Iterator[object]:
    db_path = connection_path(conn)
    lock_path = Path(f"{db_path}.writer.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        last_report = 0.0
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                now = time.monotonic()
                if on_wait is not None and now - last_report >= LOCK_REPORT_INTERVAL:
                    on_wait(_writer_description(_read_lock_metadata(lock_file)))
                    last_report = now
                time.sleep(0.1)

        metadata = {
            "pid": os.getpid(),
            "thread": threading.current_thread().name,
            "operation": operation,
            "target": target,
            "started_at": time.time(),
            "command": " ".join(sys.argv),
        }
        lock_file.seek(0)
        lock_file.truncate()
        json.dump(metadata, lock_file, ensure_ascii=True)
        lock_file.flush()
        try:
            yield lock_file
        finally:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.flush()
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def writer_transaction(
    conn: sqlite3.Connection,
    operation: str,
    target: str,
    on_wait: Callable[[str], None] | None = None,
) -> Iterator[None]:
    db_path = connection_path(conn)
    with writer_lock(conn, operation, target, on_wait=on_wait):
        conn.execute("PRAGMA busy_timeout=1000")
        last_report = 0.0
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                now = time.monotonic()
                if on_wait is not None and now - last_report >= LOCK_REPORT_INTERVAL:
                    on_wait(
                        "external/legacy SQLite writer candidates: "
                        + _open_process_candidates(db_path)
                    )
                    last_report = now
        try:
            yield
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.execute("PRAGMA busy_timeout=60000")


def _migrate(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(nodes)")}
    if "indexed" in columns and "depth" in columns:
        return
    with writer_transaction(conn, "migrate-schema", str(connection_path(conn))):
        if "indexed" not in columns:
            conn.execute("ALTER TABLE nodes ADD COLUMN indexed INTEGER NOT NULL DEFAULT 1")
        if "depth" not in columns:
            conn.execute("ALTER TABLE nodes ADD COLUMN depth INTEGER NOT NULL DEFAULT 0")
            rows = conn.execute("SELECT path FROM nodes").fetchall()
            conn.executemany(
                "UPDATE nodes SET depth = ? WHERE path = ?",
                [(len(Path(row["path"]).parts), row["path"]) for row in rows],
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_depth ON nodes(depth)")


def upsert_node_batch(conn: sqlite3.Connection, nodes: list[NodeRecord]) -> None:
    now = time.time()
    conn.executemany(
        """
        INSERT INTO nodes(path, parent_path, name, is_dir, indexed, depth, size_bytes, file_count, dir_count, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            parent_path=excluded.parent_path,
            name=excluded.name,
            is_dir=excluded.is_dir,
            indexed=excluded.indexed,
            depth=excluded.depth,
            size_bytes=excluded.size_bytes,
            file_count=excluded.file_count,
            dir_count=excluded.dir_count,
            updated_at=excluded.updated_at
        """,
        [
            (
                node.path,
                node.parent_path,
                node.name,
                int(node.is_dir),
                int(node.indexed),
                node.depth,
                node.size_bytes,
                node.file_count,
                node.dir_count,
                now,
            )
            for node in nodes
        ],
    )


def upsert_nodes(conn: sqlite3.Connection, nodes: dict[str, NodeRecord]) -> None:
    upsert_node_batch(conn, list(nodes.values()))


def ensure_ancestor_placeholders(conn: sqlite3.Connection, root_path: str) -> None:
    root = Path(root_path)
    placeholders: list[NodeRecord] = []
    for ancestor in reversed(root.parents):
        path = str(ancestor)
        if fetch_node(conn, path) is not None:
            continue
        placeholders.append(
            NodeRecord(
                path=path,
                parent_path=str(ancestor.parent) if path != "/" else None,
                name=ancestor.name or "/",
                is_dir=True,
                indexed=False,
                depth=len(ancestor.parts),
                size_bytes=0,
                file_count=0,
                dir_count=0,
            )
        )
    if placeholders:
        upsert_node_batch(conn, placeholders)


def refresh_placeholder_ancestor_aggregates(conn: sqlite3.Connection, root_path: str) -> None:
    for ancestor in Path(root_path).parents:
        path = str(ancestor)
        row = fetch_node(conn, path)
        if row is None or row["indexed"]:
            continue
        sums = conn.execute(
            """
            SELECT
                sum(size_bytes) AS size_bytes,
                sum(file_count) AS file_count,
                sum(dir_count + CASE WHEN is_dir THEN 1 ELSE 0 END) AS dir_count
            FROM nodes
            WHERE parent_path = ?
            """,
            (path,),
        ).fetchone()
        conn.execute(
            """
            UPDATE nodes
            SET size_bytes = ?, file_count = ?, dir_count = ?, updated_at = ?
            WHERE path = ?
            """,
            (
                int(sums["size_bytes"] or 0),
                int(sums["file_count"] or 0),
                int(sums["dir_count"] or 0),
                time.time(),
                path,
            ),
        )


def fetch_node(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM nodes WHERE path = ?", (path,)).fetchone()


def fetch_children(conn: sqlite3.Connection, path: str) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM nodes WHERE parent_path = ?", (path,)))


def fetch_filter_candidates(
    conn: sqlite3.Connection,
    root_path: str,
    name_pattern: str,
) -> list[sqlite3.Row]:
    if root_path == "/":
        return list(
            conn.execute(
                """
                SELECT path, name, is_dir, indexed, size_bytes, file_count
                FROM nodes
                WHERE path != '/' AND name GLOB ?
                """,
                (name_pattern,),
            )
        )
    child_lower = f"{root_path}/"
    child_upper = f"{root_path}0"
    return list(
        conn.execute(
            """
            SELECT path, name, is_dir, indexed, size_bytes, file_count
            FROM nodes
            WHERE path >= ? AND path < ? AND name GLOB ?
            """,
            (child_lower, child_upper, name_pattern),
        )
    )


def delete_subtree_rows(conn: sqlite3.Connection, root_path: str) -> None:
    child_lower = f"{root_path}/"
    child_upper = f"{root_path}0"
    conn.execute(
        "DELETE FROM nodes WHERE path = ? OR (path >= ? AND path < ?)",
        (root_path, child_lower, child_upper),
    )


def delete_nodes_incremental(
    conn: sqlite3.Connection,
    deleted: list[tuple[str, bool]],
    target: str,
    on_wait: Callable[[str], None] | None = None,
) -> int:
    if not deleted:
        return 0

    paths = list(dict.fromkeys(path for path, _is_dir in deleted))
    rows: list[sqlite3.Row] = []
    with writer_transaction(conn, "delete-index-flush", target, on_wait=on_wait):
        for offset in range(0, len(paths), 500):
            chunk = paths[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(
                conn.execute(
                    f"SELECT path, is_dir, size_bytes, file_count FROM nodes WHERE path IN ({placeholders})",
                    chunk,
                ).fetchall()
            )

        deleted_types = dict(deleted)
        deltas: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
        for row in rows:
            path = str(row["path"])
            is_dir = deleted_types.get(path, bool(row["is_dir"]))
            if is_dir:
                size_delta, file_delta, dir_delta = 0, 0, -1
            else:
                size_delta = -int(row["size_bytes"])
                file_delta = -int(row["file_count"])
                dir_delta = 0
            for parent in Path(path).parents:
                delta = deltas[str(parent)]
                delta[0] += size_delta
                delta[1] += file_delta
                delta[2] += dir_delta

        for offset in range(0, len(paths), 500):
            chunk = paths[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(f"DELETE FROM nodes WHERE path IN ({placeholders})", chunk)

        now = time.time()
        conn.executemany(
            """
            UPDATE nodes
            SET size_bytes = size_bytes + ?,
                file_count = file_count + ?,
                dir_count = dir_count + ?,
                updated_at = ?
            WHERE path = ?
            """,
            [
                (size_delta, file_delta, dir_delta, now, path)
                for path, (size_delta, file_delta, dir_delta) in deltas.items()
            ],
        )
    return len(rows)


def attach_database(conn: sqlite3.Connection, db_path: str | Path, alias: str) -> None:
    conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(db_path),))


def detach_database(conn: sqlite3.Connection, alias: str) -> None:
    conn.execute(f"DETACH DATABASE {alias}")


def insert_subtree_from_attached(conn: sqlite3.Connection, alias: str, root_path: str) -> None:
    child_lower = f"{root_path}/"
    child_upper = f"{root_path}0"
    conn.execute(
        f"""
        INSERT INTO nodes(path, parent_path, name, is_dir, indexed, depth, size_bytes, file_count, dir_count, updated_at)
        SELECT path, parent_path, name, is_dir, indexed, depth, size_bytes, file_count, dir_count, updated_at
        FROM {alias}.nodes
        WHERE path = ? OR (path >= ? AND path < ?)
        """,
        (root_path, child_lower, child_upper),
    )


def aggregate_subtree(conn: sqlite3.Connection, root_path: str) -> None:
    child_lower = f"{root_path}/"
    child_upper = f"{root_path}0"
    root_depth = len(Path(root_path).parts)
    max_depth_row = conn.execute(
        """
        SELECT max(depth) AS max_depth
        FROM nodes
        WHERE path = ? OR (path >= ? AND path < ?)
        """,
        (root_path, child_lower, child_upper),
    ).fetchone()
    max_depth = max_depth_row["max_depth"]
    if max_depth is None:
        return

    for depth in range(int(max_depth), root_depth, -1):
        rows = conn.execute(
            """
            SELECT
                parent_path,
                sum(size_bytes) AS size_bytes,
                sum(file_count) AS file_count,
                sum(dir_count + CASE WHEN is_dir THEN 1 ELSE 0 END) AS dir_count
            FROM nodes
            WHERE depth = ?
              AND parent_path IS NOT NULL
              AND path >= ?
              AND path < ?
            GROUP BY parent_path
            """,
            (depth, child_lower, child_upper),
        ).fetchall()
        conn.executemany(
            """
            UPDATE nodes
            SET size_bytes = ?, file_count = ?, dir_count = ?, updated_at = ?
            WHERE path = ?
            """,
            [
                (
                    int(row["size_bytes"] or 0),
                    int(row["file_count"] or 0),
                    int(row["dir_count"] or 0),
                    time.time(),
                    row["parent_path"],
                )
                for row in rows
            ],
        )


def apply_delta_to_ancestors(
    conn: sqlite3.Connection,
    path: str,
    size_delta: int,
    file_delta: int,
    dir_delta: int,
) -> None:
    current = Path(path)
    for parent in current.parents:
        parent_str = str(parent)
        row = fetch_node(conn, parent_str)
        if row is None:
            continue
        conn.execute(
            """
            UPDATE nodes
            SET size_bytes = size_bytes + ?,
                file_count = file_count + ?,
                dir_count = dir_count + ?,
                updated_at = ?
            WHERE path = ?
            """,
            (size_delta, file_delta, dir_delta, time.time(), parent_str),
        )

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .model import NodeRecord


DEFAULT_DB_PATH = Path("~/.cache/dux/dux.db").expanduser()


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
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(nodes)")}
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


def delete_subtree_rows(conn: sqlite3.Connection, root_path: str) -> None:
    child_lower = f"{root_path}/"
    child_upper = f"{root_path}0"
    conn.execute(
        "DELETE FROM nodes WHERE path = ? OR (path >= ? AND path < ?)",
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

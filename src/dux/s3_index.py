from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .s3 import S3Browser, S3Object, canonical_s3_uri, s3_parent


DEFAULT_S3_DB_PATH = Path("~/.cache/dux/s3.db").expanduser()


@dataclass(frozen=True)
class S3PrefixStat:
    uri: str
    parent_uri: str | None
    name: str
    size_bytes: int
    object_count: int
    latest_mtime: float | None


@dataclass(frozen=True)
class S3IndexResult:
    root_uri: str
    object_count: int
    prefix_count: int
    size_bytes: int
    scan_seconds: float
    write_seconds: float


class S3IndexStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or DEFAULT_S3_DB_PATH).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                CREATE TABLE IF NOT EXISTS s3_prefixes (
                    uri TEXT PRIMARY KEY,
                    parent_uri TEXT,
                    name TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    object_count INTEGER NOT NULL,
                    latest_mtime REAL,
                    indexed_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_s3_prefixes_parent
                    ON s3_prefixes(parent_uri);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=60000")
        return conn

    def replace_subtree(
        self, root_uri: str, stats: Iterable[S3PrefixStat]
    ) -> None:
        root = canonical_s3_uri(root_uri)
        rows = list(stats)
        indexed_at = time.time()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM s3_prefixes WHERE uri = ? OR substr(uri, 1, ?) = ?",
                (root, len(root) + 1, root + "/"),
            )
            conn.executemany(
                """
                INSERT INTO s3_prefixes (
                    uri, parent_uri, name, size_bytes, object_count,
                    latest_mtime, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        stat.uri,
                        stat.parent_uri,
                        stat.name,
                        stat.size_bytes,
                        stat.object_count,
                        stat.latest_mtime,
                        indexed_at,
                    )
                    for stat in rows
                ],
            )
            conn.commit()

    def get_many(self, uris: Iterable[str]) -> dict[str, S3PrefixStat]:
        normalized = list(dict.fromkeys(canonical_s3_uri(uri) for uri in uris))
        result: dict[str, S3PrefixStat] = {}
        with closing(self._connect()) as conn:
            for offset in range(0, len(normalized), 500):
                batch = normalized[offset : offset + 500]
                if not batch:
                    continue
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT * FROM s3_prefixes WHERE uri IN ({placeholders})",
                    batch,
                )
                for row in rows:
                    result[str(row["uri"])] = S3PrefixStat(
                        uri=str(row["uri"]),
                        parent_uri=str(row["parent_uri"]) if row["parent_uri"] else None,
                        name=str(row["name"]),
                        size_bytes=int(row["size_bytes"]),
                        object_count=int(row["object_count"]),
                        latest_mtime=(
                            float(row["latest_mtime"])
                            if row["latest_mtime"] is not None
                            else None
                        ),
                    )
        return result

    def apply_deleted(
        self,
        objects: Iterable[S3Object],
        completed_prefixes: Iterable[str] = (),
    ) -> None:
        deltas: dict[str, list[int]] = {}
        unknown_ancestors: set[str] = set()
        for item in objects:
            parent = s3_parent(item.uri)
            while parent.startswith("s3://"):
                if item.size_bytes is None:
                    unknown_ancestors.add(parent)
                else:
                    delta = deltas.setdefault(parent, [0, 0])
                    delta[0] += item.size_bytes
                    delta[1] += 1
                next_parent = s3_parent(parent)
                if next_parent == parent:
                    break
                parent = next_parent

        roots = [canonical_s3_uri(uri) for uri in completed_prefixes]
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for root in roots:
                conn.execute(
                    "DELETE FROM s3_prefixes WHERE uri = ? OR substr(uri, 1, ?) = ?",
                    (root, len(root) + 1, root + "/"),
                )
            for uri in unknown_ancestors:
                conn.execute("DELETE FROM s3_prefixes WHERE uri = ?", (uri,))
            conn.executemany(
                """
                UPDATE s3_prefixes
                SET size_bytes = max(0, size_bytes - ?),
                    object_count = max(0, object_count - ?),
                    latest_mtime = NULL,
                    indexed_at = ?
                WHERE uri = ?
                """,
                [
                    (size, count, time.time(), uri)
                    for uri, (size, count) in deltas.items()
                    if uri not in unknown_ancestors
                ],
            )
            conn.commit()


def index_s3(
    browser: S3Browser,
    root_uri: str,
    store: S3IndexStore,
    *,
    progress: Callable[[int, int, str, float], None] | None = None,
    progress_interval: int = 10000,
) -> S3IndexResult:
    root = canonical_s3_uri(root_uri)
    aggregates: dict[str, list[int | float | None]] = {
        root: [0, 0, None]
    }
    started_at = time.monotonic()
    object_count = 0
    for item in browser.iter_objects(root):
        object_count += 1
        relative = item.uri[len(root) :].lstrip("/")
        components = relative.split("/")
        prefixes = [root]
        current = root
        for component in components[:-1]:
            current = current.rstrip("/") + "/" + component
            prefixes.append(current)
        for prefix in prefixes:
            aggregate = aggregates.setdefault(prefix, [0, 0, None])
            aggregate[0] = int(aggregate[0]) + int(item.size_bytes or 0)
            aggregate[1] = int(aggregate[1]) + 1
            if item.mtime is not None:
                previous = aggregate[2]
                aggregate[2] = max(float(previous or item.mtime), item.mtime)
        if (
            progress is not None
            and progress_interval > 0
            and object_count % progress_interval == 0
        ):
            progress(
                object_count,
                len(aggregates),
                item.uri,
                time.monotonic() - started_at,
            )

    scan_seconds = time.monotonic() - started_at
    stats = [
        S3PrefixStat(
            uri=uri,
            parent_uri=None if uri == root else s3_parent(uri),
            name=uri.rpartition("/")[2],
            size_bytes=int(values[0]),
            object_count=int(values[1]),
            latest_mtime=float(values[2]) if values[2] is not None else None,
        )
        for uri, values in aggregates.items()
    ]
    write_started = time.monotonic()
    store.replace_subtree(root, stats)
    write_seconds = time.monotonic() - write_started
    root_values = aggregates[root]
    return S3IndexResult(
        root_uri=root,
        object_count=object_count,
        prefix_count=len(aggregates),
        size_bytes=int(root_values[0]),
        scan_seconds=scan_seconds,
        write_seconds=write_seconds,
    )

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class NodeRecord:
    path: str
    parent_path: str | None
    name: str
    is_dir: bool
    indexed: bool
    depth: int
    size_bytes: int
    file_count: int
    dir_count: int

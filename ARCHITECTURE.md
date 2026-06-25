# Architecture

## Current `duc` model

The current `duc` code stores:

- one directory blob per directory, keyed by device/inode
- one report record per indexed root path
- parent propagation by mutating ancestor directory blobs

That design is compact, but it makes local refresh tricky:

- path lookup is indirect
- inode replacement can break ancestor updates
- nearest-report fallback needs custom path resolution logic
- UI frames are DB-backed snapshots and must be explicitly reloaded

## New storage model

`dux` uses a path-keyed adjacency tree in SQLite:

- one row per path
- `path` is the primary key
- `parent_path` links to the parent
- aggregate fields are stored on every node:
  - `size_bytes`
  - `file_count`
  - `dir_count`

This is effectively a tree structure stored as an adjacency list. For this workload it is simpler than opaque per-directory blobs:

- subtree replacement is easy
- ancestor propagation is just repeated delta updates
- direct path lookup is trivial
- prefix collisions do not matter because lookups are exact

## Why date is not stored

`mtime` is not additive and is used only for display and sorting in the UI.

Keeping it out of the DB avoids:

- redundant writes during refresh
- mismatch between DB snapshots and current filesystem state
- extra invalidation logic

The UI resolves `mtime` on demand only for currently visible children.

## Refresh model

Refreshing `/a/b/c` does:

1. scan subtree `/a/b/c`
2. build a new in-memory node map for that subtree
3. replace rows for `/a/b/c` and descendants
4. compute `delta = new_root_agg - old_root_agg`
5. apply delta to `/a/b`, `/a`, and so on until the indexed root

That gives local refresh with explicit parent consistency.

## Concurrency model

For AFS-style workloads, directory latency is often a bigger problem than CPU.

The scanner uses worker threads with `os.scandir()`:

- each worker reads one directory
- files are recorded immediately
- child directories are queued
- aggregation is done after traversal, bottom-up in memory

This keeps the implementation simple and usually improves throughput over a single-threaded walker.

`asyncio` is not a good fit for raw filesystem syscalls here because Python filesystem APIs are blocking. Threads are the more honest tool.

## Why not delegate to `du`

System `du` can help for a coarse total size, but it is not enough as the primary engine:

- it does not maintain a queryable subtree database
- it does not give us exact parent-child rows to browse in a UI
- file-count aggregation still has to be built somewhere
- local refresh/delete propagation would still need our own state model

`du` may still be useful later as an optional verifier or fallback benchmark.

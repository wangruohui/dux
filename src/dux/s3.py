from __future__ import annotations

import threading
import time
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Protocol


class S3Client(Protocol):
    def list(self, uri: str) -> Iterable[str]: ...

    def delete(self, uri: str) -> object: ...


class S3StatsStore(Protocol):
    def get_many(self, uris: Iterable[str]) -> dict[str, object]: ...

    def apply_deleted(
        self,
        objects: Iterable[S3Object],
        completed_prefixes: Iterable[str] = (),
    ) -> None: ...


@dataclass(frozen=True)
class S3Object:
    uri: str
    size_bytes: int | None
    mtime: float | None


@dataclass(frozen=True)
class S3Entry:
    uri: str
    name: str
    is_dir: bool
    size_bytes: int | None = None
    mtime: float | None = None
    size_complete: bool = False
    object_count: int | None = None
    stats_indexed: bool = False


@dataclass(frozen=True)
class S3Listing:
    entries: tuple[S3Entry, ...]
    cached: bool


class S3DeleteCancelled(RuntimeError):
    pass


class S3BatchDeleteError(RuntimeError):
    def __init__(self, message: str, deleted_uris: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.deleted_uris = tuple(deleted_uris)


def canonical_s3_uri(uri: str) -> str:
    if not uri.startswith("s3://"):
        raise ValueError(f"not an S3 URI: {uri}")
    bucket_and_key = uri[5:]
    bucket, separator, key = bucket_and_key.partition("/")
    if not bucket:
        raise ValueError("S3 URI must include a bucket")
    key = key.strip("/") if separator else ""
    return f"s3://{bucket}" + (f"/{key}" if key else "")


def s3_parent(uri: str) -> str:
    canonical = canonical_s3_uri(uri)
    bucket_and_key = canonical[5:]
    bucket, separator, key = bucket_and_key.partition("/")
    if not separator or not key:
        return canonical
    parent_key = key.rpartition("/")[0]
    return f"s3://{bucket}" + (f"/{parent_key}" if parent_key else "")


def _join_s3_uri(parent: str, name: str) -> str:
    return canonical_s3_uri(parent).rstrip("/") + "/" + name.strip("/")


class AossClientAdapter:
    def __init__(self, config_path: str | Path | None = None) -> None:
        try:
            from aoss_client.client import Client
        except ImportError as exc:
            raise RuntimeError(
                "AOSS SDK is required for S3 browsing; install it into the dux environment"
            ) from exc
        path = Path(config_path) if config_path is not None else Path.home() / "aoss.conf"
        self._client = Client(str(path.expanduser().resolve()))
        self._mixed = self._client._get_local_client()

    def list(self, uri: str) -> Iterable[str]:
        return self._client.list(uri)

    def list_with_info(
        self, uri: str
    ) -> Iterable[tuple[str, bool, int | None, float | None]]:
        yield from self.list_with_info_cancelable(uri, None)

    def list_with_info_cancelable(
        self, uri: str, cancel_event: threading.Event | None
    ) -> Iterable[tuple[str, bool, int | None, float | None]]:
        # Client.list() discards Size, so retain it from the same delimiter-based request.
        try:
            from aoss_client.ceph.ceph import Ceph

            cluster, bucket, key = Ceph.parse_uri(
                uri, self._mixed._ceph_dict, self._mixed._default_cluster
            )
            backend = self._mixed._ceph_dict[cluster]
            client = backend._s3_resource.meta.client
        except (AttributeError, KeyError):
            for name in self.list(uri):
                yield str(name), str(name).endswith("/"), None, None
            return

        prefix = key or ""
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        paginator = client.get_paginator("list_objects")
        pages = paginator.paginate(
            Bucket=bucket,
            Prefix=prefix,
            Delimiter="/",
            PaginationConfig={"PageSize": 1000},
        )
        for page in pages:
            if cancel_event is not None and cancel_event.is_set():
                raise S3DeleteCancelled("S3 delete cancelled")
            for item in page.get("CommonPrefixes", []):
                name = str(item["Prefix"])[len(prefix) :].rstrip("/")
                if name:
                    yield name, True, None, None
            for item in page.get("Contents", []):
                name = str(item["Key"])[len(prefix) :]
                if name:
                    modified = item.get("LastModified")
                    mtime = modified.timestamp() if hasattr(modified, "timestamp") else None
                    yield name, False, int(item["Size"]), mtime

    def iter_objects(self, uri: str) -> Iterable[S3Object]:
        from aoss_client.ceph.ceph import Ceph

        cluster, bucket, key = Ceph.parse_uri(
            uri, self._mixed._ceph_dict, self._mixed._default_cluster
        )
        backend = self._mixed._ceph_dict[cluster]
        client = backend._s3_resource.meta.client
        prefix = key or ""
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        paginator = client.get_paginator("list_objects")
        pages = paginator.paginate(
            Bucket=bucket,
            Prefix=prefix,
            PaginationConfig={"PageSize": 1000},
        )
        for page in pages:
            for item in page.get("Contents", []):
                object_key = str(item["Key"])
                if object_key.endswith("/"):
                    continue
                modified = item.get("LastModified")
                mtime = modified.timestamp() if hasattr(modified, "timestamp") else None
                yield S3Object(
                    uri=f"s3://{bucket}/{object_key}",
                    size_bytes=int(item["Size"]),
                    mtime=mtime,
                )

    def delete(self, uri: str) -> object:
        try:
            from aoss_client.ceph.ceph import Ceph

            cluster, bucket, key = Ceph.parse_uri(
                uri, self._mixed._ceph_dict, self._mixed._default_cluster
            )
            backend = self._mixed._ceph_dict[cluster]
            return backend._s3_resource.meta.client.delete_object(Bucket=bucket, Key=key)
        except (AttributeError, KeyError):
            return self._client.delete(uri)

    def delete_many(self, uris: Iterable[str]) -> tuple[str, ...]:
        from aoss_client.ceph.ceph import Ceph

        grouped: dict[tuple[object, str], list[tuple[str, str]]] = {}
        for uri in uris:
            cluster, bucket, key = Ceph.parse_uri(
                uri, self._mixed._ceph_dict, self._mixed._default_cluster
            )
            backend = self._mixed._ceph_dict[cluster]
            client = backend._s3_resource.meta.client
            grouped.setdefault((client, bucket), []).append((uri, key))

        deleted: list[str] = []
        errors: list[str] = []
        for (client, bucket), items in grouped.items():
            response = client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": key} for _, key in items], "Quiet": False},
            )
            uri_by_key = {key: uri for uri, key in items}
            deleted.extend(
                uri_by_key[str(item["Key"])]
                for item in response.get("Deleted", [])
                if str(item.get("Key")) in uri_by_key
            )
            errors.extend(
                f"{item.get('Key')}: {item.get('Code', 'unknown')}"
                for item in response.get("Errors", [])
            )
        if errors:
            raise S3BatchDeleteError(
                f"S3 batch delete failed for {len(errors)} object(s): {errors[0]}",
                deleted,
            )
        return tuple(deleted)


def create_aoss_client(config_path: str | Path | None = None) -> S3Client:
    return AossClientAdapter(config_path)


class S3Browser:
    def __init__(
        self,
        client: S3Client | None = None,
        *,
        config_path: str | Path | None = None,
        cache_ttl: float = 300.0,
        cache_size: int = 512,
        max_workers: int = 256,
        stats_store: S3StatsStore | None = None,
    ) -> None:
        self._client = client
        self._config_path = config_path
        self._client_lock = threading.Lock()
        self.cache_ttl = max(0.0, cache_ttl)
        self.cache_size = max(1, cache_size)
        self.max_workers = max(1, max_workers)
        self.stats_store = stats_store
        self._cache: OrderedDict[str, tuple[float, tuple[S3Entry, ...]]] = OrderedDict()
        self._cache_lock = threading.Lock()

    def _get_client(self) -> S3Client:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                self._client = create_aoss_client(self._config_path)
            return self._client

    def list_children(
        self,
        uri: str,
        *,
        force: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> S3Listing:
        current = canonical_s3_uri(uri)
        now = time.monotonic()
        if not force:
            cached_entries: tuple[S3Entry, ...] | None = None
            with self._cache_lock:
                cached = self._cache.get(current)
                if cached is not None and now - cached[0] <= self.cache_ttl:
                    self._cache.move_to_end(current)
                    cached_entries = cached[1]
            if cached_entries is not None:
                return S3Listing(
                    self._with_stats(cached_entries), cached=True
                )

        entries_by_uri: dict[str, S3Entry] = {}
        client = self._get_client()
        cancelable_listing = getattr(client, "list_with_info_cancelable", None)
        list_with_info = getattr(client, "list_with_info", None)
        if cancel_event is not None and cancelable_listing is not None:
            raw_entries = cancelable_listing(current, cancel_event)
        elif list_with_info is None:
            raw_entries = (
                (str(name), str(name).endswith("/"), None, None)
                for name in client.list(current)
            )
        else:
            raw_entries = list_with_info(current)
        for raw_name, is_dir, size_bytes, mtime in raw_entries:
            if cancel_event is not None and cancel_event.is_set():
                raise S3DeleteCancelled("S3 delete cancelled")
            name = str(raw_name).rstrip("/")
            if not name:
                continue
            child_uri = _join_s3_uri(current, name)
            previous = entries_by_uri.get(child_uri)
            if previous is None or is_dir:
                entries_by_uri[child_uri] = S3Entry(
                    child_uri,
                    name,
                    is_dir,
                    size_bytes,
                    mtime,
                    not is_dir and size_bytes is not None,
                    None if is_dir else 1,
                )
        entries = tuple(
            sorted(entries_by_uri.values(), key=lambda entry: (not entry.is_dir, entry.name.casefold()))
        )
        with self._cache_lock:
            self._cache[current] = (now, entries)
            self._cache.move_to_end(current)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return S3Listing(self._with_stats(entries), cached=False)

    def iter_objects(self, uri: str) -> Iterable[S3Object]:
        client = self._get_client()
        iterator = getattr(client, "iter_objects", None)
        if iterator is None:
            raise RuntimeError("the configured S3 client does not support recursive listing")
        yield from iterator(canonical_s3_uri(uri))

    def _with_stats(self, entries: tuple[S3Entry, ...]) -> tuple[S3Entry, ...]:
        enriched = self._with_cached_aggregates(entries)
        if self.stats_store is None:
            return enriched
        indexed = self.stats_store.get_many(
            entry.uri for entry in enriched if entry.is_dir
        )
        return tuple(
            replace(
                entry,
                size_bytes=int(indexed[entry.uri].size_bytes),
                mtime=indexed[entry.uri].latest_mtime,
                size_complete=True,
                object_count=int(indexed[entry.uri].object_count),
                stats_indexed=True,
            )
            if entry.is_dir and entry.uri in indexed
            else entry
            for entry in enriched
        )

    def _with_cached_aggregates(
        self, entries: tuple[S3Entry, ...]
    ) -> tuple[S3Entry, ...]:
        now = time.monotonic()
        with self._cache_lock:
            cached_listings = {
                uri: cached_entries
                for uri, (created_at, cached_entries) in self._cache.items()
                if now - created_at <= self.cache_ttl
            }
        memo: dict[str, tuple[int | None, int | None, bool, float | None]] = {}

        def aggregate(
            uri: str,
        ) -> tuple[int | None, int | None, bool, float | None]:
            if uri in memo:
                return memo[uri]
            children = cached_listings.get(uri)
            if children is None:
                return None, None, False, None
            total = 0
            object_count = 0
            has_known_size = False
            has_known_count = False
            complete = True
            latest_mtime: float | None = None
            for child in children:
                if child.is_dir:
                    size, count, child_complete, mtime = aggregate(child.uri)
                else:
                    size = child.size_bytes
                    count = child.object_count
                    child_complete = child.size_complete
                    mtime = child.mtime
                if size is not None:
                    total += size
                    has_known_size = True
                if count is not None:
                    object_count += count
                    has_known_count = True
                if not child_complete:
                    complete = False
                if mtime is not None:
                    latest_mtime = max(latest_mtime or mtime, mtime)
            result = (
                total if has_known_size or complete else None,
                object_count if has_known_count or complete else None,
                complete,
                latest_mtime,
            )
            memo[uri] = result
            return result

        enriched: list[S3Entry] = []
        for entry in entries:
            if not entry.is_dir:
                enriched.append(entry)
                continue
            size, object_count, complete, mtime = aggregate(entry.uri)
            enriched.append(
                replace(
                    entry,
                    size_bytes=size,
                    mtime=mtime,
                    size_complete=complete,
                    object_count=object_count,
                )
            )
        return tuple(enriched)

    def invalidate(self, uri: str | None = None) -> None:
        with self._cache_lock:
            if uri is None:
                self._cache.clear()
                return
            target = canonical_s3_uri(uri)
            overlapping = [
                cached_uri
                for cached_uri in self._cache
                if cached_uri == target
                or cached_uri.startswith(target + "/")
                or target.startswith(cached_uri + "/")
            ]
            for cached_uri in overlapping:
                self._cache.pop(cached_uri, None)

    def delete_entries(
        self,
        entries: Iterable[S3Entry],
        *,
        cancel_event: threading.Event | None = None,
        progress: Callable[[str, int, int | None, str], None] | None = None,
    ) -> int:
        def cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        roots: list[S3Entry] = []
        for entry in sorted(entries, key=lambda item: (item.uri.count("/"), item.uri)):
            if any(entry.uri == root.uri or entry.uri.startswith(root.uri + "/") for root in roots):
                continue
            roots.append(entry)

        objects: list[S3Object] = [
            S3Object(root.uri, root.size_bytes, root.mtime)
            for root in roots
            if not root.is_dir
        ]
        directories: list[str] = [root.uri for root in roots if root.is_dir]
        scanned_dirs = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            pending = {
                pool.submit(
                    self.list_children,
                    root.uri,
                    force=True,
                    cancel_event=cancel_event,
                ): root.uri
                for root in roots
                if root.is_dir
            }
            while pending:
                if cancelled():
                    for future in pending:
                        future.cancel()
                    raise S3DeleteCancelled("S3 delete cancelled")
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    current = pending.pop(future)
                    listing = future.result()
                    scanned_dirs += 1
                    if progress is not None:
                        progress("listing", scanned_dirs, None, current)
                    for entry in listing.entries:
                        if entry.is_dir:
                            directories.append(entry.uri)
                            pending[
                                pool.submit(
                                    self.list_children,
                                    entry.uri,
                                    force=True,
                                    cancel_event=cancel_event,
                                )
                            ] = entry.uri
                        else:
                            objects.append(
                                S3Object(entry.uri, entry.size_bytes, entry.mtime)
                            )

        # S3 directories are prefixes, but deleting the marker is harmless when no marker exists.
        marker_uris = [
            directory.rstrip("/") + "/" for directory in reversed(directories)
        ]
        if cancelled():
            raise S3DeleteCancelled("S3 delete cancelled")

        completed = 0
        deleted_objects: list[S3Object] = []
        completed_normally = False
        client = self._get_client()
        items_by_uri = {item.uri: item for item in objects}
        delete_uris = [item.uri for item in objects] + marker_uris
        batches = [
            delete_uris[offset : offset + 1000]
            for offset in range(0, len(delete_uris), 1000)
        ]
        delete_many = getattr(client, "delete_many", None)

        def delete_batch(batch: list[str]) -> tuple[str, ...]:
            if delete_many is not None:
                return tuple(delete_many(batch))
            for target in batch:
                if cancelled():
                    raise S3DeleteCancelled("S3 delete cancelled")
                client.delete(target)
            return tuple(batch)

        cancellation_requested = False
        first_error: BaseException | None = None
        try:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                future_to_batch = {
                    pool.submit(delete_batch, batch): batch
                    for batch in batches
                }
                for future in as_completed(future_to_batch):
                    if cancelled() and not cancellation_requested:
                        cancellation_requested = True
                        for pending_future in future_to_batch:
                            pending_future.cancel()
                    if future.cancelled():
                        continue
                    try:
                        deleted_uris = future.result()
                    except S3BatchDeleteError as exc:
                        deleted_uris = exc.deleted_uris
                        first_error = first_error or exc
                    except S3DeleteCancelled:
                        cancellation_requested = True
                        continue
                    for target in deleted_uris:
                        item = items_by_uri.get(target)
                        if item is not None:
                            deleted_objects.append(item)
                    completed += len(deleted_uris)
                    if progress is not None:
                        progress(
                            "deleting",
                            completed,
                            len(delete_uris),
                            deleted_uris[-1] if deleted_uris else "",
                        )
            if cancellation_requested:
                raise S3DeleteCancelled("S3 delete cancelled")
            if first_error is not None:
                raise first_error
            completed_normally = True
        finally:
            if self.stats_store is not None and deleted_objects:
                self.stats_store.apply_deleted(
                    deleted_objects,
                    completed_prefixes=(
                        [root.uri for root in roots if root.is_dir]
                        if completed_normally
                        else []
                    ),
                )
            for root in roots:
                self.invalidate(root.uri)
        return completed

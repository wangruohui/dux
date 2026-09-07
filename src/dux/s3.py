from __future__ import annotations

import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Protocol


class S3Client(Protocol):
    def list(self, uri: str) -> Iterable[str]: ...

    def delete(self, uri: str) -> object: ...


@dataclass(frozen=True)
class S3Entry:
    uri: str
    name: str
    is_dir: bool
    size_bytes: int | None = None
    mtime: float | None = None
    size_complete: bool = False


@dataclass(frozen=True)
class S3Listing:
    entries: tuple[S3Entry, ...]
    cached: bool


class S3DeleteCancelled(RuntimeError):
    pass


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
    ) -> None:
        self._client = client
        self._config_path = config_path
        self._client_lock = threading.Lock()
        self.cache_ttl = max(0.0, cache_ttl)
        self.cache_size = max(1, cache_size)
        self.max_workers = max(1, max_workers)
        self._cache: OrderedDict[str, tuple[float, tuple[S3Entry, ...]]] = OrderedDict()
        self._cache_lock = threading.Lock()

    def _get_client(self) -> S3Client:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                self._client = create_aoss_client(self._config_path)
            return self._client

    def list_children(self, uri: str, *, force: bool = False) -> S3Listing:
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
                    self._with_cached_aggregates(cached_entries), cached=True
                )

        entries_by_uri: dict[str, S3Entry] = {}
        client = self._get_client()
        list_with_info = getattr(client, "list_with_info", None)
        if list_with_info is None:
            raw_entries = (
                (str(name), str(name).endswith("/"), None, None)
                for name in client.list(current)
            )
        else:
            raw_entries = list_with_info(current)
        for raw_name, is_dir, size_bytes, mtime in raw_entries:
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
                )
        entries = tuple(
            sorted(entries_by_uri.values(), key=lambda entry: (not entry.is_dir, entry.name.casefold()))
        )
        with self._cache_lock:
            self._cache[current] = (now, entries)
            self._cache.move_to_end(current)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return S3Listing(self._with_cached_aggregates(entries), cached=False)

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
        memo: dict[str, tuple[int | None, bool, float | None]] = {}

        def aggregate(uri: str) -> tuple[int | None, bool, float | None]:
            if uri in memo:
                return memo[uri]
            children = cached_listings.get(uri)
            if children is None:
                return None, False, None
            total = 0
            has_known_size = False
            complete = True
            latest_mtime: float | None = None
            for child in children:
                if child.is_dir:
                    size, child_complete, mtime = aggregate(child.uri)
                else:
                    size = child.size_bytes
                    child_complete = child.size_complete
                    mtime = child.mtime
                if size is not None:
                    total += size
                    has_known_size = True
                if not child_complete:
                    complete = False
                if mtime is not None:
                    latest_mtime = max(latest_mtime or mtime, mtime)
            result = (total if has_known_size or complete else None, complete, latest_mtime)
            memo[uri] = result
            return result

        enriched: list[S3Entry] = []
        for entry in entries:
            if not entry.is_dir:
                enriched.append(entry)
                continue
            size, complete, mtime = aggregate(entry.uri)
            enriched.append(
                replace(entry, size_bytes=size, mtime=mtime, size_complete=complete)
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

        objects: list[str] = []
        directories: list[str] = []
        pending = list(roots)
        scanned_dirs = 0
        while pending:
            if cancelled():
                raise S3DeleteCancelled("S3 delete cancelled")
            entry = pending.pop()
            if not entry.is_dir:
                objects.append(entry.uri)
                continue
            directories.append(entry.uri)
            listing = self.list_children(entry.uri, force=True)
            pending.extend(listing.entries)
            scanned_dirs += 1
            if progress is not None:
                progress("listing", scanned_dirs, None, entry.uri)

        # S3 directories are prefixes, but deleting the marker is harmless when no marker exists.
        objects.extend(directory.rstrip("/") + "/" for directory in reversed(directories))
        if cancelled():
            raise S3DeleteCancelled("S3 delete cancelled")

        completed = 0
        client = self._get_client()
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            future_to_uri = {pool.submit(client.delete, uri): uri for uri in objects}
            for future in as_completed(future_to_uri):
                uri = future_to_uri[future]
                if cancelled():
                    for pending_future in future_to_uri:
                        pending_future.cancel()
                    raise S3DeleteCancelled("S3 delete cancelled")
                future.result()
                completed += 1
                if progress is not None:
                    progress("deleting", completed, len(objects), uri)

        for root in roots:
            self.invalidate(root.uri)
        return completed

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from dux.cli import main
from dux.s3 import (
    AossClientAdapter,
    S3Browser,
    S3Entry,
    S3Object,
    canonical_s3_uri,
    s3_parent,
)
from dux.s3_index import S3IndexResult, S3IndexStore, index_s3
from dux.s3_tui import _sort_entries, run_s3_ui


class FakeS3Client:
    def __init__(self) -> None:
        self.listings: dict[
            str, list[tuple[str, bool, int | None, float | None]]
        ] = {}
        self.list_calls: list[str] = []
        self.deleted: list[str] = []
        self.objects: dict[str, list[S3Object]] = {}

    def list(self, uri: str):
        return [
            name + ("/" if is_dir else "")
            for name, is_dir, _, _ in self.listings[uri]
        ]

    def list_with_info(self, uri: str):
        self.list_calls.append(uri)
        return iter(self.listings[uri])

    def delete(self, uri: str) -> None:
        self.deleted.append(uri)

    def iter_objects(self, uri: str):
        return iter(self.objects[uri])


class S3BrowserTests(unittest.TestCase):
    def test_s3_sort_defaults_descending_and_keeps_missing_last(self) -> None:
        entries = (
            S3Entry("s3://bucket/missing", "missing", True),
            S3Entry("s3://bucket/small", "small", True, 10, 100.0, True, 2),
            S3Entry("s3://bucket/large", "large", True, 30, 300.0, True, 1),
            S3Entry("s3://bucket/mid", "mid", True, 20, 200.0, True, 3),
        )

        self.assertEqual(
            [entry.name for entry in _sort_entries(entries, "size", True)],
            ["large", "mid", "small", "missing"],
        )
        self.assertEqual(
            [entry.name for entry in _sort_entries(entries, "count", True)],
            ["mid", "small", "large", "missing"],
        )
        self.assertEqual(
            [entry.name for entry in _sort_entries(entries, "mtime", False)],
            ["small", "mid", "large", "missing"],
        )

    def test_uri_navigation(self) -> None:
        self.assertEqual(canonical_s3_uri("s3://bucket/a/b/"), "s3://bucket/a/b")
        self.assertEqual(s3_parent("s3://bucket/a/b"), "s3://bucket/a")
        self.assertEqual(s3_parent("s3://bucket"), "s3://bucket")
        with self.assertRaisesRegex(ValueError, "include a bucket"):
            canonical_s3_uri("s3://")

    def test_listing_uses_sizes_and_cache(self) -> None:
        client = FakeS3Client()
        client.listings["s3://bucket/root"] = [
            ("folder", True, None, None),
            ("file.bin", False, 1536, 100.0),
        ]
        browser = S3Browser(client, cache_ttl=300)

        first = browser.list_children("s3://bucket/root")
        second = browser.list_children("s3://bucket/root")

        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(client.list_calls, ["s3://bucket/root"])
        self.assertEqual(first.entries[0], S3Entry("s3://bucket/root/folder", "folder", True))
        self.assertEqual(first.entries[1].size_bytes, 1536)

        browser.list_children("s3://bucket/root", force=True)
        self.assertEqual(len(client.list_calls), 2)

    def test_client_initialization_is_lazy(self) -> None:
        client = FakeS3Client()
        client.listings["s3://bucket"] = []
        with patch("dux.s3.create_aoss_client", return_value=client) as create:
            browser = S3Browser()
            create.assert_not_called()
            browser.list_children("s3://bucket")
            create.assert_called_once_with(None)

    def test_aoss_adapter_retains_size_from_delimited_listing(self) -> None:
        class Paginator:
            def paginate(self, **kwargs):
                self.kwargs = kwargs
                return [
                    {
                        "CommonPrefixes": [{"Prefix": "root/folder/"}],
                        "Contents": [{"Key": "root/", "Size": 0}]
                        + [
                            {
                                "Key": f"root/file-{index:04d}.bin",
                                "Size": index,
                                "LastModified": datetime.fromtimestamp(index, timezone.utc),
                            }
                            for index in range(1000)
                        ],
                    },
                    {
                        "Contents": [
                            {
                                "Key": "root/tail.bin",
                                "Size": 10,
                                "LastModified": datetime.fromtimestamp(200, timezone.utc),
                            }
                        ]
                    },
                ]

        class LowLevelClient:
            def __init__(self) -> None:
                self.paginator = Paginator()
                self.deleted = []

            def get_paginator(self, name: str):
                self.paginator_name = name
                return self.paginator

            def delete_object(self, **kwargs):
                self.deleted.append(kwargs)

        low_level = LowLevelClient()
        backend = SimpleNamespace(
            _s3_resource=SimpleNamespace(meta=SimpleNamespace(client=low_level))
        )
        adapter = AossClientAdapter.__new__(AossClientAdapter)
        adapter._mixed = SimpleNamespace(
            _ceph_dict={"default": backend},
            _default_cluster="default",
        )

        entries = list(adapter.list_with_info("s3://bucket/root"))
        adapter.delete("s3://bucket/root/file-0000.bin")

        self.assertEqual(len(entries), 1002)
        self.assertEqual(entries[0], ("folder", True, None, None))
        self.assertEqual(entries[1], ("file-0000.bin", False, 0, 0.0))
        self.assertEqual(entries[-1], ("tail.bin", False, 10, 200.0))
        self.assertEqual(low_level.paginator_name, "list_objects")
        self.assertEqual(low_level.paginator.kwargs["Delimiter"], "/")
        self.assertEqual(
            low_level.deleted,
            [{"Bucket": "bucket", "Key": "root/file-0000.bin"}],
        )

    def test_cached_directory_size_becomes_exact_after_visiting_children(self) -> None:
        client = FakeS3Client()
        client.listings["s3://bucket"] = [("root", True, None, None)]
        client.listings["s3://bucket/root"] = [
            ("direct.bin", False, 10, 100.0),
            ("nested", True, None, None),
        ]
        client.listings["s3://bucket/root/nested"] = [
            ("deep.bin", False, 20, 200.0)
        ]
        browser = S3Browser(client)

        browser.list_children("s3://bucket")
        browser.list_children("s3://bucket/root")
        partial = browser.list_children("s3://bucket").entries[0]
        self.assertEqual(partial.size_bytes, 10)
        self.assertFalse(partial.size_complete)
        self.assertEqual(partial.mtime, 100.0)

        browser.list_children("s3://bucket/root/nested")
        complete = browser.list_children("s3://bucket").entries[0]
        self.assertEqual(complete.size_bytes, 30)
        self.assertTrue(complete.size_complete)
        self.assertEqual(complete.mtime, 200.0)

    def test_recursive_delete_removes_objects_and_prefix_markers(self) -> None:
        client = FakeS3Client()
        client.listings["s3://bucket/root"] = [
            ("file.txt", False, 1, 100.0),
            ("nested", True, None, None),
        ]
        client.listings["s3://bucket/root/nested"] = [
            ("data.bin", False, 2, 200.0)
        ]
        browser = S3Browser(client, max_workers=4)
        progress: list[tuple[str, int, int | None, str]] = []

        deleted = browser.delete_entries(
            [S3Entry("s3://bucket/root", "root", True)],
            progress=lambda *args: progress.append(args),
        )

        self.assertEqual(deleted, 4)
        self.assertEqual(
            set(client.deleted),
            {
                "s3://bucket/root/file.txt",
                "s3://bucket/root/nested/data.bin",
                "s3://bucket/root/nested/",
                "s3://bucket/root/",
            },
        )
        self.assertTrue(any(item[0] == "listing" for item in progress))
        self.assertTrue(any(item[0] == "deleting" for item in progress))

    def test_delete_invalidates_parent_listing_cache(self) -> None:
        client = FakeS3Client()
        client.listings["s3://bucket/root"] = [("file.bin", False, 10, 100.0)]
        browser = S3Browser(client)
        entry = browser.list_children("s3://bucket/root").entries[0]

        browser.delete_entries([entry])
        client.listings["s3://bucket/root"] = []
        refreshed = browser.list_children("s3://bucket/root")

        self.assertFalse(refreshed.cached)
        self.assertEqual(refreshed.entries, ())
        self.assertEqual(client.list_calls.count("s3://bucket/root"), 2)

    def test_cancel_before_delete_does_not_remove_objects(self) -> None:
        client = FakeS3Client()
        browser = S3Browser(client)
        cancelled = threading.Event()
        cancelled.set()

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            browser.delete_entries(
                [S3Entry("s3://bucket/file", "file", False)],
                cancel_event=cancelled,
            )
        self.assertEqual(client.deleted, [])

    def test_cli_routes_s3_uri_without_opening_local_database(self) -> None:
        with patch("dux.cli.run_s3_ui") as run_s3, patch("dux.cli.run_ui") as run_local:
            self.assertEqual(main(["ui", "s3://bucket/root"]), 0)

        run_s3.assert_called_once_with("s3://bucket/root", 256, stats_db_path=None)
        run_local.assert_not_called()

    def test_index_aggregates_prefixes_and_delete_updates_stats(self) -> None:
        client = FakeS3Client()
        client.objects["s3://bucket"] = [
            S3Object("s3://bucket/direct.bin", 10, 100.0),
            S3Object("s3://bucket/a/file.bin", 20, 200.0),
            S3Object("s3://bucket/a/b/deep.bin", 30, 300.0),
        ]
        browser = S3Browser(client)
        with tempfile.TemporaryDirectory() as directory:
            store = S3IndexStore(Path(directory) / "s3.db")
            result = index_s3(browser, "s3://bucket", store, progress_interval=1)

            self.assertEqual(result.object_count, 3)
            self.assertEqual(result.prefix_count, 3)
            stats = store.get_many(
                ["s3://bucket", "s3://bucket/a", "s3://bucket/a/b"]
            )
            self.assertEqual((stats["s3://bucket"].size_bytes, stats["s3://bucket"].object_count), (60, 3))
            self.assertEqual((stats["s3://bucket/a"].size_bytes, stats["s3://bucket/a"].object_count), (50, 2))
            self.assertEqual((stats["s3://bucket/a/b"].size_bytes, stats["s3://bucket/a/b"].object_count), (30, 1))

            store.apply_deleted(
                [S3Object("s3://bucket/a/b/deep.bin", 30, 300.0)],
                completed_prefixes=["s3://bucket/a/b"],
            )
            updated = store.get_many(
                ["s3://bucket", "s3://bucket/a", "s3://bucket/a/b"]
            )
            self.assertEqual((updated["s3://bucket"].size_bytes, updated["s3://bucket"].object_count), (30, 2))
            self.assertEqual((updated["s3://bucket/a"].size_bytes, updated["s3://bucket/a"].object_count), (20, 1))
            self.assertNotIn("s3://bucket/a/b", updated)

    def test_parallel_index_shards_prefixes_without_double_counting(self) -> None:
        client = FakeS3Client()
        client.listings["s3://bucket"] = [
            ("a", True, None, None),
            ("b", True, None, None),
        ]
        client.objects["s3://bucket/a"] = [
            S3Object("s3://bucket/a/one.bin", 10, 100.0)
        ]
        client.objects["s3://bucket/b"] = [
            S3Object("s3://bucket/b/two.bin", 20, 200.0)
        ]
        browser = S3Browser(client)
        with tempfile.TemporaryDirectory() as directory:
            store = S3IndexStore(Path(directory) / "s3.db")
            result = index_s3(browser, "s3://bucket", store, workers=2)

            self.assertEqual((result.object_count, result.size_bytes), (2, 30))
            stats = store.get_many(
                ["s3://bucket", "s3://bucket/a", "s3://bucket/b"]
            )
            self.assertEqual(stats["s3://bucket"].size_bytes, 30)
            self.assertEqual(stats["s3://bucket/a"].size_bytes, 10)
            self.assertEqual(stats["s3://bucket/b"].size_bytes, 20)

    def test_cli_routes_s3_index_without_local_scanner(self) -> None:
        result = S3IndexResult("s3://bucket", 1, 1, 10, 1.0, 0.1)
        with patch("dux.cli.S3Browser") as browser_type, patch(
            "dux.cli.S3IndexStore"
        ) as store_type, patch("dux.cli.index_s3", return_value=result) as scan:
            self.assertEqual(main(["index", "s3://bucket"]), 0)

        scan.assert_called_once()
        self.assertEqual(scan.call_args.kwargs["workers"], 32)
        browser_type.assert_called_once_with(max_workers=32)
        store_type.assert_called_once_with(None)

    def test_tui_lists_sizes_selects_and_navigates(self) -> None:
        client = FakeS3Client()
        client.listings["s3://bucket/root"] = [
            ("folder", True, None, None),
            ("file.bin", False, 1536, 100.0),
        ]
        client.listings["s3://bucket/root/folder"] = []
        browser = S3Browser(client)
        apps = []
        with patch(
            "textual.app.App.run",
            lambda app, *args, **kwargs: apps.append((app, kwargs)),
        ):
            run_s3_ui("s3://bucket/root", 256, browser=browser)
        app, run_options = apps[0]
        self.assertIs(run_options["mouse"], False)

        async def exercise() -> None:
            async with app.run_test(size=(100, 30)) as pilot:
                for _ in range(20):
                    if len(app.rows_by_uri) == 2:
                        break
                    await pilot.pause(0.01)
                self.assertEqual(len(app.rows_by_uri), 2)
                table = app.query_one("#table")
                file_row = table.get_row("s3://bucket/root/file.bin")
                self.assertEqual(str(file_row[1]), "1.5K")
                self.assertEqual(str(file_row[2]), "1")
                self.assertNotEqual(str(file_row[3]), "-")

                self.assertEqual(app.sort_by, "size")
                self.assertTrue(app.reverse)
                await pilot.press("c")
                self.assertEqual(app.sort_by, "count")
                self.assertTrue(app.reverse)
                await pilot.press("c")
                self.assertFalse(app.reverse)

                table.move_cursor(row=1, column=0, animate=False)
                await pilot.press("space")
                self.assertEqual(app.marked_uris, {"s3://bucket/root/folder"})
                await pilot.press("enter")
                self.assertEqual(app.current_uri, "s3://bucket/root/folder")
                await pilot.press("backspace")
                self.assertEqual(app.current_uri, "s3://bucket/root")
                for _ in range(20):
                    if len(app.rows_by_uri) == 2:
                        break
                    await pilot.pause(0.01)
                self.assertEqual(
                    str(table.get_row("s3://bucket/root/folder")[1]),
                    "0B",
                )

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()

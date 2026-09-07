from __future__ import annotations

import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dux.cli import main
from dux.s3 import AossClientAdapter, S3Browser, S3Entry, canonical_s3_uri, s3_parent
from dux.s3_tui import run_s3_ui


class FakeS3Client:
    def __init__(self) -> None:
        self.listings: dict[str, list[tuple[str, bool, int | None]]] = {}
        self.list_calls: list[str] = []
        self.deleted: list[str] = []

    def list(self, uri: str):
        return [name + ("/" if is_dir else "") for name, is_dir, _ in self.listings[uri]]

    def list_with_info(self, uri: str):
        self.list_calls.append(uri)
        return iter(self.listings[uri])

    def delete(self, uri: str) -> None:
        self.deleted.append(uri)


class S3BrowserTests(unittest.TestCase):
    def test_uri_navigation(self) -> None:
        self.assertEqual(canonical_s3_uri("s3://bucket/a/b/"), "s3://bucket/a/b")
        self.assertEqual(s3_parent("s3://bucket/a/b"), "s3://bucket/a")
        self.assertEqual(s3_parent("s3://bucket"), "s3://bucket")
        with self.assertRaisesRegex(ValueError, "include a bucket"):
            canonical_s3_uri("s3://")

    def test_listing_uses_sizes_and_cache(self) -> None:
        client = FakeS3Client()
        client.listings["s3://bucket/root"] = [
            ("folder", True, None),
            ("file.bin", False, 1536),
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
                        "Contents": [
                            {"Key": "root/", "Size": 0},
                            {"Key": "root/file.bin", "Size": 1536},
                        ],
                    }
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
        adapter.delete("s3://bucket/root/file.bin")

        self.assertEqual(entries, [("folder", True, None), ("file.bin", False, 1536)])
        self.assertEqual(low_level.paginator_name, "list_objects")
        self.assertEqual(low_level.paginator.kwargs["Delimiter"], "/")
        self.assertEqual(
            low_level.deleted,
            [{"Bucket": "bucket", "Key": "root/file.bin"}],
        )

    def test_recursive_delete_removes_objects_and_prefix_markers(self) -> None:
        client = FakeS3Client()
        client.listings["s3://bucket/root"] = [
            ("file.txt", False, 1),
            ("nested", True, None),
        ]
        client.listings["s3://bucket/root/nested"] = [("data.bin", False, 2)]
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

        run_s3.assert_called_once_with("s3://bucket/root", 256)
        run_local.assert_not_called()

    def test_tui_lists_sizes_selects_and_navigates(self) -> None:
        client = FakeS3Client()
        client.listings["s3://bucket/root"] = [
            ("folder", True, None),
            ("file.bin", False, 1536),
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
                self.assertEqual(str(table.get_row("s3://bucket/root/file.bin")[1]), "1.5K")

                await pilot.press("space")
                self.assertEqual(app.marked_uris, {"s3://bucket/root/folder"})
                await pilot.press("enter")
                self.assertEqual(app.current_uri, "s3://bucket/root/folder")
                await pilot.press("backspace")
                self.assertEqual(app.current_uri, "s3://bucket/root")

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dux.service import DeleteCancelled, DuxService
from dux import db


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        self.root.mkdir()
        self.db_path = Path(self.tmp.name) / "dux.db"
        self.service = DuxService(db_path=self.db_path, max_workers=4)

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def test_index_updates_ancestor_aggregate(self) -> None:
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "a.bin").write_bytes(b"a" * 100)
        self.service.index_path(str(self.root))

        (sub / "a.bin").unlink()
        (sub / "b.bin").write_bytes(b"b" * 20)
        self.service.index_path(str(sub))

        rows = self.service.list_children(str(self.root))
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(by_name["sub"]["size_bytes"], 20)
        self.assertEqual(by_name["sub"]["file_count"], 1)

    def test_read_only_service_falls_back_to_immutable_snapshot(self) -> None:
        (self.root / "item.bin").write_bytes(b"data")
        self.service.index_path(str(self.root))
        self.service.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        real_connect_readonly = db.connect_readonly

        def fail_standard_readonly(path, *, immutable=False):
            if not immutable:
                raise sqlite3.OperationalError("database or disk is full")
            return real_connect_readonly(path, immutable=True)

        with patch("dux.service.db.connect_readonly", side_effect=fail_standard_readonly):
            reader = DuxService(db_path=self.db_path, max_workers=1, read_only=True)
        try:
            self.assertTrue(reader.read_only)
            self.assertTrue(reader.immutable_fallback)
            self.assertIn("ignored_wal_bytes=", reader.readonly_warning)
            self.assertIsNotNone(reader.get_node(str(self.root)))
        finally:
            reader.close()

    def test_writer_wait_reports_lock_owner(self) -> None:
        holder = DuxService(db_path=self.db_path, max_workers=1)
        waiter = DuxService(db_path=self.db_path, max_workers=1)
        holder_ready = threading.Event()
        release_holder = threading.Event()
        reports: list[str] = []

        def hold_writer_lock() -> None:
            with db.writer_transaction(holder.conn, "test-holder", "/held/path"):
                holder_ready.set()
                release_holder.wait(timeout=5)

        holder_thread = threading.Thread(target=hold_writer_lock)
        holder_thread.start()
        self.assertTrue(holder_ready.wait(timeout=5))

        def wait_for_writer() -> None:
            with db.writer_transaction(
                waiter.conn,
                "test-waiter",
                "/waiting/path",
                on_wait=reports.append,
            ):
                pass

        waiter_thread = threading.Thread(target=wait_for_writer)
        waiter_thread.start()
        try:
            for _ in range(50):
                if reports:
                    break
                time.sleep(0.02)
            self.assertTrue(reports)
            self.assertIn("operation=test-holder", reports[0])
            self.assertIn("target=/held/path", reports[0])
            self.assertIn(f"pid={os.getpid()}", reports[0])
        finally:
            release_holder.set()
            holder_thread.join(timeout=5)
            waiter_thread.join(timeout=5)
            holder.close()
            waiter.close()

        self.assertFalse(holder_thread.is_alive())
        self.assertFalse(waiter_thread.is_alive())

    def test_writer_wait_reports_legacy_sqlite_candidates(self) -> None:
        legacy = DuxService(db_path=self.db_path, max_workers=1)
        waiter = DuxService(db_path=self.db_path, max_workers=1)
        legacy.conn.execute("BEGIN IMMEDIATE")
        reports: list[str] = []

        def wait_for_writer() -> None:
            with db.writer_transaction(
                waiter.conn,
                "test-waiter",
                "/waiting/path",
                on_wait=reports.append,
            ):
                pass

        waiter_thread = threading.Thread(target=wait_for_writer)
        waiter_thread.start()
        try:
            for _ in range(150):
                if reports:
                    break
                time.sleep(0.02)
            self.assertTrue(reports)
            self.assertIn("external/legacy SQLite writer candidates", reports[0])
            self.assertIn(f"pid={os.getpid()}", reports[0])
        finally:
            legacy.conn.rollback()
            waiter_thread.join(timeout=5)
            legacy.close()
            waiter.close()

        self.assertFalse(waiter_thread.is_alive())

    def test_index_scan_keeps_main_db_readable(self) -> None:
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "old.bin").write_bytes(b"a" * 100)
        self.service.index_path(str(self.root))
        for index in range(20):
            (sub / f"new-{index}.bin").write_bytes(b"b" * 10)
        (sub / "old.bin").unlink()

        reader = DuxService(db_path=self.db_path, max_workers=1)
        observed: list[int] = []
        observed_lock = threading.Lock()
        try:
            def progress(_count: int, _path: str) -> None:
                with observed_lock:
                    if observed:
                        return
                    rows = reader.list_children(str(self.root))
                    reader.ensure_navigation_path(str(self.root))
                    by_name = {row["name"]: row for row in rows}
                    observed.append(int(by_name["sub"]["file_count"]))

            self.service.index_path(str(sub), progress=progress, progress_interval=1)

            self.assertEqual(observed, [1])
            rows = self.service.list_children(str(self.root))
            by_name = {row["name"]: row for row in rows}
            self.assertEqual(by_name["sub"]["file_count"], 20)
        finally:
            reader.close()

    def test_index_reports_each_progress_interval_after_batching(self) -> None:
        for index in range(5):
            (self.root / f"file-{index}.bin").write_bytes(b"x")
        reports: list[int] = []

        self.service.index_path(
            str(self.root),
            progress=lambda count, _path: reports.append(count),
            progress_interval=2,
        )

        self.assertEqual(reports, [2, 4])

    def test_delete_propagates_to_parent(self) -> None:
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "a.bin").write_bytes(b"a" * 30)
        self.service.index_path(str(self.root))

        self.service.delete_path(str(sub / "a.bin"), permanent=True)

        rows = self.service.list_children(str(self.root / "sub"))
        self.assertEqual(rows, [])
        sub_row = self.service.list_children(str(self.root))[0]
        self.assertEqual(sub_row["size_bytes"], 0)
        self.assertEqual(sub_row["file_count"], 0)

    def test_prefix_paths_do_not_confuse_refresh(self) -> None:
        a = self.root / "flow_grpo_neo"
        b = self.root / "flow_grpo_neo_align" / "work_dir"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        (a / "keep.bin").write_bytes(b"k" * 10)
        (b / "drop.bin").write_bytes(b"d" * 100)

        self.service.index_path(str(self.root))
        self.service.index_path(str(a))

        (b / "drop.bin").unlink()
        (b / "keep.bin").write_bytes(b"x" * 20)
        self.service.index_path(str(b))

        rows = self.service.list_children(str(self.root))
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(by_name["flow_grpo_neo_align"]["size_bytes"], 20)

    def test_parent_placeholder_can_list_indexed_child(self) -> None:
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "a.bin").write_bytes(b"a" * 10)

        self.service.index_path(str(sub))

        parent = self.service.get_node(str(self.root))
        self.assertIsNotNone(parent)
        self.assertFalse(parent["indexed"])
        rows = self.service.list_children(str(self.root))
        self.assertEqual([row["name"] for row in rows], ["sub"])
        self.assertEqual(rows[0]["size_bytes"], 10)
        self.assertTrue(rows[0]["indexed"])

    def test_visible_children_include_limited_unindexed_siblings(self) -> None:
        sub = self.root / "sub"
        sibling = self.root / "sibling"
        sub.mkdir()
        sibling.mkdir()
        (sub / "a.bin").write_bytes(b"a" * 10)

        self.service.index_path(str(sub))

        rows, truncated = self.service.list_visible_children(str(self.root), sort_by="name", live_limit=10)
        by_name = {row["name"]: row for row in rows}
        self.assertFalse(truncated)
        self.assertTrue(by_name["sub"]["indexed"])
        self.assertFalse(by_name["sibling"]["indexed"])
        self.assertTrue(by_name["sibling"]["live_only"])
        self.assertIsNone(by_name["sibling"]["size_bytes"])

    def test_visible_children_sort_descending_with_unindexed_last(self) -> None:
        alpha = self.root / "alpha"
        beta = self.root / "beta"
        zulu = self.root / "zulu"
        alpha.mkdir()
        beta.mkdir()
        zulu.mkdir()
        (alpha / "small.bin").write_bytes(b"a")
        (beta / "large.bin").write_bytes(b"b" * 10)
        (beta / "extra.bin").write_bytes(b"c" * 10)
        self.service.index_path(str(alpha))
        self.service.index_path(str(beta))
        os.utime(alpha, (100, 100))
        os.utime(beta, (200, 200))
        os.utime(zulu, (300, 300))

        for sort_by in ("size", "count", "mtime", "name"):
            rows, _ = self.service.list_visible_children(
                str(self.root), sort_by=sort_by, reverse=True, live_limit=10
            )
            self.assertEqual([row["name"] for row in rows], ["beta", "alpha", "zulu"])

    def test_delete_unindexed_live_path(self) -> None:
        sub = self.root / "sub"
        sibling = self.root / "sibling"
        sub.mkdir()
        sibling.mkdir()
        (sub / "a.bin").write_bytes(b"a" * 10)
        (sibling / "b.bin").write_bytes(b"b" * 20)

        self.service.index_path(str(sub))
        self.service.delete_path(str(sibling), permanent=True)

        self.assertFalse(sibling.exists())
        rows, _ = self.service.list_visible_children(str(self.root), sort_by="name", live_limit=10)
        self.assertEqual([row["name"] for row in rows], ["sub"])

    def test_permanent_delete_reports_progress(self) -> None:
        sub = self.root / "sub"
        nested = sub / "nested"
        nested.mkdir(parents=True)
        (sub / "a.bin").write_bytes(b"a")
        (nested / "b.bin").write_bytes(b"b")
        self.service.index_path(str(self.root))
        progress: list[tuple[int, str]] = []

        self.service.delete_path(
            str(sub),
            permanent=True,
            progress=lambda count, path: progress.append((count, path)),
            progress_interval=1,
        )

        self.assertFalse(sub.exists())
        self.assertGreaterEqual(len(progress), 3)
        self.assertEqual(progress[-1][1], str(sub))

    def test_flat_directory_files_are_unlinked_in_parallel(self) -> None:
        sub = self.root / "flat"
        sub.mkdir()
        for index in range(300):
            (sub / f"file-{index}.bin").write_bytes(b"x")
        self.service.index_path(str(self.root))
        active = 0
        max_active = 0
        active_lock = threading.Lock()
        original_unlink = self.service._unlink_path

        def tracked_unlink(path: Path) -> str:
            nonlocal active, max_active
            with active_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.02)
                return original_unlink(path)
            finally:
                with active_lock:
                    active -= 1

        self.service._unlink_path = tracked_unlink
        self.service.delete_path(str(sub), permanent=True, unlink_workers=256)

        self.assertFalse(sub.exists())
        self.assertGreaterEqual(max_active, 128)

    def test_parallel_delete_paths_updates_index(self) -> None:
        left = self.root / "left"
        right = self.root / "right"
        left.mkdir()
        right.mkdir()
        (left / "a.bin").write_bytes(b"a" * 10)
        (right / "b.bin").write_bytes(b"b" * 20)
        self.service.index_path(str(self.root))
        progress: list[tuple[str, int, str]] = []

        self.service.delete_paths(
            [str(left), str(right)],
            permanent=True,
            progress=lambda target, count, path: progress.append((target, count, path)),
            progress_interval=1,
            workers=2,
        )

        self.assertFalse(left.exists())
        self.assertFalse(right.exists())
        self.assertEqual(self.service.list_children(str(self.root)), [])
        root = self.service.get_node(str(self.root))
        self.assertEqual(root["size_bytes"], 0)
        self.assertEqual(root["file_count"], 0)
        self.assertGreaterEqual(len(progress), 2)

    def test_cancelled_delete_flushes_incremental_index_updates(self) -> None:
        sub = self.root / "cancel-me"
        sub.mkdir()
        for index in range(200):
            (sub / f"file-{index}.bin").write_bytes(b"x" * 10)
        self.service.index_path(str(self.root))
        self.service.index_path = lambda *_args, **_kwargs: self.fail("cancel must not reindex")
        cancel_event = threading.Event()
        deleted_count = 0
        deleted_lock = threading.Lock()
        original_unlink = self.service._unlink_path

        def tracked_unlink(path: Path) -> str:
            nonlocal deleted_count
            result = original_unlink(path)
            with deleted_lock:
                deleted_count += 1
                if deleted_count >= 20:
                    cancel_event.set()
            return result

        self.service._unlink_path = tracked_unlink
        with self.assertRaises(DeleteCancelled):
            self.service.delete_path(
                str(sub),
                permanent=True,
                unlink_workers=8,
                cancel_event=cancel_event,
            )

        remaining = len(list(sub.iterdir()))
        self.assertGreater(remaining, 0)
        self.assertLess(remaining, 200)
        sub_row = self.service.get_node(str(sub))
        root_row = self.service.get_node(str(self.root))
        self.assertEqual(int(sub_row["file_count"]), remaining)
        self.assertEqual(int(sub_row["size_bytes"]), remaining * 10)
        self.assertEqual(int(root_row["file_count"]), remaining)
        self.assertEqual(int(root_row["size_bytes"]), remaining * 10)

    def test_filter_paths_prunes_matches_and_excluded_paths(self) -> None:
        keep = self.root / "keep"
        matched_dir = keep / "target"
        nested_match = matched_dir / "nested" / "target"
        separate_match = self.root / "other" / "target"
        excluded_match = self.root / "skip-heavy" / "target"
        nested_match.parent.mkdir(parents=True)
        separate_match.parent.mkdir(parents=True)
        excluded_match.parent.mkdir(parents=True)
        nested_match.write_bytes(b"nested")
        separate_match.write_bytes(b"separate")
        excluded_match.write_bytes(b"excluded")

        progress: list[tuple[int, int, str]] = []
        result = self.service.filter_paths(
            str(self.root),
            "target",
            exclude="heavy",
            progress=lambda dirs, matches, path: progress.append((dirs, matches, path)),
            progress_interval=1,
        )

        self.assertEqual(result.paths, [str(matched_dir), str(separate_match)])
        self.assertNotIn(str(nested_match), result.paths)
        self.assertNotIn(str(excluded_match), result.paths)
        self.assertGreaterEqual(result.scanned_dirs, 3)
        self.assertTrue(progress)

    def test_filter_paths_rejects_empty_keyword(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            self.service.filter_paths(str(self.root), "")

    def test_filter_paths_supports_basename_globs(self) -> None:
        (self.root / "alpha").mkdir()
        (self.root / "alpha" / "apple").write_bytes(b"a")
        (self.root / "beta").mkdir()
        (self.root / "beta" / "apricot").write_bytes(b"b")
        (self.root / "beta" / "pear").write_bytes(b"c")

        result = self.service.filter_paths(str(self.root), "a*")

        self.assertEqual(
            result.paths,
            [str(self.root / "alpha"), str(self.root / "beta" / "apricot")],
        )

    def test_filter_paths_merges_index_and_live_filesystem(self) -> None:
        indexed = self.root / "indexed-target"
        stale = self.root / "stale-target"
        indexed.write_bytes(b"indexed")
        stale.write_bytes(b"stale")
        self.service.index_path(str(self.root))
        stale.unlink()
        live_only = self.root / "live-target"
        live_only.write_bytes(b"live")

        result = self.service.filter_paths(str(self.root), "*target")

        self.assertEqual(result.paths, [str(indexed), str(live_only)])
        self.assertEqual(result.indexed_matches, 1)
        self.assertEqual(result.live_only_matches, 1)
        self.assertEqual(result.stale_index_matches, 1)
        entries = {entry.path: entry for entry in result.entries}
        self.assertTrue(entries[str(indexed)].indexed)
        self.assertEqual(entries[str(indexed)].size_bytes, len(b"indexed"))
        self.assertEqual(entries[str(indexed)].file_count, 1)
        self.assertGreater(entries[str(indexed)].mtime, 0)
        self.assertFalse(entries[str(live_only)].indexed)
        self.assertIsNone(entries[str(live_only)].size_bytes)
        self.assertIsNone(entries[str(live_only)].file_count)
        self.assertGreater(entries[str(live_only)].mtime, 0)

    def test_filter_paths_can_cancel_during_scan(self) -> None:
        from dux.service import FilterCancelled

        for index in range(20):
            directory = self.root / f"dir-{index}"
            directory.mkdir()
            (directory / "item").write_bytes(b"x")
        cancel_event = threading.Event()

        def cancel_after_progress(_dirs: int, _matches: int, _path: str) -> None:
            cancel_event.set()

        with self.assertRaises(FilterCancelled):
            self.service.filter_paths(
                str(self.root),
                "missing",
                progress=cancel_after_progress,
                progress_interval=1,
                cancel_event=cancel_event,
            )


if __name__ == "__main__":
    unittest.main()

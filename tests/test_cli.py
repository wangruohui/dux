from __future__ import annotations

import asyncio
import sys
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dux.cli import build_parser
from dux.service import DuxService, FilterEntry
from dux.tui import run_ui


class CliTests(unittest.TestCase):
    def test_default_scanner_workers(self) -> None:
        args = build_parser().parse_args(["index", "/tmp"])
        self.assertEqual(args.workers, 256)

    def test_ui_startup_does_not_write_navigation_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            db_path = Path(directory) / "dux.db"
            service = DuxService(db_path=db_path, max_workers=1)
            service.index_path(str(root))
            service.conn.execute("BEGIN IMMEDIATE")
            runs = []
            try:
                with patch(
                    "textual.app.App.run",
                    lambda app, *args, **kwargs: runs.append((app, kwargs)),
                ):
                    run_ui(str(db_path), str(root), 1)
                self.assertEqual(len(runs), 1)
                app, run_options = runs[0]
                self.assertIs(run_options["mouse"], False)
                self.assertTrue(app.service.read_only)
                app.service.close()
            finally:
                service.conn.rollback()
                service.close()

    def test_ui_navigation_history_and_background_refresh_path_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            child = root / "child"
            child.mkdir(parents=True)
            db_path = Path(directory) / "dux.db"
            service = DuxService(db_path=db_path, max_workers=1)
            service.index_path(str(root))
            apps = []
            try:
                with patch("textual.app.App.run", lambda app, *args, **kwargs: apps.append(app)):
                    run_ui(str(db_path), str(root), 1)
                app = apps[0]
                app._reload_table = lambda *args, **kwargs: None
                app.notify = lambda *args, **kwargs: None
                app._set_status = lambda *args, **kwargs: None

                app._navigate_to(str(child))
                self.assertEqual(app.current_path, str(child))
                app.action_go_parent()
                self.assertEqual(app.current_path, str(root))
                app.action_go_back()
                self.assertEqual(app.current_path, str(child))
                app.action_go_back()
                self.assertEqual(app.current_path, str(root))
                app.action_go_forward()
                self.assertEqual(app.current_path, str(child))

                bindings = {(binding.key, binding.action) for binding in app.BINDINGS}
                self.assertIn(("backspace", "go_parent"), bindings)
                self.assertIn(("alt+left", "go_back"), bindings)
                self.assertIn(("alt+right", "go_forward"), bindings)
                self.assertNotIn(("up", "go_parent"), bindings)

                app.action_sort_size()
                self.assertTrue(app.reverse)
                app.action_sort_size()
                self.assertFalse(app.reverse)
                app.action_sort_count()
                self.assertTrue(app.reverse)
                app.action_sort_count()
                self.assertFalse(app.reverse)
                app.action_sort_mtime()
                self.assertTrue(app.reverse)
                app.action_sort_mtime()
                self.assertFalse(app.reverse)

                preview_file = root / "preview.txt"
                preview_file.write_bytes((b"A" * 1024) + b"B")
                pushed_screens = []
                app._selected_path = lambda: str(preview_file)
                app.rows_by_key[str(preview_file)] = False
                app.push_screen = lambda screen, *args, **kwargs: pushed_screens.append(screen)
                app.action_open_selected()
                self.assertEqual(len(pushed_screens), 1)
                preview_screen = pushed_screens[0]
                self.assertEqual(preview_screen.path, str(preview_file))
                self.assertEqual(preview_screen.byte_count, 1024)
                self.assertTrue(preview_screen.truncated)
                self.assertEqual(preview_screen.content, "A" * 1024)

                queued = []
                refreshed = []
                selected_refresh_paths = [
                    str(child / "selected-refresh"),
                    str(root / "other-refresh"),
                    str(root / "other-refresh" / "overlap"),
                ]
                app._selected_path = lambda: selected_refresh_paths.pop(0)
                app.run_worker = lambda worker, **kwargs: queued.append(worker)
                app._refresh_current_worker = (
                    lambda job_id, refresh_path, cancel_event: refreshed.append(
                        (job_id, refresh_path, cancel_event)
                    )
                )
                app.action_refresh_current()
                app.action_refresh_current()
                self.assertEqual(len(queued), 2)
                refresh_cancel_events = dict(app.refresh_cancel_events)
                self.assertEqual(set(refresh_cancel_events), {1, 2})

                app.action_refresh_current()
                self.assertEqual(len(queued), 2)

                app.action_cancel_delete()
                self.assertTrue(refresh_cancel_events[2].is_set())
                self.assertFalse(refresh_cancel_events[1].is_set())
                app.action_cancel_delete()
                self.assertTrue(refresh_cancel_events[1].is_set())
                app.current_path = str(root)
                for worker in queued:
                    worker()
                self.assertEqual(
                    [(job_id, path) for job_id, path, _event in refreshed],
                    [
                        (1, str(child / "selected-refresh")),
                        (2, str(root / "other-refresh")),
                    ],
                )
                exited = []
                app.exit = lambda: exited.append(True)
                app.action_request_quit()
                self.assertTrue(app.quit_after_refresh)
                app._finish_refresh(
                    2, str(root / "other-refresh"), None, cancelled=True
                )
                self.assertEqual(exited, [])
                app._finish_refresh(
                    1, str(child / "selected-refresh"), None, cancelled=True
                )
                self.assertEqual(exited, [True])
            finally:
                service.close()

    def test_ui_falls_back_to_filesystem_first_delete_when_writer_is_full(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            target = root / "target"
            trash_target = root / "trash-target"
            target.mkdir(parents=True)
            trash_target.mkdir()
            (target / "item.bin").write_bytes(b"data")
            (trash_target / "item.bin").write_bytes(b"keep")
            db_path = Path(directory) / "dux.db"
            service = DuxService(db_path=db_path, max_workers=2)
            service.index_path(str(root))
            apps = []
            try:
                with patch("textual.app.App.run", lambda app, *args, **kwargs: apps.append(app)):
                    run_ui(str(db_path), str(root), 2)
                app = apps[0]
                finishes = []
                app.call_from_thread = lambda callback, *args: callback(*args)
                app._show_delete_job_status = lambda *args: None
                app._finish_delete = lambda *args: finishes.append(args)
                with patch(
                    "dux.tui.DuxService",
                    side_effect=sqlite3.OperationalError("database or disk is full"),
                ):
                    app._delete_worker(
                        1,
                        [str(target)],
                        threading.Event(),
                        permanent=True,
                        trash=False,
                    )

                self.assertFalse(target.exists())
                self.assertEqual(finishes[0][2], [str(target)])
                self.assertTrue(finishes[0][5])
                self.assertIsNone(service.get_node(str(target)))

                with patch(
                    "dux.tui.DuxService",
                    side_effect=sqlite3.OperationalError("database or disk is full"),
                ):
                    app._delete_worker(
                        2,
                        [str(trash_target)],
                        threading.Event(),
                        permanent=False,
                        trash=True,
                        trash_destinations={
                            str(trash_target): str(root / "trash" / "trash-target")
                        },
                    )
                self.assertTrue(trash_target.exists())
                self.assertIsNotNone(finishes[1][3])
                app.service.close()
            finally:
                service.close()

    def test_ui_delete_keys_select_trash_and_permanent_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            target = root / "target"
            target.mkdir(parents=True)
            db_path = Path(directory) / "dux.db"
            service = DuxService(db_path=db_path, max_workers=1)
            service.index_path(str(root))
            apps = []
            try:
                with patch("textual.app.App.run", lambda app, *args, **kwargs: apps.append(app)):
                    run_ui(str(db_path), str(root), 1)
                app = apps[0]
                expected_destination = "/mnt/afs/A/trash/B/C/D"
                app._selected_path = lambda: "/mnt/afs/A/B/C/D"
                app.service.trash_destination = lambda _path: expected_destination
                confirmations = []
                started = []
                app.push_screen = (
                    lambda screen, callback=None: confirmations.append((screen, callback))
                )
                app._start_delete = lambda targets, **kwargs: started.append((targets, kwargs))

                bindings = {(binding.key, binding.action) for binding in app.BINDINGS}
                self.assertIn(("delete", "trash_requested"), bindings)
                self.assertIn(
                    ("shift+delete", "permanent_delete_requested"), bindings
                )

                app.action_trash_requested()
                screen, callback = confirmations.pop()
                self.assertIn("src: /mnt/afs/A/B/C/D", screen.message)
                self.assertIn(f"dst: {expected_destination}", screen.message)
                callback(True)
                expected = {
                    "permanent": False,
                    "trash": True,
                    "trash_destinations": {
                        "/mnt/afs/A/B/C/D": expected_destination
                    },
                }
                self.assertEqual(started.pop(), (["/mnt/afs/A/B/C/D"], expected))

                app.action_permanent_delete_requested()
                screen, callback = confirmations.pop()
                self.assertIn("src: /mnt/afs/A/B/C/D", screen.message)
                self.assertIn("dst: PERMANENT DELETE", screen.message)
                callback(True)
                expected = {
                    "permanent": True,
                    "trash": False,
                    "trash_destinations": {},
                }
                self.assertEqual(started.pop(), (["/mnt/afs/A/B/C/D"], expected))
                app.service.close()
            finally:
                service.close()

    def test_ui_keyboard_item_parent_and_history_navigation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            child = root / "child"
            child.mkdir(parents=True)
            (root / "sibling").mkdir()
            db_path = Path(directory) / "dux.db"
            service = DuxService(db_path=db_path, max_workers=1)
            service.index_path(str(root))
            apps = []
            try:
                with patch("textual.app.App.run", lambda app, *args, **kwargs: apps.append(app)):
                    run_ui(str(db_path), str(root), 1)
                app = apps[0]

                async def exercise_keys() -> None:
                    async with app.run_test(size=(100, 30)) as pilot:
                        table = app.query_one("#table")
                        initial_row = table.cursor_row
                        await pilot.press("down")
                        self.assertGreater(table.cursor_row, initial_row)
                        await pilot.press("up")
                        self.assertEqual(table.cursor_row, initial_row)
                        self.assertEqual(app.current_path, str(root))
                        selected = app._selected_path()
                        await pilot.press("enter")
                        self.assertEqual(app.current_path, selected)
                        await pilot.press("backspace")
                        self.assertEqual(app.current_path, str(root))
                        await pilot.press("alt+left")
                        self.assertEqual(app.current_path, selected)
                        await pilot.press("alt+right")
                        self.assertEqual(app.current_path, str(root))

                        preview_file = root / "preview.txt"
                        preview_file.write_bytes((b"A" * 1024) + b"B")
                        app._selected_path = lambda: str(preview_file)
                        app.rows_by_key[str(preview_file)] = False
                        main_screen = app.screen
                        await pilot.press("enter")
                        await pilot.pause()
                        self.assertEqual(app.screen.path, str(preview_file))
                        self.assertEqual(app.screen.byte_count, 1024)
                        self.assertTrue(app.screen.truncated)
                        await pilot.press("q")
                        await pilot.pause()
                        self.assertIs(app.screen, main_screen)

                asyncio.run(exercise_keys())
                app.service.close()
            finally:
                service.close()

    def test_filter_selection_sorts_by_size_files_and_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            db_path = Path(directory) / "dux.db"
            service = DuxService(db_path=db_path, max_workers=1)
            service.index_path(str(root))
            entries = [
                FilterEntry(str(root / "large"), "large", True, True, 300, 1, 100.0),
                FilterEntry(str(root / "many"), "many", True, True, 200, 30, 200.0),
                FilterEntry(str(root / "newest"), "newest", True, True, 100, 2, 300.0),
                FilterEntry(str(root / "live"), "live", True, False, None, None, 400.0),
            ]
            apps = []
            try:
                with patch("textual.app.App.run", lambda app, *args, **kwargs: apps.append(app)):
                    run_ui(str(db_path), str(root), 1)
                app = apps[0]

                async def exercise_sorting() -> None:
                    async with app.run_test(size=(120, 32)) as pilot:
                        app._finish_filter(
                            str(root),
                            "*",
                            "",
                            [entry.path for entry in entries],
                            entries,
                            1,
                            0.1,
                            3,
                            1,
                            0,
                            None,
                        )
                        await pilot.pause()
                        screen = app.screen
                        table = screen.query_one("#filter-results")

                        def row_order() -> list[str]:
                            result = []
                            for row in range(table.row_count):
                                table.move_cursor(row=row, column=0, animate=False)
                                result.append(screen._current_result())
                            return result

                        self.assertEqual(
                            row_order(),
                            [entries[0].path, entries[1].path, entries[2].path, entries[3].path],
                        )
                        table.move_cursor(row=0, column=0, animate=False)
                        await pilot.press("space")
                        await pilot.press("c")
                        self.assertEqual(
                            row_order(),
                            [entries[1].path, entries[2].path, entries[0].path, entries[3].path],
                        )
                        self.assertEqual(screen.selected_paths, {entries[0].path})
                        await pilot.press("c")
                        self.assertEqual(
                            row_order(),
                            [entries[0].path, entries[2].path, entries[1].path, entries[3].path],
                        )
                        await pilot.press("m")
                        self.assertEqual(
                            row_order(),
                            [entries[2].path, entries[1].path, entries[0].path, entries[3].path],
                        )
                        await pilot.press("m")
                        self.assertEqual(
                            row_order(),
                            [entries[0].path, entries[1].path, entries[2].path, entries[3].path],
                        )
                        await pilot.press("s")
                        self.assertEqual(
                            row_order(),
                            [entries[0].path, entries[1].path, entries[2].path, entries[3].path],
                        )
                        await pilot.press("s")
                        self.assertEqual(
                            row_order(),
                            [entries[2].path, entries[1].path, entries[0].path, entries[3].path],
                        )
                        delete_modes = []
                        app._confirm_delete = lambda targets, **kwargs: delete_modes.append(
                            (targets, kwargs["permanent"])
                        )
                        screen.action_accept_results()
                        screen.action_accept_results(permanent=True)
                        self.assertEqual(
                            delete_modes,
                            [([entries[0].path], False), ([entries[0].path], True)],
                        )
                        self.assertIn("width: 94%", app.CSS)

                asyncio.run(exercise_sorting())
                app.service.close()
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()

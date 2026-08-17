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
from dux.service import DuxService
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
            apps = []
            try:
                with patch("textual.app.App.run", lambda app, *args, **kwargs: apps.append(app)):
                    run_ui(str(db_path), str(root), 1)
                self.assertEqual(len(apps), 1)
                self.assertTrue(apps[0].service.read_only)
                apps[0].service.close()
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

                bindings = {(binding.key, binding.action) for binding in app.BINDINGS}
                self.assertIn(("backspace", "go_back"), bindings)
                self.assertIn(("up", "go_parent"), bindings)

                queued = []
                refreshed = []
                app.run_worker = lambda worker, **kwargs: queued.append(worker)
                app._refresh_current_worker = lambda refresh_path: refreshed.append(refresh_path)
                app.action_refresh_current()
                app.current_path = str(child)
                queued[0]()
                self.assertEqual(refreshed, [str(root)])
                app.service.close()
            finally:
                service.close()

    def test_ui_falls_back_to_filesystem_first_delete_when_writer_is_full(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            target = root / "target"
            target.mkdir(parents=True)
            (target / "item.bin").write_bytes(b"data")
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
                app.service.close()
            finally:
                service.close()

    def test_ui_keyboard_up_and_backspace_navigation(self) -> None:
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

                async def exercise_keys() -> None:
                    async with app.run_test(size=(100, 30)) as pilot:
                        await pilot.press("enter")
                        self.assertEqual(app.current_path, str(child))
                        await pilot.press("up")
                        self.assertEqual(app.current_path, str(root))
                        await pilot.press("backspace")
                        self.assertEqual(app.current_path, str(child))
                        await pilot.press("backspace")
                        self.assertEqual(app.current_path, str(root))

                asyncio.run(exercise_keys())
                app.service.close()
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()

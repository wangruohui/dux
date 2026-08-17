from __future__ import annotations

import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()

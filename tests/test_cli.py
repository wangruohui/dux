from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dux.cli import build_parser


class CliTests(unittest.TestCase):
    def test_default_scanner_workers(self) -> None:
        args = build_parser().parse_args(["index", "/tmp"])
        self.assertEqual(args.workers, 256)


if __name__ == "__main__":
    unittest.main()

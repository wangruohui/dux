from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from duviz.service import DuvizService


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        self.root.mkdir()
        self.db_path = Path(self.tmp.name) / "duviz.db"
        self.service = DuvizService(db_path=self.db_path, max_workers=4)

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


if __name__ == "__main__":
    unittest.main()

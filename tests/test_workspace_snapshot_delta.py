from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_agent.workspace.snapshot import capture_workspace_snapshot
from local_agent.workspace.snapshot_delta import WorkspaceSnapshotDeltaError
from local_agent.workspace.snapshot_delta import build_workspace_text_mutation_plan
from local_agent.workspace.snapshot_delta import snapshots_match


class WorkspaceSnapshotDeltaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.before_root = self.root / "before"
        self.after_root = self.root / "after"
        self.before_root.mkdir()
        self.after_root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def snapshot(self, root: Path):
        return capture_workspace_snapshot(root, roots_revision=7)

    def test_builds_canonical_replace_create_delete_plan(self) -> None:
        (self.before_root / "delete.txt").write_text("delete\n", encoding="utf-8")
        (self.before_root / "keep.txt").write_text("keep\n", encoding="utf-8")
        (self.before_root / "replace.txt").write_text("before\n", encoding="utf-8")
        (self.after_root / "create.txt").write_text("create\n", encoding="utf-8")
        (self.after_root / "keep.txt").write_text("keep\n", encoding="utf-8")
        (self.after_root / "replace.txt").write_text("after\n", encoding="utf-8")

        plan = build_workspace_text_mutation_plan(
            self.snapshot(self.before_root),
            self.snapshot(self.after_root),
        )

        self.assertEqual(
            tuple(change.relative_path for change in plan.changes),
            ("create.txt", "delete.txt", "replace.txt"),
        )
        self.assertEqual(
            tuple(change.operation for change in plan.changes),
            ("create", "delete", "replace"),
        )
        self.assertTrue(plan.changed)

    def test_noop_snapshots_match_without_changes(self) -> None:
        (self.before_root / "same.bin").write_bytes(b"\x00\xff")
        (self.after_root / "same.bin").write_bytes(b"\x00\xff")
        before = self.snapshot(self.before_root)
        after = self.snapshot(self.after_root)

        plan = build_workspace_text_mutation_plan(before, after)

        self.assertFalse(plan.changed)
        self.assertTrue(snapshots_match(before, after))

    def test_rejects_directory_and_mode_changes(self) -> None:
        (self.after_root / "new-directory").mkdir()
        with self.assertRaisesRegex(
            WorkspaceSnapshotDeltaError,
            "directory_change_not_supported",
        ):
            build_workspace_text_mutation_plan(
                self.snapshot(self.before_root),
                self.snapshot(self.after_root),
            )

        (self.after_root / "new-directory").rmdir()
        before_file = self.before_root / "mode.txt"
        after_file = self.after_root / "mode.txt"
        before_file.write_text("same\n", encoding="utf-8")
        after_file.write_text("same\n", encoding="utf-8")
        after_file.chmod(0o700)
        with self.assertRaisesRegex(
            WorkspaceSnapshotDeltaError,
            "file_mode_change_not_supported",
        ):
            build_workspace_text_mutation_plan(
                self.snapshot(self.before_root),
                self.snapshot(self.after_root),
            )

    def test_rejects_changed_binary_file(self) -> None:
        (self.before_root / "binary.dat").write_bytes(b"\x00")
        (self.after_root / "binary.dat").write_bytes(b"\xff")

        with self.assertRaisesRegex(
            WorkspaceSnapshotDeltaError,
            "changed_file_not_utf8",
        ):
            build_workspace_text_mutation_plan(
                self.snapshot(self.before_root),
                self.snapshot(self.after_root),
            )

    def test_create_requires_existing_parent_directory(self) -> None:
        parent = self.after_root / "created-parent"
        parent.mkdir()
        (parent / "file.txt").write_text("new\n", encoding="utf-8")

        with self.assertRaisesRegex(
            WorkspaceSnapshotDeltaError,
            "directory_change_not_supported",
        ):
            build_workspace_text_mutation_plan(
                self.snapshot(self.before_root),
                self.snapshot(self.after_root),
            )


if __name__ == "__main__":
    unittest.main()

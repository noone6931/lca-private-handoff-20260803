from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path

from local_agent.workspace.snapshot import SNAPSHOT_MANIFEST_VERSION
from local_agent.workspace.snapshot import WorkspaceSnapshotBudget
from local_agent.workspace.snapshot import WorkspaceSnapshotError
from local_agent.workspace.snapshot import capture_workspace_snapshot


class WorkspaceSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_snapshot_is_canonical_content_addressed_and_repeatable(self) -> None:
        source = self.root / "workspace"
        nested = source / "src"
        nested.mkdir(parents=True)
        script = nested / "run.sh"
        script.write_bytes(b"#!/bin/sh\nprintf ok\n")
        script.chmod(0o755)
        readme = source / "README.md"
        readme.write_text("hello\n", encoding="utf-8")
        readme.chmod(0o644)

        first = capture_workspace_snapshot(source, roots_revision=7)
        second = capture_workspace_snapshot(
            source,
            roots_revision=7,
            expected_root_identity=first.root_identity,
        )

        self.assertEqual(first, second)
        self.assertEqual(first.total_bytes, len(script.read_bytes()) + len(readme.read_bytes()))
        self.assertEqual(
            [entry.relative_path for entry in first.entries],
            ["README.md", "src", "src/run.sh"],
        )
        self.assertEqual(first.entries[-1].mode, 0o755)
        self.assertEqual(len(first.manifest_sha256), 64)
        expected_path = base64.b64encode(b"README.md").decode("ascii")
        self.assertEqual(
            first.entries[0].manifest_line(),
            f"f\t644\t6\t5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03\t{expected_path}\n".encode(
                "ascii"
            ),
        )
        self.assertEqual(SNAPSHOT_MANIFEST_VERSION, "lca-workspace-snapshot-v1")

        readme.write_text("changed\n", encoding="utf-8")
        changed = capture_workspace_snapshot(source, roots_revision=7)
        self.assertNotEqual(changed.manifest_sha256, first.manifest_sha256)

    def test_snapshot_rejects_symlink_hardlink_and_special_entries(self) -> None:
        cases = ("symlink", "hardlink", "fifo")
        for case in cases:
            with self.subTest(case=case):
                source = self.root / case
                source.mkdir()
                original = source / "original"
                original.write_text("value", encoding="utf-8")
                if case == "symlink":
                    (source / "unsafe").symlink_to(original)
                    expected = "unsupported_entry_type"
                elif case == "hardlink":
                    os.link(original, source / "unsafe")
                    expected = "hardlink_not_supported"
                else:
                    os.mkfifo(source / "unsafe")
                    expected = "unsupported_entry_type"

                with self.assertRaises(WorkspaceSnapshotError) as raised:
                    capture_workspace_snapshot(source, roots_revision=1)
                self.assertEqual(raised.exception.kind, expected)

    def test_snapshot_rejects_control_paths_and_budget_overflow(self) -> None:
        control = self.root / "control"
        control.mkdir()
        (control / "bad\nname").write_text("x", encoding="utf-8")
        with self.assertRaises(WorkspaceSnapshotError) as raised:
            capture_workspace_snapshot(control, roots_revision=1)
        self.assertEqual(raised.exception.kind, "path_not_supported")

        budgeted = self.root / "budgeted"
        budgeted.mkdir()
        (budgeted / "large").write_bytes(b"12345")
        with self.assertRaises(WorkspaceSnapshotError) as raised:
            capture_workspace_snapshot(
                budgeted,
                roots_revision=1,
                budget=WorkspaceSnapshotBudget(
                    max_entries=2,
                    max_bytes=4,
                    max_file_bytes=4,
                    max_depth=2,
                ),
            )
        self.assertEqual(raised.exception.kind, "file_budget_exceeded")

    def test_snapshot_requires_canonical_root_and_matching_identity(self) -> None:
        source = self.root / "workspace"
        source.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(source, target_is_directory=True)

        with self.assertRaises(WorkspaceSnapshotError) as raised:
            capture_workspace_snapshot(alias, roots_revision=1)
        self.assertEqual(
            raised.exception.kind,
            "root_must_be_canonical_directory",
        )
        with self.assertRaises(WorkspaceSnapshotError) as raised:
            capture_workspace_snapshot(
                source,
                roots_revision=1,
                expected_root_identity=(1, 2),
            )
        self.assertEqual(raised.exception.kind, "root_identity_changed")

    def test_snapshot_rejects_forbidden_root_and_nested_directory_identities(
        self,
    ) -> None:
        source = self.root / "workspace"
        forbidden = source / "state"
        forbidden.mkdir(parents=True)
        identity = os.stat(forbidden, follow_symlinks=False)
        identities = frozenset({(identity.st_dev, identity.st_ino)})

        with self.assertRaises(WorkspaceSnapshotError) as raised:
            capture_workspace_snapshot(
                source,
                roots_revision=1,
                forbidden_directory_identities=identities,
            )
        self.assertEqual(
            raised.exception.kind,
            "forbidden_directory_identity",
        )

        with self.assertRaises(WorkspaceSnapshotError) as raised:
            capture_workspace_snapshot(
                forbidden,
                roots_revision=1,
                forbidden_directory_identities=identities,
            )
        self.assertEqual(
            raised.exception.kind,
            "forbidden_directory_identity",
        )


if __name__ == "__main__":
    unittest.main()

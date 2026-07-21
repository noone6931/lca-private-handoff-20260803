from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.patch.transaction import ExistingTextFileChange
from local_agent.patch.transaction import apply_existing_text_transaction
from local_agent.patch.anchored import hash_text
from local_agent.tools.base import ToolContext
from local_agent.tools.files import patch_file


class ExistingTextTransactionTests(unittest.TestCase):
    def test_two_file_commit_preserves_existing_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_bytes(b"before-one\n")
            second.write_bytes(b"before-two\n")
            first.chmod(0o640)
            second.chmod(0o600)
            modes = (first.stat().st_mode & 0o777, second.stat().st_mode & 0o777)

            result = apply_existing_text_transaction(
                (
                    ExistingTextFileChange.create(first, first.read_bytes(), b"after-one\n"),
                    ExistingTextFileChange.create(second, second.read_bytes(), b"after-two\n"),
                )
            )

            self.assertEqual(result.status, "committed")
            self.assertTrue(result.workspace_changed)
            self.assertEqual(result.changed_paths, (first, second))
            self.assertEqual(first.read_bytes(), b"after-one\n")
            self.assertEqual(second.read_bytes(), b"after-two\n")
            self.assertEqual((first.stat().st_mode & 0o777, second.stat().st_mode & 0o777), modes)

    def test_stale_file_rejects_entire_transaction_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_bytes(b"one\n")
            second.write_bytes(b"two\n")
            changes = (
                ExistingTextFileChange.create(first, b"one\n", b"new-one\n"),
                ExistingTextFileChange.create(second, b"two\n", b"new-two\n"),
            )
            second.write_bytes(b"external\n")

            result = apply_existing_text_transaction(changes)

            self.assertEqual(result.status, "stale")
            self.assertFalse(result.workspace_changed)
            self.assertEqual(result.changed_paths, ())
            self.assertEqual(first.read_bytes(), b"one\n")
            self.assertEqual(second.read_bytes(), b"external\n")

    def test_second_write_failure_restores_first_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_bytes(b"one\n")
            second.write_bytes(b"two\n")
            changes = (
                ExistingTextFileChange.create(first, b"one\n", b"new-one\n"),
                ExistingTextFileChange.create(second, b"two\n", b"new-two\n"),
            )

            def write(path: Path, content: bytes) -> None:
                if path == second:
                    raise OSError("controlled second-file failure")
                path.write_bytes(content)

            with patch("local_agent.patch.transaction._write_bytes", side_effect=write):
                result = apply_existing_text_transaction(changes)

            self.assertEqual(result.status, "rolled_back")
            self.assertFalse(result.workspace_changed)
            self.assertEqual(result.changed_paths, ())
            self.assertEqual(first.read_bytes(), b"one\n")
            self.assertEqual(second.read_bytes(), b"two\n")

    def test_partial_latest_write_is_compensated_before_earlier_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_bytes(b"one\n")
            second.write_bytes(b"two\n")
            changes = (
                ExistingTextFileChange.create(first, b"one\n", b"new-one\n"),
                ExistingTextFileChange.create(second, b"two\n", b"new-two\n"),
            )
            writes: list[tuple[Path, bytes]] = []

            def partial_second(path: Path, content: bytes) -> None:
                writes.append((path, content))
                path.write_bytes(content)
                if path == second and content == b"new-two\n":
                    raise OSError("controlled partial write")

            with patch("local_agent.patch.transaction._write_bytes", side_effect=partial_second):
                result = apply_existing_text_transaction(changes)

            self.assertEqual(result.status, "rolled_back")
            self.assertEqual(writes[-2:], [(second, b"two\n"), (first, b"one\n")])
            self.assertEqual((first.read_bytes(), second.read_bytes()), (b"one\n", b"two\n"))

    def test_compensation_failure_reports_exact_changed_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_bytes(b"one\n")
            second.write_bytes(b"two\n")
            changes = (
                ExistingTextFileChange.create(first, b"one\n", b"new-one\n"),
                ExistingTextFileChange.create(second, b"two\n", b"new-two\n"),
            )

            def write(path: Path, content: bytes) -> None:
                if path == second or (path == first and content == b"one\n"):
                    raise OSError("controlled write failure")
                path.write_bytes(content)

            with patch("local_agent.patch.transaction._write_bytes", side_effect=write):
                result = apply_existing_text_transaction(changes)

            self.assertEqual(result.status, "rollback_failed")
            self.assertTrue(result.workspace_changed)
            self.assertEqual(result.changed_paths, (first,))
            self.assertEqual(first.read_bytes(), b"new-one\n")
            self.assertEqual(second.read_bytes(), b"two\n")

    def test_duplicate_and_noncanonical_targets_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "target.txt"
            target.write_bytes(b"one\n")
            duplicate = ExistingTextFileChange.create(target, b"one\n", b"two\n")
            result = apply_existing_text_transaction((duplicate, duplicate))
            self.assertEqual((result.status, result.error_kind), ("stale", "duplicate_or_noncanonical_target"))
            self.assertEqual(target.read_bytes(), b"one\n")

            if os.name == "posix":
                link = root / "link.txt"
                link.symlink_to(target)
                linked = ExistingTextFileChange.create(link, b"one\n", b"two\n")
                result = apply_existing_text_transaction((linked,))
                self.assertEqual((result.status, result.error_kind), ("stale", "duplicate_or_noncanonical_target"))
                self.assertEqual(target.read_bytes(), b"one\n")

    def test_anchored_patch_projects_compensation_failure_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "main.py"
            target.write_text("old\n", encoding="utf-8")

            def partial_then_rollback_failure(path: Path, content: bytes) -> None:
                if content == b"new\n":
                    path.write_bytes(content)
                    raise OSError("controlled partial write")
                raise OSError("controlled rollback failure")

            with patch("local_agent.patch.transaction._write_bytes", side_effect=partial_then_rollback_failure):
                result = patch_file(
                    {
                        "path": "main.py",
                        "tag": hash_text("old\n"),
                        "start_line": 1,
                        "end_line": 1,
                        "old_text": "old",
                        "new_text": "new",
                    },
                    ToolContext(root, "yolo"),
                )

            self.assertTrue(result.is_error)
            self.assertTrue(result.metadata["workspace_changed"])
            self.assertEqual(result.metadata["changed_paths"], ["main.py"])
            self.assertEqual(result.metadata["workspace_state"], "indeterminate")
            self.assertEqual(target.read_text(), "new\n")


if __name__ == "__main__":
    unittest.main()

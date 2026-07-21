from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.patch.transaction import ExistingTextFileChange
from local_agent.patch.transaction import apply_existing_text_transaction
from local_agent.patch.anchored import hash_text
from local_agent.evidence.timeline import result_changed_workspace
from local_agent.tools.base import ToolContext
from local_agent.tools.files import patch_file
from local_agent.tools.files import rollback_patch
from local_agent.tools.files import session_patch_records
from local_agent.tools.observation import ToolResultSummary


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

    def test_compensation_exception_after_restore_uses_final_residual_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "target.txt"
            target.write_bytes(b"old\n")
            change = ExistingTextFileChange.create(target, b"old\n", b"new\n")

            def write_then_raise(path: Path, content: bytes) -> None:
                path.write_bytes(content)
                raise OSError("reported after write")

            with patch("local_agent.patch.transaction._write_bytes", side_effect=write_then_raise):
                result = apply_existing_text_transaction((change,))

            self.assertEqual(result.status, "rolled_back")
            self.assertFalse(result.workspace_changed)
            self.assertEqual(result.changed_paths, ())
            self.assertEqual(result.error_kind, "write_failed")
            self.assertEqual(target.read_bytes(), b"old\n")

    def test_compensation_exception_with_residual_remains_rollback_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "target.txt"
            target.write_bytes(b"old\n")
            change = ExistingTextFileChange.create(target, b"old\n", b"new\n")
            calls = 0

            def leave_residual(path: Path, content: bytes) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    path.write_bytes(content)
                raise OSError("controlled failure")

            with patch("local_agent.patch.transaction._write_bytes", side_effect=leave_residual):
                result = apply_existing_text_transaction((change,))

            self.assertEqual(result.status, "rollback_failed")
            self.assertTrue(result.workspace_changed)
            self.assertEqual(result.changed_paths, (target,))
            self.assertEqual(target.read_bytes(), b"new\n")

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

    def test_apply_patch_journal_failure_restores_with_typed_no_net_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "main.py"
            target.write_text("old\n", encoding="utf-8")
            context = ToolContext(root, "yolo", state_dir=root / "state", session_id="session")

            with patch("local_agent.tools.files._record_patch", side_effect=OSError("journal unavailable")):
                result = patch_file(_patch_args("old\n", "new"), context)

            self.assertTrue(result.is_error)
            self.assertEqual(result.metadata["workspace_state"], "restored")
            self.assertEqual(result.metadata["error_kind"], "patch_journal_failed")
            self.assertFalse(result.metadata["workspace_changed"])
            self.assertEqual(result.metadata["changed_paths"], [])
            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")
            self.assertFalse(result_changed_workspace(_summary("apply_patch", result)))

    def test_apply_patch_journal_failure_concurrent_drift_reports_final_residual(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "main.py"
            target.write_text("old\n", encoding="utf-8")
            context = ToolContext(root, "yolo", state_dir=root / "state", session_id="session")

            def drift_then_fail(**_kwargs) -> str:
                target.write_text("external\n", encoding="utf-8")
                raise OSError("journal unavailable")

            with patch("local_agent.tools.files._record_patch", side_effect=drift_then_fail):
                result = patch_file(_patch_args("old\n", "new"), context)

            self.assertTrue(result.is_error)
            self.assertEqual(result.metadata["workspace_state"], "indeterminate")
            self.assertTrue(result.metadata["workspace_changed"])
            self.assertEqual(result.metadata["changed_paths"], ["main.py"])
            self.assertEqual(target.read_text(encoding="utf-8"), "external\n")
            self.assertTrue(result_changed_workspace(_summary("apply_patch", result)))

    def test_single_file_rollback_journal_failure_restores_prior_apply_and_keeps_record_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "main.py"
            target.write_text("old\n", encoding="utf-8")
            context = ToolContext(root, "yolo", state_dir=root / "state", session_id="session")
            applied = patch_file(_patch_args("old\n", "new"), context)
            self.assertFalse(applied.is_error, applied.content)
            patch_id = str(session_patch_records(context)[0]["id"])

            with patch("local_agent.tools.files._record_rollback", side_effect=OSError("journal unavailable")):
                failed = rollback_patch({"patch_id": patch_id}, context)

            self.assertTrue(failed.is_error)
            self.assertEqual(failed.metadata["workspace_state"], "restored")
            self.assertFalse(failed.metadata["workspace_changed"])
            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")
            retried = rollback_patch({"patch_id": patch_id}, context)
            self.assertFalse(retried.is_error, retried.content)
            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

    def test_single_file_rollback_journal_failure_concurrent_drift_reports_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "main.py"
            target.write_text("old\n", encoding="utf-8")
            context = ToolContext(root, "yolo", state_dir=root / "state", session_id="session")
            applied = patch_file(_patch_args("old\n", "new"), context)
            self.assertFalse(applied.is_error, applied.content)
            patch_id = str(session_patch_records(context)[0]["id"])

            def drift_then_fail(*_args, **_kwargs) -> None:
                target.write_text("external\n", encoding="utf-8")
                raise OSError("journal unavailable")

            with patch("local_agent.tools.files._record_rollback", side_effect=drift_then_fail):
                failed = rollback_patch({"patch_id": patch_id}, context)

            self.assertTrue(failed.is_error)
            self.assertEqual(failed.metadata["workspace_state"], "indeterminate")
            self.assertTrue(failed.metadata["workspace_changed"])
            self.assertEqual(failed.metadata["changed_paths"], ["main.py"])
            self.assertEqual(failed.metadata["effective_changed_paths"], ["main.py"])
            self.assertEqual(target.read_text(encoding="utf-8"), "external\n")
            self.assertTrue(result_changed_workspace(_summary("rollback_patch", failed)))


def _patch_args(before: str, after: str) -> dict[str, object]:
    return {
        "path": "main.py",
        "tag": hash_text(before),
        "start_line": 1,
        "end_line": 1,
        "old_text": before.rstrip("\n"),
        "new_text": after,
    }


def _summary(name: str, result) -> ToolResultSummary:
    return ToolResultSummary(
        name,
        result.content,
        is_error=result.is_error,
        metadata=result.metadata,
    )


if __name__ == "__main__":
    unittest.main()

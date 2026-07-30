from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.patch import journal as patch_journal
from local_agent.patch import journal_io as patch_journal_io
from local_agent.patch.anchored import hash_text
from local_agent.patch.journal import append_patch_record
from local_agent.tools.base import ToolContext
from local_agent.tools.files import patch_file, rollback_patch, session_patch_records


class PatchJournalPersistenceTests(unittest.TestCase):
    def test_temporary_write_failure_preserves_old_journal_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "patches" / "session.jsonl"
            journal.parent.mkdir()
            journal.write_bytes(b'{"event":"existing"}\nmalformed')
            before = journal.read_bytes()

            with patch(
                "local_agent.patch.journal_io.write_journal_payload",
                side_effect=OSError("flush failed"),
            ):
                with self.assertRaises(OSError):
                    append_patch_record(journal, {"event": "apply", "id": "new"})

            self.assertEqual(journal.read_bytes(), before)
            self.assertEqual(list(journal.parent.glob(".session.jsonl.*.tmp")), [])

    def test_replace_failure_preserves_old_journal_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "patches" / "session.jsonl"
            journal.parent.mkdir()
            journal.write_bytes(b'{"event":"existing"}\n')
            before = journal.read_bytes()

            with patch("local_agent.patch.journal.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    append_patch_record(journal, {"event": "apply", "id": "new"})

            self.assertEqual(journal.read_bytes(), before)
            self.assertEqual(list(journal.parent.glob(".session.jsonl.*.tmp")), [])

    def test_replace_commits_then_interrupts_and_restores_old_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "patches" / "session.jsonl"
            journal.parent.mkdir()
            journal.write_bytes(b'{"event":"existing"}\n')
            before = journal.read_bytes()
            real_replace = os.replace

            def replace_then_interrupt(source: Path, destination: Path) -> None:
                real_replace(source, destination)
                raise KeyboardInterrupt()

            with (
                patch(
                    "local_agent.patch.journal.os.replace",
                    side_effect=replace_then_interrupt,
                ),
                self.assertRaises(KeyboardInterrupt) as raised,
            ):
                append_patch_record(journal, {"event": "apply", "id": "new"})

            result = raised.exception.patch_journal_result
            self.assertEqual(result.state, "restored")
            self.assertFalse(result.journal_changed)
            self.assertFalse(result.record_persisted)
            self.assertEqual(journal.read_bytes(), before)
            self.assertEqual(list(journal.parent.glob(".session.jsonl.*.tmp")), [])

    def test_success_preserves_existing_bytes_mode_and_uses_replace_as_final_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "patches" / "session.jsonl"
            journal.parent.mkdir()
            journal.write_bytes(b"malformed\n")
            journal.chmod(0o640)
            real_replace = os.replace
            real_write_payload = patch_journal_io.write_journal_payload
            events: list[str] = []

            def tracked_payload(handle, payload: bytes) -> None:
                events.append("write_sync")
                real_write_payload(handle, payload)

            def tracked_replace(source: Path, destination: Path) -> None:
                events.append("replace")
                real_replace(source, destination)

            with (
                patch(
                    "local_agent.patch.journal_io.write_journal_payload",
                    side_effect=tracked_payload,
                ),
                patch("local_agent.patch.journal.os.replace", side_effect=tracked_replace),
            ):
                append_patch_record(journal, {"event": "apply", "id": "new"})

            self.assertEqual(events, ["write_sync", "replace"])
            self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o640)
            lines = journal.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "malformed")
            self.assertEqual(json.loads(lines[1]), {"event": "apply", "id": "new"})

    def test_apply_and_rollback_replace_failures_keep_workspace_and_journal_aligned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "main.py"
            target.write_text("old\n", encoding="utf-8")
            context = ToolContext(root, "yolo", state_dir=root / "state", session_id="session")
            journal = root / "state" / "patches" / "session.jsonl"

            with patch("local_agent.patch.journal.os.replace", side_effect=OSError("replace failed")):
                failed_apply = patch_file(_patch_args("old", "new"), context)
            self.assertTrue(failed_apply.is_error)
            self.assertFalse(failed_apply.metadata["workspace_changed"])
            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")
            self.assertFalse(journal.exists())

            applied = patch_file(_patch_args("old", "new"), context)
            self.assertFalse(applied.is_error, applied.content)
            before_rollback = journal.read_bytes()
            records = session_patch_records(context)
            self.assertEqual([record["event"] for record in records], ["apply"])
            patch_id = records[0]["id"]

            with patch("local_agent.patch.journal.os.replace", side_effect=OSError("replace failed")):
                failed_rollback = rollback_patch({"patch_id": patch_id}, context)
            self.assertTrue(failed_rollback.is_error)
            self.assertFalse(failed_rollback.metadata["workspace_changed"])
            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(journal.read_bytes(), before_rollback)
            self.assertEqual([record["event"] for record in session_patch_records(context)], ["apply"])

            retried = rollback_patch({"patch_id": patch_id}, context)
            self.assertFalse(retried.is_error, retried.content)
            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")
            self.assertEqual(
                [record["event"] for record in session_patch_records(context)],
                ["apply", "rollback"],
            )


def _patch_args(before: str, after: str) -> dict[str, object]:
    return {
        "path": "main.py",
        "tag": hash_text(before + "\n"),
        "start_line": 1,
        "end_line": 1,
        "old_text": before,
        "new_text": after,
    }


if __name__ == "__main__":
    unittest.main()

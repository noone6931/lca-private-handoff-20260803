from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.workspace_migration import WorkspaceMigrationError
from local_agent.workspace_migration import migrate_session_artifacts


class WorkspaceMigrationTests(unittest.TestCase):
    def test_moves_only_current_session_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source"
            target = root / "target"
            session_id = "session-1"
            _write_artifacts(source, session_id)
            other = source / "sessions" / "other.jsonl"
            other.write_text("other\n", encoding="utf-8")

            moves = migrate_session_artifacts(
                source_state_dir=source,
                target_state_dir=target,
                session_id=session_id,
            )

            self.assertEqual(len(moves), 3)
            self.assertFalse((source / "sessions" / f"{session_id}.jsonl").exists())
            self.assertTrue((target / "sessions" / f"{session_id}.jsonl").exists())
            self.assertTrue(other.exists())

    def test_conflict_leaves_all_source_artifacts_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source"
            target = root / "target"
            session_id = "session-1"
            _write_artifacts(source, session_id)
            conflict = target / "sessions" / f"{session_id}.jsonl"
            conflict.parent.mkdir(parents=True)
            conflict.write_text("existing\n", encoding="utf-8")

            with self.assertRaisesRegex(WorkspaceMigrationError, "already exists"):
                migrate_session_artifacts(
                    source_state_dir=source,
                    target_state_dir=target,
                    session_id=session_id,
                )

            self.assertTrue((source / "sessions" / f"{session_id}.jsonl").exists())
            self.assertTrue((source / "todos" / f"{session_id}.json").exists())
            self.assertTrue((source / "patches" / f"{session_id}.jsonl").exists())

    def test_mid_migration_failure_rolls_back_completed_moves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source"
            target = root / "target"
            session_id = "session-1"
            _write_artifacts(source, session_id)
            original_replace = os.replace

            def fail_patch_move(source_path, target_path):
                if Path(source_path).parent.name == "patches":
                    raise OSError("simulated patch move failure")
                return original_replace(source_path, target_path)

            with patch("local_agent.workspace_migration.os.replace", side_effect=fail_patch_move):
                with self.assertRaisesRegex(WorkspaceMigrationError, "simulated patch move failure"):
                    migrate_session_artifacts(
                        source_state_dir=source,
                        target_state_dir=target,
                        session_id=session_id,
                    )

            self.assertTrue((source / "sessions" / f"{session_id}.jsonl").exists())
            self.assertTrue((source / "todos" / f"{session_id}.json").exists())
            self.assertTrue((source / "patches" / f"{session_id}.jsonl").exists())
            self.assertFalse((target / "sessions" / f"{session_id}.jsonl").exists())


def _write_artifacts(state_dir: Path, session_id: str) -> None:
    for category, suffix in (("sessions", ".jsonl"), ("todos", ".json"), ("patches", ".jsonl")):
        path = state_dir / category / f"{session_id}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{category}\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()

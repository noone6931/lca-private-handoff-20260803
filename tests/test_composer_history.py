from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_agent.frontends.composer_history import ComposerHistory
from local_agent.frontends.composer_history import MAX_HISTORY_ENTRY_BYTES


class ComposerHistoryTests(unittest.TestCase):
    def test_append_load_deduplicate_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            history = ComposerHistory(path)

            self.assertTrue(history.append(" first "))
            self.assertFalse(history.append("first"))
            self.assertTrue(history.append("second"))

            self.assertEqual(history.snapshot.persistent_entries, ())
            self.assertEqual(history.snapshot.local_entries, ("first", "second"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded = ComposerHistory(path)
            self.assertEqual(loaded.snapshot.persistent_entries, ("first", "second"))
            self.assertEqual(loaded.snapshot.local_entries, ())
            self.assertFalse(loaded.append("second"))

            path.chmod(0o644)
            ComposerHistory(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_navigation_restores_draft_and_respects_boundaries(self) -> None:
        history = ComposerHistory(None)
        history.append("older")
        history.append("newer")

        self.assertEqual(history.navigate(-1, "", 0), "newer")
        self.assertIsNone(history.navigate(-1, "newer", 2))
        self.assertEqual(history.navigate(-1, "newer", 0), "older")
        self.assertEqual(history.navigate(1, "older", len("older")), "newer")
        self.assertEqual(history.navigate(1, "newer", len("newer")), "")
        self.assertIsNone(history.navigate(-1, "draft", len("draft")))
        self.assertIsNone(history.navigate(-1, "two\nlines", len("two\nlines")))

    def test_edit_reset_allows_new_empty_navigation(self) -> None:
        history = ComposerHistory(None)
        history.append("saved")
        self.assertEqual(history.navigate(-1, "", 0), "saved")

        history.reset_navigation()

        self.assertEqual(history.navigate(-1, "", 0), "saved")

    def test_malformed_partial_and_invalid_utf8_disable_persistence_without_echo(self) -> None:
        payloads = (
            b'{"v":1,"prompt":"secret"}',
            b'{"v":1,"prompt":}\n',
            b"\xff\n",
        )
        for index, payload in enumerate(payloads):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "history.jsonl"
                path.write_bytes(payload)

                history = ComposerHistory(path)

                self.assertFalse(history.snapshot.persistence_enabled)
                self.assertEqual(history.snapshot.persistent_entries, ())
                self.assertNotIn("secret", history.snapshot.error)

    def test_entry_and_count_bounds_compact_in_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "local_agent.frontends.composer_history.MAX_HISTORY_ENTRIES", 2
        ):
            path = Path(tmp) / "history.jsonl"
            history = ComposerHistory(path)
            for value in ("one", "two", "three"):
                history.append(value)

            self.assertEqual(history.snapshot.local_entries, ("two", "three"))
            self.assertEqual(
                ComposerHistory(path).snapshot.persistent_entries,
                ("two", "three"),
            )

        history = ComposerHistory(None)
        self.assertFalse(history.append("x" * (MAX_HISTORY_ENTRY_BYTES + 1)))
        self.assertEqual(history.snapshot.local_entries, ())

    def test_encoded_record_budget_can_reload_a_valid_escape_heavy_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            prompt = '"' * MAX_HISTORY_ENTRY_BYTES

            self.assertTrue(ComposerHistory(path).append(prompt))

            self.assertEqual(ComposerHistory(path).snapshot.persistent_entries, (prompt,))

    def test_total_file_budget_compacts_before_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "local_agent.frontends.composer_history.MAX_HISTORY_FILE_BYTES", 100
        ):
            path = Path(tmp) / "history.jsonl"
            history = ComposerHistory(path)
            history.append("a" * 30)
            history.append("b" * 30)
            history.append("c" * 30)

            self.assertLessEqual(path.stat().st_size, 100)
            self.assertEqual(
                ComposerHistory(path).snapshot.persistent_entries,
                ("b" * 30, "c" * 30),
            )

    def test_write_failure_keeps_local_recall_and_disables_further_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = ComposerHistory(Path(tmp) / "history.jsonl")
            with patch("local_agent.frontends.composer_history.os.open", side_effect=OSError) as opened:
                self.assertTrue(history.append("still local"))
                self.assertTrue(history.append("second local"))

            self.assertEqual(opened.call_count, 1)
            self.assertFalse(history.snapshot.persistence_enabled)
            self.assertEqual(history.snapshot.local_entries, ("still local", "second local"))
            self.assertEqual(history.navigate(-1, "", 0), "second local")

    def test_rebind_clears_partition_state_and_unavailable_target_disables_old_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = ComposerHistory(root / "one.jsonl")
            first.append("old local")
            second_path = root / "two.jsonl"
            second_path.write_text(json.dumps({"v": 1, "prompt": "new persistent"}) + "\n", encoding="utf-8")

            self.assertTrue(first.rebind(second_path))
            self.assertEqual(first.snapshot.persistent_entries, ("new persistent",))
            self.assertEqual(first.snapshot.local_entries, ())

            blocked_parent = root / "not-a-directory"
            blocked_parent.write_text("file", encoding="utf-8")
            self.assertFalse(first.rebind(blocked_parent / "history.jsonl"))
            self.assertFalse(first.snapshot.persistence_enabled)
            self.assertEqual(first.snapshot.persistent_entries, ())
            self.assertEqual(first.snapshot.local_entries, ())


if __name__ == "__main__":
    unittest.main()

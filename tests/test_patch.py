from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_agent.patch.anchored import (
    PatchError,
    apply_anchored_patch,
    format_tagged_read,
    hash_text,
    resolve_workspace_path,
)


class AnchoredPatchTests(unittest.TestCase):
    def test_format_tagged_read_includes_hash_and_line_numbers(self) -> None:
        """Verifies format_tagged_read includes file hash and line numbers."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "app.py"
            text = "print('hi')\n"
            target.write_text(text, encoding="utf-8")

            rendered = format_tagged_read(target, workspace, text)

        self.assertIn(f"[app.py#{hash_text(text)}]", rendered)
        self.assertIn(f"tag: {hash_text(text)}", rendered)
        self.assertIn("1:print('hi')", rendered)

    def test_apply_patch_updates_file_and_returns_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "app.py"
            original = "def main():\n    return 'old'\n"
            target.write_text(original, encoding="utf-8")

            result = apply_anchored_patch(
                workspace=workspace,
                path="app.py",
                tag=hash_text(original),
                start_line=2,
                end_line=2,
                old_text="    return 'old'",
                new_text="    return 'new'",
            )

            self.assertEqual(target.read_text(encoding="utf-8"), "def main():\n    return 'new'\n")
            self.assertIn("-    return 'old'", result.diff)
            self.assertIn("+    return 'new'", result.diff)
            self.assertEqual((result.effective_start_line, result.effective_end_line), (2, 2))

    def test_apply_patch_recovers_five_off_by_range_shapes_from_unique_exact_anchor(self) -> None:
        cases = {
            "short_end": (2, 2),
            "long_end": (2, 5),
            "early_start": (1, 4),
            "late_start": (3, 4),
            "shifted_single": (5, 5),
        }
        original = "prefix\nanchor one\nanchor two\nanchor three\nsuffix\n"
        old_text = "anchor one\nanchor two\nanchor three"
        for name, (start_line, end_line) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp).resolve()
                target = workspace / "notes.txt"
                target.write_text(original, encoding="utf-8")

                result = apply_anchored_patch(
                    workspace=workspace,
                    path="notes.txt",
                    tag=hash_text(original),
                    start_line=start_line,
                    end_line=end_line,
                    old_text=old_text,
                    new_text="replacement",
                )

                self.assertEqual(target.read_text(encoding="utf-8"), "prefix\nreplacement\nsuffix\n")
                self.assertEqual((result.effective_start_line, result.effective_end_line), (2, 4))

    def test_apply_patch_exact_anchor_recovery_fails_closed_when_absent_or_ambiguous(self) -> None:
        cases = {
            "absent": ("prefix\nunique\nsuffix\n", "not present", "no complete exact anchor"),
            "ambiguous": ("prefix\nrepeat\nrepeat\nsuffix\n", "repeat", "multiple exact locations"),
        }
        for name, (original, old_text, message) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp).resolve()
                target = workspace / "notes.txt"
                target.write_text(original, encoding="utf-8")

                with self.assertRaisesRegex(PatchError, message):
                    apply_anchored_patch(
                        workspace=workspace,
                        path="notes.txt",
                        tag=hash_text(original),
                        start_line=1,
                        end_line=1,
                        old_text=old_text,
                        new_text="replacement",
                    )

                self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_apply_patch_dry_run_returns_diff_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "app.py"
            original = "def main():\n    return 'old'\n"
            target.write_text(original, encoding="utf-8")

            result = apply_anchored_patch(
                workspace=workspace,
                path="app.py",
                tag=hash_text(original),
                start_line=2,
                end_line=2,
                old_text="    return 'old'",
                new_text="    return 'new'",
                dry_run=True,
            )

            self.assertEqual(target.read_text(encoding="utf-8"), original)
            self.assertIn("-    return 'old'", result.diff)
            self.assertIn("+    return 'new'", result.diff)
            self.assertEqual(result.new_tag, hash_text("def main():\n    return 'new'\n"))

    def test_apply_patch_dry_run_uses_same_exact_anchor_recovery_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "app.py"
            original = "header\ndef main():\n    return 'old'\nfooter\n"
            target.write_text(original, encoding="utf-8")

            result = apply_anchored_patch(
                workspace=workspace,
                path="app.py",
                tag=hash_text(original),
                start_line=1,
                end_line=1,
                old_text="def main():\n    return 'old'",
                new_text="def main():\n    return 'new'",
                dry_run=True,
            )

            self.assertEqual(target.read_text(encoding="utf-8"), original)
            self.assertEqual((result.effective_start_line, result.effective_end_line), (2, 3))
            self.assertIn("+    return 'new'", result.diff)

    def test_apply_patch_inserts_before_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "README.md"
            original = "# Title\n\nExisting section\n"
            target.write_text(original, encoding="utf-8")

            result = apply_anchored_patch(
                workspace=workspace,
                path="README.md",
                tag=hash_text(original),
                mode="insert_before",
                start_line=3,
                end_line=3,
                old_text="Existing section",
                new_text="Inserted section",
            )

            self.assertEqual(target.read_text(encoding="utf-8"), "# Title\n\nInserted section\nExisting section\n")
            self.assertIn("+Inserted section", result.diff)

    def test_apply_patch_inserts_after_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "README.md"
            original = "# Title\nExisting section\n"
            target.write_text(original, encoding="utf-8")

            result = apply_anchored_patch(
                workspace=workspace,
                path="README.md",
                tag=hash_text(original),
                mode="insert_after",
                start_line=1,
                end_line=1,
                old_text="# Title",
                new_text="Inserted section",
            )

            self.assertEqual(target.read_text(encoding="utf-8"), "# Title\nInserted section\nExisting section\n")
            self.assertIn("+Inserted section", result.diff)

    def test_apply_patch_insert_modes_recover_the_same_unique_multiline_anchor(self) -> None:
        original = "header\nanchor one\nanchor two\nfooter\n"
        expected = {
            "insert_before": "header\ninserted\nanchor one\nanchor two\nfooter\n",
            "insert_after": "header\nanchor one\nanchor two\ninserted\nfooter\n",
        }
        for mode in ("insert_before", "insert_after"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp).resolve()
                target = workspace / "notes.txt"
                target.write_text(original, encoding="utf-8")

                result = apply_anchored_patch(
                    workspace=workspace,
                    path="notes.txt",
                    tag=hash_text(original),
                    mode=mode,
                    start_line=1,
                    end_line=1,
                    old_text="anchor one\nanchor two",
                    new_text="inserted",
                )

                self.assertEqual(target.read_text(encoding="utf-8"), expected[mode])
                self.assertEqual((result.effective_start_line, result.effective_end_line), (2, 3))

    def test_apply_patch_can_use_blank_line_as_insert_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "README.md"
            original = "# Title\n\nExisting section\n"
            target.write_text(original, encoding="utf-8")

            apply_anchored_patch(
                workspace=workspace,
                path="README.md",
                tag=hash_text(original),
                mode="insert_after",
                start_line=2,
                end_line=2,
                old_text="",
                new_text="Inserted section",
            )

            self.assertEqual(target.read_text(encoding="utf-8"), "# Title\n\nInserted section\nExisting section\n")

    def test_apply_patch_inserts_after_last_line_without_trailing_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "README.md"
            original = "title"
            target.write_text(original, encoding="utf-8")

            apply_anchored_patch(
                workspace=workspace,
                path="README.md",
                tag=hash_text(original),
                mode="insert_after",
                start_line=1,
                end_line=1,
                old_text="title",
                new_text="subtitle",
            )

            self.assertEqual(target.read_text(encoding="utf-8"), "title\nsubtitle\n")

    def test_apply_patch_insert_requires_non_empty_new_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "README.md"
            original = "title\n"
            target.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(PatchError, "require non-empty new_text"):
                apply_anchored_patch(
                    workspace=workspace,
                    path="README.md",
                    tag=hash_text(original),
                    mode="insert_after",
                    start_line=1,
                    end_line=1,
                    old_text="title",
                    new_text="",
                )

    def test_apply_patch_handles_last_line_without_trailing_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "README.md"
            original = "title"
            target.write_text(original, encoding="utf-8")

            apply_anchored_patch(
                workspace=workspace,
                path="README.md",
                tag=hash_text(original),
                start_line=1,
                end_line=1,
                old_text="title",
                new_text="new title",
            )

            self.assertEqual(target.read_text(encoding="utf-8"), "new title")

    def test_apply_patch_preserves_crlf_and_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "app.py"
            original = "\ufeffdef main():\r\n    return 'old'\r\n"
            target.write_text(original, encoding="utf-8", newline="")

            result = apply_anchored_patch(
                workspace=workspace,
                path="app.py",
                tag=hash_text(original),
                start_line=1,
                end_line=1,
                old_text="    return 'old'",
                new_text="    return 'new'",
            )

            updated = target.read_bytes().decode("utf-8")
            self.assertEqual(updated, "\ufeffdef main():\r\n    return 'new'\r\n")
            self.assertEqual(result.new_tag, hash_text(updated))
            self.assertEqual((result.effective_start_line, result.effective_end_line), (2, 2))

    def test_stale_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "app.py"
            target.write_text("fresh\n", encoding="utf-8")

            with self.assertRaisesRegex(PatchError, "File changed"):
                apply_anchored_patch(
                    workspace=workspace,
                    path="app.py",
                    tag="deadbeef",
                    start_line=1,
                    end_line=1,
                    old_text="fresh",
                    new_text="updated",
                )

    def test_path_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            with self.assertRaisesRegex(PatchError, "Path escapes workspace"):
                resolve_workspace_path(workspace, "../outside.txt")

    def test_path_escape_hint_names_primary_workspace_when_parent_was_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp).resolve()
            workspace = parent / "repo"
            workspace.mkdir()

            with self.assertRaises(PatchError) as raised:
                resolve_workspace_path(workspace, str(parent))

        message = str(raised.exception)
        self.assertIn(f"Primary workspace (--cwd): {workspace}", message)
        self.assertIn("requested path is a parent of the primary workspace", message)
        self.assertIn("Use '.' for the primary workspace", message)

    def test_path_escape_hint_lists_allowed_directories(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_tmp, tempfile.TemporaryDirectory() as allowed_tmp:
            workspace = Path(workspace_tmp).resolve()
            allowed = Path(allowed_tmp).resolve()

            with self.assertRaises(PatchError) as raised:
                resolve_workspace_path(workspace, "/tmp/outside.txt", (allowed,))

        message = str(raised.exception)
        self.assertIn("Workspace roots:", message)
        self.assertIn(f"Primary workspace (--cwd): {workspace}", message)
        self.assertIn(str(allowed), message)


if __name__ == "__main__":
    unittest.main()

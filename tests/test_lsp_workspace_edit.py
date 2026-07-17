from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.lsp.workspace_edit import MAX_WORKSPACE_EDITS
from local_agent.lsp.workspace_edit import MAX_WORKSPACE_EDIT_FILES
from local_agent.lsp.workspace_edit import MAX_WORKSPACE_FILE_BYTES
from local_agent.lsp.workspace_edit import MAX_WORKSPACE_PREVIEW_BYTES
from local_agent.lsp.workspace_edit import MAX_WORKSPACE_TOTAL_BYTES
from local_agent.lsp.workspace_edit import WorkspaceEditError
from local_agent.lsp.workspace_edit import build_workspace_edit_preview
from local_agent.lsp.workspace_edit import exact_symbol_position


def _position(line: int, character: int) -> dict[str, int]:
    return {"line": line, "character": character}


def _edit(
    start_line: int,
    start_character: int,
    end_line: int,
    end_character: int,
    new_text: str,
) -> dict[str, object]:
    return {
        "range": {
            "start": _position(start_line, start_character),
            "end": _position(end_line, end_character),
        },
        "newText": new_text,
    }


class WorkspaceEditPreviewTests(unittest.TestCase):
    def _preview(
        self,
        workspace: Path,
        payload: object,
        *,
        allowed_roots: tuple[Path, ...] = (),
        project_root: Path | None = None,
    ):
        return build_workspace_edit_preview(
            payload,
            workspace=workspace,
            allowed_roots=allowed_roots,
            project_root=project_root or workspace,
        )

    def test_changes_preview_multiple_files_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            first = workspace / "one.py"
            second = workspace / "two.py"
            first.write_text("old = 1\nprint(old)\n", encoding="utf-8")
            second.write_text("from one import old\n", encoding="utf-8")
            before = {path: path.read_bytes() for path in (first, second)}

            preview = self._preview(
                workspace,
                {
                    "changes": {
                        first.as_uri(): [_edit(0, 0, 0, 3, "new"), _edit(1, 6, 1, 9, "new")],
                        second.as_uri(): [_edit(0, 16, 0, 19, "new")],
                    }
                },
            )

            self.assertEqual(preview.edit_count, 3)
            self.assertEqual(preview.paths, (first, second))
            self.assertIn("+new = 1", preview.unified_diff)
            self.assertIn("+from one import new", preview.unified_diff)
            self.assertEqual({path: path.read_bytes() for path in (first, second)}, before)

    def test_document_changes_text_edits_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.ts"
            target.write_text("const old = 1;\n", encoding="utf-8")
            preview = self._preview(
                workspace,
                {
                    "documentChanges": [
                        {
                            "textDocument": {"uri": target.as_uri(), "version": 7},
                            "edits": [_edit(0, 6, 0, 9, "fresh")],
                        }
                    ]
                },
            )
            self.assertIn("+const fresh = 1;", preview.unified_diff)

    def test_utf16_positions_handle_chinese_and_non_bmp_characters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.ts"
            target.write_text("中😀old = old;\n", encoding="utf-8")
            preview = self._preview(
                workspace,
                {
                    "changes": {
                        target.as_uri(): [
                            _edit(0, 3, 0, 6, "fresh"),
                            _edit(0, 9, 0, 12, "fresh"),
                        ]
                    }
                },
            )
            self.assertIn("+中😀fresh = fresh;", preview.unified_diff)
            position, count = exact_symbol_position("中😀old = old;\n", line=1, symbol="old", occurrence=1)
            self.assertEqual((position.line, position.character, count), (0, 3, 2))

    def test_crlf_and_utf8_bom_source_remain_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.java"
            original = b"\xef\xbb\xbfclass A {\r\n  int old;\r\n}\r\n"
            target.write_bytes(original)
            preview = self._preview(
                workspace,
                {"changes": {target.as_uri(): [_edit(1, 6, 1, 9, "fresh\nname")] }},
            )
            self.assertIn("fresh\r\n", preview.unified_diff)
            self.assertEqual(target.read_bytes(), original)

    def test_multiple_edits_apply_in_reverse_offset_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("alpha beta gamma\n", encoding="utf-8")
            preview = self._preview(
                workspace,
                {
                    "changes": {
                        target.as_uri(): [
                            _edit(0, 0, 0, 5, "a"),
                            _edit(0, 11, 0, 16, "g"),
                        ]
                    }
                },
            )
            self.assertIn("+a beta g", preview.unified_diff)

    def test_invalid_ranges_and_shapes_fail_closed_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("abcdef\n", encoding="utf-8")
            original = target.read_bytes()
            payloads = (
                {"changes": {target.as_uri(): [_edit(0, 1, 0, 4, "x"), _edit(0, 3, 0, 5, "y")]}},
                {"changes": {target.as_uri(): [_edit(0, 1, 0, 4, "x"), _edit(0, 1, 0, 4, "y")]}},
                {"changes": {target.as_uri(): [_edit(0, 5, 0, 2, "x")]}},
                {"changes": {target.as_uri(): [_edit(7, 0, 7, 1, "x")]}},
                {"changes": {target.as_uri(): [_edit(0, 99, 0, 99, "x")]}},
                {"changes": {}, "documentChanges": []},
                {"documentChanges": [{"kind": "create", "uri": target.as_uri()}]},
                {"documentChanges": [{"textDocument": {"uri": target.as_uri(), "version": "latest"}, "edits": []}]},
                {"changes": {target.parent.as_uri() + "/bad%0Aname.py": [_edit(0, 0, 0, 0, "x")]}},
                {"changes": {target.as_uri(): [{"range": {}, "newText": "x", "annotationId": "a"}]}},
            )
            for payload in payloads:
                with self.subTest(payload=json.dumps(payload, sort_keys=True)):
                    with self.assertRaises(WorkspaceEditError):
                        self._preview(workspace, payload)
                    self.assertEqual(target.read_bytes(), original)

    def test_surrogate_pair_boundary_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.ts"
            target.write_text("😀name\n", encoding="utf-8")
            with self.assertRaisesRegex(WorkspaceEditError, "surrogate"):
                self._preview(workspace, {"changes": {target.as_uri(): [_edit(0, 1, 0, 2, "x")]}})

    def test_non_file_uri_and_paths_outside_selected_root_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            project = workspace / "project"
            other = workspace / "other"
            project.mkdir(parents=True)
            other.mkdir()
            target = project / "main.py"
            outside = other / "other.py"
            target.write_text("old\n", encoding="utf-8")
            outside.write_text("old\n", encoding="utf-8")
            with self.assertRaisesRegex(WorkspaceEditError, "file://"):
                self._preview(workspace, {"changes": {"https://host/main.py": [_edit(0, 0, 0, 3, "x")]}}, project_root=project)
            with self.assertRaisesRegex(WorkspaceEditError, "project root"):
                self._preview(workspace, {"changes": {outside.as_uri(): [_edit(0, 0, 0, 3, "x")]}}, project_root=project)

    def test_encoded_controls_query_and_fragment_are_typed_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old\n", encoding="utf-8")
            original = target.read_bytes()
            uris = (
                workspace.as_uri() + "/%00name.py",
                workspace.as_uri() + "/%0Aname.py",
                target.as_uri() + "?version=secret",
                target.as_uri() + "#secret-fragment",
            )
            for uri in uris:
                with self.subTest(uri=uri):
                    with self.assertRaises(WorkspaceEditError) as caught:
                        self._preview(workspace, {"changes": {uri: [_edit(0, 0, 0, 0, "x")]}})
                    self.assertNotIn(uri, str(caught.exception))
                    self.assertNotIn("secret", str(caught.exception))
                    self.assertEqual(target.read_bytes(), original)

    def test_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            project = workspace / "project"
            outside = root / "outside.py"
            project.mkdir(parents=True)
            outside.write_text("old\n", encoding="utf-8")
            link = project / "linked.py"
            link.symlink_to(outside)
            with self.assertRaisesRegex(WorkspaceEditError, "authorized roots"):
                self._preview(workspace, {"changes": {link.as_uri(): [_edit(0, 0, 0, 3, "x")]}}, project_root=project)

    def test_file_edit_byte_and_preview_budgets_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("a\n", encoding="utf-8")
            file_changes = {}
            for index in range(MAX_WORKSPACE_EDIT_FILES + 1):
                path = workspace / f"f{index}.py"
                path.write_text("a\n", encoding="utf-8")
                file_changes[path.as_uri()] = [_edit(0, 0, 0, 1, "b")]
            with self.assertRaisesRegex(WorkspaceEditError, "file preview limit"):
                self._preview(workspace, {"changes": file_changes})

            edits = [_edit(0, 0, 0, 0, str(index)) for index in range(MAX_WORKSPACE_EDITS + 1)]
            with self.assertRaisesRegex(WorkspaceEditError, "edit preview limit"):
                self._preview(workspace, {"changes": {target.as_uri(): edits}})

            with patch("local_agent.lsp.workspace_edit.MAX_WORKSPACE_FILE_BYTES", 1):
                with self.assertRaisesRegex(WorkspaceEditError, "byte file limit"):
                    self._preview(workspace, {"changes": {target.as_uri(): [_edit(0, 0, 0, 1, "b")]}})

            second = workspace / "second.py"
            second.write_text("b\n", encoding="utf-8")
            with patch("local_agent.lsp.workspace_edit.MAX_WORKSPACE_TOTAL_BYTES", 3):
                with self.assertRaisesRegex(WorkspaceEditError, "cumulative input limit"):
                    self._preview(
                        workspace,
                        {
                            "changes": {
                                target.as_uri(): [_edit(0, 0, 0, 1, "b")],
                                second.as_uri(): [_edit(0, 0, 0, 1, "c")],
                            }
                        },
                    )

            with patch("local_agent.lsp.workspace_edit.MAX_WORKSPACE_PREVIEW_BYTES", 4):
                with self.assertRaisesRegex(WorkspaceEditError, "output limit"):
                    self._preview(workspace, {"changes": {target.as_uri(): [_edit(0, 0, 0, 1, "b")]}})
            self.assertLess(MAX_WORKSPACE_PREVIEW_BYTES, 100_000)
            self.assertLess(MAX_WORKSPACE_TOTAL_BYTES, MAX_WORKSPACE_FILE_BYTES * MAX_WORKSPACE_EDIT_FILES)


if __name__ == "__main__":
    unittest.main()

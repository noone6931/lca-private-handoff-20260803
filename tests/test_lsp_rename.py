from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from local_agent.lsp.client import LspClientError, StdioLspClient
from local_agent.lsp.config import LspServerConfig
from local_agent.tools import create_default_registry
from local_agent.tools.base import ToolContext, ToolRegistry
from local_agent.tools.lsp_rename import lsp_rename_tools


class _RenameClient:
    def __init__(self, response: object = None, error: BaseException | None = None):
        self.response = response
        self.error = error
        self.calls: list[tuple[Path, dict[str, int], str]] = []

    def rename(self, path: Path, position: dict[str, int], new_name: str) -> object:
        self.calls.append((path, position, new_name))
        if self.error is not None:
            raise self.error
        return self.response


class LspRenamePreviewToolTests(unittest.TestCase):
    def _server(self, name: str = "test-lsp") -> LspServerConfig:
        return LspServerConfig(
            name=name,
            command=("test-lsp",),
            file_types=(".py",),
            root_markers=("project.marker",),
            language_id="python",
        )

    def _execute(
        self,
        workspace: Path,
        arguments: dict[str, object],
        *,
        client: _RenameClient,
        servers: list[LspServerConfig] | None = None,
        context: ToolContext | None = None,
    ):
        available = servers if servers is not None else [self._server()]
        registry = ToolRegistry(lsp_rename_tools())
        with (
            patch("local_agent.tools.lsp_rename.lsp_config.external_lsp_enabled", return_value=True),
            patch("local_agent.tools.lsp_rename.lsp_config.servers_for_path", return_value=available),
            patch("local_agent.tools.lsp_rename.lsp_config.root_for_path", return_value=workspace),
            patch("local_agent.tools.lsp_rename.get_client", return_value=client),
        ):
            return registry.execute(
                "lsp_rename_preview",
                arguments,
                context or ToolContext(workspace=workspace, approval_mode="yolo"),
            )

    def test_schema_is_read_only_and_registered_in_normal_coding_registry(self) -> None:
        tool = lsp_rename_tools()[0]
        self.assertEqual(tool.name, "lsp_rename_preview")
        self.assertEqual(tool.tier, "read")
        self.assertFalse(tool.input_schema["additionalProperties"])
        self.assertNotIn("apply", tool.input_schema["properties"])
        self.assertIn("lsp_rename_preview", create_default_registry().tool_names())

    def test_exact_symbol_occurrence_is_required_when_line_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old = old\n", encoding="utf-8")
            client = _RenameClient({"changes": {}})
            missing = self._execute(
                workspace,
                {"path": "main.py", "line": 1, "symbol": "old", "new_name": "fresh"},
                client=client,
            )
            self.assertTrue(missing.is_error)
            self.assertIn("provide occurrence from 1 to 2", missing.content)
            self.assertEqual(client.calls, [])

            response = {"changes": {target.as_uri(): [{
                "range": {"start": {"line": 0, "character": 6}, "end": {"line": 0, "character": 9}},
                "newText": "fresh",
            }]}}
            client = _RenameClient(response)
            result = self._execute(
                workspace,
                {
                    "path": "main.py",
                    "line": 1,
                    "symbol": "old",
                    "new_name": "fresh",
                    "occurrence": 2,
                },
                client=client,
            )
            self.assertFalse(result.is_error, result.content)
            self.assertEqual(client.calls[0][1], {"line": 0, "character": 6})

    def test_valid_external_rename_returns_bounded_multifile_preview_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            first = workspace / "one.py"
            second = workspace / "two.py"
            first.write_text("old = 1\n", encoding="utf-8")
            second.write_text("print(old)\n", encoding="utf-8")
            before = {path: path.read_bytes() for path in (first, second)}
            response = {
                "changes": {
                    first.as_uri(): [{
                        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}},
                        "newText": "fresh",
                    }],
                    second.as_uri(): [{
                        "range": {"start": {"line": 0, "character": 6}, "end": {"line": 0, "character": 9}},
                        "newText": "fresh",
                    }],
                }
            }
            result = self._execute(
                workspace,
                {"path": "one.py", "line": 1, "symbol": "old", "new_name": "fresh"},
                client=_RenameClient(response),
            )
            self.assertFalse(result.is_error, result.content)
            self.assertIn("Semantic rename preview (in-memory/read-only", result.content)
            self.assertIn("LCA did not apply a WorkspaceEdit or execute a command", result.content)
            self.assertNotIn("no files were written", result.content)
            self.assertIn("Files: 2; edits: 2", result.content)
            self.assertIn("apply_patch separately", result.content)
            self.assertEqual(result.metadata["preview"], True)
            self.assertEqual(result.metadata["read_only"], True)
            self.assertEqual(result.metadata["evidence_eligible"], False)
            self.assertEqual(result.metadata["file_count"], 2)
            self.assertEqual({path: path.read_bytes() for path in (first, second)}, before)
            self.assertFalse((workspace / ".local-agent").exists())

    def test_external_server_is_required_and_selection_is_unambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old\n", encoding="utf-8")
            arguments = {"path": "main.py", "line": 1, "symbol": "old", "new_name": "fresh"}
            registry = ToolRegistry(lsp_rename_tools())
            with patch("local_agent.tools.lsp_rename.lsp_config.external_lsp_enabled", return_value=False):
                disabled = registry.execute(
                    "lsp_rename_preview",
                    arguments,
                    ToolContext(workspace=workspace, approval_mode="yolo"),
                )
            self.assertTrue(disabled.is_error)
            self.assertIn("requires an external LSP", disabled.content)

            no_server = self._execute(workspace, arguments, client=_RenameClient(), servers=[])
            self.assertTrue(no_server.is_error)
            self.assertIn("No external LSP server", no_server.content)

            servers = [self._server("one"), self._server("two")]
            ambiguous = self._execute(workspace, arguments, client=_RenameClient(), servers=servers)
            self.assertTrue(ambiguous.is_error)
            self.assertIn("Multiple LSP servers", ambiguous.content)
            unknown = self._execute(
                workspace,
                {**arguments, "server": "three"},
                client=_RenameClient(),
                servers=servers,
            )
            self.assertTrue(unknown.is_error)
            self.assertIn("Available servers: one, two", unknown.content)

            response = {"changes": {target.as_uri(): [{
                "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}},
                "newText": "fresh",
            }]}}
            selected_client = _RenameClient(response)
            selected = self._execute(
                workspace,
                {**arguments, "server": "two"},
                client=selected_client,
                servers=servers,
            )
            self.assertFalse(selected.is_error, selected.content)
            self.assertEqual(selected.metadata["server"], "two")
            self.assertEqual(len(selected_client.calls), 1)

    def test_null_empty_malformed_and_server_errors_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old\n", encoding="utf-8")
            arguments = {"path": "main.py", "line": 1, "symbol": "old", "new_name": "fresh"}
            for response in (None, {}, [], "not-an-edit"):
                with self.subTest(response=response):
                    result = self._execute(workspace, arguments, client=_RenameClient(response))
                    self.assertTrue(result.is_error)
            failure = self._execute(
                workspace,
                arguments,
                client=_RenameClient(error=LspClientError("secret /outside/server/path")),
            )
            self.assertTrue(failure.is_error)
            self.assertIn("LspClientError", failure.content)
            self.assertNotIn("/outside/server/path", failure.content)

    def test_line_occurrence_and_new_name_validation_do_not_guess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old\n", encoding="utf-8")
            base = {"path": "main.py", "line": 1, "symbol": "old", "new_name": "fresh"}
            client = _RenameClient()
            cases = (
                ({**base, "line": 2}, "exceeds the file length"),
                ({**base, "symbol": "missing"}, "does not occur"),
                ({**base, "occurrence": 2}, "outside the available range"),
                ({**base, "new_name": "bad\nname"}, "cannot contain"),
            )
            for arguments, expected in cases:
                with self.subTest(arguments=arguments):
                    result = self._execute(workspace, arguments, client=client)
                    self.assertTrue(result.is_error)
                    self.assertIn(expected, result.content)
            self.assertEqual(client.calls, [])

    def test_per_tool_execution_policy_deny_prevents_lsp_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old\n", encoding="utf-8")
            client = _RenameClient()
            context = ToolContext(
                workspace=workspace,
                approval_mode="yolo",
                tool_approval={"lsp_rename_preview": "deny"},
            )
            result = self._execute(
                workspace,
                {"path": "main.py", "line": 1, "symbol": "old", "new_name": "fresh"},
                client=client,
                context=context,
            )
            self.assertTrue(result.is_error)
            self.assertEqual(result.metadata["execution_status"], "denied")
            self.assertEqual(client.calls, [])

    def test_complete_tool_result_obeys_encoded_output_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old\n", encoding="utf-8")
            response = {"changes": {target.as_uri(): [{
                "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}},
                "newText": "fresh",
            }]}}
            with patch("local_agent.tools.lsp_rename.MAX_WORKSPACE_PREVIEW_BYTES", 32):
                result = self._execute(
                    workspace,
                    {"path": "main.py", "line": 1, "symbol": "old", "new_name": "fresh"},
                    client=_RenameClient(response),
                )
            self.assertTrue(result.is_error)
            self.assertIn("complete output limit", result.content)

    def test_client_rename_uses_did_open_and_text_document_rename(self) -> None:
        client = object.__new__(StdioLspClient)
        client.ensure_open = Mock()
        client.request = Mock(return_value={"changes": {}})
        target = Path("/tmp/main.py")
        result = client.rename(target, {"line": 2, "character": 4}, "fresh", timeout=3.0)
        client.ensure_open.assert_called_once_with(target)
        client.request.assert_called_once_with(
            "textDocument/rename",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 2, "character": 4},
                "newName": "fresh",
            },
            timeout=3.0,
        )
        self.assertEqual(result, {"changes": {}})


if __name__ == "__main__":
    unittest.main()

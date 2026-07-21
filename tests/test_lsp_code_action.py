from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from local_agent.lsp.client import CODE_ACTION_KINDS, LspClientError, StdioLspClient
from local_agent.lsp.config import LspServerConfig
from local_agent.lsp.workspace_edit_store import default_workspace_edit_plan_store
from local_agent.tools import create_default_registry
from local_agent.tools.base import ToolContext, ToolRegistry
from local_agent.tools.lsp_code_action import MAX_CODE_ACTIONS
from local_agent.tools.lsp_code_action import lsp_code_action_tools


def _range_edit(
    start_line: int,
    start_character: int,
    end_line: int,
    end_character: int,
    new_text: str,
) -> dict[str, object]:
    return {
        "range": {
            "start": {"line": start_line, "character": start_character},
            "end": {"line": end_line, "character": end_character},
        },
        "newText": new_text,
    }


class _CodeActionClient:
    def __init__(
        self,
        response: object = None,
        *,
        resolve_response: object = None,
        request_error: BaseException | None = None,
        resolve_error: BaseException | None = None,
    ):
        self.response = response
        self.resolve_response = resolve_response
        self.request_error = request_error
        self.resolve_error = resolve_error
        self.calls: list[tuple[Path, dict[str, int], str | None]] = []
        self.resolve_calls: list[dict[str, object]] = []

    def code_actions(self, path: Path, position: dict[str, int], *, kind: str | None = None) -> object:
        self.calls.append((path, position, kind))
        if self.request_error is not None:
            raise self.request_error
        return self.response

    def resolve_code_action(self, action: dict[str, object]) -> object:
        self.resolve_calls.append(action)
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.resolve_response


class LspCodeActionPreviewTests(unittest.TestCase):
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
        client: _CodeActionClient,
        servers: list[LspServerConfig] | None = None,
        context: ToolContext | None = None,
    ):
        available = servers if servers is not None else [self._server()]
        registry = ToolRegistry(lsp_code_action_tools())
        with (
            patch("local_agent.tools.lsp_code_action.lsp_config.external_lsp_enabled", return_value=True),
            patch("local_agent.tools.lsp_code_action.lsp_config.servers_for_path", return_value=available),
            patch("local_agent.tools.lsp_code_action.lsp_config.root_for_path", return_value=workspace),
            patch("local_agent.tools.lsp_code_action.get_client", return_value=client),
        ):
            return registry.execute(
                "lsp_code_action_preview",
                arguments,
                context or ToolContext(workspace=workspace, approval_mode="yolo"),
            )

    def test_schema_is_read_only_registered_and_has_no_apply_or_query(self) -> None:
        tool = lsp_code_action_tools()[0]
        self.assertEqual(tool.name, "lsp_code_action_preview")
        self.assertEqual(tool.tier, "read")
        self.assertFalse(tool.input_schema["additionalProperties"])
        self.assertNotIn("apply", tool.input_schema["properties"])
        self.assertNotIn("query", tool.input_schema["properties"])
        self.assertIn("lsp_code_action_preview", create_default_registry().tool_names())

    def test_list_is_bounded_stable_and_redacts_raw_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old = 1\n", encoding="utf-8")
            actions = [
                {
                    "title": f"Action {index}",
                    "kind": "quickfix",
                    "isPreferred": index == 0,
                    "diagnostics": [{"message": "RAW_DIAGNOSTIC_SECRET"}],
                    "data": {"token": "RAW_DATA_SECRET"},
                }
                for index in range(MAX_CODE_ACTIONS + 5)
            ]
            client = _CodeActionClient(actions)
            result = self._execute(
                workspace,
                {"path": "main.py", "line": 1, "symbol": "old", "kind": "quickfix"},
                client=client,
            )
            self.assertFalse(result.is_error, result.content)
            payload = json.loads(result.content)
            self.assertEqual(payload["shown"], MAX_CODE_ACTIONS)
            self.assertEqual(payload["total"], MAX_CODE_ACTIONS + 5)
            self.assertTrue(payload["truncated"])
            self.assertEqual([item["index"] for item in payload["actions"]], list(range(MAX_CODE_ACTIONS)))
            self.assertEqual(client.calls[0][2], "quickfix")
            self.assertNotIn("RAW_DIAGNOSTIC_SECRET", result.content)
            self.assertNotIn("RAW_DATA_SECRET", result.content)
            self.assertNotIn("diagnostics", result.content)
            self.assertNotIn("data", result.content)
            self.assertEqual(result.metadata["preview"], True)
            self.assertEqual(result.metadata["read_only"], True)
            self.assertEqual(result.metadata["evidence_eligible"], False)

    def test_list_command_metadata_never_exposes_command_or_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old\n", encoding="utf-8")
            command = {
                "title": "Run formatter",
                "command": "SECRET_COMMAND",
                "arguments": ["SECRET_ARGUMENT"],
            }
            result = self._execute(
                workspace,
                {"path": "main.py", "line": 1, "symbol": "old"},
                client=_CodeActionClient([command]),
            )
            self.assertFalse(result.is_error, result.content)
            payload = json.loads(result.content)
            self.assertTrue(payload["actions"][0]["command_present"])
            self.assertNotIn("SECRET_COMMAND", result.content)
            self.assertNotIn("SECRET_ARGUMENT", result.content)

    def test_direct_text_edit_returns_preview_without_writing_or_patch_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old = 1\n", encoding="utf-8")
            before = target.read_bytes()
            action = {
                "title": "Use fresh name",
                "kind": "quickfix",
                "edit": {"changes": {target.as_uri(): [_range_edit(0, 0, 0, 3, "fresh")] }},
            }
            client = _CodeActionClient([action])
            plan_snapshot = default_workspace_edit_plan_store().snapshot()
            result = self._execute(
                workspace,
                {"path": "main.py", "line": 1, "symbol": "old", "action_index": 0},
                client=client,
            )
            self.assertFalse(result.is_error, result.content)
            self.assertIn("Semantic code action preview", result.content)
            self.assertIn("LCA did not apply a WorkspaceEdit or execute a command", result.content)
            self.assertNotIn("no files were written", result.content)
            self.assertIn("+fresh = 1", result.content)
            self.assertEqual(target.read_bytes(), before)
            self.assertFalse((workspace / ".local-agent").exists())
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(client.resolve_calls, [])
            self.assertEqual(result.metadata["mode"], "preview")
            self.assertEqual(result.metadata["evidence_eligible"], False)
            self.assertNotIn("plan_id", result.metadata)
            self.assertEqual(default_workspace_edit_plan_store().snapshot(), plan_snapshot)

    def test_resolve_once_requires_same_title_and_text_only_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old\n", encoding="utf-8")
            unresolved = {"title": "Resolve fix", "kind": "quickfix", "data": {"opaque": "SECRET"}}
            resolved = {
                "title": "Resolve fix",
                "kind": "quickfix",
                "edit": {"changes": {target.as_uri(): [_range_edit(0, 0, 0, 3, "fresh")] }},
                "data": {"opaque": "SECRET"},
            }
            client = _CodeActionClient([unresolved], resolve_response=resolved)
            result = self._execute(
                workspace,
                {"path": "main.py", "line": 1, "symbol": "old", "action_index": 0},
                client=client,
            )
            self.assertFalse(result.is_error, result.content)
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(len(client.resolve_calls), 1)
            self.assertNotIn("SECRET", result.content)

    def test_command_disabled_and_edit_plus_command_are_refused_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old\n", encoding="utf-8")
            edit = {"changes": {target.as_uri(): [_range_edit(0, 0, 0, 3, "fresh")]}}
            cases = (
                ({"title": "Run", "command": "do.run", "arguments": []}, "Command items"),
                ({"title": "Disabled", "disabled": {"reason": "not available"}, "edit": edit}, "Disabled"),
                (
                    {
                        "title": "Mixed",
                        "edit": edit,
                        "command": {"title": "Run", "command": "do.run", "arguments": []},
                    },
                    "containing commands",
                ),
            )
            for action, expected in cases:
                with self.subTest(action=action["title"]):
                    client = _CodeActionClient([action])
                    result = self._execute(
                        workspace,
                        {"path": "main.py", "line": 1, "symbol": "old", "action_index": 0},
                        client=client,
                    )
                    self.assertTrue(result.is_error)
                    self.assertIn(expected, result.content)
                    self.assertEqual(client.resolve_calls, [])

    def test_resolve_title_swap_missing_edit_command_and_error_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old\n", encoding="utf-8")
            unresolved = {"title": "Original", "data": {"opaque": 1}}
            cases = (
                ({"title": "Changed", "edit": {"changes": {}}}, None, "title identity"),
                ({"title": "Original", "data": {"opaque": 1}}, None, "no text-only"),
                (
                    {"title": "Original", "command": {"title": "Run", "command": "do.run"}},
                    None,
                    "containing commands",
                ),
                (None, LspClientError("RAW_SERVER_SECRET"), "failed safely"),
            )
            for resolved, error, expected in cases:
                with self.subTest(expected=expected):
                    client = _CodeActionClient([unresolved], resolve_response=resolved, resolve_error=error)
                    result = self._execute(
                        workspace,
                        {"path": "main.py", "line": 1, "symbol": "old", "action_index": 0},
                        client=client,
                    )
                    self.assertTrue(result.is_error)
                    self.assertIn(expected, result.content)
                    self.assertNotIn("RAW_SERVER_SECRET", result.content)
                    self.assertEqual(len(client.resolve_calls), 1)

    def test_invalid_action_shapes_fail_closed_without_raw_payload_echo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old\n", encoding="utf-8")
            cases = (
                "not-an-array",
                ["not-an-object"],
                [{"title": "Bad", "unknown": "RAW_SECRET"}],
                [{"title": "Bad\nTitle"}],
                [{"title": "Bad", "diagnostics": "RAW_SECRET"}],
                [{"title": "Bad", "command": {"command": "missing-title"}}],
                [{"title": "Bad", "edit": "RAW_SECRET"}],
            )
            for response in cases:
                with self.subTest(response=response):
                    result = self._execute(
                        workspace,
                        {"path": "main.py", "line": 1, "symbol": "old"},
                        client=_CodeActionClient(response),
                    )
                    self.assertTrue(result.is_error)
                    self.assertNotIn("RAW_SECRET", result.content)

    def test_workspace_edit_utf16_crlf_bom_and_multifile_validation_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            first = workspace / "main.py"
            second = workspace / "other.py"
            first.write_bytes(b"\xef\xbb\xbfold = 1\r\n")
            second.write_text("😀old\n", encoding="utf-8")
            before = {path: path.read_bytes() for path in (first, second)}
            action = {
                "title": "Rename both",
                "edit": {
                    "documentChanges": [
                        {
                            "textDocument": {"uri": first.as_uri(), "version": 1},
                            "edits": [_range_edit(0, 0, 0, 3, "fresh")],
                        },
                        {
                            "textDocument": {"uri": second.as_uri(), "version": 1},
                            "edits": [_range_edit(0, 2, 0, 5, "fresh")],
                        },
                    ]
                },
            }
            result = self._execute(
                workspace,
                {"path": "main.py", "line": 1, "symbol": "old", "action_index": 0},
                client=_CodeActionClient([action]),
            )
            self.assertFalse(result.is_error, result.content)
            self.assertIn("+fresh = 1", result.content)
            self.assertIn("+😀fresh", result.content)
            self.assertEqual({path: path.read_bytes() for path in (first, second)}, before)

    def test_invalid_workspace_edits_never_return_partial_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            outside = root / "outside.py"
            workspace.mkdir()
            target = workspace / "main.py"
            target.write_text("old\n", encoding="utf-8")
            outside.write_text("old\n", encoding="utf-8")
            link = workspace / "linked.py"
            link.symlink_to(outside)
            before = target.read_bytes()
            edits = (
                {"changes": {target.as_uri(): [_range_edit(0, 0, 0, 2, "a"), _range_edit(0, 1, 0, 3, "b")]}},
                {"changes": {outside.as_uri(): [_range_edit(0, 0, 0, 3, "fresh")]}},
                {"changes": {link.as_uri(): [_range_edit(0, 0, 0, 3, "fresh")]}},
                {"changes": {"https://host/main.py": [_range_edit(0, 0, 0, 3, "fresh")]}},
                {"documentChanges": [{"kind": "create", "uri": target.as_uri()}]},
                {"changes": {target.as_uri(): [_range_edit(0, 99, 0, 99, "fresh")]}},
            )
            for edit in edits:
                with self.subTest(edit=edit):
                    action = {"title": "Unsafe", "edit": edit}
                    result = self._execute(
                        workspace,
                        {"path": "main.py", "line": 1, "symbol": "old", "action_index": 0},
                        client=_CodeActionClient([action]),
                    )
                    self.assertTrue(result.is_error)
                    self.assertNotIn("Semantic code action preview", result.content)
                    self.assertEqual(target.read_bytes(), before)

    def test_workspace_edit_cumulative_byte_budget_propagates_to_code_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            first = workspace / "main.py"
            second = workspace / "other.py"
            first.write_text("a\n", encoding="utf-8")
            second.write_text("b\n", encoding="utf-8")
            action = {
                "title": "Bounded",
                "edit": {
                    "changes": {
                        first.as_uri(): [_range_edit(0, 0, 0, 1, "x")],
                        second.as_uri(): [_range_edit(0, 0, 0, 1, "y")],
                    }
                },
            }
            with patch("local_agent.lsp.workspace_edit.MAX_WORKSPACE_TOTAL_BYTES", 3):
                result = self._execute(
                    workspace,
                    {"path": "main.py", "line": 1, "symbol": "a", "action_index": 0},
                    client=_CodeActionClient([action]),
                )
            self.assertTrue(result.is_error)
            self.assertIn("cumulative input limit", result.content)
            self.assertEqual(first.read_text(encoding="utf-8"), "a\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "b\n")

    def test_lone_surrogate_workspace_edit_is_typed_through_code_action_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old\n", encoding="utf-8")
            action = {
                "title": "Invalid replacement",
                "edit": {"changes": {target.as_uri(): [_range_edit(0, 0, 0, 3, "\ud800")]}},
            }

            result = self._execute(
                workspace,
                {"path": "main.py", "line": 1, "symbol": "old", "action_index": 0},
                client=_CodeActionClient([action]),
            )

            self.assertTrue(result.is_error)
            self.assertIn("valid UTF-8 encodable text", result.content)
            self.assertNotIn("Semantic code action preview", result.content)
            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

    def test_external_server_and_exact_position_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old = old\n", encoding="utf-8")
            registry = ToolRegistry(lsp_code_action_tools())
            arguments = {"path": "main.py", "line": 1, "symbol": "old"}
            with patch("local_agent.tools.lsp_code_action.lsp_config.external_lsp_enabled", return_value=False):
                disabled = registry.execute(
                    "lsp_code_action_preview",
                    arguments,
                    ToolContext(workspace=workspace, approval_mode="yolo"),
                )
            self.assertTrue(disabled.is_error)
            ambiguous = self._execute(workspace, arguments, client=_CodeActionClient([]))
            self.assertTrue(ambiguous.is_error)
            self.assertIn("provide occurrence", ambiguous.content)
            self.assertEqual(self._execute(workspace, {**arguments, "occurrence": 1}, client=_CodeActionClient([]), servers=[]).is_error, True)

    def test_per_tool_deny_prevents_all_lsp_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old\n", encoding="utf-8")
            client = _CodeActionClient([])
            context = ToolContext(
                workspace=workspace,
                approval_mode="yolo",
                tool_approval={"lsp_code_action_preview": "deny"},
            )
            result = self._execute(
                workspace,
                {"path": "main.py", "line": 1, "symbol": "old"},
                client=client,
                context=context,
            )
            self.assertTrue(result.is_error)
            self.assertEqual(result.metadata["execution_status"], "denied")
            self.assertEqual(client.calls, [])
            self.assertEqual(client.resolve_calls, [])

    def test_client_uses_diagnostic_snapshot_kind_filter_and_resolve_request(self) -> None:
        client = object.__new__(StdioLspClient)
        target = Path("/tmp/main.py")
        client._diagnostics = {target.as_uri(): [{"message": "existing"}]}
        client.ensure_open = Mock()
        client.request = Mock(side_effect=[[{"title": "Fix"}], {"title": "Fix", "edit": {}}])

        actions = client.code_actions(target, {"line": 2, "character": 4}, kind="quickfix", timeout=3.0)
        resolved = client.resolve_code_action({"title": "Fix", "data": {"id": 1}}, timeout=2.0)

        self.assertEqual(actions, [{"title": "Fix"}])
        self.assertEqual(resolved, {"title": "Fix", "edit": {}})
        client.ensure_open.assert_called_once_with(target)
        first = client.request.call_args_list[0]
        self.assertEqual(first.args[0], "textDocument/codeAction")
        self.assertEqual(first.args[1]["range"], {
            "start": {"line": 2, "character": 4},
            "end": {"line": 2, "character": 4},
        })
        self.assertEqual(first.args[1]["context"], {
            "diagnostics": [{"message": "existing"}],
            "triggerKind": 1,
            "only": ["quickfix"],
        })
        client.request.assert_called_with(
            "codeAction/resolve",
            {"title": "Fix", "data": {"id": 1}},
            timeout=2.0,
        )

    def test_initialize_advertises_literal_code_action_and_edit_resolve_capabilities(self) -> None:
        client = object.__new__(StdioLspClient)
        client.workspace = Path("/tmp/workspace")
        client.request = Mock(return_value={"capabilities": {}})
        client.notify = Mock()
        client._wait_for_project_load = Mock()

        client._initialize()

        initialize = client.request.call_args
        self.assertEqual(initialize.args[0], "initialize")
        code_action = initialize.args[1]["capabilities"]["textDocument"]["codeAction"]
        self.assertEqual(code_action, {
            "dynamicRegistration": False,
            "codeActionLiteralSupport": {
                "codeActionKind": {"valueSet": list(CODE_ACTION_KINDS)},
            },
            "dataSupport": True,
            "resolveSupport": {"properties": ["edit"]},
        })
        client.notify.assert_called_once_with("initialized", {})

    def test_resolve_rejects_inflight_workspace_apply_edit_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "main.py"
            target.write_text("old = 1\n", encoding="utf-8")
            before = target.read_bytes()
            client = object.__new__(StdioLspClient)
            client._next_id = 1
            client._send = Mock()
            client._read_message = Mock(side_effect=[
                {
                    "jsonrpc": "2.0",
                    "id": 700,
                    "method": "workspace/applyEdit",
                    "params": {
                        "edit": {
                            "changes": {
                                target.as_uri(): [_range_edit(0, 0, 0, 3, "unsafe")],
                            },
                        },
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"title": "Fix", "edit": {}},
                },
            ])

            resolved = client.resolve_code_action({"title": "Fix", "data": {"id": 1}})

            self.assertEqual(resolved, {"title": "Fix", "edit": {}})
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(client._send.call_count, 2)
            self.assertEqual(client._send.call_args_list[0].args[0]["method"], "codeAction/resolve")
            self.assertEqual(client._send.call_args_list[1].args[0], {
                "jsonrpc": "2.0",
                "id": 700,
                "result": {
                    "applied": False,
                    "failureReason": "LCA external LSP client is read-only.",
                },
            })


if __name__ == "__main__":
    unittest.main()

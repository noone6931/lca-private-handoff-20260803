from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.inventory_contract import MAX_INVENTORY_GLOB_PATHS, inventory_glob_call_hint
from local_agent.lsp.client import StdioLspClient
from local_agent.lsp.client import close_all_clients
from local_agent.lsp.config import LspServerConfig
from local_agent.patch.anchored import hash_text
from local_agent.tools import create_default_registry
from local_agent.tools.base import Tool, ToolContext, ToolRegistry, ToolResult, VisionInspectionUnavailableError
from local_agent.tools.files import file_tools, inspect_image, patch_file, read_file, rollback_patch, write_file
from local_agent.tools.git import capture_git_baseline, git_diff, git_status
from local_agent.tools.interaction import ask_user
from local_agent.tools.lsp import lsp_definition, lsp_diagnostics, lsp_references, lsp_status, lsp_symbols, lsp_tools
from local_agent.tools.memory import learn, memory_read
from local_agent.tools.search import glob_files
from local_agent.tools.search import list_files
from local_agent.tools.search import search_code
from local_agent.tools.search import search_tools
from local_agent.tools.shell import run_shell, run_tests, shell_tools
from local_agent.tools.todo import todo_add, todo_read, todo_tools, todo_update


class _FakeStdin:
    def __init__(self, *lines: str):
        self._lines = list(lines)

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        if not self._lines:
            return ""
        return self._lines.pop(0)


def _record_command(args: dict[str, str], received: list[dict[str, str]]) -> ToolResult:
    received.append(args)
    return ToolResult("command accepted")


def _write_fake_lsp_server(path: Path) -> None:
    path.write_text(
        r'''
from __future__ import annotations

import json
import sys


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return None
        if line in {b"\r\n", b"\n"}:
            break
        decoded = line.decode("ascii").strip()
        if ":" in decoded:
            key, value = decoded.split(":", 1)
            headers[key.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def send(payload):
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()


while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "capabilities": {
                    "documentSymbolProvider": True,
                    "definitionProvider": True,
                    "referencesProvider": True,
                }
            },
        })
    elif method == "textDocument/didOpen":
        uri = message["params"]["textDocument"]["uri"]
        send({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": uri,
                "diagnostics": [
                    {
                        "range": {
                            "start": {"line": 1, "character": 2},
                            "end": {"line": 1, "character": 8},
                        },
                        "severity": 2,
                        "message": "fake diagnostic",
                    }
                ],
            },
        })
    elif method == "textDocument/documentSymbol":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": [
                {
                    "name": "loadUser",
                    "kind": 12,
                    "range": {
                        "start": {"line": 0, "character": 16},
                        "end": {"line": 2, "character": 1},
                    },
                    "selectionRange": {
                        "start": {"line": 0, "character": 16},
                        "end": {"line": 0, "character": 24},
                    },
                }
            ],
        })
    elif method == "textDocument/definition":
        uri = message["params"]["textDocument"]["uri"]
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "uri": uri,
                "range": {
                    "start": {"line": 0, "character": 16},
                    "end": {"line": 0, "character": 24},
                },
            },
        })
    elif method == "textDocument/references":
        uri = message["params"]["textDocument"]["uri"]
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": [
                {
                    "uri": uri,
                    "range": {
                        "start": {"line": 0, "character": 16},
                        "end": {"line": 0, "character": 24},
                    },
                },
                {
                    "uri": uri,
                    "range": {
                        "start": {"line": 1, "character": 9},
                        "end": {"line": 1, "character": 17},
                    },
                },
            ],
        })
    else:
        if request_id is not None:
            send({"jsonrpc": "2.0", "id": request_id, "result": None})
'''.lstrip(),
        encoding="utf-8",
    )


def _write_fake_lsp_server_requiring_workspace_folders(path: Path) -> None:
    path.write_text(
        r'''
from __future__ import annotations

import json
import sys


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return None
        if line in {b"\r\n", b"\n"}:
            break
        decoded = line.decode("ascii").strip()
        if ":" in decoded:
            key, value = decoded.split(":", 1)
            headers[key.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def send(payload):
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()


while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"capabilities": {"documentSymbolProvider": True}},
        })
    elif method == "textDocument/documentSymbol":
        server_request_id = 700
        send({
            "jsonrpc": "2.0",
            "id": server_request_id,
            "method": "workspace/workspaceFolders",
            "params": {},
        })
        while True:
            response = read_message()
            if response is None:
                sys.exit(0)
            if response.get("id") == server_request_id and isinstance(response.get("result"), list):
                break
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": [
                {
                    "name": "loadUser",
                    "kind": 12,
                    "range": {
                        "start": {"line": 0, "character": 16},
                        "end": {"line": 0, "character": 24},
                    },
                    "selectionRange": {
                        "start": {"line": 0, "character": 16},
                        "end": {"line": 0, "character": 24},
                    },
                }
            ],
        })
    else:
        if request_id is not None:
            send({"jsonrpc": "2.0", "id": request_id, "result": None})
'''.lstrip(),
        encoding="utf-8",
    )


def _write_fake_lsp_server_requiring_configuration(path: Path) -> None:
    path.write_text(
        r'''
from __future__ import annotations

import json
import sys


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return None
        if line in {b"\r\n", b"\n"}:
            break
        decoded = line.decode("ascii").strip()
        if ":" in decoded:
            key, value = decoded.split(":", 1)
            headers[key.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def send(payload):
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()


while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"capabilities": {"documentSymbolProvider": True}},
        })
    elif method == "textDocument/documentSymbol":
        server_request_id = 800
        send({
            "jsonrpc": "2.0",
            "id": server_request_id,
            "method": "workspace/configuration",
            "params": {
                "items": [
                    {"section": "java.import.maven.enabled"},
                    {"section": "java.configuration.updateBuildConfiguration"},
                ]
            },
        })
        while True:
            response = read_message()
            if response is None:
                sys.exit(0)
            if response.get("id") == server_request_id:
                result = response.get("result")
                if result != [True, "automatic"]:
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32000, "message": f"bad configuration response: {result!r}"},
                    })
                    break
                send({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": [
                        {
                            "name": "loadUser",
                            "kind": 12,
                            "range": {
                                "start": {"line": 0, "character": 16},
                                "end": {"line": 0, "character": 24},
                            },
                            "selectionRange": {
                                "start": {"line": 0, "character": 16},
                                "end": {"line": 0, "character": 24},
                            },
                        }
                    ],
                })
                break
    else:
        if request_id is not None:
            send({"jsonrpc": "2.0", "id": request_id, "result": None})
'''.lstrip(),
        encoding="utf-8",
    )


def _write_fake_jdtls_with_project_probe(path: Path) -> None:
    path.write_text(
        r'''
from __future__ import annotations

import json
import sys


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return None
        if line in {b"\r\n", b"\n"}:
            break
        decoded = line.decode("ascii").strip()
        if ":" in decoded:
            key, value = decoded.split(":", 1)
            headers[key.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def send(payload):
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()


while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"capabilities": {"executeCommandProvider": {"commands": ["java.project.getAll", "java.project.listSourcePaths"]}}},
        })
    elif method == "workspace/executeCommand":
        command = message.get("params", {}).get("command")
        if command == "java.project.getAll":
            result = [{"name": "demo"}]
        elif command == "java.project.listSourcePaths":
            result = {"status": True, "data": ["src/main/java"]}
        else:
            result = None
        send({"jsonrpc": "2.0", "id": request_id, "result": result})
    else:
        if request_id is not None:
            send({"jsonrpc": "2.0", "id": request_id, "result": None})
'''.lstrip(),
        encoding="utf-8",
    )


class ToolTests(unittest.TestCase):
    def test_image_read_returns_metadata_then_inspection_keeps_bytes_out_of_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            image = workspace / "example.png"
            raw = b"\x89PNG\r\n\x1a\n" + b"visible-image-bytes"
            image.write_bytes(raw)
            seen: list[tuple[str, str, bytes, str]] = []

            def inspect(path: Path, mime: str, data: bytes, question: str) -> str:
                seen.append((str(path), mime, data, question))
                return json.dumps(
                    {
                        "observations": ["The image contains a settlement example."],
                        "uncertainties": ["small footer text is unclear"],
                        "inferences": ["it may represent a business rule"],
                    }
                )

            context = ToolContext(workspace=workspace, approval_mode="yolo", vision_inspector=inspect)
            metadata = read_file({"path": "example.png"}, context)
            observation = inspect_image({"path": "example.png", "question": "What is visible?"}, context)

        self.assertFalse(metadata.is_error)
        self.assertTrue(metadata.metadata["image_metadata"])
        self.assertIn("inspect_image", metadata.content)
        self.assertFalse(observation.is_error)
        self.assertIn("settlement example", observation.content)
        self.assertNotIn("business rule", observation.content)
        self.assertIn("model-generated visual observation", observation.content)
        self.assertIn("inferences=1", observation.content)
        self.assertNotIn("visible-image-bytes", observation.content)
        self.assertEqual(observation.metadata["observation_origin"], "vision_model")
        self.assertEqual(observation.metadata["observation_reliability"], "model_declared_visible_observations")
        self.assertEqual(observation.metadata["vision_contract"], "structured_direct_observations")
        self.assertEqual(observation.metadata["vision_uncertainty_items"], 1)
        self.assertEqual(observation.metadata["vision_inference_items"], 1)
        self.assertTrue(observation.metadata["vision_inferences_separated"])
        self.assertEqual(seen[0][1], "image/png")
        self.assertEqual(seen[0][2], raw)

    def test_image_inspection_rejects_unstructured_vision_output_as_non_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "example.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            result = inspect_image(
                {"path": "example.png", "question": "Infer the business rule."},
                ToolContext(workspace=workspace, approval_mode="yolo", vision_inspector=lambda *_args: "It is a business rule."),
            )

        self.assertTrue(result.is_error)
        self.assertTrue(result.metadata["image_inspection_unavailable"])
        self.assertEqual(result.metadata["reason"], "invalid_vision_contract")
        self.assertNotIn("business rule", result.content)

    def test_image_inspection_requires_exact_structured_lists(self) -> None:
        invalid_payloads = [
            {"observations": "This means the workflow is approved", "uncertainties": [], "inferences": []},
            {"observations": ["visible"], "inferences": []},
            {"observations": ["visible"], "uncertainties": [], "inferences": [], "extra": []},
            {"observations": ["visible", 3], "uncertainties": [], "inferences": []},
            {"observations": ["visible"], "uncertainties": ["unclear"], "inferences": ["business rule"], "extra": "x"},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as tmp:
                    workspace = Path(tmp).resolve()
                    (workspace / "example.png").write_bytes(b"\x89PNG\r\n\x1a\n")
                    result = inspect_image(
                        {"path": "example.png"},
                        ToolContext(
                            workspace=workspace,
                            approval_mode="yolo",
                            vision_inspector=lambda *_args, payload=payload: json.dumps(payload),
                        ),
                    )
                self.assertTrue(result.is_error)
                self.assertEqual(result.metadata["reason"], "invalid_vision_contract")

    def test_image_inspection_rejects_escape_oversize_and_unavailable_vision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            outside = Path(tmp).parent / "outside.png"
            try:
                outside.write_bytes(b"\x89PNG\r\n\x1a\n")
                unavailable = inspect_image({"path": str(outside)}, ToolContext(workspace=workspace, approval_mode="yolo"))
                self.assertTrue(unavailable.is_error)
                self.assertIn("outside", unavailable.content)
            finally:
                outside.unlink(missing_ok=True)

            image = workspace / "large.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * (8 * 1024 * 1024))
            too_large = inspect_image({"path": "large.png"}, ToolContext(workspace=workspace, approval_mode="yolo"))
            self.assertTrue(too_large.is_error)
            self.assertIn("too large", too_large.content)

            small = workspace / "small.png"
            small.write_bytes(b"\x89PNG\r\n\x1a\n")
            no_vision = inspect_image({"path": "small.png"}, ToolContext(workspace=workspace, approval_mode="yolo"))
            self.assertTrue(no_vision.is_error)
            self.assertTrue(no_vision.metadata["image_inspection_unavailable"])

    def test_registry_applies_read_approval_to_image_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "example.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            invoked: list[str] = []
            registry = ToolRegistry(file_tools())
            result = registry.execute(
                "inspect_image",
                {"path": "example.png"},
                ToolContext(
                    workspace=workspace,
                    approval_mode="yolo",
                    tool_approval={"inspect_image": "deny"},
                    vision_inspector=lambda *_args: invoked.append("vision") or "unexpected",
                ),
            )

        self.assertTrue(result.is_error)
        self.assertEqual(result.metadata["denial_kind"], "approval")
        self.assertEqual(invoked, [])

    def test_large_image_read_still_returns_metadata_before_text_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            image = workspace / "large.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * (300 * 1024))

            result = read_file({"path": "large.png"}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertFalse(result.is_error)
        self.assertTrue(result.metadata["image_metadata"])
        self.assertEqual(result.metadata["size_bytes"], 300 * 1024 + 8)

    def test_oversize_image_metadata_does_not_hash_or_read_the_whole_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            image = workspace / "oversize.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * (8 * 1024 * 1024 + 1))
            with patch("local_agent.tools.files._sha256_file") as digest:
                result = read_file({"path": "oversize.png"}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertFalse(result.is_error)
        self.assertTrue(result.metadata["image_metadata"])
        self.assertFalse(result.metadata["inspect_image_available"])
        self.assertFalse(result.metadata["sha256_computed"])
        self.assertNotIn("sha256", result.metadata)
        self.assertIn("exceeds", result.content)
        digest.assert_not_called()

    def test_vision_capability_failure_is_typed_for_tool_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "example.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            calls: list[str] = []

            def unavailable(*_args: object) -> str:
                calls.append("provider")
                raise VisionInspectionUnavailableError("AI_VISION_MODEL is not configured")

            result = inspect_image(
                {"path": "example.png"},
                ToolContext(workspace=workspace, approval_mode="yolo", vision_inspector=unavailable),
            )

        self.assertTrue(result.is_error)
        self.assertTrue(result.metadata["image_inspection_unavailable"])
        self.assertIn("AI_VISION_MODEL", result.content)
        self.assertEqual(calls, ["provider"])

    def test_registry_preapproval_projection_never_requires_background_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = create_default_registry()
            workspace = Path(tmp).resolve()
            self.assertTrue(registry.is_preapproved("read_file", ToolContext(workspace=workspace, approval_mode="yolo")))
            self.assertTrue(registry.is_preapproved("read_file", ToolContext(workspace=workspace, approval_mode="always-ask")))
            self.assertTrue(registry.is_preapproved("read_file", ToolContext(workspace=workspace, approval_mode="write")))
            self.assertTrue(
                registry.is_preapproved(
                    "read_file", ToolContext(workspace=workspace, approval_mode="always-ask", tool_approval={"read_file": "allow"})
                )
            )
            self.assertTrue(
                registry.is_preapproved(
                    "read_file",
                    ToolContext(workspace=workspace, approval_mode="always-ask", session_tool_approval={"read_file": "allow_always"}),
                )
            )
            for policy, session in (("deny", None), ("prompt", None), (None, "reject_always")):
                self.assertFalse(
                    registry.is_preapproved(
                        "read_file",
                        ToolContext(
                            workspace=workspace,
                            approval_mode="always-ask",
                            tool_approval={"read_file": policy} if policy else None,
                            session_tool_approval={"read_file": session} if session else None,
                        ),
                    )
                )
    def test_registry_rejects_provider_tool_outside_runtime_allowlist(self) -> None:
        calls: list[str] = []

        def handler(_args: dict[str, object], _context: ToolContext) -> ToolResult:
            calls.append("blocked")
            return ToolResult("should not run")

        registry = ToolRegistry(
            [
                Tool(
                    name="blocked",
                    description="test-only tool",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    tier="read",
                    handler=handler,
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = registry.execute(
                "blocked",
                {},
                ToolContext(
                    workspace=Path(tmp).resolve(),
                    approval_mode="yolo",
                    runtime_tool_allowlist=frozenset({"read_file"}),
                ),
            )

        self.assertTrue(result.is_error)
        self.assertIn("Runtime tool choice restriction", result.content)
        self.assertEqual(calls, [])

    def test_registry_limits_candidate_read_to_selected_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            selected = workspace / "selected.py"
            unrelated = workspace / "unrelated.py"
            selected.write_text("selected = True\n", encoding="utf-8")
            unrelated.write_text("unrelated = True\n", encoding="utf-8")
            context = ToolContext(
                workspace=workspace,
                approval_mode="yolo",
                runtime_tool_allowlist=frozenset({"read_file"}),
                runtime_read_file_paths=frozenset({str(selected)}),
            )
            registry = ToolRegistry(file_tools())

            permitted = registry.execute("read_file", {"path": "selected.py"}, context)
            denied = registry.execute("read_file", {"path": "unrelated.py"}, context)

        self.assertFalse(permitted.is_error)
        self.assertTrue(denied.is_error)
        self.assertIn("Runtime candidate read restriction", denied.content)
        self.assertNotIn("apply_patch", denied.content)

    def test_registry_rejects_candidate_read_after_revisit_budget_is_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            selected = workspace / "selected.py"
            selected.write_text("selected = True\n", encoding="utf-8")
            context = ToolContext(
                workspace=workspace,
                approval_mode="yolo",
                runtime_tool_allowlist=frozenset({"read_file"}),
                runtime_read_file_paths=frozenset({str(selected)}),
                runtime_read_file_remaining=0,
            )
            result = ToolRegistry(file_tools()).execute("read_file", {"path": "selected.py"}, context)

        self.assertTrue(result.is_error)
        self.assertIn("Runtime candidate read budget exhausted", result.content)
        self.assertNotIn("apply_patch", result.content)

    def test_list_files_skips_agent_and_cache_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "src").mkdir()
            (workspace / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
            (workspace / ".local-agent" / "sessions").mkdir(parents=True)
            (workspace / ".local-agent" / "sessions" / "s.jsonl").write_text("{}", encoding="utf-8")
            (workspace / "__pycache__").mkdir()
            (workspace / "__pycache__" / "app.pyc").write_text("cache", encoding="utf-8")

            result = list_files({}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertFalse(result.is_error)
        self.assertIn("src/app.py", result.content)
        self.assertNotIn(".local-agent", result.content)
        self.assertNotIn("__pycache__", result.content)
        self.assertEqual(result.metadata["files"], ["src/app.py"])

    def test_list_files_metadata_files_is_bounded_with_display_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            for name in ("a.py", "b.py", "c.py"):
                (workspace / name).write_text(name, encoding="utf-8")

            result = list_files(
                {"max_results": 2},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertFalse(result.is_error)
        self.assertTrue(result.metadata["truncated"])
        self.assertEqual(result.metadata["entry_count"], 2)
        self.assertEqual(len(result.metadata["files"]), 2)
        self.assertEqual(result.metadata["files"], result.content.splitlines()[:2])

    def test_list_files_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            result = list_files({"path": "../outside"}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertTrue(result.is_error)
        self.assertIn("Path escapes workspace", result.content)

    def test_glob_files_is_the_only_filename_discovery_schema(self) -> None:
        registry = ToolRegistry(search_tools())
        schemas = {schema["function"]["name"]: schema["function"] for schema in registry.schemas()}

        self.assertIn("glob_files", schemas)
        self.assertIn("paths", schemas["glob_files"]["parameters"]["properties"])
        self.assertIn("filename", schemas["glob_files"]["description"])
        self.assertIn("does not search filenames", schemas["search_code"]["description"])
        self.assertNotIn("path", schemas["glob_files"]["parameters"]["properties"])

    def test_registry_compatibly_applies_observed_glob_path_scope_without_exposing_a_second_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            source = workspace / "src" / "App.java"
            source.parent.mkdir()
            source.write_text("class App {}\n", encoding="utf-8")
            result = ToolRegistry(search_tools()).execute(
                "glob_files",
                {"path": str(workspace), "paths": ["src/**/*.java"]},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        payload = json.loads(result.content)
        self.assertFalse(result.is_error)
        self.assertEqual(payload["files"], ["src/App.java"])
        self.assertIn("path scope applied to relative paths", payload["compatibility_normalized"])
        self.assertEqual(result.metadata["compatibility_normalized"], ["path scope applied to relative paths"])

    def test_registry_compatibly_normalizes_omp_style_glob_pattern_and_string_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            source = workspace / "src" / "App.java"
            source.parent.mkdir()
            source.write_text("class App {}\n", encoding="utf-8")
            result = ToolRegistry(search_tools()).execute(
                "glob_files",
                {"path": str(workspace), "pattern": "src/**/*.java", "limit": "3"},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        payload = json.loads(result.content)
        self.assertFalse(result.is_error)
        self.assertEqual(payload["files"], ["src/App.java"])
        self.assertEqual(
            result.metadata["compatibility_normalized"],
            ["pattern -> paths[0]", "path scope applied to relative paths", "limit string -> integer"],
        )

    def test_registry_normalizes_json_encoded_glob_paths_array(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            source = workspace / "src" / "App.java"
            source.parent.mkdir()
            source.write_text("class App {}\n", encoding="utf-8")
            result = ToolRegistry(search_tools()).execute(
                "glob_files",
                {
                    "paths": json.dumps(["src/**/*.java"]),
                    "hidden": "False",
                    "gitignore": "True",
                    "limit": "200",
                },
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        payload = json.loads(result.content)
        self.assertFalse(result.is_error)
        self.assertEqual(payload["files"], ["src/App.java"])
        self.assertEqual(
            result.metadata["compatibility_normalized"],
            [
                "paths JSON string -> array",
                "hidden string -> boolean (false)",
                "gitignore string -> boolean (true)",
                "limit string -> integer",
            ],
        )

    def test_registry_drops_blank_glob_siblings_but_rejects_an_empty_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "pom.xml").write_text("<project />\n", encoding="utf-8")
            registry = ToolRegistry(search_tools())
            context = ToolContext(workspace=workspace, approval_mode="yolo")

            usable = registry.execute("glob_files", {"paths": ["", "pom.xml", "  "]}, context)
            empty = registry.execute("glob_files", {"paths": ["", "  "]}, context)

        self.assertFalse(usable.is_error)
        self.assertIn("removed empty paths entries", usable.metadata["compatibility_normalized"])
        self.assertTrue(empty.is_error)
        self.assertIn("non-empty authorized path or pattern", empty.content)

    def test_registry_allows_parallel_root_local_glob_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            primary = root / "primary"
            additional = root / "additional"
            primary.mkdir()
            additional.mkdir()
            (primary / "pom.xml").write_text("<project />\n", encoding="utf-8")
            (additional / "pom.xml").write_text("<project />\n", encoding="utf-8")
            registry = ToolRegistry(search_tools())
            context = ToolContext(
                workspace=primary,
                allowed_dirs=(additional,),
                approval_mode="yolo",
            )

            primary_only = registry.execute("glob_files", {"paths": ["**/pom.xml"]}, context)
            additional_only = registry.execute(
                "glob_files",
                {"paths": [f"{additional}/**/pom.xml"]},
                context,
            )

        self.assertFalse(primary_only.is_error)
        self.assertEqual(primary_only.metadata["searched_roots"], [str(primary)])
        self.assertFalse(additional_only.is_error)
        self.assertEqual(additional_only.metadata["searched_roots"], [str(additional)])

    def test_inventory_glob_hint_stays_within_schema_for_three_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            roots = tuple(root / f"repo-{index}" for index in range(3))
            for repo in roots:
                repo.mkdir()
                (repo / "src").mkdir()
                (repo / "src" / "owner.sql").write_text("select 1;\n", encoding="utf-8")
            hint = inventory_glob_call_hint(str(repo) for repo in roots)
            start = hint.index("glob_files(") + len("glob_files(")
            args = json.loads(hint[start:-1])
            paths = args["paths"]
            registry = ToolRegistry(search_tools())
            context = ToolContext(
                workspace=roots[0],
                allowed_dirs=roots[1:],
                approval_mode="yolo",
            )

            result = registry.execute("glob_files", args, context)

        self.assertLessEqual(len(paths), MAX_INVENTORY_GLOB_PATHS)
        for repo in roots:
            self.assertTrue(
                any(path.startswith(f"{repo}/") for path in paths),
                f"missing bounded selector for {repo}: {paths}",
            )
        self.assertFalse(result.is_error, result.content)

    def test_inventory_glob_hint_reports_single_call_limit_when_roots_exceed_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            roots = tuple(root / f"repo-{index}" for index in range(MAX_INVENTORY_GLOB_PATHS + 1))
            for repo in roots:
                repo.mkdir()
            hint = inventory_glob_call_hint(str(repo) for repo in roots)
            registry = ToolRegistry(search_tools())
            context = ToolContext(
                workspace=roots[0],
                allowed_dirs=roots[1:],
                approval_mode="yolo",
            )

            result = registry.execute("glob_files", {"paths": [f"{roots[0]}/**/pom.xml"]}, context)

        self.assertIn("single-call inventory contract cannot represent", hint)
        self.assertNotIn("glob_files(", hint)
        self.assertNotIn("Split", hint)
        self.assertNotIn("Split", result.content)
        self.assertFalse(result.is_error)

    def test_registry_normalizes_search_code_camel_case_string_result_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "App.java").write_text("needle\nneedle\n", encoding="utf-8")
            result = ToolRegistry(search_tools()).execute(
                "search_code",
                {"pattern": "needle", "maxResults": "1"},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertFalse(result.is_error)
        self.assertIn("compatibility normalized", result.content)
        self.assertEqual(
            result.metadata["compatibility_normalized"],
            ["maxResults -> max_results", "max_results string -> integer"],
        )

    def test_registry_rejects_conflicting_glob_pattern_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = ToolRegistry(search_tools()).execute(
                "glob_files",
                {"paths": ["src/**/*.java"], "pattern": "src/**/*.py"},
                ToolContext(workspace=Path(tmp).resolve(), approval_mode="yolo"),
            )

        self.assertTrue(result.is_error)
        self.assertIn("pattern and paths differ", result.content)

    def test_unknown_tool_suggests_only_currently_exposed_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            registry = ToolRegistry(search_tools())
            result = registry.execute(
                "glob_file",
                {},
                ToolContext(
                    workspace=workspace,
                    approval_mode="yolo",
                    runtime_tool_allowlist=frozenset({"glob_files", "read_file"}),
                ),
            )

        self.assertTrue(result.is_error)
        self.assertIn("Unknown tool: glob_file", result.content)
        self.assertIn("glob_files", result.content)
        self.assertNotIn("search_code", result.content)
        self.assertEqual(result.metadata["suggested_tools"], ["glob_files"])

    def test_unknown_tool_does_not_suggest_denied_or_hidden_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            registry = create_default_registry()
            context = ToolContext(
                workspace=workspace,
                approval_mode="yolo",
                runtime_tool_allowlist=frozenset({"read_file", "shell", "run_tests"}),
                tool_approval={"shell": "deny"},
                session_tool_approval={"run_tests": "reject_always"},
            )
            result = registry.execute("read_fil", {}, context)
            hidden_result = registry.execute(
                "run_shell",
                {},
                ToolContext(
                    workspace=workspace,
                    approval_mode="yolo",
                    runtime_tool_allowlist=frozenset(),
                ),
            )

        self.assertEqual(result.metadata["suggested_tools"], ["read_file"])
        self.assertNotIn("shell", result.content)
        self.assertNotIn("run_tests", result.content)
        self.assertEqual(hidden_result.metadata["suggested_tools"], [])

    def test_glob_files_finds_exact_directory_and_multiple_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            java_file = workspace / "src" / "main" / "java" / "App.java"
            java_file.parent.mkdir(parents=True)
            java_file.write_text("class App {}\n", encoding="utf-8")
            python_file = workspace / "scripts" / "check.py"
            python_file.parent.mkdir()
            python_file.write_text("print('ok')\n", encoding="utf-8")
            context = ToolContext(workspace=workspace, approval_mode="yolo")

            exact = glob_files({"paths": ["src/main/java/App.java"]}, context)
            directory = glob_files({"paths": ["src/main"]}, context)
            patterns = glob_files({"paths": ["src/**/*.java", "scripts/*.py"]}, context)

        self.assertFalse(exact.is_error)
        self.assertEqual(json.loads(exact.content)["files"], ["src/main/java/App.java"])
        self.assertEqual(json.loads(directory.content)["files"], ["src/main/java/App.java"])
        payload = json.loads(patterns.content)
        self.assertEqual(payload["files"], ["scripts/check.py", "src/main/java/App.java"])
        self.assertTrue(payload["complete"])
        self.assertEqual(patterns.metadata["negative_evidence_type"], "path_match")

    def test_glob_files_scans_allowed_root_without_broadening_access(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_tmp, tempfile.TemporaryDirectory() as allowed_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            workspace = Path(workspace_tmp).resolve()
            allowed = Path(allowed_tmp).resolve()
            outside = Path(outside_tmp).resolve()
            (workspace / "src").mkdir()
            (workspace / "src" / "App.java").write_text("class App {}\n", encoding="utf-8")
            external = allowed / "External.java"
            external.write_text("class External {}\n", encoding="utf-8")
            (outside / "Secret.java").write_text("class Secret {}\n", encoding="utf-8")
            context = ToolContext(workspace=workspace, approval_mode="yolo", allowed_dirs=(allowed,))

            result = glob_files({"paths": ["src/**/*.java", str(allowed / "*.java")]}, context)
            escaped = glob_files({"paths": [str(outside / "*.java")]}, context)

        payload = json.loads(result.content)
        self.assertFalse(result.is_error)
        self.assertEqual(payload["files"], [str(external), "src/App.java"])
        self.assertEqual(set(payload["searched_roots"]), {str(workspace), str(allowed)})
        self.assertTrue(escaped.is_error)
        self.assertIn("Path escapes workspace", escaped.content)

    def test_glob_files_respects_hidden_gitignore_limit_and_missing_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / ".gitignore").write_text("ignored.py\nbuild/\n", encoding="utf-8")
            for name in ("visible.py", ".hidden.py", "ignored.py", "a.py", "b.py", "c.py"):
                (workspace / name).write_text("pass\n", encoding="utf-8")
            (workspace / "build").mkdir()
            (workspace / "build" / "generated.py").write_text("pass\n", encoding="utf-8")
            context = ToolContext(workspace=workspace, approval_mode="yolo")

            default_result = glob_files({"paths": ["**/*.py"]}, context)
            include_all = glob_files({"paths": ["**/*.py"], "hidden": True, "gitignore": False}, context)
            limited = glob_files({"paths": ["*.py"], "limit": 2}, context)
            missing = glob_files({"paths": ["missing.py"]}, context)

        self.assertNotIn("ignored.py", json.loads(default_result.content)["files"])
        self.assertNotIn("build/generated.py", json.loads(default_result.content)["files"])
        self.assertIn(".hidden.py", json.loads(include_all.content)["files"])
        limited_payload = json.loads(limited.content)
        self.assertTrue(limited_payload["truncated"])
        self.assertFalse(limited_payload["complete"])
        self.assertEqual(limited.metadata["negative_evidence_type"], "incomplete")
        self.assertTrue(missing.is_error)
        self.assertEqual(missing.metadata["negative_evidence_type"], "exact_path_missing")

    def test_glob_files_filters_symlink_targets_outside_authorized_roots(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            workspace = Path(workspace_tmp).resolve()
            outside = Path(outside_tmp).resolve()
            (workspace / "inside.py").write_text("inside\n", encoding="utf-8")
            (outside / "secret.py").write_text("secret\n", encoding="utf-8")
            try:
                (workspace / "linked").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks are unavailable: {exc}")

            result = glob_files({"paths": ["**/*.py"]}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertFalse(result.is_error)
        self.assertEqual(json.loads(result.content)["files"], ["inside.py"])

    def test_glob_files_bounds_large_structured_output_without_claiming_complete_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            for index in range(400):
                directory = workspace / f"module-{index:03d}-with-a-deliberately-long-name-for-output-sizing"
                directory.mkdir()
                (directory / "VeryLongSourceFileNameForGlobOutputSizing.java").write_text("class App {}\n", encoding="utf-8")

            result = glob_files({"paths": ["**/*.java"], "limit": 1000}, ToolContext(workspace=workspace, approval_mode="yolo"))

        payload = json.loads(result.content)
        self.assertLessEqual(len(result.content), 30000)
        self.assertTrue(payload["output_char_limit_reached"])
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["negative_evidence_type"], "incomplete")
        self.assertEqual(payload["observed_match_count"], 400)
        self.assertLess(payload["file_count"], 400)

    def test_search_code_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            result = search_code(
                {"pattern": "secret", "path": "../outside"},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertTrue(result.is_error)
        self.assertIn("Path escapes workspace", result.content)

    @unittest.skipIf(shutil.which("rg") is None, "ripgrep is not installed")
    def test_search_code_returns_relative_paths_and_truncates_total_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "a.txt").write_text("needle one\n", encoding="utf-8")
            (workspace / "b.txt").write_text("needle two\n", encoding="utf-8")

            result = search_code(
                {"pattern": "needle", "max_results": 1},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertFalse(result.is_error)
        self.assertNotIn(str(workspace), result.content)
        self.assertNotIn("./", result.content)
        self.assertIn("needle", result.content)
        self.assertIn("... truncated after 1 matches", result.content)

    @unittest.skipIf(shutil.which("rg") is None, "ripgrep is not installed")
    def test_search_code_bounds_minified_lines_and_per_file_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "bundle.css").write_text(
                "".join(f".selector-{index}{{content:'needle {'x' * 900}'}}\n" for index in range(25)),
                encoding="utf-8",
            )

            result = search_code(
                {"pattern": "needle", "max_results": 50},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertFalse(result.is_error)
        self.assertLess(len(result.content), 13000)
        self.assertTrue(result.metadata["truncated"])
        self.assertTrue(result.metadata["per_file_limit_reached"])
        self.assertTrue(result.metadata["line_truncated"])
        self.assertGreater(result.metadata["line_truncated_count"], 0)
        self.assertEqual(result.metadata["column_limit"], 512)
        self.assertIn("truncated after 20 matches per file", result.content)

    def test_lsp_symbols_and_definition_use_python_ast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            source = workspace / "pkg" / "module.py"
            source.parent.mkdir()
            source.write_text(
                "class Service:\n"
                "    def run(self):\n"
                "        return helper()\n"
                "\n"
                "def helper():\n"
                "    return 1\n",
                encoding="utf-8",
            )
            context = ToolContext(workspace=workspace, approval_mode="yolo")

            symbols = lsp_symbols({"path": "pkg", "query": "Service"}, context)
            definition = lsp_definition({"symbol": "helper", "path": "pkg"}, context)

        self.assertFalse(symbols.is_error)
        self.assertNotIn("[lsp confidence]", symbols.content)
        self.assertIn("pkg/module.py:1:1: class Service", symbols.content)
        self.assertFalse(definition.is_error)
        self.assertNotIn("[lsp confidence]", definition.content)
        self.assertIn("pkg/module.py:5:1: function helper", definition.content)

    def test_lsp_symbol_aliases_are_registered_and_match_lsp_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            source = workspace / "pkg" / "module.py"
            source.parent.mkdir()
            source.write_text(
                "class Service:\n"
                "    pass\n",
                encoding="utf-8",
            )
            context = ToolContext(workspace=workspace, approval_mode="yolo")
            registry = ToolRegistry(lsp_tools())

            workspace_symbols = registry.execute(
                "lsp_workspace_symbols",
                {"path": "pkg", "query": "Service"},
                context,
            )
            document_symbols = registry.execute(
                "lsp_document_symbols",
                {"path": "pkg/module.py", "query": "Service"},
                context,
            )

        self.assertIn("lsp_workspace_symbols", registry.tool_names())
        self.assertIn("lsp_document_symbols", registry.tool_names())
        self.assertFalse(workspace_symbols.is_error)
        self.assertFalse(document_symbols.is_error)
        self.assertIn("pkg/module.py:1:1: class Service", workspace_symbols.content)
        self.assertIn("pkg/module.py:1:1: class Service", document_symbols.content)

    def test_lsp_references_finds_identifier_occurrences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            source = workspace / "app.py"
            source.write_text(
                "def helper():\n"
                "    return 1\n"
                "\n"
                "value = helper()\n",
                encoding="utf-8",
            )

            result = lsp_references(
                {"symbol": "helper", "path": "."},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertFalse(result.is_error)
        self.assertIn("app.py:1:5: def helper():", result.content)
        self.assertIn("app.py:4:9: value = helper()", result.content)

    def test_lsp_diagnostics_reports_python_syntax_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "bad.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

            result = lsp_diagnostics({}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertFalse(result.is_error)
        self.assertIn("bad.py:1", result.content)
        self.assertIn("SyntaxError", result.content)

    def test_lsp_supports_java_symbols_definitions_and_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            source = workspace / "src" / "main" / "java" / "UserService.java"
            source.parent.mkdir(parents=True)
            source.write_text(
                "package demo;\n"
                "\n"
                "public class UserService {\n"
                "    public User findUser(String id) {\n"
                "        return new User(id);\n"
                "    }\n",
                encoding="utf-8",
            )
            context = ToolContext(workspace=workspace, approval_mode="yolo")

            symbols = lsp_symbols({"path": "src", "query": "UserService"}, context)
            definition = lsp_definition({"symbol": "findUser", "path": "src"}, context)
            diagnostics = lsp_diagnostics({"path": "src"}, context)

        self.assertFalse(symbols.is_error)
        self.assertIn("[lsp confidence]", symbols.content)
        self.assertIn("UserService.java:3:14: class UserService", symbols.content)
        self.assertFalse(definition.is_error)
        self.assertIn("[lsp confidence]", definition.content)
        self.assertIn("UserService.java:4:17: method UserService.findUser", definition.content)
        self.assertFalse(diagnostics.is_error)
        self.assertIn("[lsp confidence]", diagnostics.content)
        self.assertIn("DelimiterError", diagnostics.content)

    def test_lsp_query_prioritizes_matching_paths_beyond_file_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            src = workspace / "src" / "main" / "java"
            for index in range(320):
                dummy = src / "aaa" / f"Dummy{index:03d}.java"
                dummy.parent.mkdir(parents=True, exist_ok=True)
                dummy.write_text(f"package demo;\npublic class Dummy{index:03d} {{}}\n", encoding="utf-8")
            target = src / "zzz" / "IntentionConfigManagerController.java"
            target.parent.mkdir(parents=True)
            target.write_text(
                "package demo;\n"
                "public class IntentionConfigManagerController {\n"
                "    public void addIntentionConfig() {}\n"
                "}\n",
                encoding="utf-8",
            )
            context = ToolContext(workspace=workspace, approval_mode="yolo")

            symbols = lsp_symbols({"path": "src", "query": "IntentionConfigManagerController"}, context)
            definition = lsp_definition({"path": "src", "symbol": "IntentionConfigManagerController"}, context)

        self.assertFalse(symbols.is_error)
        self.assertIn("IntentionConfigManagerController.java:2:14: class IntentionConfigManagerController", symbols.content)
        self.assertFalse(definition.is_error)
        self.assertIn("IntentionConfigManagerController.java:2:14: class IntentionConfigManagerController", definition.content)

    def test_lsp_supports_vue_symbols_and_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            source = workspace / "src" / "UserCard.vue"
            source.parent.mkdir()
            source.write_text(
                "<template>\n"
                "  <button @click=\"saveUser\">Save</button>\n"
                "</template>\n"
                "<script setup lang=\"ts\">\n"
                "interface UserCardProps { id: string }\n"
                "const emitSave = () => saveUser()\n"
                "function saveUser() {\n"
                "  console.log('save')\n"
                "}\n"
                "</script>\n",
                encoding="utf-8",
            )
            context = ToolContext(workspace=workspace, approval_mode="yolo")

            symbols = lsp_symbols({"path": "src", "query": "saveUser"}, context)
            references = lsp_references({"symbol": "saveUser", "path": "src"}, context)

        self.assertFalse(symbols.is_error)
        self.assertIn("[lsp confidence]", symbols.content)
        self.assertIn("UserCard.vue:7:10: function saveUser", symbols.content)
        self.assertFalse(references.is_error)
        self.assertIn("[lsp confidence]", references.content)
        self.assertIn("UserCard.vue:2:19:", references.content)
        self.assertIn("UserCard.vue:6:24:", references.content)

    def test_lsp_supports_typescript_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            source = workspace / "src" / "service.ts"
            source.parent.mkdir()
            source.write_text(
                "export interface User { id: string }\n"
                "export const loadUser = async (id: string) => ({ id })\n",
                encoding="utf-8",
            )
            context = ToolContext(workspace=workspace, approval_mode="yolo")

            symbols = lsp_symbols({"path": "src"}, context)
            definition = lsp_definition({"symbol": "loadUser", "path": "src"}, context)

        self.assertFalse(symbols.is_error)
        self.assertIn("service.ts:1:18: interface User", symbols.content)
        self.assertIn("service.ts:2:14: function loadUser", symbols.content)
        self.assertFalse(definition.is_error)
        self.assertIn("service.ts:2:14: function loadUser", definition.content)

    def test_lsp_uses_external_language_server_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "package.json").write_text("{}\n", encoding="utf-8")
            source = workspace / "src" / "service.ts"
            source.parent.mkdir()
            source.write_text(
                "export function loadUser() {\n"
                "  return loadUser()\n"
                "}\n",
                encoding="utf-8",
            )
            fake_server = workspace / "fake_lsp.py"
            _write_fake_lsp_server(fake_server)
            command = f"{sys.executable} {fake_server}"
            context = ToolContext(workspace=workspace, approval_mode="yolo")
            with patch.dict("os.environ", {"AGENT_LSP_TYPESCRIPT_COMMAND": command}):
                try:
                    symbols = lsp_symbols({"path": "src/service.ts", "query": "loadUser"}, context)
                    definition = lsp_definition({"symbol": "loadUser", "path": "src"}, context)
                    references = lsp_references({"symbol": "loadUser", "path": "src"}, context)
                    diagnostics = lsp_diagnostics({"path": "src/service.ts"}, context)
                finally:
                    close_all_clients()

        self.assertFalse(symbols.is_error)
        self.assertIn("[lsp provider] external typescript-language-server", symbols.content)
        self.assertIn("service.ts:1:17: function loadUser", symbols.content)
        self.assertFalse(definition.is_error)
        self.assertIn("service.ts:1:17: export function loadUser() {", definition.content)
        self.assertFalse(references.is_error)
        self.assertIn("service.ts:2:10: return loadUser()", references.content)
        self.assertFalse(diagnostics.is_error)
        self.assertIn("Warning: fake diagnostic", diagnostics.content)

    def test_lsp_client_responds_to_server_workspace_folder_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            source = workspace / "src" / "service.ts"
            source.parent.mkdir()
            source.write_text("export function loadUser() {}\n", encoding="utf-8")
            fake_server = workspace / "fake_lsp.py"
            _write_fake_lsp_server_requiring_workspace_folders(fake_server)
            server = LspServerConfig(
                name="fake-lsp",
                command=(sys.executable, str(fake_server)),
                file_types=(".ts",),
                root_markers=("package.json",),
                language_id="typescript",
            )
            (workspace / "package.json").write_text("{}\n", encoding="utf-8")
            client = StdioLspClient(server, workspace)
            try:
                symbols = client.document_symbols(source)
            finally:
                client.close()

        self.assertEqual([symbol.name for symbol in symbols], ["loadUser"])

    def test_lsp_client_responds_to_server_configuration_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            source = workspace / "src" / "service.ts"
            source.parent.mkdir()
            source.write_text("export function loadUser() {}\n", encoding="utf-8")
            fake_server = workspace / "fake_lsp.py"
            _write_fake_lsp_server_requiring_configuration(fake_server)
            server = LspServerConfig(
                name="fake-lsp",
                command=(sys.executable, str(fake_server)),
                file_types=(".ts",),
                root_markers=("package.json",),
                language_id="typescript",
            )
            (workspace / "package.json").write_text("{}\n", encoding="utf-8")
            client = StdioLspClient(server, workspace)
            try:
                symbols = client.document_symbols(source)
            finally:
                client.close()

        self.assertEqual([symbol.name for symbol in symbols], ["loadUser"])

    def test_lsp_strict_external_empty_result_falls_back_to_lightweight_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "pom.xml").write_text("<project />\n", encoding="utf-8")
            source = workspace / "src" / "UserService.java"
            source.parent.mkdir()
            source.write_text(
                "package demo;\n\n"
                "public class UserService {\n"
                "  public String findUser() { return \"ok\"; }\n"
                "}\n",
                encoding="utf-8",
            )
            fake_server = workspace / "fake_lsp.py"
            _write_fake_lsp_server(fake_server)
            command = f"{sys.executable} {fake_server}"
            context = ToolContext(workspace=workspace, approval_mode="yolo")
            with patch.dict("os.environ", {"AGENT_LSP_MODE": "external", "AGENT_LSP_JDTLS_COMMAND": command}):
                try:
                    result = lsp_symbols({"path": "src/UserService.java", "query": "UserService"}, context)
                finally:
                    close_all_clients()

        self.assertFalse(result.is_error)
        self.assertIn("[lsp provider] external unavailable", result.content)
        self.assertIn("[lsp fallback] using lightweight static navigation", result.content)
        self.assertIn("UserService.java:3:14: class UserService", result.content)

    def test_lsp_uses_nested_project_root_for_external_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            service = workspace / "service-a"
            (service / "src").mkdir(parents=True)
            (service / "package.json").write_text("{}\n", encoding="utf-8")
            (service / "src" / "service.ts").write_text("export function loadUser() {}\n", encoding="utf-8")
            fake_server = workspace / "fake_lsp.py"
            _write_fake_lsp_server(fake_server)
            command = f"{sys.executable} {fake_server}"
            context = ToolContext(workspace=workspace, approval_mode="yolo")
            with patch.dict("os.environ", {"AGENT_LSP_TYPESCRIPT_COMMAND": command}):
                try:
                    file_result = lsp_symbols({"path": "service-a/src/service.ts", "query": "loadUser"}, context)
                    dir_result = lsp_symbols({"path": "service-a/src", "query": "loadUser"}, context)
                finally:
                    close_all_clients()

        self.assertFalse(file_result.is_error)
        self.assertIn("[lsp provider] external typescript-language-server", file_result.content)
        self.assertIn("service-a/src/service.ts:1:17: function loadUser", file_result.content)
        self.assertFalse(dir_result.is_error)
        self.assertIn("[lsp provider] external typescript-language-server", dir_result.content)
        self.assertIn("service-a/src/service.ts:1:17: function loadUser", dir_result.content)

    def test_lsp_status_reports_external_server_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            fake_server = workspace / "fake_lsp.py"
            _write_fake_lsp_server(fake_server)
            command = f"{sys.executable} {fake_server}"
            with patch.dict("os.environ", {"AGENT_LSP_TYPESCRIPT_COMMAND": command}):
                result = lsp_status({}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertFalse(result.is_error)
        self.assertIn("External LSP mode", result.content)
        self.assertIn("typescript-language-server", result.content)

    def test_lsp_status_probe_reports_java_project_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "pom.xml").write_text("<project />\n", encoding="utf-8")
            source = workspace / "src" / "main" / "java" / "demo" / "Hello.java"
            source.parent.mkdir(parents=True)
            source.write_text("package demo;\npublic class Hello {}\n", encoding="utf-8")
            fake_server = workspace / "fake_jdtls.py"
            _write_fake_jdtls_with_project_probe(fake_server)
            command = f"{sys.executable} {fake_server}"
            with patch.dict("os.environ", {"AGENT_LSP_JDTLS_COMMAND": command}):
                try:
                    result = lsp_status({"probe": True}, ToolContext(workspace=workspace, approval_mode="yolo"))
                finally:
                    close_all_clients()

        self.assertFalse(result.is_error)
        self.assertIn("[lsp probe]", result.content)
        self.assertIn("java.project.getAll: 1 project(s)", result.content)
        self.assertIn("java.project.listSourcePaths: 1 source path(s)", result.content)
        self.assertIn("project health: jdtls has imported Java project metadata", result.content)

    def test_lsp_status_probe_reports_missing_maven_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "pom.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>local.agent.test</groupId>
    <artifactId>missing-parent-for-lca-test</artifactId>
    <version>999.0.0</version>
  </parent>
  <artifactId>demo</artifactId>
</project>
""",
                encoding="utf-8",
            )
            source = workspace / "src" / "main" / "java" / "demo" / "Hello.java"
            source.parent.mkdir(parents=True)
            source.write_text("package demo;\npublic class Hello {}\n", encoding="utf-8")
            fake_server = workspace / "fake_jdtls.py"
            _write_fake_jdtls_with_project_probe(fake_server)
            command = f"{sys.executable} {fake_server}"
            with patch.dict("os.environ", {"AGENT_LSP_JDTLS_COMMAND": command}):
                try:
                    result = lsp_status({"probe": True}, ToolContext(workspace=workspace, approval_mode="yolo"))
                finally:
                    close_all_clients()

        self.assertFalse(result.is_error)
        self.assertIn("Maven parent probe", result.content)
        self.assertIn("local.agent.test:missing-parent-for-lca-test:999.0.0: missing", result.content)
        self.assertIn("action: add the parent POM", result.content)

    def test_lsp_status_probe_reports_maven_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            fake_home = workspace / "home"
            settings_dir = fake_home / ".m2"
            settings_dir.mkdir(parents=True)
            (settings_dir / "settings.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <localRepository>~/company-m2</localRepository>
  <mirrors>
    <mirror>
      <id>private-mirror</id>
      <url>https://example.invalid/repository/maven-public/</url>
      <mirrorOf>*</mirrorOf>
    </mirror>
  </mirrors>
  <servers>
    <server>
      <id>private-mirror</id>
      <username>secret-user</username>
      <password>secret-password</password>
    </server>
  </servers>
  <profiles>
    <profile>
      <id>private-profile</id>
      <repositories>
        <repository>
          <id>private-repo</id>
          <url>https://example.invalid/repository/releases/</url>
        </repository>
      </repositories>
    </profile>
  </profiles>
  <activeProfiles>
    <activeProfile>private-profile</activeProfile>
  </activeProfiles>
</settings>
""",
                encoding="utf-8",
            )
            (workspace / "pom.xml").write_text("<project />\n", encoding="utf-8")
            source = workspace / "src" / "main" / "java" / "demo" / "Hello.java"
            source.parent.mkdir(parents=True)
            source.write_text("package demo;\npublic class Hello {}\n", encoding="utf-8")
            fake_server = workspace / "fake_jdtls.py"
            _write_fake_jdtls_with_project_probe(fake_server)
            command = f"{sys.executable} {fake_server}"
            with (
                patch.dict("os.environ", {"AGENT_LSP_JDTLS_COMMAND": command}),
                patch.object(Path, "home", return_value=fake_home),
            ):
                try:
                    result = lsp_status({"probe": True}, ToolContext(workspace=workspace, approval_mode="yolo"))
                finally:
                    close_all_clients()

        self.assertFalse(result.is_error)
        self.assertIn("Maven environment probe", result.content)
        self.assertIn("settings: ~/.m2/settings.xml (exists)", result.content)
        self.assertIn("localRepository: ~/company-m2 (settings.xml)", result.content)
        self.assertIn("mirrors=1, servers=1, profiles=1, repositories=1, activeProfiles=1", result.content)
        self.assertNotIn("example.invalid", result.content)
        self.assertNotIn("private-mirror", result.content)
        self.assertNotIn("secret-password", result.content)

    def test_lsp_tools_reject_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            result = lsp_definition(
                {"symbol": "anything", "path": "../outside"},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertTrue(result.is_error)
        self.assertIn("Path escapes workspace and allowed directories", result.content)

    def test_lsp_symbols_can_scan_explicitly_allowed_directory(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_tmp, tempfile.TemporaryDirectory() as allowed_tmp:
            workspace = Path(workspace_tmp).resolve()
            allowed = Path(allowed_tmp).resolve()
            source = allowed / "ExternalService.java"
            source.write_text("public class ExternalService {}\n", encoding="utf-8")

            result = lsp_symbols(
                {"path": str(allowed), "query": "ExternalService"},
                ToolContext(workspace=workspace, approval_mode="yolo", allowed_dirs=(allowed,)),
            )

        self.assertFalse(result.is_error)
        self.assertIn(f"{source}:1:14: class ExternalService", result.content)

    def test_read_file_rejects_large_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "large.txt"
            target.write_text("x" * (256 * 1024 + 1), encoding="utf-8")

            result = read_file({"path": "large.txt"}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertTrue(result.is_error)
        self.assertIn("File too large", result.content)

    def test_read_file_can_access_explicitly_allowed_directory(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_tmp, tempfile.TemporaryDirectory() as allowed_tmp:
            workspace = Path(workspace_tmp).resolve()
            allowed = Path(allowed_tmp).resolve()
            target = allowed / "requirements.md"
            target.write_text("outside requirement\n", encoding="utf-8")

            result = read_file(
                {"path": str(target)},
                ToolContext(workspace=workspace, approval_mode="yolo", allowed_dirs=(allowed,)),
            )

        self.assertFalse(result.is_error)
        self.assertIn(str(target), result.content)
        self.assertIn("outside requirement", result.content)

    def test_read_file_outputs_pure_patch_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "README.md"
            original = "hello\n"
            target.write_text(original, encoding="utf-8")

            result = read_file({"path": "README.md"}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertFalse(result.is_error)
        self.assertIn(f"[README.md#{hash_text(original)}]", result.content)
        self.assertIn(f"tag: {hash_text(original)}", result.content)

    def test_read_file_rejects_unallowed_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            workspace = Path(workspace_tmp).resolve()
            target = Path(outside_tmp).resolve() / "secret.md"
            target.write_text("secret\n", encoding="utf-8")

            result = read_file({"path": str(target)}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertTrue(result.is_error)
        self.assertIn("Path escapes workspace and allowed directories", result.content)

    def test_list_files_returns_absolute_paths_for_allowed_directory(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_tmp, tempfile.TemporaryDirectory() as allowed_tmp:
            workspace = Path(workspace_tmp).resolve()
            allowed = Path(allowed_tmp).resolve()
            target = allowed / "requirements.md"
            target.write_text("outside requirement\n", encoding="utf-8")

            result = list_files(
                {"path": str(allowed)},
                ToolContext(workspace=workspace, approval_mode="yolo", allowed_dirs=(allowed,)),
            )

        self.assertFalse(result.is_error)
        self.assertIn(str(target), result.content)
        self.assertEqual(result.metadata["files"], [str(target)])

    def test_list_files_root_includes_allowed_directory_hint(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_tmp, tempfile.TemporaryDirectory() as allowed_tmp:
            workspace = Path(workspace_tmp).resolve()
            allowed = Path(allowed_tmp).resolve()
            (workspace / "src").mkdir()
            (workspace / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
            (allowed / "requirements.md").write_text("outside requirement\n", encoding="utf-8")

            result = list_files(
                {},
                ToolContext(workspace=workspace, approval_mode="yolo", allowed_dirs=(allowed,)),
            )

        self.assertFalse(result.is_error)
        self.assertIn("Workspace roots:", result.content)
        self.assertIn(f"Primary workspace (--cwd): {workspace}", result.content)
        self.assertIn(str(allowed), result.content)
        self.assertIn("Files under primary workspace:", result.content)
        self.assertIn("src/app.py", result.content)

    def test_list_files_missing_path_suggests_allowed_directories(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_tmp, tempfile.TemporaryDirectory() as allowed_tmp:
            workspace = Path(workspace_tmp).resolve()
            allowed = Path(allowed_tmp).resolve()

            result = list_files(
                {"path": "requirements"},
                ToolContext(workspace=workspace, approval_mode="yolo", allowed_dirs=(allowed,)),
            )

        self.assertTrue(result.is_error)
        self.assertIn("Path not found: requirements", result.content)
        self.assertIn("Workspace roots:", result.content)
        self.assertIn(str(allowed), result.content)

    def test_patch_file_can_edit_explicitly_allowed_directory(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_tmp, tempfile.TemporaryDirectory() as allowed_tmp:
            workspace = Path(workspace_tmp).resolve()
            allowed = Path(allowed_tmp).resolve()
            target = allowed / "requirements.md"
            original = "old requirement\n"
            target.write_text(original, encoding="utf-8")
            context = ToolContext(workspace=workspace, approval_mode="yolo", allowed_dirs=(allowed,))

            result = patch_file(
                {
                    "path": str(target),
                    "tag": hash_text(original),
                    "start_line": 1,
                    "end_line": 1,
                    "old_text": "old requirement",
                    "new_text": "new requirement",
                },
                context,
            )
            persisted = target.read_text(encoding="utf-8")

        self.assertFalse(result.is_error)
        self.assertIn("+new requirement", result.content)
        self.assertEqual(persisted, "new requirement\n")

    def test_read_file_truncates_long_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "many.txt"
            target.write_text("".join(f"{i}\n" for i in range(500)), encoding="utf-8")

            result = read_file({"path": "many.txt"}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertFalse(result.is_error)
        self.assertIn("... more lines exist after line 400", result.content)
        self.assertIn("only if needed for the task", result.content)

    def test_read_file_bounds_single_minified_line_without_changing_full_file_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "bundle.css"
            original = ".selector{" + ("x" * 5000) + "}\n"
            target.write_text(original, encoding="utf-8")

            result = read_file(
                {"path": "bundle.css", "start_line": 1, "end_line": 1},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertFalse(result.is_error)
        self.assertLess(len(result.content), 1100)
        self.assertIn(f"[bundle.css#{hash_text(original)}]", result.content)
        self.assertIn("line(s) truncated to 768 characters", result.content)
        self.assertTrue(result.metadata["line_truncated"])
        self.assertEqual(result.metadata["truncated_line_count"], 1)

    def test_write_file_refuses_to_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "README.md"
            target.write_text("existing\n", encoding="utf-8")

            result = write_file(
                {"path": "README.md", "content": "replacement\n"},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertTrue(result.is_error)
        self.assertIn("Refusing to overwrite", result.content)

    def test_write_file_dry_run_previews_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "src" / "NewValidator.java"

            result = write_file(
                {
                    "path": "src/NewValidator.java",
                    "content": "public class NewValidator {}\n",
                    "dry_run": True,
                },
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )
            target_exists = target.exists()

        self.assertFalse(result.is_error)
        self.assertIn("New file preview only", result.content)
        self.assertIn("+++ b/src/NewValidator.java", result.content)
        self.assertIn("+public class NewValidator {}", result.content)
        self.assertFalse(target_exists)

    def test_write_file_records_patch_and_rollback_deletes_created_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            state_dir = workspace / "state"
            target = workspace / "src" / "NewValidator.java"
            context = ToolContext(workspace=workspace, approval_mode="yolo", state_dir=state_dir, session_id="s1")

            write_result = write_file(
                {"path": "src/NewValidator.java", "content": "public class NewValidator {}\n"},
                context,
            )
            rollback_result = rollback_patch({}, context)
            second_rollback = rollback_patch({}, context)
            patch_log = state_dir / "patches" / "s1.jsonl"
            target_exists = target.exists()
            patch_log_text = patch_log.read_text(encoding="utf-8")

        self.assertFalse(write_result.is_error)
        self.assertIn("Patch id:", write_result.content)
        self.assertIn("+++ b/src/NewValidator.java", write_result.content)
        self.assertFalse(rollback_result.is_error)
        self.assertIn("Deleted created file", rollback_result.content)
        self.assertFalse(target_exists)
        self.assertTrue(second_rollback.is_error)
        self.assertIn("No unapplied patch record", second_rollback.content)
        self.assertIn('"before_exists": false', patch_log_text)

    def test_write_file_schema_description_matches_create_only_behavior(self) -> None:
        write_tool = next(tool for tool in file_tools() if tool.name == "write_file")

        self.assertIn("Create a new text file", write_tool.description)
        self.assertIn("Refuses to overwrite existing files", write_tool.description)
        self.assertIn("dry_run=true", write_tool.description)
        self.assertNotIn("fully overwrite", write_tool.description)

    def test_patch_file_dry_run_previews_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "README.md"
            original = "old\n"
            target.write_text(original, encoding="utf-8")

            result = patch_file(
                {
                    "path": "README.md",
                    "tag": hash_text(original),
                    "start_line": 1,
                    "end_line": 1,
                    "old_text": "old",
                    "new_text": "new",
                    "dry_run": True,
                },
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )
            persisted = target.read_text(encoding="utf-8")

        self.assertFalse(result.is_error)
        self.assertIn("Patch preview only", result.content)
        self.assertIn("+new", result.content)
        self.assertEqual(persisted, original)

    def test_patch_file_relevance_checker_blocks_real_write_but_not_dry_run(self) -> None:
        calls: list[tuple[str, str]] = []

        def checker(raw_path: str, resolved_path: Path) -> str:
            calls.append((raw_path, resolved_path.name))
            return "blocked by relevance gate"

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "README.md"
            original = "old\n"
            target.write_text(original, encoding="utf-8")
            context = ToolContext(
                workspace=workspace,
                approval_mode="yolo",
                patch_relevance_checker=checker,
            )

            preview = patch_file(
                {
                    "path": "README.md",
                    "tag": hash_text(original),
                    "start_line": 1,
                    "end_line": 1,
                    "old_text": "old",
                    "new_text": "new",
                    "dry_run": True,
                },
                context,
            )
            write = patch_file(
                {
                    "path": "README.md",
                    "tag": hash_text(original),
                    "start_line": 1,
                    "end_line": 1,
                    "old_text": "old",
                    "new_text": "new",
                },
                context,
            )
            persisted = target.read_text(encoding="utf-8")

        self.assertFalse(preview.is_error)
        self.assertIn("Patch preview only", preview.content)
        self.assertTrue(write.is_error)
        self.assertIn("blocked by relevance gate", write.content)
        self.assertEqual(calls, [("README.md", "README.md")])
        self.assertEqual(persisted, original)

    def test_patch_file_accepts_path_hash_tag_from_read_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "README.md"
            original = "old\n"
            target.write_text(original, encoding="utf-8")

            result = patch_file(
                {
                    "path": "README.md",
                    "tag": f"[README.md#{hash_text(original)}]",
                    "start_line": 1,
                    "end_line": 1,
                    "old_text": "old",
                    "new_text": "new",
                    "dry_run": True,
                },
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )
            persisted = target.read_text(encoding="utf-8")

        self.assertFalse(result.is_error)
        self.assertIn("Interpreted tag", result.content)
        self.assertIn("Pass only the pure hash tag next time", result.content)
        self.assertIn("+new", result.content)
        self.assertEqual(persisted, original)

    def test_rollback_patch_restores_latest_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            state_dir = workspace / "state"
            target = workspace / "README.md"
            original = "old\n"
            context = ToolContext(workspace=workspace, approval_mode="yolo", state_dir=state_dir, session_id="s1")
            target.write_text(original, encoding="utf-8")

            patch_result = patch_file(
                {
                    "path": "README.md",
                    "tag": hash_text(original),
                    "start_line": 1,
                    "end_line": 1,
                    "old_text": "old",
                    "new_text": "new",
                },
                context,
            )
            rollback_result = rollback_patch({}, context)
            second_rollback = rollback_patch({}, context)
            persisted = target.read_text(encoding="utf-8")
            patch_log = state_dir / "patches" / "s1.jsonl"
            patch_log_exists = patch_log.exists()
            workspace_patch_dir_exists = (workspace / ".local-agent" / "patches").exists()

        self.assertFalse(patch_result.is_error)
        self.assertTrue(patch_log_exists)
        self.assertFalse(workspace_patch_dir_exists)
        self.assertIn("Patch id:", patch_result.content)
        self.assertFalse(rollback_result.is_error)
        self.assertIn("Rolled back patch", rollback_result.content)
        self.assertIn("+old", rollback_result.content)
        self.assertEqual(persisted, original)
        self.assertTrue(second_rollback.is_error)
        self.assertIn("No unapplied patch record", second_rollback.content)

    def test_rollback_patch_refuses_when_file_changed_after_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "README.md"
            original = "old\n"
            context = ToolContext(workspace=workspace, approval_mode="yolo", session_id="s1")
            target.write_text(original, encoding="utf-8")

            patch_result = patch_file(
                {
                    "path": "README.md",
                    "tag": hash_text(original),
                    "start_line": 1,
                    "end_line": 1,
                    "old_text": "old",
                    "new_text": "new",
                },
                context,
            )
            target.write_text("manual\n", encoding="utf-8")
            rollback_result = rollback_patch({}, context)
            persisted = target.read_text(encoding="utf-8")

        self.assertFalse(patch_result.is_error)
        self.assertTrue(rollback_result.is_error)
        self.assertIn("Refusing rollback", rollback_result.content)
        self.assertEqual(persisted, "manual\n")

    def test_run_tests_runs_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            tests = workspace / "tests"
            tests.mkdir()
            (tests / "test_ok.py").write_text(
                "import unittest\n\n"
                "class OkTests(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            result = run_tests(
                {"command": "PYTHONPATH=. python3 -m unittest discover -s tests", "timeout": 10},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertFalse(result.is_error)
        self.assertIn("OK", result.content)
        self.assertIn("[exit_code] 0", result.content)
        self.assertEqual(result.metadata["argv"], ["python3", "-m", "unittest", "discover", "-s", "tests"])
        self.assertEqual(result.metadata["environment_keys"], ["PYTHONPATH"])
        self.assertEqual(result.metadata["exit_code"], 0)
        self.assertEqual(result.metadata["working_directory"], str(workspace))
        self.assertEqual(result.metadata["execution_status"], "succeeded")

    def test_run_tests_propagates_real_python_test_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            tests = workspace / "tests"
            tests.mkdir()
            (tests / "test_fail.py").write_text(
                "import unittest\n\n"
                "class FailTests(unittest.TestCase):\n"
                "    def test_failure(self):\n"
                "        self.fail('expected failure')\n",
                encoding="utf-8",
            )
            result = run_tests(
                {"command": "python3 -m unittest discover -s tests", "timeout": 10},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertTrue(result.is_error)
        self.assertIn("FAILED", result.content)
        self.assertEqual(result.metadata["exit_code"], 1)
        self.assertEqual(result.metadata["execution_status"], "failed")

    def test_run_tests_rejects_shell_syntax_and_arbitrary_exec_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            side_effect = workspace / "should-not-exist.txt"
            commands = (
                "false || true",
                "mvn test | tail || true",
                "mvn test > test.log",
                f"python3 -c \"from pathlib import Path; Path('{side_effect}').write_text('bad')\"",
                f"bash -c \"touch {side_effect}\"",
                f"touch {side_effect}",
                "python3 -m unittest $(echo tests.test_ok)",
                "python3 -m unittest `echo tests.test_ok`",
                "python3 -m unittest tests.test_ok\nprintf nope",
            )
            for command in commands:
                with self.subTest(command=command):
                    result = run_tests(
                        {"command": command},
                        ToolContext(workspace=workspace, approval_mode="yolo"),
                    )
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.metadata["execution_status"], "not_run")
                    self.assertIsNone(result.metadata["exit_code"])
                    self.assertFalse(side_effect.exists())

    def test_run_tests_supports_test_runner_families_without_shell(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="ok\n", stderr="")
        commands = (
            "python3 -m pytest tests",
            "pytest tests",
            "mvn test",
            "./mvnw verify",
            "gradle test",
            "./gradlew check",
            "npm test",
            "pnpm run test:unit",
            "yarn test",
            "bun test",
            "go test ./...",
            "cargo test",
            "dotnet test",
            "make check",
            "tox",
            "nox",
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            with patch("local_agent.tools.shell.subprocess.run", return_value=completed) as mocked_run:
                for command in commands:
                    with self.subTest(command=command):
                        result = run_tests(
                            {"command": command},
                            ToolContext(workspace=workspace, approval_mode="yolo"),
                        )
                        self.assertFalse(result.is_error)
                        self.assertEqual(result.metadata["execution_status"], "succeeded")
                        self.assertFalse(mocked_run.call_args.kwargs["shell"])

    def test_run_tests_maven_policy_preserves_env_and_real_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            bin_dir = workspace / "bin"
            bin_dir.mkdir()
            maven = bin_dir / "mvn"
            maven.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            maven.chmod(0o755)
            context = ToolContext(workspace=workspace, approval_mode="yolo")
            command = (
                f"PATH={bin_dir}:$PATH JAVA_HOME=/java8 mvn -s settings.xml "
                "-Dmaven.repo.local=.m2 -Dmaven.compiler.source=1.8 "
                "-Dmaven.compiler.target=1.8 test"
            )

            passed = run_tests({"command": command}, context)
            maven.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
            failed = run_tests({"command": command.replace(" test", " verify")}, context)
            skipped = run_tests({"command": "mvn -DskipTests test"}, context)

        self.assertFalse(passed.is_error)
        self.assertEqual(passed.metadata["exit_code"], 0)
        self.assertEqual(passed.metadata["environment_keys"], ["JAVA_HOME", "PATH"])
        self.assertTrue(failed.is_error)
        self.assertEqual(failed.metadata["exit_code"], 7)
        self.assertEqual(failed.metadata["execution_status"], "failed")
        self.assertTrue(skipped.is_error)
        self.assertEqual(skipped.metadata["execution_status"], "not_run")

    def test_run_tests_cwd_is_canonical_and_cannot_escape_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            module = workspace / "module"
            outside = root / "outside"
            module.mkdir(parents=True)
            outside.mkdir()
            tests = module / "tests"
            tests.mkdir()
            (tests / "test_ok.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            context = ToolContext(workspace=workspace, approval_mode="yolo")

            allowed = run_tests(
                {"command": "python3 -m unittest discover -s tests", "cwd": "module"},
                context,
            )
            denied = run_tests(
                {"command": "python3 -m unittest discover -s tests", "cwd": "../outside"},
                context,
            )

        self.assertFalse(allowed.is_error)
        self.assertEqual(allowed.metadata["working_directory"], str(module))
        self.assertTrue(denied.is_error)
        self.assertEqual(denied.metadata["execution_status"], "not_run")
        self.assertIn("escapes workspace", denied.content)

    def test_run_tests_allow_policy_does_not_bypass_denied_shell(self) -> None:
        registry = ToolRegistry(shell_tools())
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            tests = workspace / "tests"
            tests.mkdir()
            (tests / "test_ok.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            side_effect = workspace / "side-effect.txt"
            context = ToolContext(
                workspace=workspace,
                approval_mode="yolo",
                tool_approval={"shell": "deny", "run_tests": "allow"},
            )

            valid = registry.execute(
                "run_tests",
                {"command": "python3 -m unittest discover -s tests"},
                context,
            )
            escaped = registry.execute("run_tests", {"command": f"touch {side_effect}"}, context)
            shell = registry.execute("shell", {"command": f"touch {side_effect}"}, context)
            side_effect_exists = side_effect.exists()

        self.assertFalse(valid.is_error)
        self.assertTrue(escaped.is_error)
        self.assertEqual(escaped.metadata["execution_status"], "not_run")
        self.assertTrue(shell.is_error)
        self.assertEqual(shell.metadata["execution_status"], "denied")
        self.assertFalse(side_effect_exists)

    def test_run_tests_rejects_bare_test_module_with_actionable_command_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_tests(
                {"command": "tests.test_math"},
                ToolContext(workspace=Path(tmp).resolve(), approval_mode="yolo"),
            )

        self.assertTrue(result.is_error)
        self.assertIn("python3 -m unittest tests.test_math", result.content)
        self.assertEqual(result.metadata["executed_command"], "tests.test_math")
        self.assertEqual(result.metadata["execution_status"], "not_run")

    def test_shell_timeout_is_clamped_to_remaining_budget(self) -> None:
        calls: list[dict] = []

        def fake_run(*args, **kwargs):
            calls.append(kwargs)
            return type("Completed", (), {"stdout": "ok\n", "stderr": "", "returncode": 0})()

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            context = ToolContext(
                workspace=workspace,
                approval_mode="yolo",
                deadline_monotonic=110.0,
            )
            with patch("local_agent.tools.shell.time.monotonic", return_value=100.0):
                with patch("local_agent.tools.shell.subprocess.run", side_effect=fake_run):
                    result = run_shell({"command": "echo ok", "timeout": 600}, context)

        self.assertFalse(result.is_error)
        self.assertEqual(calls[0]["timeout"], 10)

    def test_shell_rejects_dangerous_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            result = run_shell(
                {"command": "rm -rf /", "timeout": 10},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertTrue(result.is_error)
        self.assertIn("Refusing dangerous command", result.content)

    def test_write_tool_approval_in_non_interactive_stdin_returns_tool_error(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_write",
                    description="sample write",
                    tier="write",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "ok", "is_error": False})(),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="ask")
            with patch("sys.stdin.isatty", return_value=False):
                result = registry.execute("sample_write", "{}", context)

        self.assertTrue(result.is_error)
        self.assertIn("requires approval", result.content)
        self.assertIn("stdin is not interactive", result.content)

    def test_approval_prompt_emits_requested_and_result_events(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_exec",
                    description="sample exec",
                    tier="exec",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "ok", "is_error": False})(),
                )
            ]
        )
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="always-ask",
                event_callback=lambda event_type, payload: events.append((event_type, payload)),
            )
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="y"):
                result = registry.execute("sample_exec", "{}", context)

        self.assertFalse(result.is_error)
        self.assertEqual(events[0][0], "ApprovalRequested")
        self.assertEqual(events[0][1]["tool"], "sample_exec")
        self.assertEqual(events[1][0], "ApprovalResult")
        self.assertEqual(events[1][1]["decision"], "allow_once")
        self.assertTrue(events[1][1]["allowed"])

    def test_write_tool_approval_eof_returns_tool_error(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_write",
                    description="sample write",
                    tier="write",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "ok", "is_error": False})(),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="ask")
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=EOFError):
                result = registry.execute("sample_write", "{}", context)

        self.assertTrue(result.is_error)
        self.assertIn("stdin closed", result.content)
        self.assertEqual(result.metadata["execution_status"], "denied")
        self.assertEqual(result.metadata["denial_kind"], "approval")

    def test_auto_approve_tool_bypasses_prompt_in_ask_mode(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_write",
                    description="sample write",
                    tier="write",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "ok", "is_error": False})(),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="ask",
                auto_approve_tools=("sample_write",),
            )
            with patch("sys.stdin.isatty", return_value=False):
                result = registry.execute("sample_write", "{}", context)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "ok")

    def test_tool_approval_allow_bypasses_prompt_in_ask_mode(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_exec",
                    description="sample exec",
                    tier="exec",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "ok", "is_error": False})(),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="ask",
                tool_approval={"sample_exec": "allow"},
            )
            with patch("sys.stdin.isatty", return_value=False):
                result = registry.execute("sample_exec", "{}", context)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "ok")

    def test_tool_approval_deny_blocks_even_in_yolo_mode(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_exec",
                    description="sample exec",
                    tier="exec",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "ok", "is_error": False})(),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="yolo",
                tool_approval={"sample_exec": "deny"},
            )
            result = registry.execute("sample_exec", "{}", context)

        self.assertTrue(result.is_error)
        self.assertIn("denied by tool_approval", result.content)

    def test_tool_approval_prompt_forces_prompt_for_read_tool(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_read",
                    description="sample read",
                    tier="read",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "ok", "is_error": False})(),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="auto-read",
                tool_approval={"sample_read": "prompt"},
            )
            with patch("sys.stdin.isatty", return_value=False):
                result = registry.execute("sample_read", "{}", context)

        self.assertTrue(result.is_error)
        self.assertIn("requires approval", result.content)

    def test_write_approval_mode_allows_write_but_prompts_exec(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_write",
                    description="sample write",
                    tier="write",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "write ok", "is_error": False})(),
                ),
                Tool(
                    name="sample_exec",
                    description="sample exec",
                    tier="exec",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "exec ok", "is_error": False})(),
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="write")
            with patch("sys.stdin.isatty", return_value=False):
                write_result = registry.execute("sample_write", "{}", context)
                exec_result = registry.execute("sample_exec", "{}", context)

        self.assertFalse(write_result.is_error)
        self.assertEqual(write_result.content, "write ok")
        self.assertTrue(exec_result.is_error)
        self.assertIn("requires approval", exec_result.content)

    def test_session_allow_answer_allows_same_tool_without_reprompt(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_exec",
                    description="sample exec",
                    tier="exec",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "ok", "is_error": False})(),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            session_policy: dict[str, str] = {}
            context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="always-ask",
                session_tool_approval=session_policy,
            )
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="s") as ask:
                first = registry.execute("sample_exec", "{}", context)
            with patch("sys.stdin.isatty", return_value=False):
                second = registry.execute("sample_exec", "{}", context)

        self.assertFalse(first.is_error)
        self.assertFalse(second.is_error)
        self.assertEqual(session_policy, {"sample_exec": "allow_always"})
        self.assertEqual(ask.call_count, 1)

    def test_config_prompt_is_not_overridden_by_session_allow(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_exec",
                    description="sample exec",
                    tier="exec",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "ok", "is_error": False})(),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="yolo",
                tool_approval={"sample_exec": "prompt"},
                session_tool_approval={"sample_exec": "allow_always"},
            )
            with patch("sys.stdin.isatty", return_value=False):
                result = registry.execute("sample_exec", "{}", context)

        self.assertTrue(result.is_error)
        self.assertIn("requires approval", result.content)

    def test_approval_deadline_already_exhausted_cancels_without_input(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_exec",
                    description="sample exec",
                    tier="exec",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "ok", "is_error": False})(),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="always-ask",
                deadline_monotonic=9.0,
            )
            fake_stdin = _FakeStdin("y\n")
            with (
                patch("local_agent.tools.base.sys.stdin", fake_stdin),
                patch("local_agent.tools.base.time.monotonic", return_value=10.0),
                patch("builtins.input", side_effect=AssertionError("input should not be called")),
                patch("local_agent.tools.base.select.select", side_effect=AssertionError("select should not be called")),
            ):
                result = registry.execute("sample_exec", "{}", context)

        self.assertTrue(result.is_error)
        self.assertIn("approval cancelled because budget_seconds is exhausted", result.content)

    def test_approval_select_timeout_returns_tool_error(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_exec",
                    description="sample exec",
                    tier="exec",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "ok", "is_error": False})(),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="always-ask",
                deadline_monotonic=20.0,
            )
            fake_stdin = _FakeStdin("y\n")
            with (
                patch("local_agent.tools.base.sys.stdin", fake_stdin),
                patch("local_agent.tools.base.time.monotonic", return_value=10.0),
                patch("builtins.print"),
                patch("local_agent.tools.base.select.select", return_value=([], [], [])) as wait,
            ):
                result = registry.execute("sample_exec", "{}", context)

        self.assertTrue(result.is_error)
        self.assertIn("approval cancelled because budget_seconds is exhausted", result.content)
        wait.assert_called_once_with([fake_stdin], [], [], 10.0)

    def test_approval_timed_input_y_allows_execution(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_exec",
                    description="sample exec",
                    tier="exec",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "ok", "is_error": False})(),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="always-ask",
                deadline_monotonic=20.0,
            )
            fake_stdin = _FakeStdin("y\n")
            with (
                patch("local_agent.tools.base.sys.stdin", fake_stdin),
                patch("local_agent.tools.base.time.monotonic", return_value=10.0),
                patch("builtins.print"),
                patch("local_agent.tools.base.select.select", return_value=([fake_stdin], [], [])),
            ):
                result = registry.execute("sample_exec", "{}", context)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "ok")

    def test_approval_timed_input_session_allow_and_deny_are_preserved(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_exec",
                    description="sample exec",
                    tier="exec",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "ok", "is_error": False})(),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            allow_policy: dict[str, str] = {}
            allow_context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="always-ask",
                session_tool_approval=allow_policy,
                deadline_monotonic=20.0,
            )
            allow_stdin = _FakeStdin("s\n")
            with (
                patch("local_agent.tools.base.sys.stdin", allow_stdin),
                patch("local_agent.tools.base.time.monotonic", return_value=10.0),
                patch("builtins.print"),
                patch("local_agent.tools.base.select.select", return_value=([allow_stdin], [], [])),
            ):
                allow_result = registry.execute("sample_exec", "{}", allow_context)

            deny_policy: dict[str, str] = {}
            deny_context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="always-ask",
                session_tool_approval=deny_policy,
                deadline_monotonic=20.0,
            )
            deny_stdin = _FakeStdin("d\n")
            with (
                patch("local_agent.tools.base.sys.stdin", deny_stdin),
                patch("local_agent.tools.base.time.monotonic", return_value=10.0),
                patch("builtins.print"),
                patch("local_agent.tools.base.select.select", return_value=([deny_stdin], [], [])),
            ):
                deny_result = registry.execute("sample_exec", "{}", deny_context)

        self.assertFalse(allow_result.is_error)
        self.assertEqual(allow_policy, {"sample_exec": "allow_always"})
        self.assertTrue(deny_result.is_error)
        self.assertIn("denied tool execution for this session", deny_result.content)
        self.assertEqual(deny_policy, {"sample_exec": "reject_always"})

    def test_write_mode_still_auto_allows_write_and_times_exec_approval(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_write",
                    description="sample write",
                    tier="write",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "write ok", "is_error": False})(),
                ),
                Tool(
                    name="sample_exec",
                    description="sample exec",
                    tier="exec",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "exec ok", "is_error": False})(),
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="write",
                deadline_monotonic=20.0,
            )
            fake_stdin = _FakeStdin("y\n")
            with (
                patch("local_agent.tools.base.sys.stdin", fake_stdin),
                patch("local_agent.tools.base.time.monotonic", return_value=10.0),
                patch("builtins.print"),
                patch("local_agent.tools.base.select.select", return_value=([], [], [])) as wait,
            ):
                write_result = registry.execute("sample_write", "{}", context)
                exec_result = registry.execute("sample_exec", "{}", context)

        self.assertFalse(write_result.is_error)
        self.assertEqual(write_result.content, "write ok")
        self.assertTrue(exec_result.is_error)
        self.assertIn("approval cancelled because budget_seconds is exhausted", exec_result.content)
        wait.assert_called_once_with([fake_stdin], [], [], 10.0)

    def test_session_deny_answer_blocks_same_tool_without_reprompt(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_exec",
                    description="sample exec",
                    tier="exec",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "ok", "is_error": False})(),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            session_policy: dict[str, str] = {}
            context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="always-ask",
                session_tool_approval=session_policy,
            )
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="d") as ask:
                first = registry.execute("sample_exec", "{}", context)
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="y") as second_ask:
                second = registry.execute("sample_exec", "{}", context)

        self.assertTrue(first.is_error)
        self.assertTrue(second.is_error)
        self.assertIn("denied by session approval", second.content)
        self.assertEqual(session_policy, {"sample_exec": "reject_always"})
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(second_ask.call_count, 0)

    def test_state_tool_does_not_require_approval_in_ask_mode(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_state",
                    description="sample state",
                    tier="state",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "ok", "is_error": False})(),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="ask")
            with patch("sys.stdin.isatty", return_value=False):
                result = registry.execute("sample_state", "{}", context)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "ok")

    def test_todo_add_update_and_read_use_session_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            state_dir = workspace / "state"
            context = ToolContext(
                workspace=workspace,
                approval_mode="ask",
                state_dir=state_dir,
                session_id="session-1",
            )

            added = todo_add({"id": "T1", "task": "Wire budget seconds"}, context)
            updated = todo_update({"id": "T1", "status": "done", "note": "covered by tests"}, context)
            read = todo_read({}, context)
            todo_file = state_dir / "todos" / "session-1.json"
            todo_file_exists = todo_file.exists()
            workspace_todo_dir_exists = (workspace / ".local-agent" / "todos").exists()

        self.assertFalse(added.is_error)
        self.assertFalse(updated.is_error)
        self.assertFalse(read.is_error)
        self.assertTrue(todo_file_exists)
        self.assertFalse(workspace_todo_dir_exists)
        self.assertIn("[done] T1: Wire budget seconds", read.content)
        self.assertIn("covered by tests", read.content)

    def test_todo_accepts_key_content_aliases_with_guidance(self) -> None:
        registry = ToolRegistry(todo_tools())
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            context = ToolContext(
                workspace=workspace,
                approval_mode="yolo",
                state_dir=workspace / "state",
                session_id="session-1",
            )

            added = registry.execute(
                "todo_add",
                '{"key": "step1", "content": "Read requirement", "status": "pending"}',
                context,
            )
            updated = registry.execute(
                "todo_update",
                '{"key": "step1", "content": "Summarize requirement", "status": "done"}',
                context,
            )
            read = todo_read({}, context)

        self.assertFalse(added.is_error)
        self.assertFalse(updated.is_error)
        self.assertIn("[compatibility normalized]", added.content)
        self.assertIn("key -> id", added.content)
        self.assertIn("content -> task", updated.content)
        self.assertIn("status pending -> todo", added.content)
        self.assertIn("[done] step1: Summarize requirement", read.content)

    def test_registry_normalizes_observed_apply_patch_argument_variants(self) -> None:
        registry = ToolRegistry(file_tools())
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "README.md"
            target.write_text("old value\n", encoding="utf-8")
            result = registry.execute(
                "apply_patch",
                {
                    "path": "README.md",
                    "file_hash": hash_text("old value\n"),
                    "file_hash_tag": hash_text("old value\n"),
                    "source_hash_tag": hash_text("old value\n"),
                    "hash_tag": hash_text("old value\n"),
                    "start_line": "1",
                    "end_line": "1",
                    "old_str": "old value\n",
                    "new_str": "new value\n",
                    "mode": "edit",
                    "dry_run": "True",
                },
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

            self.assertEqual(target.read_text(encoding="utf-8"), "old value\n")

        self.assertFalse(result.is_error)
        self.assertIn("Patch preview only", result.content)
        self.assertIn("[compatibility normalized]", result.content)
        self.assertIn("file_hash -> tag", result.content)
        self.assertIn("ignored redundant file_hash_tag", result.content)
        self.assertIn("ignored redundant source_hash_tag", result.content)
        self.assertIn("ignored redundant hash_tag", result.content)
        self.assertIn("mode edit -> replace", result.content)
        self.assertIn("start_line string -> integer", result.content)
        self.assertIn("dry_run string -> boolean (true)", result.content)

    def test_registry_rejects_conflicting_compatibility_aliases(self) -> None:
        registry = ToolRegistry(file_tools())
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            result = registry.execute(
                "apply_patch",
                {
                    "path": "README.md",
                    "tag": "canonical",
                    "file_hash": "legacy",
                    "start_line": 1,
                    "end_line": 1,
                    "old_text": "old",
                    "new_text": "new",
                },
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertTrue(result.is_error)
        self.assertIn("Conflicting compatibility arguments", result.content)

    def test_apply_patch_preview_checker_blocks_real_write_but_not_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "README.md"
            target.write_text("old value\n", encoding="utf-8")
            args = {
                "path": "README.md",
                "tag": hash_text("old value\n"),
                "start_line": 1,
                "end_line": 1,
                "old_text": "old value\n",
                "new_text": "new value\n",
            }
            context = ToolContext(
                workspace=workspace,
                approval_mode="yolo",
                patch_preview_checker=lambda patch_args, path: "Preview contract: preview required.",
            )

            preview = patch_file({**args, "dry_run": True}, context)
            real_write = patch_file(args, context)

        self.assertFalse(preview.is_error)
        self.assertTrue(real_write.is_error)
        self.assertIn("Preview contract", real_write.content)

    def test_registry_normalizes_cmd_for_run_tests_only(self) -> None:
        received: list[dict[str, str]] = []
        registry = ToolRegistry(
            [
                Tool(
                    name="run_tests",
                    description="test",
                    tier="exec",
                    input_schema={
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    handler=lambda args, context: _record_command(args, received),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = registry.execute(
                "run_tests",
                {"cmd": "PYTHONPATH=src python3 -m unittest tests.test_task_contract"},
                ToolContext(workspace=Path(tmp).resolve(), approval_mode="yolo"),
            )

        self.assertFalse(result.is_error)
        self.assertEqual(received, [{"command": "PYTHONPATH=src python3 -m unittest tests.test_task_contract"}])
        self.assertIn("cmd -> command", result.content)

    def test_todo_add_missing_arguments_returns_actionable_example(self) -> None:
        registry = ToolRegistry(todo_tools())
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="yolo")
            result = registry.execute("todo_add", "{}", context)

        self.assertTrue(result.is_error)
        self.assertIn("Missing required todo argument(s): id, task", result.content)
        self.assertIn('todo_add {"id":"step-1","task":"Read the requirement"', result.content)

    def test_todo_update_unknown_id_lists_known_ids_and_example(self) -> None:
        registry = ToolRegistry(todo_tools())
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            context = ToolContext(
                workspace=workspace,
                approval_mode="yolo",
                state_dir=workspace / "state",
                session_id="session-1",
            )

            added = registry.execute("todo_add", '{"id": "T1", "task": "Read requirement"}', context)
            result = registry.execute("todo_update", '{"id": "step1", "status": "done"}', context)

        self.assertFalse(added.is_error)
        self.assertTrue(result.is_error)
        self.assertIn("Todo not found: step1", result.content)
        self.assertIn("Known todo id(s): T1", result.content)
        self.assertIn('todo_update {"id":"step-1","status":"done"', result.content)

    def test_learn_writes_learned_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            context = ToolContext(workspace=workspace, approval_mode="yolo")

            learned = learn({"topic": "tests", "lesson": "Run focused tests before full tests."}, context)
            read = memory_read({"name": "learned"}, context)

        self.assertFalse(learned.is_error)
        self.assertIn(".local-agent/memory/learned.md", learned.content)
        self.assertFalse(read.is_error)
        self.assertIn("tests", read.content)
        self.assertIn("Run focused tests before full tests.", read.content)

    def test_memory_read_allows_custom_safe_memory_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            memory_dir = workspace / ".local-agent" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "enterprise-service-boundary.md").write_text("zqyl-charge\n", encoding="utf-8")
            context = ToolContext(workspace=workspace, approval_mode="yolo")

            result = memory_read({"name": "enterprise-service-boundary"}, context)

        self.assertFalse(result.is_error)
        self.assertIn("zqyl-charge", result.content)

    def test_memory_read_rejects_unsafe_memory_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="yolo")

            result = memory_read({"name": "../secrets"}, context)

        self.assertTrue(result.is_error)
        self.assertIn("Invalid memory name", result.content)

    def test_ask_user_non_interactive_returns_tool_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="ask")
            with patch("sys.stdin.isatty", return_value=False):
                result = ask_user({"question": "Continue?"}, context)

        self.assertTrue(result.is_error)
        self.assertIn("stdin is not interactive", result.content)

    def test_ask_user_non_interactive_can_use_default_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="ask")
            with patch("sys.stdin.isatty", return_value=False):
                result = ask_user({"question": "Continue?", "default_answer": "skip"}, context)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "skip")

    def test_ask_user_returns_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="ask")
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="yes"):
                result = ask_user({"question": "Continue?"}, context)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "yes")

    def test_ask_user_timeout_can_use_default_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="ask")
            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("local_agent.tools.interaction._read_timed_answer", return_value=None),
            ):
                result = ask_user(
                    {"question": "Continue?", "timeout_seconds": 1, "default_answer": "continue"},
                    context,
                )

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "continue")

    def test_ask_user_uses_remaining_budget_as_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="ask",
                deadline_monotonic=105.0,
            )
            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("local_agent.tools.interaction.time.monotonic", return_value=100.0),
                patch("local_agent.tools.interaction._read_timed_answer", return_value="yes") as read_answer,
            ):
                result = ask_user({"question": "Continue?"}, context)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "yes")
        self.assertEqual(read_answer.call_args.args[1], 5)

    def test_ask_user_clamps_requested_timeout_to_remaining_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="ask",
                deadline_monotonic=105.0,
            )
            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("local_agent.tools.interaction.time.monotonic", return_value=100.0),
                patch("local_agent.tools.interaction._read_timed_answer", return_value="yes") as read_answer,
            ):
                result = ask_user({"question": "Continue?", "timeout_seconds": 3600}, context)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "yes")
        self.assertEqual(read_answer.call_args.args[1], 5)

    def test_git_diff_explains_untracked_files_when_diff_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            subprocess.run(["git", "init"], cwd=workspace, text=True, capture_output=True, check=True)
            (workspace / "README.md").write_text("hello\n", encoding="utf-8")

            result = git_diff({}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertFalse(result.is_error)
        self.assertIn("(empty diff)", result.content)
        self.assertIn("?? README.md", result.content)
        self.assertIn("git diff does not show untracked files", result.content)

    def test_git_status_non_repository_only_describes_primary_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            result = git_status({}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertTrue(result.is_error)
        self.assertIn(f"primary workspace only: {workspace}", result.content)
        self.assertIn("Use /move", result.content)
        self.assertEqual(result.metadata["git_probe_root"], str(workspace))
        self.assertFalse(result.metadata["git_repository"])

    def test_git_diff_adds_diff_summary_with_counts_and_hunk_snippets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            subprocess.run(["git", "init"], cwd=workspace, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=workspace, check=True)
            (workspace / "README.md").write_text(
                "# local-coding-agent\n\n"
                "一个个人本地编程助手 Agent 的 MVP。\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=workspace, text=True, capture_output=True, check=True)
            (workspace / "README.md").write_text(
                "# local-coding-agent\n"
                "# local-coding-agent\n\n"
                "> smoke test marker\n\n"
                "一个个人本地编程助手 Agent 的 MVP。\n",
                encoding="utf-8",
            )

            result = git_diff({}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertFalse(result.is_error)
        self.assertIn("[diff summary]", result.content)
        self.assertIn("Total: 1 file(s), +3 -0, 1 hunk(s).", result.content)
        self.assertIn("README.md: +3 -0, 1 hunk(s).", result.content)
        self.assertIn("added: # local-coding-agent | <blank line> | > smoke test marker", result.content)

    def test_git_diff_adds_run_attribution_for_baseline_and_session_patches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            subprocess.run(["git", "init"], cwd=workspace, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=workspace, check=True)
            (workspace / "README.md").write_text("original readme\n", encoding="utf-8")
            (workspace / "app.py").write_text("print('old')\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=workspace, text=True, capture_output=True, check=True)
            (workspace / "README.md").write_text("pre-existing readme change\n", encoding="utf-8")
            baseline = capture_git_baseline(workspace)
            context = ToolContext(
                workspace=workspace,
                approval_mode="yolo",
                state_dir=workspace / ".agent-state",
                session_id="session-1",
                git_baseline=baseline,
            )

            patch_file(
                {
                    "path": "README.md",
                    "tag": hash_text("pre-existing readme change\n"),
                    "start_line": 1,
                    "end_line": 1,
                    "old_text": "pre-existing readme change\n",
                    "new_text": "this-session readme change\n",
                },
                context,
            )
            patch_file(
                {
                    "path": str(workspace / "app.py"),
                    "tag": hash_text("print('old')\n"),
                    "start_line": 1,
                    "end_line": 1,
                    "old_text": "print('old')\n",
                    "new_text": "print('new')\n",
                },
                context,
            )

            result = git_diff({}, context)

        self.assertFalse(result.is_error)
        self.assertIn("[diff attribution]", result.content)
        self.assertIn("Pre-existing dirty files at run start: README.md", result.content)
        self.assertIn("This-session apply_patch files: README.md, app.py", result.content)
        self.assertIn("Files with both pre-existing and this-session changes: README.md", result.content)
        self.assertIn("summarize pre-existing and this-session changes separately", result.content)
        self.assertNotIn("Current diff files not present at baseline and not recorded by apply_patch: app.py", result.content)

    def test_git_diff_adds_relevance_reviewer_for_low_relevance_session_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "deployMessage" / "nacos" / "app.properties"
            target.parent.mkdir(parents=True)
            subprocess.run(["git", "init"], cwd=workspace, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=workspace, check=True)
            original = "old=true\n"
            target.write_text(original, encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=workspace, text=True, capture_output=True, check=True)
            baseline = capture_git_baseline(workspace)
            context = ToolContext(
                workspace=workspace,
                approval_mode="yolo",
                state_dir=workspace / ".agent-state",
                session_id="session-1",
                git_baseline=baseline,
                current_user_request="实现 Java 导入校验需求",
            )

            patch_file(
                {
                    "path": "deployMessage/nacos/app.properties",
                    "tag": hash_text(original),
                    "start_line": 1,
                    "end_line": 1,
                    "old_text": "old=true",
                    "new_text": "old=false",
                },
                context,
            )
            result = git_diff({}, context)

        self.assertFalse(result.is_error)
        self.assertIn("[diff reviewer]", result.content)
        self.assertIn("Potential relevance warning", result.content)
        self.assertIn("deployMessage/nacos/app.properties", result.content)

    def test_git_diff_adds_implementation_reviewer_for_comment_only_code_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "src" / "ExemptCompanyDto.java"
            target.parent.mkdir()
            subprocess.run(["git", "init"], cwd=workspace, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=workspace, check=True)
            original = (
                "public class ExemptCompanyDto {\n"
                "    /**\n"
                "     * 企业ID\n"
                "     */\n"
                "    private String companyId;\n"
                "}\n"
            )
            target.write_text(original, encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=workspace, text=True, capture_output=True, check=True)
            baseline = capture_git_baseline(workspace)
            context = ToolContext(
                workspace=workspace,
                approval_mode="yolo",
                state_dir=workspace / ".agent-state",
                session_id="session-1",
                git_baseline=baseline,
                current_user_request="实现 Java 导入校验需求",
            )

            patch_file(
                {
                    "path": "src/ExemptCompanyDto.java",
                    "tag": hash_text(original),
                    "start_line": 2,
                    "end_line": 4,
                    "old_text": "    /**\n     * 企业ID\n     */",
                    "new_text": "    /**\n     * 企业ID\n     * <p>业务规则：</p>\n     */",
                },
                context,
            )
            result = git_diff({}, context)

        self.assertFalse(result.is_error)
        self.assertIn("[diff reviewer]", result.content)
        self.assertIn("implementation-quality warning", result.content)
        self.assertIn("src/ExemptCompanyDto.java", result.content)
        self.assertIn("do not claim behavior", result.content)

    def test_git_diff_does_not_treat_vue_template_markup_as_comment_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "src" / "UserCard.vue"
            target.parent.mkdir()
            subprocess.run(["git", "init"], cwd=workspace, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=workspace, check=True)
            original = (
                "<template>\n"
                "  <p>{{ oldTitle }}</p>\n"
                "</template>\n"
                "<script setup>\n"
                "const oldTitle = 'old'\n"
                "</script>\n"
            )
            target.write_text(original, encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=workspace, text=True, capture_output=True, check=True)
            baseline = capture_git_baseline(workspace)
            context = ToolContext(
                workspace=workspace,
                approval_mode="yolo",
                state_dir=workspace / ".agent-state",
                session_id="session-1",
                git_baseline=baseline,
                current_user_request="实现 Vue 标题展示需求",
            )

            patch_file(
                {
                    "path": "src/UserCard.vue",
                    "tag": hash_text(original),
                    "start_line": 2,
                    "end_line": 2,
                    "old_text": "  <p>{{ oldTitle }}</p>",
                    "new_text": "  <p>{{ title }}</p>",
                },
                context,
            )
            result = git_diff({}, context)

        self.assertFalse(result.is_error)
        self.assertIn("src/UserCard.vue", result.content)
        self.assertNotIn("implementation-quality warning", result.content)

    def test_tool_registry_validates_required_enum_and_extra_args(self) -> None:
        calls: list[dict] = []

        def handler(args, context):
            calls.append(args)
            return type("Result", (), {"content": "ok", "is_error": False})()

        registry = ToolRegistry(
            [
                Tool(
                    name="sample",
                    description="sample",
                    tier="read",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "enum": ["project"]},
                            "count": {"type": "integer", "minimum": 1, "maximum": 3},
                        },
                        "required": ["name", "count"],
                        "additionalProperties": False,
                    },
                    handler=handler,
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="yolo")
            ok = registry.execute("sample", '{"name": "project", "count": "2"}', context)
            bad_enum = registry.execute("sample", '{"name": "other", "count": 1}', context)
            extra = registry.execute("sample", '{"name": "project", "count": 1, "extra": true}', context)

        self.assertFalse(ok.is_error)
        self.assertEqual(calls[0]["count"], 2)
        self.assertTrue(bad_enum.is_error)
        self.assertIn("must be one of", bad_enum.content)
        self.assertTrue(extra.is_error)
        self.assertIn("Unexpected argument", extra.content)


if __name__ == "__main__":
    unittest.main()

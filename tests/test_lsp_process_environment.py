from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from local_agent.lsp import config as lsp_config
from local_agent.lsp.client import StdioLspClient, _child_process_environment, close_all_clients, get_client
from local_agent.lsp.config import JDTLS_METADATA_CONTAINMENT_OPTION
from local_agent.lsp.config import LspProcessEnvironment
from local_agent.lsp.config import LspServerConfig


def _write_environment_lsp(path: Path) -> None:
    path.write_text(
        r'''
from __future__ import annotations

import json
import os
import sys


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return None
        if line in {b"\r\n", b"\n"}:
            break
        key, value = line.decode("ascii").split(":", 1)
        headers[key.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def send(payload):
    body = json.dumps(payload).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    sys.stdout.buffer.write(header + body)
    sys.stdout.buffer.flush()


while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"capabilities": {}}})
    elif method == "textDocument/documentSymbol":
        value = os.environ.get("JAVA_TOOL_OPTIONS", "")
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": [{
                "name": value,
                "kind": 13,
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 1},
                },
                "selectionRange": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 1},
                },
            }],
        })
    elif request_id is not None:
        send({"jsonrpc": "2.0", "id": request_id, "result": None})
'''.lstrip(),
        encoding="utf-8",
    )


class LspProcessEnvironmentTests(unittest.TestCase):
    def _server(self, name: str, command: tuple[str, ...] = ("server",)) -> LspServerConfig:
        return LspServerConfig(
            name=name,
            command=command,
            file_types=(".java",),
            root_markers=("pom.xml",),
            language_id="java",
        )

    def test_default_and_override_jdtls_configs_have_metadata_containment(self) -> None:
        default = next(server for server in lsp_config.DEFAULT_SERVER_CONFIGS if server.name == "jdtls")
        self.assertEqual(
            default.process_environment.append[-1],
            ("JAVA_TOOL_OPTIONS", JDTLS_METADATA_CONTAINMENT_OPTION),
        )

        with (
            patch("local_agent.lsp.config._command_override", return_value=None),
            patch("local_agent.lsp.config._resolve_command", side_effect=lambda _workspace, command: command),
        ):
            defaults = lsp_config.resolved_server_configs(Path("/tmp").resolve())
        resolved_default = next(server for server in defaults if server.name == "jdtls")
        self.assertEqual(resolved_default.command, ("jdtls",))
        self.assertEqual(
            resolved_default.process_environment.append[-1],
            ("JAVA_TOOL_OPTIONS", JDTLS_METADATA_CONTAINMENT_OPTION),
        )

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            override = workspace / "custom-jdtls"
            override.write_text("not a java launcher\n", encoding="utf-8")
            with patch.dict(
                "os.environ",
                {"AGENT_LSP_JDTLS_COMMAND": str(override)},
                clear=True,
            ):
                resolved = lsp_config.resolved_server_configs(workspace)

        jdtls = next(server for server in resolved if server.name == "jdtls")
        self.assertEqual(jdtls.command, (str(override),))
        self.assertEqual(
            jdtls.process_environment.append[-1],
            ("JAVA_TOOL_OPTIONS", JDTLS_METADATA_CONTAINMENT_OPTION),
        )

    def test_child_environment_preserves_parent_and_appends_false_last(self) -> None:
        parent = {
            "JAVA_TOOL_OPTIONS": "-Xmx256m -Djava.import.generatesMetadataFilesAtProjectRoot=true",
            "UNCHANGED": "value",
        }
        original = dict(parent)

        merged = _child_process_environment(self._server("jdtls"), parent)

        self.assertEqual(parent, original)
        self.assertEqual(merged["UNCHANGED"], "value")
        self.assertEqual(
            merged["JAVA_TOOL_OPTIONS"],
            f"{original['JAVA_TOOL_OPTIONS']} {JDTLS_METADATA_CONTAINMENT_OPTION}",
        )
        self.assertTrue(merged["JAVA_TOOL_OPTIONS"].endswith(JDTLS_METADATA_CONTAINMENT_OPTION))

        non_jdtls = _child_process_environment(self._server("typescript-language-server"), parent)
        self.assertEqual(non_jdtls, original)

    def test_fake_custom_jdtls_receives_child_only_environment_and_closes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            source = workspace / "Sample.java"
            source.write_text("class Sample {}\n", encoding="utf-8")
            fake_server = workspace / "fake_jdtls.py"
            _write_environment_lsp(fake_server)
            inherited = "-Xms64m -Djava.import.generatesMetadataFilesAtProjectRoot=true"
            server = self._server("jdtls", (sys.executable, str(fake_server)))

            with patch.dict("os.environ", {"JAVA_TOOL_OPTIONS": inherited}, clear=False):
                client = StdioLspClient(server, workspace)
                try:
                    self.assertTrue(client.alive())
                    symbols = client.document_symbols(source)
                    self.assertEqual(os.environ["JAVA_TOOL_OPTIONS"], inherited)
                finally:
                    client.close()
                self.assertFalse(client.alive())

        self.assertEqual(len(symbols), 1)
        self.assertEqual(
            symbols[0].name,
            f"{inherited} {JDTLS_METADATA_CONTAINMENT_OPTION}",
        )

    def test_client_cache_identity_includes_complete_server_config(self) -> None:
        workspace = Path("/tmp/lsp-client-cache").resolve()
        server = self._server("jdtls", ("custom-jdtls", "--stdio"))
        equivalent = self._server("jdtls", ("custom-jdtls", "--stdio"))
        different_environment = LspServerConfig(
            name="jdtls",
            command=("custom-jdtls", "--stdio"),
            file_types=(".java",),
            root_markers=("pom.xml",),
            language_id="java",
            process_environment=LspProcessEnvironment(append=(("CUSTOM_LSP_FLAG", "one"),)),
        )
        first = Mock()
        first.alive.return_value = True
        second = Mock()
        second.alive.return_value = True
        close_all_clients()

        with patch("local_agent.lsp.client.StdioLspClient", side_effect=(first, second)) as constructor:
            with patch.dict("os.environ", {"JAVA_TOOL_OPTIONS": "-Xms64m"}, clear=False):
                self.assertIs(get_client(server, workspace), first)
            with patch.dict("os.environ", {"JAVA_TOOL_OPTIONS": "-Xmx256m"}, clear=False):
                self.assertIs(get_client(equivalent, workspace), first)
                self.assertIs(get_client(different_environment, workspace), second)

        self.assertEqual(constructor.call_count, 2)
        first.close.assert_not_called()
        second.close.assert_not_called()
        close_all_clients()
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

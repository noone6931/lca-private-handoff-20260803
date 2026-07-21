from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.lsp.client import LspClientError
from local_agent.lsp.client import StdioLspClient
from local_agent.lsp.client import close_all_clients
from local_agent.lsp.client import get_client
from local_agent.lsp.config import LspServerConfig


def _write_lifecycle_lsp(path: Path) -> None:
    path.write_text(
        r'''
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


mode = sys.argv[1]
state = Path(sys.argv[2])
state.mkdir(parents=True, exist_ok=True)
instance = 1
if mode == "request_cache":
    count_path = state / "instances"
    try:
        instance = int(count_path.read_text(encoding="utf-8")) + 1
    except (FileNotFoundError, ValueError):
        instance = 1
    count_path.write_text(str(instance), encoding="utf-8")


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


def spawn_marker(name, delay):
    marker = state / name
    code = (
        "import time; from pathlib import Path; "
        f"time.sleep({delay!r}); Path({str(marker)!r}).write_text('called', encoding='utf-8')"
    )
    subprocess.Popen([sys.executable, "-c", code])


while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        if mode == "init_timeout":
            spawn_marker("initialize-descendant.marker", 0.6)
            time.sleep(0.6)
            (state / "initialize-direct.marker").write_text("called", encoding="utf-8")
            continue
        if mode in {"close_descendant", "leader_exit"}:
            spawn_marker(f"{mode}.marker", 0.8)
        send({"jsonrpc": "2.0", "id": request_id, "result": {"capabilities": {}}})
    elif method == "initialized" and mode == "leader_exit":
        raise SystemExit(0)
    elif method == "workspace/symbol":
        if mode == "request_cache" and instance == 1:
            time.sleep(60)
        if mode == "request_eof":
            raise SystemExit(0)
        if mode == "request_error":
            send({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": "expected test error"},
            })
            continue
        send({"jsonrpc": "2.0", "id": request_id, "result": []})
    elif request_id is not None:
        send({"jsonrpc": "2.0", "id": request_id, "result": None})
'''.lstrip(),
        encoding="utf-8",
    )


class LspProcessLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        close_all_clients()

    def tearDown(self) -> None:
        close_all_clients()

    def _server(self, script: Path, mode: str, state: Path) -> LspServerConfig:
        return LspServerConfig(
            name="fake-lsp",
            command=(sys.executable, str(script), mode, str(state)),
            file_types=(".py",),
            root_markers=("pyproject.toml",),
            language_id="python",
        )

    @unittest.skipUnless(os.name == "posix", "POSIX process-group lifecycle")
    def test_initialize_timeout_closes_direct_process_and_descendant_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            script = workspace / "lifecycle_lsp.py"
            state = workspace / "state"
            _write_lifecycle_lsp(script)
            server = self._server(script, "init_timeout", state)

            with patch("local_agent.lsp.client.DEFAULT_LSP_TIMEOUT_SECONDS", 0.05):
                with self.assertRaisesRegex(LspClientError, "timed out: initialize"):
                    StdioLspClient(server, workspace)
            time.sleep(0.8)

            self.assertFalse((state / "initialize-direct.marker").exists())
            self.assertFalse((state / "initialize-descendant.marker").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX process-group lifecycle")
    def test_close_terminates_descendant_even_after_direct_leader_exits(self) -> None:
        for mode in ("close_descendant", "leader_exit"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp).resolve()
                script = workspace / "lifecycle_lsp.py"
                state = workspace / "state"
                _write_lifecycle_lsp(script)
                client = StdioLspClient(self._server(script, mode, state), workspace)
                if mode == "leader_exit":
                    deadline = time.monotonic() + 1.0
                    while client._process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertIsNotNone(client._process.poll())

                client.close()
                client.close()
                time.sleep(1.0)

                self.assertFalse((state / f"{mode}.marker").exists())
                self.assertFalse(client.alive())

    def test_request_timeout_evicts_unhealthy_client_and_next_lookup_rebuilds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            script = workspace / "lifecycle_lsp.py"
            state = workspace / "state"
            _write_lifecycle_lsp(script)
            server = self._server(script, "request_cache", state)

            first = get_client(server, workspace)
            with self.assertRaisesRegex(LspClientError, "timed out: workspace/symbol"):
                first.workspace_symbols("anything", timeout=0.05)
            self.assertFalse(first.alive())

            second = get_client(server, workspace)
            self.assertIsNot(second, first)
            self.assertEqual(second.workspace_symbols("anything", timeout=1.0), [])
            second.close()

            self.assertEqual((state / "instances").read_text(encoding="utf-8"), "2")

    def test_transport_eof_is_unhealthy_but_json_rpc_error_keeps_server_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            script = workspace / "lifecycle_lsp.py"
            _write_lifecycle_lsp(script)

            eof_client = StdioLspClient(
                self._server(script, "request_eof", workspace / "eof-state"),
                workspace,
            )
            with self.assertRaisesRegex(LspClientError, "transport closed"):
                eof_client.workspace_symbols("anything", timeout=1.0)
            self.assertFalse(eof_client.alive())

            error_client = StdioLspClient(
                self._server(script, "request_error", workspace / "error-state"),
                workspace,
            )
            with self.assertRaisesRegex(LspClientError, "expected test error"):
                error_client.workspace_symbols("anything", timeout=1.0)
            self.assertTrue(error_client.alive())
            error_client.close()


if __name__ == "__main__":
    unittest.main()

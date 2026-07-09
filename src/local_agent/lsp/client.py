from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from local_agent.patch.anchored import display_workspace_path

from .config import LspServerConfig

DEFAULT_LSP_TIMEOUT_SECONDS = 8.0
DIAGNOSTICS_SETTLE_SECONDS = 1.5
PROJECT_LOAD_TIMEOUT_SECONDS = 15.0
PROJECT_LOAD_NO_PROGRESS_GRACE_SECONDS = 0.25


@dataclass(frozen=True)
class LspLocation:
    path: Path
    line: int
    column: int


@dataclass(frozen=True)
class LspSymbol:
    path: Path
    name: str
    kind: str
    line: int
    column: int
    container: str | None = None


@dataclass(frozen=True)
class LspDiagnostic:
    path: Path
    line: int
    column: int
    severity: str
    message: str


class LspClientError(RuntimeError):
    pass


class StdioLspClient:
    def __init__(self, server: LspServerConfig, workspace: Path):
        self.server = server
        self.workspace = workspace
        self._next_id = 1
        self._messages: queue.Queue[dict[str, Any] | BaseException | None] = queue.Queue()
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._open_uris: set[str] = set()
        self._progress_tokens: set[str] = set()
        self._saw_progress = False
        self._project_loaded = threading.Event()
        self._process = subprocess.Popen(
            list(server.command),
            cwd=workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=False,
        )
        self._reader = threading.Thread(target=self._reader_loop, name=f"lsp-reader-{server.name}", daemon=True)
        self._reader.start()
        self._initialize()

    def alive(self) -> bool:
        return self._process.poll() is None

    def document_symbols(self, path: Path, *, timeout: float = DEFAULT_LSP_TIMEOUT_SECONDS) -> list[LspSymbol]:
        self.ensure_open(path)
        result = self.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": path.as_uri()}},
            timeout=timeout,
        )
        return _parse_document_symbols(result, path)

    def workspace_symbols(self, query_text: str, *, timeout: float = DEFAULT_LSP_TIMEOUT_SECONDS) -> list[LspSymbol]:
        result = self.request("workspace/symbol", {"query": query_text}, timeout=timeout)
        return _parse_workspace_symbols(result)

    def execute_command(
        self,
        command: str,
        arguments: list[Any] | None = None,
        *,
        timeout: float = DEFAULT_LSP_TIMEOUT_SECONDS,
    ) -> Any:
        return self.request(
            "workspace/executeCommand",
            {"command": command, "arguments": arguments or []},
            timeout=timeout,
        )

    def definition(self, path: Path, symbol: str, *, timeout: float = DEFAULT_LSP_TIMEOUT_SECONDS) -> list[LspLocation]:
        position = _symbol_position(path, symbol)
        if position is None:
            return []
        self.ensure_open(path)
        result = self.request(
            "textDocument/definition",
            {"textDocument": {"uri": path.as_uri()}, "position": position},
            timeout=timeout,
        )
        return _parse_locations(result)

    def references(self, path: Path, symbol: str, *, timeout: float = DEFAULT_LSP_TIMEOUT_SECONDS) -> list[LspLocation]:
        position = _symbol_position(path, symbol)
        if position is None:
            return []
        self.ensure_open(path)
        result = self.request(
            "textDocument/references",
            {
                "textDocument": {"uri": path.as_uri()},
                "position": position,
                "context": {"includeDeclaration": True},
            },
            timeout=timeout,
        )
        return _parse_locations(result)

    def diagnostics(self, path: Path, *, timeout: float = DEFAULT_LSP_TIMEOUT_SECONDS) -> list[LspDiagnostic]:
        uri = path.as_uri()
        self.ensure_open(path)
        deadline = time.monotonic() + min(timeout, DIAGNOSTICS_SETTLE_SECONDS)
        while time.monotonic() < deadline:
            self._drain_one_message(deadline)
            if uri in self._diagnostics:
                break
        return _parse_diagnostics(uri, self._diagnostics.get(uri, []))

    def ensure_open(self, path: Path) -> None:
        uri = path.as_uri()
        if uri in self._open_uris:
            return
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8")
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": _language_id(self.server, path),
                    "version": 1,
                    "text": text,
                }
            },
        )
        self._open_uris.add(uri)

    def request(self, method: str, params: dict[str, Any], *, timeout: float = DEFAULT_LSP_TIMEOUT_SECONDS) -> Any:
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            message = self._read_message(deadline)
            if isinstance(message, BaseException):
                raise LspClientError(str(message)) from message
            if message is None:
                raise LspClientError(f"LSP request timed out: {method}")
            if self._handle_notification(message):
                continue
            if self._handle_server_request(message):
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise LspClientError(str(message["error"]))
            return message.get("result")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self) -> None:
        if self._process.poll() is not None:
            _close_pipe(self._process.stdin)
            _close_pipe(self._process.stdout)
            return
        try:
            if self._process.stdin is not None:
                self._process.stdin.close()
            if self._process.poll() is None:
                self._process.terminate()
                self._process.wait(timeout=1)
        except Exception:  # noqa: BLE001 - cleanup must be best-effort.
            if self._process.poll() is None:
                self._process.kill()
        finally:
            _close_pipe(self._process.stdin)
            _close_pipe(self._process.stdout)

    def _initialize(self) -> None:
        self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootPath": str(self.workspace),
                "rootUri": self.workspace.as_uri(),
                "workspaceFolders": _workspace_folders(self.workspace),
                "initializationOptions": {},
                "capabilities": {
                    "textDocument": {
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                        "definition": {"linkSupport": True},
                        "references": {},
                        "publishDiagnostics": {"relatedInformation": True},
                    },
                    "workspace": {
                        "configuration": True,
                        "workspaceFolders": True,
                        "symbol": {},
                    },
                    "window": {"workDoneProgress": True},
                },
            },
            timeout=DEFAULT_LSP_TIMEOUT_SECONDS,
        )
        self.notify("initialized", {})
        self._wait_for_project_load()

    def _send(self, payload: dict[str, Any]) -> None:
        if self._process.stdin is None:
            raise LspClientError("LSP process stdin is closed")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._process.stdin.write(header + body)
        self._process.stdin.flush()

    def _reader_loop(self) -> None:
        try:
            while True:
                message = _read_framed_message(self._process.stdout)
                if message is None:
                    self._messages.put(None)
                    return
                self._messages.put(message)
        except BaseException as exc:  # noqa: BLE001 - reader failures become request failures.
            self._messages.put(exc)

    def _read_message(self, deadline: float) -> dict[str, Any] | BaseException | None:
        timeout = max(0.0, deadline - time.monotonic())
        try:
            return self._messages.get(timeout=timeout)
        except queue.Empty:
            return None

    def _drain_one_message(self, deadline: float) -> None:
        message = self._read_message(deadline)
        if isinstance(message, dict):
            self._handle_notification(message) or self._handle_server_request(message)

    def _record_diagnostics(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        uri = params.get("uri")
        diagnostics = params.get("diagnostics")
        if isinstance(uri, str) and isinstance(diagnostics, list):
            self._diagnostics[uri] = diagnostics

    def _handle_notification(self, message: dict[str, Any]) -> bool:
        method = message.get("method")
        if method == "textDocument/publishDiagnostics":
            self._record_diagnostics(message.get("params"))
            return True
        if method == "$/progress":
            self._record_progress(message.get("params"))
            return True
        if method in {"window/logMessage", "window/showMessage", "telemetry/event", "language/status"}:
            return True
        return False

    def _record_progress(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        token = params.get("token")
        value = params.get("value")
        if token is None or not isinstance(value, dict):
            return
        kind = value.get("kind")
        key = str(token)
        if kind == "begin":
            self._saw_progress = True
            self._progress_tokens.add(key)
            return
        if kind == "end":
            self._progress_tokens.discard(key)
            if not self._progress_tokens:
                self._project_loaded.set()

    def _wait_for_project_load(self) -> None:
        deadline = time.monotonic() + PROJECT_LOAD_TIMEOUT_SECONDS
        no_progress_deadline = time.monotonic() + PROJECT_LOAD_NO_PROGRESS_GRACE_SECONDS
        while time.monotonic() < deadline:
            if self._project_loaded.is_set():
                return
            if not self._saw_progress and time.monotonic() >= no_progress_deadline:
                return
            self._drain_one_message(min(deadline, time.monotonic() + 0.1))

    def _handle_server_request(self, message: dict[str, Any]) -> bool:
        method = message.get("method")
        request_id = message.get("id")
        if not isinstance(method, str) or request_id is None:
            return False
        if method == "workspace/workspaceFolders":
            self._send_response(request_id, _workspace_folders(self.workspace))
            return True
        if method == "workspace/configuration":
            items = message.get("params", {}).get("items", [])
            result = _configuration_response(items)
            self._send_response(request_id, result)
            return True
        if method in {
            "client/registerCapability",
            "client/unregisterCapability",
            "window/workDoneProgress/create",
            "window/showMessageRequest",
        }:
            self._send_response(request_id, None)
            return True
        if method == "window/showDocument":
            self._send_response(request_id, {"success": False})
            return True
        if method == "workspace/applyEdit":
            self._send_response(
                request_id,
                {"applied": False, "failureReason": "LCA external LSP client is read-only."},
            )
            return True
        self._send_response(
            request_id,
            None,
            error={"code": -32601, "message": f"Unsupported server request: {method}"},
        )
        return True

    def _send_response(self, request_id: Any, result: Any, *, error: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is None:
            payload["result"] = result
        else:
            payload["error"] = error
        self._send(payload)


_CLIENTS: dict[tuple[str, str, tuple[str, ...]], StdioLspClient] = {}


def _workspace_folders(workspace: Path) -> list[dict[str, str]]:
    return [{"uri": workspace.as_uri(), "name": workspace.name or "workspace"}]


def _configuration_response(items: Any) -> list[Any]:
    if not isinstance(items, list):
        return []
    return [_configuration_item_response(item) for item in items]


def _configuration_item_response(item: Any) -> Any:
    section = item.get("section") if isinstance(item, dict) else None
    if section == "java":
        return {
            "configuration": {"updateBuildConfiguration": "automatic"},
            "import": {"maven": {"enabled": True}, "gradle": {"enabled": True}},
        }
    return {
        "java.configuration.updateBuildConfiguration": "automatic",
        "java.import.maven.enabled": True,
        "java.import.gradle.enabled": True,
    }.get(section, {})


def _close_pipe(pipe: Any) -> None:
    if pipe is None:
        return
    try:
        pipe.close()
    except Exception:  # noqa: BLE001 - process cleanup is best-effort.
        return


def get_client(server: LspServerConfig, workspace: Path) -> StdioLspClient:
    key = (str(workspace), server.name, server.command)
    client = _CLIENTS.get(key)
    if client is not None and client.alive():
        return client
    if client is not None:
        client.close()
    client = StdioLspClient(server, workspace)
    _CLIENTS[key] = client
    return client


def close_all_clients() -> None:
    for client in list(_CLIENTS.values()):
        client.close()
    _CLIENTS.clear()


atexit.register(close_all_clients)


def render_provider_header(server: LspServerConfig) -> str:
    return f"[lsp provider] external {server.name}"


def render_symbols(
    symbols: list[LspSymbol],
    workspace: Path,
    allowed_roots: tuple[Path, ...],
    *,
    max_results: int,
) -> list[str]:
    lines: list[str] = []
    for symbol in symbols[:max_results]:
        rel = display_workspace_path(workspace, symbol.path, allowed_roots)
        scoped = f"{symbol.container}.{symbol.name}" if symbol.container else symbol.name
        lines.append(f"{rel}:{symbol.line}:{symbol.column}: {symbol.kind} {scoped}")
    if len(symbols) > max_results:
        lines.append(f"... truncated after {max_results} symbols")
    return lines


def render_locations(
    locations: list[LspLocation],
    workspace: Path,
    allowed_roots: tuple[Path, ...],
    *,
    max_results: int,
) -> list[str]:
    lines: list[str] = []
    for location in locations[:max_results]:
        rel = display_workspace_path(workspace, location.path, allowed_roots)
        lines.append(f"{rel}:{location.line}:{location.column}: {_line_snippet(location.path, location.line)}")
    if len(locations) > max_results:
        lines.append(f"... truncated after {max_results} locations")
    return lines


def render_diagnostics(
    diagnostics: list[LspDiagnostic],
    workspace: Path,
    allowed_roots: tuple[Path, ...],
    *,
    max_results: int,
) -> list[str]:
    if not diagnostics:
        return ["OK"]
    lines: list[str] = []
    for diagnostic in diagnostics[:max_results]:
        rel = display_workspace_path(workspace, diagnostic.path, allowed_roots)
        lines.append(f"{rel}:{diagnostic.line}:{diagnostic.column}: {diagnostic.severity}: {diagnostic.message}")
    if len(diagnostics) > max_results:
        lines.append(f"... truncated after {max_results} diagnostics")
    return lines


def _read_framed_message(stdout: Any) -> dict[str, Any] | None:
    if stdout is None:
        return None
    headers: dict[str, str] = {}
    while True:
        line = stdout.readline()
        if line == b"":
            return None
        if line in {b"\r\n", b"\n"}:
            break
        decoded = line.decode("ascii", errors="replace").strip()
        if ":" in decoded:
            key, value = decoded.split(":", 1)
            headers[key.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = stdout.read(length)
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def _parse_document_symbols(value: Any, fallback_path: Path, container: str | None = None) -> list[LspSymbol]:
    if not isinstance(value, list):
        return []
    symbols: list[LspSymbol] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        kind = _symbol_kind(item.get("kind"))
        selection = item.get("selectionRange") or item.get("range") or {}
        start = selection.get("start") if isinstance(selection, dict) else None
        if isinstance(start, dict) and name:
            symbols.append(
                LspSymbol(
                    path=_path_from_symbol_item(item) or fallback_path,
                    name=name,
                    kind=kind,
                    line=int(start.get("line", 0)) + 1,
                    column=int(start.get("character", 0)) + 1,
                    container=container,
                )
            )
        child_container = f"{container}.{name}" if container and name else name or container
        children = item.get("children")
        if isinstance(children, list):
            symbols.extend(_parse_document_symbols(children, fallback_path, child_container))
    return symbols


def _parse_workspace_symbols(value: Any) -> list[LspSymbol]:
    if not isinstance(value, list):
        return []
    symbols: list[LspSymbol] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        location = item.get("location") or {}
        uri = location.get("uri") if isinstance(location, dict) else None
        range_value = location.get("range") if isinstance(location, dict) else None
        start = range_value.get("start") if isinstance(range_value, dict) else None
        path = _path_from_uri(uri) if isinstance(uri, str) else None
        if path is None or not isinstance(start, dict) or not name:
            continue
        container = item.get("containerName")
        symbols.append(
            LspSymbol(
                path=path,
                name=name,
                kind=_symbol_kind(item.get("kind")),
                line=int(start.get("line", 0)) + 1,
                column=int(start.get("character", 0)) + 1,
                container=str(container) if container else None,
            )
        )
    return symbols


def _parse_locations(value: Any) -> list[LspLocation]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    locations: list[LspLocation] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        uri = item.get("targetUri") or item.get("uri")
        range_value = item.get("targetSelectionRange") or item.get("targetRange") or item.get("range") or {}
        start = range_value.get("start") if isinstance(range_value, dict) else None
        path = _path_from_uri(uri) if isinstance(uri, str) else None
        if path is None or not isinstance(start, dict):
            continue
        locations.append(
            LspLocation(
                path=path,
                line=int(start.get("line", 0)) + 1,
                column=int(start.get("character", 0)) + 1,
            )
        )
    return locations


def _parse_diagnostics(uri: str, diagnostics: list[dict[str, Any]]) -> list[LspDiagnostic]:
    path = _path_from_uri(uri)
    if path is None:
        return []
    parsed: list[LspDiagnostic] = []
    for item in diagnostics:
        range_value = item.get("range") if isinstance(item, dict) else None
        start = range_value.get("start") if isinstance(range_value, dict) else None
        if not isinstance(start, dict):
            continue
        parsed.append(
            LspDiagnostic(
                path=path,
                line=int(start.get("line", 0)) + 1,
                column=int(start.get("character", 0)) + 1,
                severity=_severity(item.get("severity")),
                message=str(item.get("message") or ""),
            )
        )
    return parsed


def _path_from_symbol_item(item: dict[str, Any]) -> Path | None:
    location = item.get("location")
    if not isinstance(location, dict):
        return None
    uri = location.get("uri")
    return _path_from_uri(uri) if isinstance(uri, str) else None


def _path_from_uri(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    return Path(unquote(parsed.path)).resolve()


def _symbol_position(path: Path, symbol: str) -> dict[str, int] | None:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError:
        lines = path.read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(lines):
        column = line.find(symbol)
        if column != -1:
            return {"line": line_number, "character": column}
    return None


def _language_id(server: LspServerConfig, path: Path) -> str:
    if server.name == "typescript-language-server":
        if path.suffix in {".js", ".jsx"}:
            return "javascriptreact" if path.suffix == ".jsx" else "javascript"
        return "typescriptreact" if path.suffix == ".tsx" else "typescript"
    return server.language_id


def _line_snippet(path: Path, line_number: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    if 1 <= line_number <= len(lines):
        return lines[line_number - 1].strip()
    return ""


def _symbol_kind(value: Any) -> str:
    names = {
        1: "file",
        2: "module",
        3: "namespace",
        4: "package",
        5: "class",
        6: "method",
        7: "property",
        8: "field",
        9: "constructor",
        10: "enum",
        11: "interface",
        12: "function",
        13: "variable",
        14: "constant",
        23: "struct",
    }
    try:
        return names.get(int(value), "symbol")
    except (TypeError, ValueError):
        return "symbol"


def _severity(value: Any) -> str:
    return {
        1: "Error",
        2: "Warning",
        3: "Info",
        4: "Hint",
    }.get(value, "Diagnostic")

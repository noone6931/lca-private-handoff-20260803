from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_agent.lsp import config as lsp_config
from local_agent.lsp.client import LspClientError, get_client
from local_agent.lsp.workspace_edit import MAX_WORKSPACE_FILE_BYTES
from local_agent.lsp.workspace_edit import MAX_WORKSPACE_PREVIEW_BYTES
from local_agent.lsp.workspace_edit import WorkspaceEditError
from local_agent.lsp.workspace_edit import build_workspace_edit_preview
from local_agent.lsp.workspace_edit import exact_symbol_position
from local_agent.patch.anchored import PatchError, display_workspace_path, resolve_workspace_path

from .base import Tool, ToolContext, ToolResult

MAX_CODE_ACTIONS = 20
MAX_CODE_ACTION_TITLE_CHARS = 256
MAX_CODE_ACTION_KIND_CHARS = 128


@dataclass(frozen=True)
class _CodeActionItem:
    raw: dict[str, Any]
    title: str
    kind: str | None
    is_command_item: bool
    is_preferred: bool
    disabled: bool
    edit_present: bool
    command_present: bool
    resolve_needed: bool

    def safe_metadata(self, index: int) -> dict[str, Any]:
        return {
            "index": index,
            "title": self.title,
            "kind": self.kind,
            "isPreferred": self.is_preferred,
            "disabled": self.disabled,
            "edit_present": self.edit_present,
            "command_present": self.command_present,
            "resolve_needed": self.resolve_needed,
        }


def lsp_code_action_tools() -> list[Tool]:
    return [
        Tool(
            name="lsp_code_action_preview",
            description=(
                "List or preview semantic code actions from one external LSP server without executing commands "
                "or writing files. Omit action_index to list bounded metadata; pass a listed 0-based index to "
                "re-request and preview its text-only WorkspaceEdit."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "line": {"type": "integer", "minimum": 1},
                    "symbol": {"type": "string", "minLength": 1, "maxLength": 256},
                    "occurrence": {"type": "integer", "minimum": 1},
                    "server": {"type": "string", "minLength": 1, "maxLength": 128},
                    "kind": {"type": "string", "minLength": 1, "maxLength": MAX_CODE_ACTION_KIND_CHARS},
                    "action_index": {"type": "integer", "minimum": 0, "maximum": MAX_CODE_ACTIONS - 1},
                },
                "required": ["path", "line", "symbol"],
                "additionalProperties": False,
            },
            tier="read",
            handler=_lsp_code_action_preview,
        )
    ]


def _lsp_code_action_preview(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    path_value = arguments["path"]
    symbol = arguments["symbol"]
    kind = arguments.get("kind")
    invalid = _safe_text_error(symbol, "symbol", 256)
    if kind is not None:
        invalid = invalid or _safe_text_error(kind, "kind", MAX_CODE_ACTION_KIND_CHARS)
    if invalid:
        return ToolResult(invalid, is_error=True)
    try:
        path = resolve_workspace_path(context.workspace, path_value, context.allowed_dirs)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
    if not path.exists() or not path.is_file():
        return ToolResult(f"LSP code action target is not an existing regular file: {path_value}", is_error=True)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return ToolResult(f"Unable to read LSP code action target: {type(exc).__name__}", is_error=True)
    if len(raw) > MAX_WORKSPACE_FILE_BYTES:
        return ToolResult(
            f"LSP code action target exceeds the {MAX_WORKSPACE_FILE_BYTES}-byte preview limit.",
            is_error=True,
        )
    try:
        text = raw.decode("utf-8-sig")
        position, match_count = exact_symbol_position(
            text,
            line=arguments["line"],
            symbol=symbol,
            occurrence=arguments.get("occurrence"),
        )
    except (UnicodeDecodeError, WorkspaceEditError) as exc:
        return ToolResult(f"Cannot locate code action target: {exc}", is_error=True)

    if not lsp_config.external_lsp_enabled():
        return ToolResult(
            "lsp_code_action_preview requires an external LSP server; lightweight fallback cannot provide code actions.",
            is_error=True,
        )
    authorization_root = _authorization_root(path, context.workspace, context.allowed_dirs)
    servers = lsp_config.servers_for_path(authorization_root, path)
    selected, selection_error = _select_server(servers, arguments.get("server"))
    if selection_error:
        return ToolResult(selection_error, is_error=True)
    assert selected is not None
    project_root = lsp_config.root_for_path(authorization_root, path, selected)
    if project_root is None:
        return ToolResult("The selected LSP server has no project root for this file.", is_error=True)

    try:
        client = get_client(selected, project_root)
        response = client.code_actions(
            path,
            {"line": position.line, "character": position.character},
            kind=kind,
        )
    except (LspClientError, OSError) as exc:
        return ToolResult(f"LSP code action request failed safely: {type(exc).__name__}.", is_error=True)
    actions, parse_error = _parse_action_response(response)
    if parse_error:
        return ToolResult(parse_error, is_error=True)
    if not actions:
        return ToolResult(
            "No code actions are available at the exact target position.",
            useless=True,
            metadata=_base_metadata(selected.name, project_root, context, mode="list"),
        )

    action_index = arguments.get("action_index")
    if action_index is None:
        return _render_action_list(
            actions,
            total_count=len(response),
            server_name=selected.name,
            project_root=project_root,
            context=context,
            match_count=match_count,
        )
    if action_index >= len(actions):
        return ToolResult(
            f"action_index {action_index} is outside the listed range 0-{len(actions) - 1}.",
            is_error=True,
        )
    action = actions[action_index]
    action_error = _selected_action_error(action)
    if action_error:
        return ToolResult(action_error, is_error=True)

    selected_action = action
    if not selected_action.edit_present:
        if not selected_action.resolve_needed:
            return ToolResult("Selected CodeAction has no text edit and is not safely resolvable.", is_error=True)
        try:
            resolved_raw = client.resolve_code_action(selected_action.raw)
        except (LspClientError, OSError) as exc:
            return ToolResult(f"LSP code action resolve failed safely: {type(exc).__name__}.", is_error=True)
        resolved, resolved_error = _parse_action_item(resolved_raw)
        if resolved_error or resolved is None or resolved.is_command_item:
            return ToolResult("Resolved code action is not a valid CodeAction.", is_error=True)
        if resolved.title != selected_action.title:
            return ToolResult("Resolved CodeAction changed title identity; preview refused.", is_error=True)
        selected_action = resolved
        action_error = _selected_action_error(selected_action)
        if action_error:
            return ToolResult(action_error, is_error=True)
        if not selected_action.edit_present:
            return ToolResult("Resolved CodeAction contains no text-only WorkspaceEdit.", is_error=True)

    try:
        preview = build_workspace_edit_preview(
            selected_action.raw.get("edit"),
            workspace=context.workspace,
            allowed_roots=context.allowed_dirs,
            project_root=project_root,
        )
    except WorkspaceEditError as exc:
        return ToolResult(f"LSP code action preview failed: {exc}", is_error=True)
    return _render_action_preview(
        action=selected_action,
        action_index=action_index,
        preview=preview,
        server_name=selected.name,
        project_root=project_root,
        context=context,
    )


def _parse_action_response(value: Any) -> tuple[list[_CodeActionItem], str | None]:
    if value is None:
        return [], None
    if not isinstance(value, list):
        return [], "LSP codeAction response must be an array or null."
    actions: list[_CodeActionItem] = []
    for raw in value[:MAX_CODE_ACTIONS]:
        item, error = _parse_action_item(raw)
        if error or item is None:
            return [], error or "LSP codeAction response contains an invalid item."
        actions.append(item)
    return actions, None


def _parse_action_item(value: Any) -> tuple[_CodeActionItem | None, str | None]:
    if not isinstance(value, dict):
        return None, "LSP codeAction item must be an object."
    title = value.get("title")
    title_error = _safe_text_error(title, "CodeAction title", MAX_CODE_ACTION_TITLE_CHARS)
    if title_error:
        return None, title_error
    if isinstance(value.get("command"), str):
        if set(value) - {"title", "command", "arguments"}:
            return None, "LSP Command item contains unsupported fields."
        command = value["command"]
        if not command or ("arguments" in value and not isinstance(value["arguments"], list)):
            return None, "LSP Command item has an invalid shape."
        return _CodeActionItem(dict(value), title, None, True, False, False, False, True, False), None

    allowed = {"title", "kind", "diagnostics", "isPreferred", "disabled", "edit", "command", "data"}
    if set(value) - allowed:
        return None, "CodeAction contains unsupported fields."
    kind = value.get("kind")
    if kind is not None:
        kind_error = _safe_text_error(kind, "CodeAction kind", MAX_CODE_ACTION_KIND_CHARS)
        if kind_error:
            return None, kind_error
    diagnostics = value.get("diagnostics")
    if diagnostics is not None and not isinstance(diagnostics, list):
        return None, "CodeAction diagnostics must be an array when present."
    preferred = value.get("isPreferred", False)
    if preferred is None:
        preferred = False
    if not isinstance(preferred, bool):
        return None, "CodeAction isPreferred must be boolean when present."
    disabled = value.get("disabled")
    if disabled is not None:
        if not isinstance(disabled, dict) or set(disabled) != {"reason"}:
            return None, "CodeAction disabled must contain only a reason."
        if _safe_text_error(disabled.get("reason"), "CodeAction disabled reason", MAX_CODE_ACTION_TITLE_CHARS):
            return None, "CodeAction disabled reason is invalid."
    command = value.get("command")
    if command is not None and not _valid_command(command):
        return None, "CodeAction command has an invalid shape."
    edit = value.get("edit")
    if edit is not None and not isinstance(edit, dict):
        return None, "CodeAction edit must be a WorkspaceEdit object when present."
    edit_present = edit is not None
    command_present = command is not None
    return (
        _CodeActionItem(
            raw=dict(value),
            title=title,
            kind=kind,
            is_command_item=False,
            is_preferred=preferred,
            disabled=disabled is not None,
            edit_present=edit_present,
            command_present=command_present,
            resolve_needed=not edit_present and not command_present and value.get("data") is not None,
        ),
        None,
    )


def _valid_command(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) - {"title", "command", "arguments"}:
        return False
    if not isinstance(value.get("title"), str) or not isinstance(value.get("command"), str):
        return False
    if not value["title"] or not value["command"]:
        return False
    return "arguments" not in value or isinstance(value["arguments"], list)


def _selected_action_error(action: _CodeActionItem) -> str | None:
    if action.is_command_item:
        return "Command items cannot be previewed or executed by lsp_code_action_preview."
    if action.disabled:
        return "Disabled CodeActions cannot be previewed."
    if action.command_present:
        return "CodeActions containing commands are refused even when they also contain edits."
    return None


def _render_action_list(
    actions: list[_CodeActionItem],
    *,
    total_count: int,
    server_name: str,
    project_root: Path,
    context: ToolContext,
    match_count: int,
) -> ToolResult:
    safe_actions = [action.safe_metadata(index) for index, action in enumerate(actions)]
    payload = {
        "mode": "list",
        "server": server_name,
        "project_root": display_workspace_path(context.workspace, project_root, context.allowed_dirs),
        "target_occurrences_on_line": match_count,
        "shown": len(safe_actions),
        "total": total_count,
        "truncated": total_count > len(safe_actions),
        "actions": safe_actions,
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if len(content.encode("utf-8")) > MAX_WORKSPACE_PREVIEW_BYTES:
        return ToolResult("Code action metadata exceeds the bounded output limit.", is_error=True)
    return ToolResult(
        content,
        metadata={
            **_base_metadata(server_name, project_root, context, mode="list"),
            "action_count": len(safe_actions),
            "truncated": total_count > len(safe_actions),
        },
    )


def _render_action_preview(
    *,
    action: _CodeActionItem,
    action_index: int,
    preview: Any,
    server_name: str,
    project_root: Path,
    context: ToolContext,
) -> ToolResult:
    display_paths = tuple(
        display_workspace_path(context.workspace, candidate, context.allowed_dirs)
        for candidate in preview.paths
    )
    display_root = display_workspace_path(context.workspace, project_root, context.allowed_dirs)
    lines = [
        "Semantic code action preview (in-memory/read-only; LCA did not apply a WorkspaceEdit or execute a command)",
        f"Server: {server_name}",
        f"Project root: {display_root}",
        f"Action: {action_index}: {action.title}",
        f"Kind: {action.kind or '(none)'}",
        f"Files: {len(display_paths)}; edits: {preview.edit_count}",
        "Candidate files:",
        *(f"- {candidate}" for candidate in display_paths),
        "",
        preview.unified_diff,
        "Read the affected files and use apply_patch separately to make an approved change.",
    ]
    content = "\n".join(lines)
    if len(content.encode("utf-8")) > MAX_WORKSPACE_PREVIEW_BYTES:
        return ToolResult("Code action preview exceeds the bounded complete output limit.", is_error=True)
    return ToolResult(
        content,
        metadata={
            **_base_metadata(server_name, project_root, context, mode="preview"),
            "action_index": action_index,
            "action_title": action.title,
            "action_kind": action.kind,
            "files": list(display_paths),
            "file_count": len(display_paths),
            "edit_count": preview.edit_count,
        },
    )


def _base_metadata(
    server_name: str,
    project_root: Path,
    context: ToolContext,
    *,
    mode: str,
) -> dict[str, Any]:
    return {
        "preview": True,
        "read_only": True,
        "evidence_eligible": False,
        "mode": mode,
        "server": server_name,
        "project_root": display_workspace_path(context.workspace, project_root, context.allowed_dirs),
    }


def _safe_text_error(value: Any, label: str, limit: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return f"{label} must contain 1-{limit} characters."
    if any(char in {"\r", "\n"} or unicodedata.category(char) == "Cc" for char in value):
        return f"{label} cannot contain newline or control characters."
    return None


def _authorization_root(path: Path, workspace: Path, allowed_roots: tuple[Path, ...]) -> Path:
    matches: list[Path] = []
    for root in (workspace, *allowed_roots):
        resolved = root.expanduser().resolve()
        try:
            path.relative_to(resolved)
            matches.append(resolved)
        except ValueError:
            continue
    if not matches:
        raise WorkspaceEditError("LSP code action target is outside the authorized roots.")
    return max(matches, key=lambda candidate: len(candidate.parts))


def _select_server(servers: list[Any], requested: str | None) -> tuple[Any | None, str | None]:
    names = [server.name for server in servers]
    if requested is not None:
        for server in servers:
            if server.name == requested:
                return server, None
        available = ", ".join(names) or "(none)"
        return None, f"Unknown or unavailable LSP server '{requested}'. Available servers: {available}."
    if not servers:
        return None, "No external LSP server matches the target file and project root."
    if len(servers) > 1:
        return None, f"Multiple LSP servers match this file; specify server: {', '.join(names)}."
    return servers[0], None

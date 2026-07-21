from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

from local_agent.lsp import config as lsp_config
from local_agent.lsp.client import LspClientError, get_client
from local_agent.lsp.workspace_edit import MAX_WORKSPACE_FILE_BYTES
from local_agent.lsp.workspace_edit import MAX_WORKSPACE_PREVIEW_BYTES
from local_agent.lsp.workspace_edit import WorkspaceEditError
from local_agent.lsp.workspace_edit import build_workspace_edit_preview
from local_agent.lsp.workspace_edit import exact_symbol_position
from local_agent.lsp.workspace_edit_store import WorkspaceEditPlanScope
from local_agent.lsp.workspace_edit_store import WorkspaceEditPlanStoreError
from local_agent.lsp.workspace_edit_store import default_workspace_edit_plan_store
from local_agent.patch.anchored import PatchError, display_workspace_path, resolve_workspace_path

from .base import Tool, ToolContext, ToolResult

MAX_RENAME_TEXT_CHARS = 256


def lsp_rename_tools() -> list[Tool]:
    return [
        Tool(
            name="lsp_rename_preview",
            description=(
                "Preview a semantic symbol rename from one external LSP server without writing files. "
                "Locate the symbol exactly by 1-indexed line and occurrence, then read the affected files "
                "and use apply_patch separately for any approved change."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "line": {"type": "integer", "minimum": 1},
                    "symbol": {"type": "string", "minLength": 1, "maxLength": MAX_RENAME_TEXT_CHARS},
                    "new_name": {"type": "string", "minLength": 1, "maxLength": MAX_RENAME_TEXT_CHARS},
                    "occurrence": {"type": "integer", "minimum": 1},
                    "server": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                "required": ["path", "line", "symbol", "new_name"],
                "additionalProperties": False,
            },
            tier="read",
            handler=_lsp_rename_preview,
        )
    ]


def _lsp_rename_preview(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    path_value = arguments["path"]
    symbol = arguments["symbol"]
    new_name = arguments["new_name"]
    invalid = _rename_text_error(symbol, "symbol") or _rename_text_error(new_name, "new_name")
    if invalid:
        return ToolResult(invalid, is_error=True)
    try:
        path = resolve_workspace_path(context.workspace, path_value, context.allowed_dirs)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
    if not path.exists() or not path.is_file():
        return ToolResult(f"LSP rename target is not an existing regular file: {path_value}", is_error=True)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return ToolResult(f"Unable to read LSP rename target: {type(exc).__name__}", is_error=True)
    if len(raw) > MAX_WORKSPACE_FILE_BYTES:
        return ToolResult(
            f"LSP rename target exceeds the {MAX_WORKSPACE_FILE_BYTES}-byte preview limit.",
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
        return ToolResult(f"Cannot locate semantic rename target: {exc}", is_error=True)

    if not lsp_config.external_lsp_enabled():
        return ToolResult(
            "lsp_rename_preview requires an external LSP server; lightweight fallback cannot rename symbols.",
            is_error=True,
        )
    authorization_root = _authorization_root(path, context.workspace, context.allowed_dirs)
    servers = lsp_config.servers_for_path(authorization_root, path)
    requested_server = arguments.get("server")
    selected, selection_error = _select_server(servers, requested_server)
    if selection_error:
        return ToolResult(selection_error, is_error=True)
    assert selected is not None
    project_root = lsp_config.root_for_path(authorization_root, path, selected)
    if project_root is None:
        return ToolResult("The selected LSP server has no project root for this file.", is_error=True)
    try:
        client = get_client(selected, project_root)
        workspace_edit = client.rename(
            path,
            {"line": position.line, "character": position.character},
            new_name,
        )
        preview = build_workspace_edit_preview(
            workspace_edit,
            workspace=context.workspace,
            allowed_roots=context.allowed_dirs,
            project_root=project_root,
        )
        stored = default_workspace_edit_plan_store().register(
            preview,
            source="rename",
            scope=WorkspaceEditPlanScope.create(
                session_id=context.session_id,
                run_id=context.run_id,
                workspace=context.workspace,
                allowed_roots=context.allowed_dirs,
            ),
        )
    except WorkspaceEditError as exc:
        return ToolResult(f"LSP rename preview failed: {exc}", is_error=True)
    except WorkspaceEditPlanStoreError as exc:
        return ToolResult(f"LSP rename preview plan could not be retained: {exc}", is_error=True)
    except (LspClientError, OSError) as exc:
        return ToolResult(f"LSP rename request failed safely: {type(exc).__name__}.", is_error=True)

    display_paths = tuple(
        display_workspace_path(context.workspace, candidate, context.allowed_dirs)
        for candidate in preview.paths
    )
    display_root = display_workspace_path(context.workspace, project_root, context.allowed_dirs)
    lines = [
        "Semantic rename preview (in-memory/read-only; LCA did not apply a WorkspaceEdit or execute a command)",
        f"Server: {selected.name}",
        f"Project root: {display_root}",
        f"Target occurrences on line: {match_count}",
        f"Files: {len(display_paths)}; edits: {preview.edit_count}",
        f"Apply plan: {stored.plan_id}",
        f"Plan digest: {preview.digest}",
        "Candidate files:",
        *(f"- {candidate}" for candidate in display_paths),
        "",
        preview.unified_diff,
        "Read the affected files, then use apply_workspace_edit with this plan_id for the approved exact change.",
    ]
    content = "\n".join(lines)
    if len(content.encode("utf-8")) > MAX_WORKSPACE_PREVIEW_BYTES:
        default_workspace_edit_plan_store().consume(
            stored.plan_id,
            scope=WorkspaceEditPlanScope.create(
                session_id=context.session_id,
                run_id=context.run_id,
                workspace=context.workspace,
                allowed_roots=context.allowed_dirs,
            ),
        )
        return ToolResult(
            f"LSP rename preview exceeds the {MAX_WORKSPACE_PREVIEW_BYTES}-byte complete output limit.",
            is_error=True,
        )
    return ToolResult(
        content,
        metadata={
            "preview": True,
            "read_only": True,
            "evidence_eligible": False,
            "server": selected.name,
            "project_root": display_root,
            "files": list(display_paths),
            "file_count": len(display_paths),
            "edit_count": preview.edit_count,
            "plan_id": stored.plan_id,
            "plan_digest": preview.digest,
            "plan_source": stored.source,
        },
    )


def _rename_text_error(value: str, label: str) -> str | None:
    if not value or len(value) > MAX_RENAME_TEXT_CHARS:
        return f"{label} must contain 1-{MAX_RENAME_TEXT_CHARS} characters."
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
        raise WorkspaceEditError("LSP rename target is outside the authorized roots.")
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

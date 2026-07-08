from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from local_agent.patch.anchored import PatchError
from local_agent.patch.anchored import display_workspace_path
from local_agent.patch.anchored import resolve_workspace_path

from .base import Tool, ToolContext, ToolResult

SKIPPED_DIRS = {".git", ".local-agent", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__"}


def search_tools() -> list[Tool]:
    return [
        Tool(
            name="list_files",
            description=(
                "List files under the workspace or an explicitly allowed directory, "
                "skipping local agent and build/cache directories."
            ),
            tier="read",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                "additionalProperties": False,
            },
            handler=list_files,
        ),
        Tool(
            name="search_code",
            description=(
                "Search workspace or explicitly allowed directory files with ripgrep. "
                "Returns workspace-relative paths for the main workspace and absolute paths for allowed directories."
            ),
            tier="read",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            handler=search_code,
        )
    ]


def list_files(args: dict[str, Any], context: ToolContext) -> ToolResult:
    max_results = min(max(int(args.get("max_results") or 200), 1), 1000)
    raw_path = args.get("path") or "."
    try:
        root = resolve_workspace_path(context.workspace, raw_path, context.allowed_dirs)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
    if not root.exists():
        return ToolResult(_with_workspace_roots_hint(f"Path not found: {raw_path}", context), is_error=True)
    if root.is_file():
        return ToolResult(display_workspace_path(context.workspace, root, context.allowed_dirs))

    results: list[str] = []
    for path in _walk_files(root):
        results.append(display_workspace_path(context.workspace, path, context.allowed_dirs))
        if len(results) >= max_results:
            results.append(f"... truncated after {max_results} files")
            break
    content = "\n".join(results) if results else "(no files)"
    if _is_primary_root_listing(raw_path) and context.allowed_dirs:
        content = f"{_workspace_roots_hint(context)}\n\nFiles under primary workspace:\n{content}"
    return ToolResult(content)


def search_code(args: dict[str, Any], context: ToolContext) -> ToolResult:
    max_results = min(max(int(args.get("max_results") or 80), 1), 200)
    raw_path = args.get("path") or "."
    try:
        path = resolve_workspace_path(context.workspace, raw_path, context.allowed_dirs)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
    if not path.exists():
        return ToolResult(f"Path not found: {raw_path}", is_error=True)
    search_path = _rg_search_path(path, context)
    command = [
        "rg",
        "--line-number",
        "--column",
        "--color",
        "never",
        "--",
        args["pattern"],
        search_path,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=context.workspace,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except FileNotFoundError:
        return ToolResult("ripgrep is not installed. Please install rg in the VM image.", is_error=True)
    if completed.returncode not in {0, 1}:
        output = completed.stderr or completed.stdout or f"rg failed with exit code {completed.returncode}."
        return ToolResult(output[:20000], is_error=True)
    if not completed.stdout:
        content = "No matches."
        if _is_primary_root_listing(raw_path) and context.allowed_dirs:
            content = _with_workspace_roots_hint(content, context)
        return ToolResult(content, useless=True)
    output = _normalize_search_output_paths(completed.stdout, context.workspace)
    return ToolResult(_truncate_search_output(output, max_results)[:20000])


def _is_primary_root_listing(raw_path: Any) -> bool:
    return str(raw_path or ".").strip() in {"", "."}


def _workspace_roots_hint(context: ToolContext) -> str:
    lines = [
        "Workspace roots:",
        f"- Primary workspace (--cwd): {context.workspace}",
    ]
    if context.allowed_dirs:
        lines.append("- Additional allowed directories; use these exact absolute paths for external docs/specs/code:")
        lines.extend(f"  - {path}" for path in context.allowed_dirs)
    return "\n".join(lines)


def _with_workspace_roots_hint(content: str, context: ToolContext) -> str:
    if not context.allowed_dirs:
        return content
    return f"{content}\n\n{_workspace_roots_hint(context)}"


def _walk_files(root: Path):
    for child in sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if child.is_dir():
            if child.name in SKIPPED_DIRS:
                continue
            yield from _walk_files(child)
        elif child.is_file():
            yield child


def _truncate_search_output(output: str, max_results: int) -> str:
    if output == "No matches.":
        return output
    lines = output.splitlines()
    if len(lines) <= max_results:
        return output
    kept = lines[:max_results]
    kept.append(f"... truncated after {max_results} matches")
    return "\n".join(kept)


def _normalize_search_output_paths(output: str, workspace: Path) -> str:
    workspace_prefix = str(workspace) + "/"
    normalized: list[str] = []
    for line in output.splitlines():
        if line.startswith(workspace_prefix):
            line = line[len(workspace_prefix) :]
        if line.startswith("./"):
            line = line[2:]
        normalized.append(line)
    return "\n".join(normalized)


def _rg_search_path(path: Path, context: ToolContext) -> str:
    if path == context.workspace:
        return "."
    try:
        return str(path.relative_to(context.workspace))
    except ValueError:
        return str(path)

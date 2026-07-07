from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from local_agent.patch.anchored import PatchError, resolve_workspace_path

from .base import Tool, ToolContext, ToolResult

SKIPPED_DIRS = {".git", ".local-agent", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__"}


def search_tools() -> list[Tool]:
    return [
        Tool(
            name="list_files",
            description="List files under a workspace path, skipping local agent and build/cache directories.",
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
            description="Search workspace files with ripgrep. Returns workspace-relative paths.",
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
        root = resolve_workspace_path(context.workspace, raw_path)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
    if not root.exists():
        return ToolResult(f"Path not found: {raw_path}", is_error=True)
    if root.is_file():
        return ToolResult(str(root.relative_to(context.workspace)))

    results: list[str] = []
    for path in _walk_files(root):
        results.append(str(path.relative_to(context.workspace)))
        if len(results) >= max_results:
            results.append(f"... truncated after {max_results} files")
            break
    return ToolResult("\n".join(results) if results else "(no files)")


def search_code(args: dict[str, Any], context: ToolContext) -> ToolResult:
    max_results = min(max(int(args.get("max_results") or 80), 1), 200)
    raw_path = args.get("path") or "."
    try:
        path = resolve_workspace_path(context.workspace, raw_path)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
    if not path.exists():
        return ToolResult(f"Path not found: {raw_path}", is_error=True)
    search_path = "." if path == context.workspace else str(path.relative_to(context.workspace))
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
    output = _normalize_search_output_paths(completed.stdout, context.workspace) if completed.stdout else "No matches."
    return ToolResult(_truncate_search_output(output, max_results)[:20000])


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

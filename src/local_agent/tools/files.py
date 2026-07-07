from __future__ import annotations

from pathlib import Path
from typing import Any

from local_agent.patch.anchored import apply_anchored_patch, hash_text, resolve_workspace_path

from .base import Tool, ToolContext, ToolResult

MAX_READ_BYTES = 256 * 1024
MAX_READ_LINES = 400


def file_tools() -> list[Tool]:
    return [
        Tool(
            name="read_file",
            description="Read a text file inside the workspace. Returns a hash tag and numbered lines.",
            tier="read",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=read_file,
        ),
        Tool(
            name="apply_patch",
            description=(
                "Apply a safe anchored patch to a previously read file. "
                "Use mode=replace to replace the anchored lines, insert_before to insert before them, "
                "or insert_after to insert after them. old_text must match the anchored lines. "
                "Set dry_run=true to preview the diff without changing the file."
            ),
            tier="write",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "tag": {"type": "string"},
                    "mode": {"type": "string", "enum": ["replace", "insert_before", "insert_after"]},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["path", "tag", "start_line", "end_line", "old_text", "new_text"],
                "additionalProperties": False,
            },
            handler=patch_file,
        ),
        Tool(
            name="write_file",
            description="Create or fully overwrite a text file inside the workspace.",
            tier="write",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            handler=write_file,
        ),
    ]


def read_file(args: dict[str, Any], context: ToolContext) -> ToolResult:
    path = resolve_workspace_path(context.workspace, args["path"])
    if not path.exists() or not path.is_file():
        return ToolResult(f"File not found: {args['path']}", is_error=True)
    file_size = path.stat().st_size
    if file_size > MAX_READ_BYTES:
        return ToolResult(
            f"File too large to read safely: {args['path']} is {file_size} bytes, limit is {MAX_READ_BYTES} bytes.",
            is_error=True,
        )
    raw = path.read_bytes()
    if b"\x00" in raw:
        return ToolResult(f"Refusing to read likely binary file: {args['path']}", is_error=True)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return ToolResult(f"File is not valid UTF-8 text: {args['path']}: {exc}", is_error=True)

    start_line = int(args.get("start_line") or 1)
    if start_line < 1:
        return ToolResult("start_line must be >= 1.", is_error=True)
    end_line = args.get("end_line")
    max_line = int(end_line) if end_line else start_line + MAX_READ_LINES - 1
    if max_line < start_line:
        return ToolResult("end_line must be >= start_line.", is_error=True)
    if max_line - start_line + 1 > MAX_READ_LINES:
        max_line = start_line + MAX_READ_LINES - 1

    lines = text.splitlines()
    rel = path.relative_to(context.workspace)
    rendered = [f"[{rel}#{hash_text(text)}]"]
    for index, line in enumerate(lines, start=1):
        if start_line <= index <= max_line:
            rendered.append(f"{index}:{line}")
    if max_line < len(lines):
        rendered.append(f"... truncated after line {max_line}; use start_line/end_line to continue.")
    rendered = "\n".join(rendered)
    return ToolResult(rendered)


def patch_file(args: dict[str, Any], context: ToolContext) -> ToolResult:
    result = apply_anchored_patch(
        workspace=context.workspace,
        path=args["path"],
        tag=args["tag"],
        start_line=int(args["start_line"]),
        end_line=int(args["end_line"]),
        old_text=args["old_text"],
        new_text=args["new_text"],
        mode=args.get("mode") or "replace",
        dry_run=bool(args.get("dry_run")),
    )
    if args.get("dry_run"):
        return ToolResult(
            f"Patch preview only. File not changed. New tag after apply would be: {result.new_tag}\n\n{result.diff}"
        )
    return ToolResult(f"Applied patch. New tag: {result.new_tag}\n\n{result.diff}")


def write_file(args: dict[str, Any], context: ToolContext) -> ToolResult:
    path = resolve_workspace_path(context.workspace, args["path"])
    if path.exists():
        return ToolResult(
            f"Refusing to overwrite existing file with write_file: {args['path']}. Use apply_patch instead.",
            is_error=True,
        )
    Path(path.parent).mkdir(parents=True, exist_ok=True)
    path.write_text(args["content"], encoding="utf-8")
    return ToolResult(f"Wrote {args['path']}")

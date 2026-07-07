from __future__ import annotations

import difflib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_agent.patch.anchored import apply_anchored_patch
from local_agent.patch.anchored import display_workspace_path
from local_agent.patch.anchored import hash_text
from local_agent.patch.anchored import PatchError
from local_agent.patch.anchored import resolve_workspace_path

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
            name="rollback_patch",
            description=(
                "Roll back a patch previously applied by apply_patch in this session. "
                "If patch_id is omitted, rolls back the latest unapplied rollback candidate. "
                "The target file must still match the recorded after tag."
            ),
            tier="write",
            input_schema={
                "type": "object",
                "properties": {
                    "patch_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
            handler=rollback_patch,
        ),
        Tool(
            name="write_file",
            description="Create a new text file inside the workspace. Refuses to overwrite existing files; use apply_patch for existing files.",
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
    try:
        path = resolve_workspace_path(context.workspace, args["path"], context.allowed_dirs)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
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
    rel = display_workspace_path(context.workspace, path, context.allowed_dirs)
    rendered = [f"[{rel}#{hash_text(text)}]"]
    for index, line in enumerate(lines, start=1):
        if start_line <= index <= max_line:
            rendered.append(f"{index}:{line}")
    if max_line < len(lines):
        rendered.append(
            f"... more lines exist after line {max_line}; continue with start_line/end_line only if needed for the task."
        )
    rendered = "\n".join(rendered)
    return ToolResult(rendered)


def patch_file(args: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        path = resolve_workspace_path(context.workspace, args["path"], context.allowed_dirs)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
    if not path.exists():
        return ToolResult(f"Target file does not exist: {args['path']}", is_error=True)
    before_text = path.read_bytes().decode("utf-8")
    before_tag = hash_text(before_text)
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
        allowed_roots=context.allowed_dirs,
    )
    if args.get("dry_run"):
        return ToolResult(
            f"Patch preview only. File not changed. New tag after apply would be: {result.new_tag}\n\n{result.diff}"
        )
    patch_id = _record_patch(
        context=context,
        path=args["path"],
        before_text=before_text,
        before_tag=before_tag,
        after_tag=result.new_tag,
        diff=result.diff,
    )
    return ToolResult(f"Applied patch. Patch id: {patch_id}. New tag: {result.new_tag}\n\n{result.diff}")


def rollback_patch(args: dict[str, Any], context: ToolContext) -> ToolResult:
    records = _load_patch_records(context)
    patch_id = args.get("patch_id")
    record = _find_rollback_record(records, patch_id)
    if record is None:
        if patch_id:
            return ToolResult(f"Patch record not found or already rolled back: {patch_id}", is_error=True)
        return ToolResult("No unapplied patch record found for this session.", is_error=True)

    try:
        path = resolve_workspace_path(context.workspace, str(record["path"]), context.allowed_dirs)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
    if not path.exists():
        return ToolResult(f"Target file does not exist: {record['path']}", is_error=True)
    current_text = path.read_bytes().decode("utf-8")
    current_tag = hash_text(current_text)
    after_tag = str(record["after_tag"])
    if current_tag != after_tag:
        return ToolResult(
            f"Refusing rollback for {record['id']}: expected current tag {after_tag}, got {current_tag}. "
            "The file changed after the patch; inspect git_diff and roll back manually.",
            is_error=True,
        )

    before_text = str(record["before_text"])
    path.write_bytes(before_text.encode("utf-8"))
    _record_rollback(context, str(record["id"]))
    diff = "".join(
        difflib.unified_diff(
            current_text.splitlines(keepends=True),
            before_text.splitlines(keepends=True),
            fromfile=f"a/{record['path']}",
            tofile=f"b/{record['path']}",
        )
    )
    return ToolResult(f"Rolled back patch {record['id']}. Restored tag: {record['before_tag']}\n\n{diff}")


def write_file(args: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        path = resolve_workspace_path(context.workspace, args["path"], context.allowed_dirs)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
    if path.exists():
        return ToolResult(
            f"Refusing to overwrite existing file with write_file: {args['path']}. Use apply_patch instead.",
            is_error=True,
        )
    Path(path.parent).mkdir(parents=True, exist_ok=True)
    path.write_text(args["content"], encoding="utf-8")
    return ToolResult(f"Wrote {display_workspace_path(context.workspace, path, context.allowed_dirs)}")


def _record_patch(
    *,
    context: ToolContext,
    path: str,
    before_text: str,
    before_tag: str,
    after_tag: str,
    diff: str,
) -> str:
    patch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    record = {
        "event": "apply",
        "id": patch_id,
        "time": datetime.now(timezone.utc).isoformat(),
        "path": path,
        "before_tag": before_tag,
        "after_tag": after_tag,
        "before_text": before_text,
        "diff": diff,
    }
    _append_patch_record(context, record)
    return patch_id


def _record_rollback(context: ToolContext, patch_id: str) -> None:
    _append_patch_record(
        context,
        {
            "event": "rollback",
            "patch_id": patch_id,
            "time": datetime.now(timezone.utc).isoformat(),
        },
    )


def _append_patch_record(context: ToolContext, record: dict[str, Any]) -> None:
    path = _patch_log_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_patch_records(context: ToolContext) -> list[dict[str, Any]]:
    path = _patch_log_path(context)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def _find_rollback_record(records: list[dict[str, Any]], patch_id: str | None) -> dict[str, Any] | None:
    rolled_back = {
        str(record.get("patch_id"))
        for record in records
        if record.get("event") == "rollback" and record.get("patch_id")
    }
    applied = [
        record
        for record in records
        if record.get("event") == "apply"
        and isinstance(record.get("id"), str)
        and isinstance(record.get("path"), str)
        and isinstance(record.get("before_text"), str)
        and isinstance(record.get("before_tag"), str)
        and isinstance(record.get("after_tag"), str)
        and record["id"] not in rolled_back
    ]
    if patch_id:
        for record in reversed(applied):
            if record["id"] == patch_id:
                return record
        return None
    return applied[-1] if applied else None


def _patch_log_path(context: ToolContext) -> Path:
    session_id = context.session_id or "default"
    return context.workspace / ".local-agent" / "patches" / f"{session_id}.jsonl"

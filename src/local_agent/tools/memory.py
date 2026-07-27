from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..memory.storage import PROJECT_MEMORY_NAMES
from ..memory.storage import ProjectMemoryStore
from ..memory.storage import ProjectMemoryStoreError
from .base import Tool, ToolContext, ToolResult

MEMORY_NAMES = PROJECT_MEMORY_NAMES
MAX_MEMORY_NOTE_CHARS = 4000
MAX_LESSON_CHARS = 2000


def memory_tools() -> list[Tool]:
    return [
        Tool(
            name="memory_read",
            description=(
                "Read project Markdown memory by safe basename. Built-in names include project, decisions, "
                "conventions, and learned; project-specific names such as enterprise-service-boundary are also allowed."
            ),
            tier="read",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Memory file basename without .md; use letters, numbers, underscores, or hyphens.",
                    }
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=memory_read,
        ),
        Tool(
            name="memory_write",
            description="Append a concise memory note to project Markdown memory.",
            tier="write",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": list(MEMORY_NAMES)},
                    "note": {"type": "string"},
                },
                "required": ["name", "note"],
                "additionalProperties": False,
            },
            handler=memory_write,
        ),
        Tool(
            name="learn",
            description=(
                "Persist a reusable project lesson to Markdown memory. "
                "Use only when the user asks you to remember something or when a durable project convention is clear."
            ),
            tier="write",
            input_schema={
                "type": "object",
                "properties": {
                    "lesson": {"type": "string"},
                    "topic": {"type": "string"},
                },
                "required": ["lesson"],
                "additionalProperties": False,
            },
            handler=learn,
        ),
    ]


def memory_read(args: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        document = ProjectMemoryStore(
            context.workspace,
            expected_workspace_identity=context.workspace_identity,
        ).read(args["name"])
    except ProjectMemoryStoreError as exc:
        if exc.kind == "invalid_memory_name":
            return ToolResult(
                "Invalid memory name. Use only letters, numbers, underscores, or hyphens.",
                is_error=True,
            )
        return _memory_store_error("read", exc)
    if document is None:
        return ToolResult(f"No memory yet: {args['name']}")
    return ToolResult(document.text[:20000])


def memory_write(args: dict[str, Any], context: ToolContext) -> ToolResult:
    if args["name"] not in MEMORY_NAMES:
        return ToolResult("Invalid memory name. Use only letters, numbers, underscores, or hyphens.", is_error=True)
    note = _clean_note(args["note"], max_chars=MAX_MEMORY_NOTE_CHARS)
    if not note:
        return ToolResult("Memory note is empty after cleanup.", is_error=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        result = ProjectMemoryStore(
            context.workspace,
            expected_workspace_identity=context.workspace_identity,
        ).append(
            args["name"],
            f"\n## {stamp}\n\n{note}\n",
        )
    except ProjectMemoryStoreError as exc:
        return _memory_store_error("write", exc)
    return ToolResult(f"Appended memory: {result.lexical_path.relative_to(context.workspace)}")


def learn(args: dict[str, Any], context: ToolContext) -> ToolResult:
    lesson = _clean_note(args["lesson"], max_chars=MAX_LESSON_CHARS)
    if not lesson:
        return ToolResult("Lesson is empty after cleanup.", is_error=True)
    topic = _clean_note(args.get("topic") or "general", max_chars=80).replace("\n", " ")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        result = ProjectMemoryStore(
            context.workspace,
            expected_workspace_identity=context.workspace_identity,
        ).append(
            "learned",
            f"\n## {stamp} - {topic}\n\n{lesson}\n",
        )
    except ProjectMemoryStoreError as exc:
        return _memory_store_error("learn", exc)
    return ToolResult(f"Learned lesson in {result.lexical_path.relative_to(context.workspace)}")


def _memory_store_error(operation: str, error: ProjectMemoryStoreError) -> ToolResult:
    return ToolResult(
        f"Project memory {operation} failed safely ({error.kind}).",
        is_error=True,
        metadata={
            "execution_status": "failed",
            "denial_kind": "project_memory_containment",
            "reason": error.kind,
            "workspace_changed": error.workspace_changed,
        },
    )


def _clean_note(note: str, *, max_chars: int) -> str:
    cleaned = note.replace("\x00", "").strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 14].rstrip() + "\n...<truncated>"
    return cleaned

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .base import Tool, ToolContext, ToolResult

MEMORY_NAMES = ("project", "decisions", "conventions", "learned")
MAX_MEMORY_NOTE_CHARS = 4000
MAX_LESSON_CHARS = 2000


def memory_tools() -> list[Tool]:
    return [
        Tool(
            name="memory_read",
            description="Read project Markdown memory.",
            tier="read",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": list(MEMORY_NAMES)}
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
    path = _memory_path(context, args["name"])
    if not path.exists():
        return ToolResult(f"No memory yet: {args['name']}")
    return ToolResult(path.read_text(encoding="utf-8")[:20000])


def memory_write(args: dict[str, Any], context: ToolContext) -> ToolResult:
    path = _memory_path(context, args["name"])
    path.parent.mkdir(parents=True, exist_ok=True)
    note = _clean_note(args["note"], max_chars=MAX_MEMORY_NOTE_CHARS)
    if not note:
        return ToolResult("Memory note is empty after cleanup.", is_error=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## {stamp}\n\n{note}\n")
    return ToolResult(f"Appended memory: {path.relative_to(context.workspace)}")


def learn(args: dict[str, Any], context: ToolContext) -> ToolResult:
    lesson = _clean_note(args["lesson"], max_chars=MAX_LESSON_CHARS)
    if not lesson:
        return ToolResult("Lesson is empty after cleanup.", is_error=True)
    topic = _clean_note(args.get("topic") or "general", max_chars=80).replace("\n", " ")
    path = _memory_path(context, "learned")
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## {stamp} - {topic}\n\n{lesson}\n")
    return ToolResult(f"Learned lesson in {path.relative_to(context.workspace)}")


def _memory_path(context: ToolContext, name: str):
    return context.workspace / ".local-agent" / "memory" / f"{name}.md"


def _clean_note(note: str, *, max_chars: int) -> str:
    cleaned = note.replace("\x00", "").strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 14].rstrip() + "\n...<truncated>"
    return cleaned

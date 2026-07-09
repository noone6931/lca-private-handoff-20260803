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
    path = _memory_path(context, args["name"], allow_custom=True)
    if path is None:
        return ToolResult("Invalid memory name. Use only letters, numbers, underscores, or hyphens.", is_error=True)
    if not path.exists():
        return ToolResult(f"No memory yet: {args['name']}")
    return ToolResult(path.read_text(encoding="utf-8")[:20000])


def memory_write(args: dict[str, Any], context: ToolContext) -> ToolResult:
    path = _memory_path(context, args["name"], allow_custom=False)
    if path is None:
        return ToolResult(f"Invalid memory name: {args['name']}", is_error=True)
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
    path = _memory_path(context, "learned", allow_custom=False)
    if path is None:
        return ToolResult("Invalid learned memory path.", is_error=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## {stamp} - {topic}\n\n{lesson}\n")
    return ToolResult(f"Learned lesson in {path.relative_to(context.workspace)}")


def _memory_path(context: ToolContext, name: str, *, allow_custom: bool):
    cleaned = str(name).strip()
    if allow_custom:
        if not _is_safe_memory_name(cleaned):
            return None
    elif cleaned not in MEMORY_NAMES:
        return None
    return context.workspace / ".local-agent" / "memory" / f"{cleaned}.md"


def _is_safe_memory_name(name: str) -> bool:
    if not name or name in {".", ".."}:
        return False
    return all(char.isalnum() or char in {"_", "-"} for char in name)


def _clean_note(note: str, *, max_chars: int) -> str:
    cleaned = note.replace("\x00", "").strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 14].rstrip() + "\n...<truncated>"
    return cleaned

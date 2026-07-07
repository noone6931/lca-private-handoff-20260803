from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .base import Tool, ToolContext, ToolResult


def memory_tools() -> list[Tool]:
    return [
        Tool(
            name="memory_read",
            description="Read project Markdown memory.",
            tier="read",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": ["project", "decisions", "conventions"]}
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
                    "name": {"type": "string", "enum": ["project", "decisions", "conventions"]},
                    "note": {"type": "string"},
                },
                "required": ["name", "note"],
                "additionalProperties": False,
            },
            handler=memory_write,
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
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## {stamp}\n\n{args['note'].strip()}\n")
    return ToolResult(f"Appended memory: {path.relative_to(context.workspace)}")


def _memory_path(context: ToolContext, name: str):
    return context.workspace / ".local-agent" / "memory" / f"{name}.md"

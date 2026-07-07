from __future__ import annotations

import sys
from typing import Any

from .base import Tool, ToolContext, ToolResult


def interaction_tools() -> list[Tool]:
    return [
        Tool(
            name="ask_user",
            description="Ask the user a short clarification question and wait for their answer.",
            tier="interaction",
            input_schema={
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                },
                "required": ["question"],
                "additionalProperties": False,
            },
            handler=ask_user,
        )
    ]


def ask_user(args: dict[str, Any], context: ToolContext) -> ToolResult:
    question = args["question"].strip()
    if not question:
        return ToolResult("Question must not be empty.", is_error=True)
    if not sys.stdin.isatty():
        return ToolResult(
            "Cannot ask the user because stdin is not interactive. "
            "Make the requirement explicit in the prompt or run in an interactive terminal.",
            is_error=True,
        )
    try:
        answer = input(f"\n[agent question] {question}\n> ").strip()
    except EOFError:
        return ToolResult("Cannot ask the user because stdin closed before an answer.", is_error=True)
    if not answer:
        return ToolResult("User gave an empty answer.", is_error=True)
    return ToolResult(answer)

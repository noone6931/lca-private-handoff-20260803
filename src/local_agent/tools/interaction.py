from __future__ import annotations

import select
import sys
import time
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
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
                    "default_answer": {"type": "string"},
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
    default_answer = _default_answer(args)
    if not sys.stdin.isatty():
        if default_answer is not None:
            return ToolResult(default_answer)
        return ToolResult(
            "Cannot ask the user because stdin is not interactive. "
            "Make the requirement explicit in the prompt or run in an interactive terminal.",
            is_error=True,
        )
    timeout = _effective_timeout(args, context)
    try:
        if timeout is None:
            answer = input(f"\n[agent question] {question}\n> ").strip()
        else:
            answer = _read_timed_answer(f"\n[agent question] {question}\n> ", timeout)
    except EOFError:
        if default_answer is not None:
            return ToolResult(default_answer)
        return ToolResult("Cannot ask the user because stdin closed before an answer.", is_error=True)
    if answer is None:
        if default_answer is not None:
            return ToolResult(default_answer)
        return ToolResult(f"No answer received within {timeout} seconds.", is_error=True)
    if not answer:
        if default_answer is not None:
            return ToolResult(default_answer)
        return ToolResult("User gave an empty answer.", is_error=True)
    return ToolResult(answer)


def _default_answer(args: dict[str, Any]) -> str | None:
    if "default_answer" not in args:
        return None
    default = args["default_answer"].strip()
    if not default:
        raise ValueError("default_answer must not be empty.")
    return default


def _effective_timeout(args: dict[str, Any], context: ToolContext) -> int | None:
    requested = args.get("timeout_seconds")
    if requested is not None:
        return int(requested)
    if context.deadline_monotonic is None:
        return None
    remaining = context.deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return 0
    return max(1, int(remaining))


def _read_timed_answer(prompt: str, timeout: int) -> str | None:
    print(prompt, end="", flush=True)
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except (OSError, ValueError):
        return None
    if not ready:
        return None
    return sys.stdin.readline().strip()

from __future__ import annotations

import subprocess
from typing import Any

from .base import Tool, ToolContext, ToolResult


def git_tools() -> list[Tool]:
    return [
        Tool(
            name="git_status",
            description="Show local git status.",
            tier="read",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=git_status,
        ),
        Tool(
            name="git_diff",
            description="Show local git diff.",
            tier="read",
            input_schema={
                "type": "object",
                "properties": {"staged": {"type": "boolean"}},
                "additionalProperties": False,
            },
            handler=git_diff,
        ),
    ]


def git_status(args: dict[str, Any], context: ToolContext) -> ToolResult:
    return _git(context, ["status", "--short"])


def git_diff(args: dict[str, Any], context: ToolContext) -> ToolResult:
    command = ["diff", "--staged"] if args.get("staged") else ["diff"]
    result = _git(context, command)
    if not result.is_error and result.content == "(empty)":
        status = _git(context, ["status", "--short"])
        if not status.is_error and status.content != "(empty)":
            return ToolResult(
                "(empty diff)\n\n"
                "[git status --short]\n"
                f"{status.content}\n"
                "Note: git diff does not show untracked files. Create an initial commit or stage files to see diffs."
            )
    return result


def _git(context: ToolContext, args: list[str]) -> ToolResult:
    completed = subprocess.run(
        ["git", *args],
        cwd=context.workspace,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    output = completed.stdout or completed.stderr or "(empty)"
    return ToolResult(output[:30000], is_error=completed.returncode != 0)

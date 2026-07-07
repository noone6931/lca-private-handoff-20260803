from __future__ import annotations

import re
import subprocess
from typing import Any

from .base import Tool, ToolContext, ToolResult

DANGEROUS_COMMAND_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\brm\s+-[^\n;]*r[^\n;]*f\s+/",
        r"\bsudo\s+rm\b",
        r"\bmkfs(\.\w+)?\b",
        r"\bdd\b.*\bof=/dev/",
        r"\bshutdown\b",
        r"\breboot\b",
        r":\(\)\s*\{\s*:\|:",
        r"\bcurl\b.*\|\s*(?:sh|bash|zsh)\b",
        r"\bwget\b.*\|\s*(?:sh|bash|zsh)\b",
        r"\bchmod\s+-R\s+777\s+/",
        r"\bchown\s+-R\b.*\s+/",
    ]
]


def shell_tools() -> list[Tool]:
    return [
        Tool(
            name="run_tests",
            description="Run the project's test command in the workspace. Defaults to Python unittest.",
            tier="exec",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 600},
                },
                "additionalProperties": False,
            },
            handler=run_tests,
        ),
        Tool(
            name="shell",
            description="Run a local shell command in the workspace.",
            tier="exec",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 600},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            handler=run_shell,
        )
    ]


def run_tests(args: dict[str, Any], context: ToolContext) -> ToolResult:
    command = args.get("command") or "PYTHONPATH=src python3 -m unittest discover -s tests"
    return _run_command(command, args, context, default_timeout=120)


def run_shell(args: dict[str, Any], context: ToolContext) -> ToolResult:
    return _run_command(args["command"], args, context, default_timeout=60)


def _run_command(command: str, args: dict[str, Any], context: ToolContext, *, default_timeout: int) -> ToolResult:
    dangerous_reason = _dangerous_command_reason(command)
    if dangerous_reason:
        return ToolResult(dangerous_reason, is_error=True)
    timeout = min(max(int(args.get("timeout") or default_timeout), 1), 600)
    completed = subprocess.run(
        command,
        cwd=context.workspace,
        shell=True,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = completed.stdout
    if completed.stderr:
        output += "\n[stderr]\n" + completed.stderr
    output += f"\n[exit_code] {completed.returncode}"
    return ToolResult(output[:30000], is_error=completed.returncode != 0)


def _dangerous_command_reason(command: str) -> str | None:
    for pattern in DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(command):
            return f"Refusing dangerous command matching pattern: {pattern.pattern}"
    return None

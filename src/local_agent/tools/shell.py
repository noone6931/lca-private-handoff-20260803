from __future__ import annotations

import re
import subprocess
import time
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
            description=(
                "Run a complete executable test command in the workspace. Defaults to Python unittest. "
                "When command is provided, include its runner, for example "
                "`PYTHONPATH=src python3 -m unittest tests.test_config`; a bare module name such as "
                "`tests.test_config` is not an executable command."
            ),
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
    if _looks_like_test_module_name(command):
        return ToolResult(
            "run_tests command looks like a Python test module, not an executable command. "
            f"Use `python3 -m unittest {command}` (and add PYTHONPATH=... when the project needs it).",
            is_error=True,
        )
    return _run_command(command, args, context, default_timeout=120)


def run_shell(args: dict[str, Any], context: ToolContext) -> ToolResult:
    return _run_command(args["command"], args, context, default_timeout=60)


def _run_command(command: str, args: dict[str, Any], context: ToolContext, *, default_timeout: int) -> ToolResult:
    dangerous_reason = _dangerous_command_reason(command)
    if dangerous_reason:
        return ToolResult(dangerous_reason, is_error=True)
    timeout = min(max(int(args.get("timeout") or default_timeout), 1), 600)
    timeout = _clamp_timeout_to_budget(timeout, context)
    if timeout < 1:
        return ToolResult("Command was not run because budget_seconds is exhausted.", is_error=True)
    try:
        completed = subprocess.run(
            command,
            cwd=context.workspace,
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = f"Command timed out after {timeout} seconds."
        if exc.stdout:
            output += f"\n[stdout]\n{exc.stdout}"
        if exc.stderr:
            output += f"\n[stderr]\n{exc.stderr}"
        return ToolResult(output[:30000], is_error=True)
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


def _clamp_timeout_to_budget(timeout: int, context: ToolContext) -> int:
    if context.deadline_monotonic is None:
        return timeout
    remaining = context.deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return 0
    return min(timeout, max(1, int(remaining)))


def _looks_like_test_module_name(command: object) -> bool:
    """Reject a common ambiguous model argument without executing or guessing it."""

    return isinstance(command, str) and bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", command.strip()))

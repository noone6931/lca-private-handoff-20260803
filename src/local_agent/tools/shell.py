from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from local_agent.patch.anchored import PatchError, resolve_workspace_path

from .base import Tool, ToolContext, ToolResult
from .execution_metadata import execution_error_result
from .execution_metadata import legacy_test_metadata as _test_metadata
from .execution_metadata import with_execution_metadata
from .process_environment import build_child_process_environment
from .process_output import process_tool_result as _process_tool_result
from .process_runtime import run_process as _run_process
from .shell_execution import run_shell_process
from .test_runner_policy import resolve_test_runner, test_environment_denial_reason
from .test_runner_policy import test_runner_denial_reason as _test_runner_denial_reason

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

_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=(.*)", re.DOTALL)
_ENV_REFERENCE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
def shell_tools() -> list[Tool]:
    return [
        Tool(
            name="run_tests",
            description=(
                "Exec-tier: run repository test/build code without shell interpretation; this is not a sandbox. "
                "Defaults to Python unittest. Do not use it for cat, grep, cd, or shell inspection: use read_file "
                "for file content, search_code for text search, and cwd for a module directory. For example, "
                "`PYTHONPATH=src python3 -m unittest tests.test_config`. Pipes, redirects, shell operators, "
                "loader/injection environments, runner paths, and arbitrary executables are rejected. "
                "Allowed test/build code may still have side effects."
            ),
            tier="exec",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
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
    trusted_path = os.environ.get("PATH", "")
    try:
        working_directory = _resolve_test_cwd(args.get("cwd"), context)
    except (PatchError, OSError) as exc:
        return _test_not_run(command, context.workspace.resolve(), str(exc))
    if _looks_like_test_module_name(command):
        return _test_not_run(
            command,
            working_directory,
            "run_tests command looks like a Python test module, not an executable command. "
            f"Use `python3 -m unittest {command}` (and add PYTHONPATH=... when the project needs it).",
        )
    parsed, denial = _parse_test_command(command)
    if denial:
        return _test_not_run(command, working_directory, denial)
    assert parsed is not None
    environment, argv = parsed
    policy_denial = test_environment_denial_reason(environment)
    if policy_denial:
        return _test_not_run(command, working_directory, policy_denial, argv=argv, environment=environment)
    resolved_runner, policy_denial = resolve_test_runner(
        argv,
        working_directory=working_directory,
        trusted_path=trusted_path,
    )
    if policy_denial:
        return _test_not_run(command, working_directory, policy_denial, argv=argv, environment=environment)
    assert resolved_runner is not None
    return _run_test_process(
        command=command,
        argv=resolved_runner.argv,
        runner_executable=resolved_runner.executable,
        environment=environment,
        working_directory=working_directory,
        args=args,
        context=context,
    )


def run_shell(args: dict[str, Any], context: ToolContext) -> ToolResult:
    command = args["command"]
    timeout = _clamp_timeout_to_budget(min(max(int(args.get("timeout") or 60), 1), 600), context)
    return run_shell_process(
        command, timeout=timeout, context=context, dangerous_reason=_dangerous_command_reason(command), process_runner=_run_process
    )


def _resolve_test_cwd(raw_cwd: object, context: ToolContext) -> Path:
    if raw_cwd is None or (isinstance(raw_cwd, str) and not raw_cwd.strip()):
        path = context.workspace.resolve()
    elif isinstance(raw_cwd, str):
        path = resolve_workspace_path(context.workspace, raw_cwd, context.allowed_dirs)
    else:
        raise PatchError("run_tests cwd must be a string path.")
    if not path.is_dir():
        raise PatchError(f"run_tests working directory does not exist or is not a directory: {path}")
    return path


def _parse_test_command(command: object) -> tuple[tuple[dict[str, str], tuple[str, ...]] | None, str | None]:
    if not isinstance(command, str) or not command.strip():
        return None, "run_tests command must be a non-empty string."
    control = _shell_control_reason(command)
    if control:
        return None, control
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        return None, f"run_tests command could not be parsed: {exc}"
    if not tokens:
        return None, "run_tests command must contain a test runner."

    environment = dict(build_child_process_environment().values)
    explicit_environment: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        match = _ENV_ASSIGNMENT.fullmatch(tokens[index])
        if match is None:
            break
        key, raw_value = tokens[index].split("=", 1)
        value = _expand_environment_references(raw_value, {**environment, **explicit_environment})
        explicit_environment[key] = value
        index += 1
    argv = tuple(tokens[index:])
    if not argv:
        return None, "run_tests command contains environment assignments but no test runner."
    return (explicit_environment, argv), None


def _shell_control_reason(command: str) -> str | None:
    if "\n" in command or "\r" in command:
        return "run_tests rejects multiline commands. Use one structured test runner invocation."
    if "$(" in command or "`" in command:
        return "run_tests rejects command substitution and backticks."
    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char in "|&;<>":
            return "run_tests rejects shell operators, pipes, and redirections. Use read_file/search_code for inspection, the cwd argument for a module, and a bare test runner command."
    return None


def _expand_environment_references(value: str, environment: dict[str, str]) -> str:
    return _ENV_REFERENCE.sub(lambda match: environment.get(match.group(1) or match.group(2) or "", ""), value)


def _run_test_process(
    *,
    command: str,
    argv: tuple[str, ...],
    runner_executable: str,
    environment: dict[str, str],
    working_directory: Path,
    args: dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    timeout = min(max(int(args.get("timeout") or 120), 1), 600)
    timeout = _clamp_timeout_to_budget(timeout, context)
    if timeout < 1:
        return execution_error_result(
            "Test command was not run because budget_seconds is exhausted.",
            metadata=_test_metadata(command, working_directory, argv, environment, exit_code=None, status="failed", runner_executable=runner_executable),
            command=command, argv=argv, shell=False, working_directory=working_directory, outcome="not_run",
        )
    try:
        completed = _run_process(
            list(argv),
            cwd=working_directory,
            env=build_child_process_environment(overrides=environment).values,
            shell=False,
            timeout=timeout,
            cancel_event=context.cancel_event,
        )
    except subprocess.TimeoutExpired as exc:
        projected = _process_tool_result(
            exc,
            terminal_line=f"[timeout] Test command timed out after {timeout} seconds.",
            is_error=True,
            metadata=_test_metadata(command, working_directory, argv, environment, exit_code=None, status="failed", runner_executable=runner_executable),
            label_stdout=True,
        )
        return with_execution_metadata(
            projected, command=command, argv=argv, shell=False, working_directory=working_directory,
            outcome="timed_out", exit_code=None,
        )
    except OSError as exc:
        return execution_error_result(
            f"Test runner could not be started: {exc}",
            metadata=_test_metadata(command, working_directory, argv, environment, exit_code=None, status="failed", runner_executable=runner_executable),
            command=command, argv=argv, shell=False, working_directory=working_directory, outcome="spawn_failed",
        )
    status = "succeeded" if completed.returncode == 0 else "failed"
    projected = _process_tool_result(
        completed,
        terminal_line=f"[exit_code] {completed.returncode}",
        is_error=completed.returncode != 0,
        metadata=_test_metadata(
            command,
            working_directory,
            argv,
            environment,
            exit_code=completed.returncode,
            status=status,
            runner_executable=runner_executable,
        ),
    )
    return with_execution_metadata(
        projected, command=command, argv=argv, shell=False, working_directory=working_directory,
        outcome="exited", exit_code=completed.returncode,
    )


def _test_not_run(
    command: object,
    working_directory: Path,
    reason: str,
    *,
    argv: tuple[str, ...] = (),
    environment: dict[str, str] | None = None,
) -> ToolResult:
    rendered_command = command if isinstance(command, str) else str(command)
    return execution_error_result(
        reason,
        metadata=_test_metadata(
            rendered_command,
            working_directory,
            argv,
            environment or {},
            exit_code=None,
            status="not_run",
        ),
        command=rendered_command,
        argv=argv,
        shell=False,
        working_directory=working_directory,
        outcome="not_run",
    )


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

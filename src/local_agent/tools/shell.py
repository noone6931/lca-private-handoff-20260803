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
_PYTHON_RUNNER = re.compile(r"python(?:\d+(?:\.\d+)*)?", re.IGNORECASE)
_MAVEN_VALUE_OPTIONS = frozenset(
    {
        "-D",
        "-f",
        "--file",
        "-l",
        "--log-file",
        "-P",
        "--activate-profiles",
        "-pl",
        "--projects",
        "-rf",
        "--resume-from",
        "-s",
        "--settings",
        "-T",
        "--threads",
        "-t",
        "--toolchains",
    }
)
_GRADLE_VALUE_OPTIONS = frozenset(
    {
        "--console",
        "--dependency-verification",
        "--max-workers",
        "--priority",
        "--tests",
        "--warning-mode",
    }
)
_GRADLE_FORBIDDEN_OPTIONS = frozenset(
    {"-b", "--build-file", "-c", "--settings-file", "-I", "--init-script", "-p", "--project-dir"}
)
_NODE_CWD_OPTIONS = frozenset({"--cwd", "--dir", "--prefix", "-C"})


def shell_tools() -> list[Tool]:
    return [
        Tool(
            name="run_tests",
            description=(
                "Run an approved test runner without shell interpretation. Defaults to Python unittest. "
                "Use cwd for a module directory and leading environment assignments when needed, for example "
                "`PYTHONPATH=src python3 -m unittest tests.test_config`. Pipes, redirects, shell operators, "
                "and arbitrary executables are rejected."
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
    policy_denial = _test_runner_denial_reason(argv)
    if policy_denial:
        return _test_not_run(command, working_directory, policy_denial, argv=argv, environment=environment)
    return _run_test_process(
        command=command,
        argv=argv,
        environment=environment,
        working_directory=working_directory,
        args=args,
        context=context,
    )


def run_shell(args: dict[str, Any], context: ToolContext) -> ToolResult:
    return _run_command(args["command"], args, context, default_timeout=60)


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

    environment = dict(os.environ)
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
            return "run_tests rejects shell operators, pipes, and redirections. Use cwd and runner arguments instead."
    return None


def _expand_environment_references(value: str, environment: dict[str, str]) -> str:
    return _ENV_REFERENCE.sub(lambda match: environment.get(match.group(1) or match.group(2) or "", ""), value)


def _test_runner_denial_reason(argv: tuple[str, ...]) -> str | None:
    runner = Path(argv[0]).name.casefold()
    runner_args = argv[1:]
    if _PYTHON_RUNNER.fullmatch(runner):
        if len(runner_args) >= 2 and runner_args[0] == "-m" and runner_args[1] in {"pytest", "unittest"}:
            return None
        return "run_tests permits Python only as `python -m unittest ...` or `python -m pytest ...`; `-c` is not allowed."
    if runner in {"pytest", "py.test"}:
        return None
    if runner in {"mvn", "mvnw"}:
        return _maven_runner_denial_reason(runner_args)
    if runner in {"gradle", "gradlew"}:
        return _gradle_runner_denial_reason(runner_args)
    if runner in {"npm", "pnpm", "yarn", "bun"}:
        return _node_runner_denial_reason(runner, runner_args)
    if runner == "go":
        return _subcommand_denial_reason("go", runner_args, {"test"})
    if runner == "cargo":
        return _subcommand_denial_reason("cargo", runner_args, {"test"})
    if runner == "dotnet":
        return _subcommand_denial_reason("dotnet", runner_args, {"test"})
    if runner == "make":
        targets = [arg for arg in runner_args if not arg.startswith("-") and "=" not in arg]
        if targets and all(target in {"check", "test"} for target in targets):
            return None
        return "run_tests permits make only with test/check targets."
    if runner in {"tox", "nox"}:
        return None
    return (
        f"run_tests runner '{runner}' is not allowed. Use unittest/pytest, Maven, Gradle, a package-manager "
        "test task, go/cargo/dotnet test, make test/check, tox, or nox."
    )


def _maven_runner_denial_reason(args: tuple[str, ...]) -> str | None:
    for arg in args:
        lowered = arg.casefold()
        if lowered in {"-dskiptests", "-dmaven.test.skip"}:
            return "run_tests rejects Maven test-skipping properties."
        if lowered.startswith("-dskiptests=") and lowered.split("=", 1)[1] != "false":
            return "run_tests rejects Maven test-skipping properties."
        if lowered.startswith("-dmaven.test.skip=") and lowered.split("=", 1)[1] != "false":
            return "run_tests rejects Maven test-skipping properties."
        if lowered.startswith("-dmaven.ext.class.path="):
            return "run_tests rejects Maven extension classpath overrides."
    goals = _positional_arguments(args, _MAVEN_VALUE_OPTIONS)
    if not any(goal in {"test", "verify"} for goal in goals):
        return "run_tests Maven commands must include the test or verify lifecycle goal."
    invalid = [goal for goal in goals if goal not in {"clean", "test", "verify"}]
    if invalid:
        return f"run_tests rejects non-test Maven goals: {', '.join(invalid)}."
    return None


def _gradle_runner_denial_reason(args: tuple[str, ...]) -> str | None:
    for index, arg in enumerate(args):
        if arg in _GRADLE_FORBIDDEN_OPTIONS or any(
            arg.startswith(f"{option}=") for option in _GRADLE_FORBIDDEN_OPTIONS if option.startswith("--")
        ):
            return "run_tests rejects Gradle build/init/project path overrides; use the cwd argument."
        if index > 0 and args[index - 1] in _GRADLE_FORBIDDEN_OPTIONS:
            return "run_tests rejects Gradle build/init/project path overrides; use the cwd argument."
    tasks = _positional_arguments(args, _GRADLE_VALUE_OPTIONS)
    if not tasks:
        return "run_tests Gradle commands must include a test or check task."
    invalid = [task for task in tasks if task != "clean" and task.rsplit(":", 1)[-1] not in {"check", "test"}]
    if invalid or not any(task.rsplit(":", 1)[-1] in {"check", "test"} for task in tasks):
        return "run_tests Gradle commands may only invoke clean plus test/check tasks."
    return None


def _node_runner_denial_reason(runner: str, args: tuple[str, ...]) -> str | None:
    if any(arg in _NODE_CWD_OPTIONS or any(arg.startswith(f"{option}=") for option in _NODE_CWD_OPTIONS) for arg in args):
        return "run_tests rejects package-manager cwd/prefix overrides; use the cwd argument."
    operands = [arg for arg in args if not arg.startswith("-")]
    if not operands:
        return f"run_tests {runner} commands must invoke a test script."
    script = operands[1] if operands[0] == "run" and len(operands) > 1 else operands[0]
    if script == "test" or script.startswith("test:"):
        return None
    return f"run_tests {runner} commands may only invoke test or test:* scripts."


def _subcommand_denial_reason(runner: str, args: tuple[str, ...], allowed: set[str]) -> str | None:
    command = next((arg for arg in args if not arg.startswith("-")), None)
    if command in allowed:
        return None
    return f"run_tests {runner} commands must use the test subcommand."


def _positional_arguments(args: tuple[str, ...], value_options: frozenset[str]) -> list[str]:
    positional: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            break
        if arg in value_options:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        positional.append(arg)
    return positional


def _run_test_process(
    *,
    command: str,
    argv: tuple[str, ...],
    environment: dict[str, str],
    working_directory: Path,
    args: dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    timeout = min(max(int(args.get("timeout") or 120), 1), 600)
    timeout = _clamp_timeout_to_budget(timeout, context)
    metadata = _test_metadata(command, working_directory, argv, environment, exit_code=None, status="failed")
    if timeout < 1:
        return ToolResult("Test command was not run because budget_seconds is exhausted.", is_error=True, metadata=metadata)
    try:
        completed = subprocess.run(
            list(argv),
            cwd=working_directory,
            env={**os.environ, **environment},
            shell=False,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = f"Test command timed out after {timeout} seconds."
        if exc.stdout:
            output += f"\n[stdout]\n{exc.stdout}"
        if exc.stderr:
            output += f"\n[stderr]\n{exc.stderr}"
        return ToolResult(output[:30000], is_error=True, metadata=metadata)
    except OSError as exc:
        return ToolResult(f"Test runner could not be started: {exc}", is_error=True, metadata=metadata)
    output = completed.stdout
    if completed.stderr:
        output += "\n[stderr]\n" + completed.stderr
    output += f"\n[exit_code] {completed.returncode}"
    status = "succeeded" if completed.returncode == 0 else "failed"
    return ToolResult(
        output[:30000],
        is_error=completed.returncode != 0,
        metadata=_test_metadata(
            command,
            working_directory,
            argv,
            environment,
            exit_code=completed.returncode,
            status=status,
        ),
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
    return ToolResult(
        reason,
        is_error=True,
        metadata=_test_metadata(
            rendered_command,
            working_directory,
            argv,
            environment or {},
            exit_code=None,
            status="not_run",
        ),
    )


def _test_metadata(
    command: str,
    working_directory: Path,
    argv: tuple[str, ...],
    environment: dict[str, str],
    *,
    exit_code: int | None,
    status: str,
) -> dict[str, Any]:
    return {
        "executed_command": command,
        "display_command": shlex.join(argv) if argv else command,
        "argv": list(argv),
        "environment_keys": sorted(environment),
        "working_directory": str(working_directory.resolve()),
        "exit_code": exit_code,
        "execution_status": status,
    }


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

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


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
_INJECTION_ENVIRONMENT_NAMES = frozenset(
    {
        "BASH_ENV",
        "ENV",
        "GRADLE_OPTS",
        "JAVA_TOOL_OPTIONS",
        "LD_PRELOAD",
        "MAVEN_OPTS",
        "NODE_OPTIONS",
        "RUSTC_WRAPPER",
        "_JAVA_OPTIONS",
    }
)
_WORKSPACE_WRAPPERS = frozenset({"./gradlew", "./mvnw"})


@dataclass(frozen=True)
class ResolvedTestRunner:
    argv: tuple[str, ...]
    executable: str


def test_environment_denial_reason(environment: Mapping[str, str]) -> str | None:
    for name in environment:
        normalized = name.upper()
        if normalized in _INJECTION_ENVIRONMENT_NAMES or normalized.startswith("DYLD_"):
            return f"run_tests rejects loader/injection environment variable {name}."
    return None


def test_runner_denial_reason(argv: tuple[str, ...]) -> str | None:
    requested_runner = argv[0]
    if _has_path_component(requested_runner) and requested_runner not in _WORKSPACE_WRAPPERS:
        return "run_tests rejects runner paths; use a trusted bare runner or cwd-local ./mvnw or ./gradlew."
    runner = requested_runner.removeprefix("./").casefold()
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


def resolve_test_runner(
    argv: tuple[str, ...],
    *,
    working_directory: Path,
    trusted_path: str | None = None,
) -> tuple[ResolvedTestRunner | None, str | None]:
    denial = test_runner_denial_reason(argv)
    if denial:
        return None, denial
    requested_runner = argv[0]
    if requested_runner in _WORKSPACE_WRAPPERS:
        root = working_directory.resolve()
        executable = (root / requested_runner.removeprefix("./")).resolve()
        if executable.parent != root:
            return None, "run_tests cwd-local wrapper resolves outside the canonical working directory."
        if not executable.is_file() or not os.access(executable, os.X_OK):
            return None, f"run_tests cwd-local wrapper is missing or not executable: {requested_runner}"
    else:
        located = shutil.which(requested_runner, path=trusted_path if trusted_path is not None else os.environ.get("PATH"))
        if located is None:
            return None, f"run_tests trusted PATH could not resolve runner '{requested_runner}'."
        executable = Path(located).resolve()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            return None, f"run_tests resolved runner is missing or not executable: {requested_runner}"
    executable_text = str(executable)
    return ResolvedTestRunner(argv=(executable_text, *argv[1:]), executable=executable_text), None


def _has_path_component(value: str) -> bool:
    return "/" in value or "\\" in value


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

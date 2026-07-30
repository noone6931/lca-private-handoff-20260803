"""Focused execution path for the run_tests tool."""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .base import ToolContext, ToolResult
from .execution_interrupt import attach_interrupted_tool_result
from .execution_metadata import execution_argv_from_observation
from .execution_metadata import execution_error_result, legacy_test_metadata, with_execution_metadata
from .isolation_routing import IsolationExecutionError, isolation_metadata, run_context_process
from .process_environment import build_child_process_environment
from .process_output import process_tool_result
from .shell_execution import isolation_execution_error_result


def run_test_process(
    *,
    command: str,
    argv: tuple[str, ...],
    isolated_argv: tuple[str, ...],
    runner_executable: str,
    environment: dict[str, str],
    working_directory: Path,
    timeout: int,
    context: ToolContext,
    local_runner: Callable[..., Any],
) -> ToolResult:
    if timeout < 1:
        return execution_error_result(
            "Test command was not run because budget_seconds is exhausted.",
            metadata=legacy_test_metadata(
                command,
                working_directory,
                argv,
                environment,
                exit_code=None,
                status="failed",
                runner_executable=runner_executable,
            ),
            command=command,
            argv=argv,
            shell=False,
            working_directory=working_directory,
            outcome="not_run",
        )
    try:
        completed = run_context_process(
            context,
            list(argv),
            local_runner=local_runner,
            isolated_command=isolated_argv,
            isolated_environment=environment,
            cwd=working_directory,
            env=build_child_process_environment(overrides=environment).values,
            shell=False,
            timeout=timeout,
            cancel_event=context.cancel_event,
        )
    except KeyboardInterrupt as exc:
        attach_interrupted_tool_result(
            exc,
            command=command,
            argv=argv,
            shell=False,
            working_directory=working_directory,
            metadata=legacy_test_metadata(
                command,
                working_directory,
                argv,
                environment,
                exit_code=None,
                status="interrupted",
                runner_executable=runner_executable,
            ),
        )
        raise
    except subprocess.TimeoutExpired as exc:
        projected = process_tool_result(
            exc,
            terminal_line=f"[timeout] Test command timed out after {timeout} seconds.",
            is_error=True,
            metadata=legacy_test_metadata(
                command,
                working_directory,
                argv,
                environment,
                exit_code=None,
                status="failed",
                runner_executable=runner_executable,
            ),
            label_stdout=True,
        )
        return with_execution_metadata(
            projected,
            command=command,
            argv=argv,
            shell=False,
            working_directory=working_directory,
            outcome="timed_out",
            exit_code=None,
        )
    except IsolationExecutionError as exc:
        return isolation_execution_error_result(
            exc,
            command=command,
            argv=argv,
            shell=False,
            working_directory=working_directory,
            metadata=legacy_test_metadata(
                command,
                working_directory,
                argv,
                environment,
                exit_code=exc.returncode,
                status=exc.execution_outcome,
                runner_executable=runner_executable,
            ),
        )
    except OSError as exc:
        return execution_error_result(
            f"Test runner could not be started: {exc}",
            metadata=legacy_test_metadata(
                command,
                working_directory,
                argv,
                environment,
                exit_code=None,
                status="failed",
                runner_executable=runner_executable,
            ),
            command=command,
            argv=argv,
            shell=False,
            working_directory=working_directory,
            outcome="spawn_failed",
        )
    status = "succeeded" if completed.returncode == 0 else "failed"
    projected = process_tool_result(
        completed,
        terminal_line=f"[exit_code] {completed.returncode}",
        is_error=completed.returncode != 0,
        metadata={
            **legacy_test_metadata(
                command,
                working_directory,
                argv,
                environment,
                exit_code=completed.returncode,
                status=status,
                runner_executable=runner_executable,
            ),
            **isolation_metadata(completed),
        },
    )
    return with_execution_metadata(
        projected,
        command=command,
        argv=execution_argv_from_observation(completed, argv),
        shell=False,
        working_directory=working_directory,
        outcome="exited",
        exit_code=completed.returncode,
    )


__all__ = ["run_test_process"]

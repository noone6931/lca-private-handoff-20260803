"""Focused execution path for the shell tool."""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from .base import ToolContext, ToolResult
from .execution_metadata import execution_argv_from_observation
from .execution_metadata import execution_error_result, legacy_shell_metadata, with_execution_metadata
from .execution_interrupt import attach_interrupted_tool_result
from .isolation_routing import IsolationExecutionError
from .isolation_routing import isolation_metadata
from .process_environment import build_child_process_environment
from .process_output import process_tool_result


def run_shell_process(
    command: str,
    *,
    timeout: int,
    context: ToolContext,
    dangerous_reason: str | None,
    process_runner: Callable[..., Any],
) -> ToolResult:
    working_directory = context.workspace.resolve()
    if dangerous_reason:
        return _error(command, working_directory, dangerous_reason, "not_run")
    if timeout < 1:
        return _error(
            command,
            working_directory,
            "Command was not run because budget_seconds is exhausted.",
            "not_run",
        )
    try:
        completed = process_runner(
            command,
            cwd=working_directory,
            env=build_child_process_environment().values,
            shell=True,
            timeout=timeout,
            cancel_event=context.cancel_event,
        )
    except KeyboardInterrupt as exc:
        attach_interrupted_tool_result(
            exc,
            command=command,
            argv=None,
            shell=True,
            working_directory=working_directory,
            metadata=legacy_shell_metadata(
                command,
                working_directory,
                exit_code=None,
                status="interrupted",
            ),
        )
        raise
    except subprocess.TimeoutExpired as exc:
        projected = process_tool_result(
            exc,
            terminal_line=f"[timeout] Command timed out after {timeout} seconds.",
            is_error=True,
            metadata=legacy_shell_metadata(command, working_directory, exit_code=None, status="timed_out"),
            label_stdout=True,
        )
        return with_execution_metadata(
            projected,
            command=command,
            argv=None,
            shell=True,
            working_directory=working_directory,
            outcome="timed_out",
            exit_code=None,
        )
    except IsolationExecutionError as exc:
        return isolation_execution_error_result(
            exc,
            command=command,
            argv=None,
            shell=True,
            working_directory=working_directory,
            metadata=legacy_shell_metadata(
                command,
                working_directory,
                exit_code=exc.returncode,
                status=exc.execution_outcome,
            ),
        )
    except OSError as exc:
        return _error(command, working_directory, f"Shell command could not be started: {exc}", "spawn_failed")
    projected = process_tool_result(
        completed,
        terminal_line=f"[exit_code] {completed.returncode}",
        is_error=completed.returncode != 0,
        metadata={
            **legacy_shell_metadata(
                command,
                working_directory,
                exit_code=completed.returncode,
                status="succeeded" if completed.returncode == 0 else "failed",
            ),
            **isolation_metadata(completed),
        },
    )
    return with_execution_metadata(
        projected,
        command=command,
        argv=None,
        shell=True,
        working_directory=working_directory,
        outcome="exited",
        exit_code=completed.returncode,
    )


def isolation_execution_error_result(
    exc: IsolationExecutionError,
    *,
    command: str,
    argv: tuple[str, ...] | None,
    shell: bool,
    working_directory: Path,
    metadata: dict[str, Any],
) -> ToolResult:
    execution_argv = (
        execution_argv_from_observation(exc.completed, argv)
        if argv is not None
        else None
    )
    projected = process_tool_result(
        exc,
        terminal_line=f"[isolation] {exc.reason_code}",
        is_error=True,
        metadata={**metadata, **exc.isolation_metadata},
        label_stdout=True,
    )
    return with_execution_metadata(
        projected,
        command=command,
        argv=execution_argv,
        shell=shell,
        working_directory=working_directory,
        outcome=exc.execution_outcome,
        exit_code=exc.returncode,
    )


def _error(
    command: str, cwd: Path, content: str, outcome: Literal["not_run", "spawn_failed"]
) -> ToolResult:
    return execution_error_result(
        content,
        metadata=legacy_shell_metadata(command, cwd, exit_code=None, status=outcome),
        command=command,
        argv=None,
        shell=True,
        working_directory=cwd,
        outcome=outcome,
    )


__all__ = ["isolation_execution_error_result", "run_shell_process"]

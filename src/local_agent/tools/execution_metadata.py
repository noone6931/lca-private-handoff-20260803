"""Typed metadata for process-backed tools.

This module describes what the tool boundary observed. Session-level provenance
is added later, after the tool result has joined the originating call.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from .base import ToolResult


EXECUTION_METADATA_KEY = "execution_v1"
ExecutionOutcome = Literal["exited", "timed_out", "cancelled", "not_run", "spawn_failed"]


def legacy_test_metadata(
    command: str,
    working_directory: Path,
    argv: tuple[str, ...],
    environment: dict[str, str],
    *,
    exit_code: int | None,
    status: str,
    runner_executable: str | None = None,
) -> dict[str, Any]:
    """Preserve the established run_tests metadata surface."""

    import shlex

    return {
        "executed_command": command,
        "display_command": shlex.join(argv) if argv else command,
        "argv": list(argv),
        "environment_keys": sorted(environment),
        "working_directory": str(working_directory.resolve()),
        "exit_code": exit_code,
        "execution_status": status,
        "execution_capability": "exec",
        "runner_executable": runner_executable,
        "sandboxed": False,
        "trust_boundary": "executes repository-controlled test/build code",
    }


def legacy_shell_metadata(
    command: str,
    working_directory: Path,
    *,
    exit_code: int | None,
    status: str,
) -> dict[str, Any]:
    """Expose the shell launch identity without pretending it has an argv."""

    return {
        "executed_command": command,
        "display_command": command,
        "argv": None,
        "working_directory": str(working_directory.resolve()),
        "exit_code": exit_code,
        "execution_status": status,
        "execution_capability": "exec",
        "sandboxed": False,
        "trust_boundary": "executes a local shell command",
    }


def with_execution_metadata(
    result: ToolResult,
    *,
    command: str,
    argv: Sequence[str] | None,
    shell: bool,
    working_directory: Path,
    outcome: ExecutionOutcome,
    exit_code: int | None,
) -> ToolResult:
    """Attach the unified process observation after bounded output projection."""

    if outcome == "exited":
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise ValueError("An exited execution requires an integer exit code.")
    elif exit_code is not None:
        raise ValueError(f"Execution outcome {outcome!r} cannot carry an exit code.")
    metadata = dict(result.metadata)
    metadata[EXECUTION_METADATA_KEY] = {
        "version": 1,
        "command": {
            "text": command,
            "argv": list(argv) if argv is not None else None,
            "shell": shell,
        },
        "cwd": str(working_directory.resolve()),
        "outcome": {"kind": outcome, "exit_code": exit_code},
        "output": _output_provenance(metadata),
    }
    return ToolResult(
        result.content,
        is_error=result.is_error,
        useless=result.useless,
        metadata=metadata,
    )


def execution_error_result(
    content: str,
    *,
    metadata: Mapping[str, Any],
    command: str,
    argv: Sequence[str] | None,
    shell: bool,
    working_directory: Path,
    outcome: Literal["cancelled", "not_run", "spawn_failed"],
) -> ToolResult:
    return with_execution_metadata(
        ToolResult(content, is_error=True, metadata=dict(metadata)),
        command=command,
        argv=argv,
        shell=shell,
        working_directory=working_directory,
        outcome=outcome,
        exit_code=None,
    )


def _output_provenance(metadata: Mapping[str, Any]) -> dict[str, Any]:
    capture = metadata.get("output_capture")
    if not isinstance(capture, Mapping):
        return {"provenance": "none", "bounded": True}
    return {
        "provenance": "bounded_process_capture_v1",
        "bounded": True,
        "capture": _capture_summary(capture),
    }


def _capture_summary(capture: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for stream in ("stdout", "stderr", "total", "display"):
        raw = capture.get(stream)
        if not isinstance(raw, Mapping):
            continue
        summary[stream] = {
            str(key): value
            for key, value in raw.items()
            if key
            in {
                "observed_bytes",
                "captured_bytes",
                "dropped_bytes",
                "observed_chars",
                "captured_chars",
                "dropped_chars",
                "truncated",
            }
            and (isinstance(value, (int, bool)) and not isinstance(value, float))
        }
    return summary


__all__ = [
    "EXECUTION_METADATA_KEY",
    "execution_error_result",
    "legacy_shell_metadata",
    "legacy_test_metadata",
    "with_execution_metadata",
]

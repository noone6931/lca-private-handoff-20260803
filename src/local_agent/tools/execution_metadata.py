"""Typed metadata for process-backed tools.

This module describes what the tool boundary observed. Session-level provenance
is added later, after the tool result has joined the originating call.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .base import ToolResult


EXECUTION_METADATA_KEY = "execution_v1"
ExecutionOutcome = Literal["exited", "timed_out", "cancelled", "not_run", "spawn_failed", "indeterminate"]
EXECUTION_OUTCOMES = frozenset({"exited", "timed_out", "cancelled", "not_run", "spawn_failed", "indeterminate"})
_COMMAND_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_REDACTED_SHELL_COMMAND = "[redacted shell command]"
_REDACTED_TEST_COMMAND = "[redacted test command]"


@dataclass(frozen=True)
class ParsedExecutionMetadata:
    command: str
    command_digest: str
    argv: tuple[str, ...] | None
    shell: bool
    cwd: str
    outcome: str
    exit_code: int | None
    output: Mapping[str, Any]


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

    safe_argv = _safe_execution_argv(argv)
    return {
        "executed_command": _REDACTED_TEST_COMMAND,
        "display_command": shlex.join(safe_argv) if safe_argv else _REDACTED_TEST_COMMAND,
        "command_digest": execution_command_digest(command),
        "argv": list(safe_argv),
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
        "executed_command": _REDACTED_SHELL_COMMAND,
        "display_command": _REDACTED_SHELL_COMMAND,
        "command_digest": execution_command_digest(command),
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
    safe_argv = _safe_execution_argv(argv)
    metadata[EXECUTION_METADATA_KEY] = {
        "version": 1,
        "command": {
            "text": _REDACTED_SHELL_COMMAND if shell else _REDACTED_TEST_COMMAND,
            "digest": execution_command_digest(command),
            "argv": list(safe_argv) if safe_argv is not None else None,
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


def execution_argv_from_observation(
    observed: object,
    fallback: Sequence[str],
) -> tuple[str, ...]:
    """Prefer the argv attached to the process observation over a routing argv."""

    raw = getattr(observed, "args", None)
    if (
        isinstance(raw, (list, tuple))
        and raw
        and all(isinstance(item, str) and bool(item) for item in raw)
    ):
        return _safe_execution_argv(raw) or ()
    return _safe_execution_argv(fallback) or ()


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


def parse_execution_metadata(
    metadata: Mapping[str, Any],
    *,
    tool_name: str,
    authorized_roots: Sequence[str] | None = None,
) -> ParsedExecutionMetadata | None:
    """Validate the single execution metadata schema used by tools and evidence."""

    if metadata.get("version") != 1 or tool_name not in {"shell", "run_tests"}:
        return None
    command = metadata.get("command")
    outcome = metadata.get("outcome")
    output = metadata.get("output")
    if not isinstance(command, Mapping) or not isinstance(outcome, Mapping) or not isinstance(output, Mapping):
        return None
    text, raw_argv, shell = command.get("text"), command.get("argv"), command.get("shell")
    digest = command.get("digest")
    cwd, status, exit_code = metadata.get("cwd"), outcome.get("kind"), outcome.get("exit_code")
    if not _metadata_identity(text, max_chars=65_536) or not isinstance(shell, bool):
        return None
    if digest is None:
        digest = execution_command_digest(str(text))
    if not isinstance(digest, str) or _COMMAND_DIGEST.fullmatch(digest) is None:
        return None
    if status not in EXECUTION_OUTCOMES or (tool_name == "shell") != shell:
        return None
    canonical_cwd = _canonical_cwd(cwd, authorized_roots)
    if canonical_cwd is None:
        return None
    if raw_argv is None:
        argv = None
    elif isinstance(raw_argv, list) and all(_metadata_identity(item, max_chars=16_384) for item in raw_argv):
        argv = tuple(str(item) for item in raw_argv)
    else:
        return None
    if (tool_name == "shell" and argv is not None) or (tool_name == "run_tests" and argv is None):
        return None
    if status == "exited":
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            return None
    elif exit_code is not None:
        return None
    bounded_output = _bounded_output(output)
    if bounded_output is None:
        return None
    return ParsedExecutionMetadata(
        command=str(text),
        command_digest=digest,
        argv=argv,
        shell=shell,
        cwd=canonical_cwd,
        outcome=str(status),
        exit_code=exit_code,
        output=bounded_output,
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


def _bounded_output(output: Mapping[str, Any]) -> Mapping[str, Any] | None:
    provenance = output.get("provenance")
    if output.get("bounded") is not True or provenance not in {"none", "bounded_process_capture_v1"}:
        return None
    if provenance == "none":
        return {"provenance": "none", "bounded": True}
    capture = output.get("capture")
    if not isinstance(capture, Mapping):
        return None
    safe_capture: dict[str, Any] = {}
    for stream in ("stdout", "stderr", "total", "display"):
        values = capture.get(stream)
        if not isinstance(values, Mapping):
            continue
        safe_values = {
            str(key): value
            for key, value in values.items()
            if key.endswith(("_bytes", "_chars")) or key == "truncated"
            if isinstance(value, (int, bool)) and not isinstance(value, float)
        }
        if any(isinstance(value, int) and not isinstance(value, bool) and value < 0 for value in safe_values.values()):
            return None
        safe_capture[stream] = safe_values
    return {"provenance": provenance, "bounded": True, "capture": safe_capture}


def _canonical_cwd(value: Any, authorized_roots: Sequence[str] | None) -> str | None:
    if not _metadata_identity(value, max_chars=4096):
        return None
    try:
        cwd = Path(str(value))
        if not cwd.is_absolute() or cwd.resolve() != cwd:
            return None
        if authorized_roots is not None and not any(
            cwd == Path(root) or cwd.is_relative_to(Path(root)) for root in authorized_roots
        ):
            return None
        return str(cwd)
    except (OSError, ValueError):
        return None


def _metadata_identity(value: Any, *, max_chars: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= max_chars and "\x00" not in value


def _safe_execution_argv(argv: Sequence[str] | None) -> tuple[str, ...] | None:
    if argv is None:
        return None
    values = tuple(str(item) for item in argv)
    if not values or values[0] != "/usr/bin/env":
        return values
    safe = [values[0]]
    index = 1
    while index < len(values):
        item = values[index]
        name, separator, _value = item.partition("=")
        if not separator or _ENVIRONMENT_NAME.fullmatch(name) is None:
            break
        safe.append(f"{name}=[redacted]")
        index += 1
    return (*safe, *values[index:])


def execution_command_digest(command: str) -> str:
    return "sha256:" + hashlib.sha256(command.encode("utf-8")).hexdigest()


__all__ = [
    "EXECUTION_METADATA_KEY",
    "ParsedExecutionMetadata",
    "execution_argv_from_observation",
    "execution_command_digest",
    "execution_error_result",
    "legacy_shell_metadata",
    "legacy_test_metadata",
    "parse_execution_metadata",
    "with_execution_metadata",
]

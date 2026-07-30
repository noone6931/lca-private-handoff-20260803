from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..patch.journal import PatchJournalMutationResult
from ..protocol.cancellation import RunCancelled
from .base import ToolResult
from .execution_metadata import EXECUTION_OUTCOMES
from .execution_metadata import with_execution_metadata
from .process_output import ProcessOutputCapture
from .process_output import process_tool_result
from .workspace_mutation_contracts import WorkspaceMutationCommitResult


class CapturedRunCancelled(RunCancelled):
    """Cancellation raised after bounded process output has been collected."""

    def __init__(self, message: str, capture: ProcessOutputCapture) -> None:
        super().__init__(message)
        self.output_capture = capture
        self.execution_started = True
        self.execution_outcome = "cancelled"
        self.returncode = None


def attach_execution_control_facts(
    error: BaseException,
    *,
    execution_started: bool,
    execution_outcome: str,
    exit_code: int | None,
    isolation_metadata: Mapping[str, object] | None = None,
    output_capture: ProcessOutputCapture | None = None,
) -> None:
    if not execution_started:
        return
    setattr(error, "execution_started", True)
    setattr(error, "execution_outcome", execution_outcome)
    setattr(error, "returncode", exit_code)
    if isolation_metadata is not None:
        setattr(error, "isolation_metadata", dict(isolation_metadata))
    if output_capture is not None:
        setattr(error, "output_capture", output_capture)
        setattr(error, "stdout", output_capture.stdout.text)
        setattr(error, "stderr", output_capture.stderr.text)


def attach_active_process_interruption(
    error: BaseException,
    output_capture: ProcessOutputCapture,
) -> None:
    attach_execution_control_facts(
        error,
        execution_started=True,
        execution_outcome="cancelled",
        exit_code=None,
        output_capture=output_capture,
    )


def attach_interrupted_tool_result(
    error: KeyboardInterrupt,
    *,
    command: str,
    argv: Sequence[str] | None,
    shell: bool,
    working_directory: Path,
    metadata: Mapping[str, Any],
) -> None:
    observation = _execution_observation(error)
    if observation is None:
        return
    outcome, exit_code = observation
    durable = dict(metadata)
    durable["execution_status"] = "interrupted"
    durable["exit_code"] = exit_code
    isolation = getattr(error, "isolation_metadata", None)
    if isinstance(isolation, Mapping):
        durable.update(dict(isolation))
    mutation = getattr(error, "workspace_mutation_result", None)
    if isinstance(mutation, WorkspaceMutationCommitResult):
        durable.update(mutation.metadata())
    journal = getattr(error, "patch_journal_result", None)
    if isinstance(journal, PatchJournalMutationResult):
        durable["patch_journal"] = journal.metadata()
    projected = process_tool_result(
        error,
        terminal_line="[interrupted] Process-backed tool finalization was interrupted.",
        is_error=True,
        metadata=durable,
        label_stdout=True,
    )
    result = with_execution_metadata(
        projected,
        command=command,
        argv=argv,
        shell=shell,
        working_directory=working_directory,
        outcome=outcome,
        exit_code=exit_code,
    )
    setattr(error, "interrupted_tool_result", result)


def interrupted_tool_result(error: BaseException) -> ToolResult | None:
    result = getattr(error, "interrupted_tool_result", None)
    return result if isinstance(result, ToolResult) else None


def _execution_observation(
    error: BaseException,
) -> tuple[str, int | None] | None:
    if getattr(error, "execution_started", False) is not True:
        return None
    outcome = getattr(error, "execution_outcome", "indeterminate")
    if outcome not in EXECUTION_OUTCOMES or outcome in {"not_run", "spawn_failed"}:
        outcome = "indeterminate"
    exit_code = getattr(error, "returncode", None)
    if outcome == "exited":
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            return "indeterminate", None
        return "exited", exit_code
    return str(outcome), None


__all__ = [
    "CapturedRunCancelled",
    "attach_active_process_interruption",
    "attach_execution_control_facts",
    "attach_interrupted_tool_result",
    "interrupted_tool_result",
]

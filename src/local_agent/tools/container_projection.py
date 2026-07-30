from __future__ import annotations

from dataclasses import replace

from ..execution.container_instance import ContainerCapturedExecution
from ..execution.container_staging import ContainerStagingCleanupResult
from ..execution.container_staging import ContainerStagingOutputResult
from ..execution.contracts import AppliedIsolationProof
from ..protocol.cancellation import CancellationSignal
from ..protocol.cancellation import RunCancelled
from .container_outcome import ContainerCleanupSummary
from .container_outcome import ContainerExecutionOutcome
from .container_unwind import ContainerUnwindSummary
from .process_output import CapturedCompletedProcess
from .process_output import CapturedText
from .process_output import ProcessOutputCapture
from .process_output import StreamCaptureSummary


def cleanup_summary(reason_code: str, verified: bool, unresolved: bool) -> ContainerCleanupSummary:
    return ContainerCleanupSummary(reason_code, verified, unresolved)


def container_closure_verified(outcome: ContainerExecutionOutcome) -> bool:
    cleanup = outcome.cleanup
    return (
        not outcome.recovery_unresolved
        and (cleanup is None or cleanup.verified)
    )


def finalize_staged_outcome(
    outcome: ContainerExecutionOutcome,
    output: ContainerStagingOutputResult,
    cleanup: ContainerStagingCleanupResult,
) -> ContainerExecutionOutcome:
    cleanup_fact = cleanup_summary(
        cleanup.reason_code,
        cleanup.verified,
        cleanup.unresolved,
    )
    transport_succeeded = outcome.reason_code == "container_execution_completed"
    output_available = (
        outcome.workspace_output_captured
        and output.verified
        and cleanup.verified
    )
    return replace(
        outcome,
        reason_code=(
            "staging_cleanup_unverified"
            if not cleanup.verified
            else "staging_output_not_captured"
            if transport_succeeded and not outcome.workspace_output_captured
            else output.reason_code
            if transport_succeeded and not output.verified
            else outcome.reason_code
        ),
        staging_cleanup=cleanup_fact,
        workspace_transport="staged-copy",
        workspace_output_plan=(
            output.plan
            if transport_succeeded and output_available
            else None
        ),
    )


def completed_process(captured: ContainerCapturedExecution) -> CapturedCompletedProcess:
    return CapturedCompletedProcess(
        list(captured.exited.waited.released.verified.plan.command_argv),
        captured.command_exit_code,
        _process_capture(captured),
    )


def pre_instance_stop_outcome(
    attempt_id: str,
    cancel_event: CancellationSignal | None,
) -> ContainerExecutionOutcome:
    cancellation = (
        RunCancelled("Run cancelled before container control step.")
        if cancel_event is not None and cancel_event.is_set()
        else None
    )
    return ContainerExecutionOutcome(
        "container_cancelled" if cancellation is not None else "container_deadline_exhausted",
        attempt_id,
        cancellation=cancellation,
        execution_outcome="cancelled" if cancellation is not None else "timed_out",
    )


def instance_failure_outcome(
    reason_code: str,
    attempt_id: str,
    cleanup: ContainerCleanupSummary,
    cancellation: RunCancelled | CancellationSignal | None,
    *,
    proof: AppliedIsolationProof | None,
) -> ContainerExecutionOutcome:
    cancellation_error = _cancellation(
        cancellation,
        "Run cancelled before container control step.",
    )
    return ContainerExecutionOutcome(
        reason_code,
        attempt_id,
        proof=proof,
        cleanup=cleanup,
        cancellation=cancellation_error,
        execution_outcome="cancelled" if cancellation_error is not None else "not_run",
    )


def released_failure_outcome(
    reason_code: str,
    attempt_id: str,
    cleanup: ContainerCleanupSummary,
    cancellation: RunCancelled | CancellationSignal | None,
    unwind: ContainerUnwindSummary,
    *,
    proof: AppliedIsolationProof,
    release_verified: bool,
) -> ContainerExecutionOutcome:
    cancellation_error = _cancellation(
        cancellation,
        "Run cancelled while isolated user code was active.",
    )
    execution_outcome = (
        "cancelled"
        if cancellation_error is not None
        else "timed_out"
        if reason_code in {"wait_not_run", "wait_timed_out"}
        else "indeterminate"
    )
    return ContainerExecutionOutcome(
        reason_code,
        attempt_id,
        proof=proof,
        cleanup=cleanup,
        cancellation=cancellation_error,
        command_release_state="verified" if release_verified else "ambiguous",
        execution_outcome=execution_outcome,
        user_output=unwind.user_output if release_verified else None,
        termination_reason_code=unwind.reason_code,
        user_output_reason_code=unwind.output_reason_code if release_verified else None,
    )


def create_failure_outcome(
    reason_code: str,
    attempt_id: str,
    recovery: ContainerCleanupSummary | None,
    cancellation: RunCancelled | None,
    *,
    unresolved_without_correlation: bool,
) -> ContainerExecutionOutcome:
    return ContainerExecutionOutcome(
        reason_code,
        attempt_id,
        cleanup=recovery,
        cancellation=cancellation,
        execution_outcome="cancelled" if cancellation is not None else "not_run",
        recovery_unresolved=(
            unresolved_without_correlation
            or recovery is None
            or recovery.unresolved
        ),
    )


def _cancellation(
    value: RunCancelled | CancellationSignal | None,
    message: str,
) -> RunCancelled | None:
    if isinstance(value, RunCancelled):
        return value
    if value is not None and value.is_set():
        return RunCancelled(message)
    return None


def _process_capture(captured: ContainerCapturedExecution) -> ProcessOutputCapture:
    def stream(text, summary):
        return CapturedText(
            text,
            StreamCaptureSummary(
                summary.observed_bytes,
                summary.captured_bytes,
                summary.dropped_bytes,
                summary.truncated,
            ),
        )

    return ProcessOutputCapture(
        stream(captured.stdout, captured.output_capture.stdout),
        stream(captured.stderr, captured.output_capture.stderr),
    )


__all__ = [
    "cleanup_summary",
    "completed_process",
    "container_closure_verified",
    "create_failure_outcome",
    "finalize_staged_outcome",
    "instance_failure_outcome",
    "pre_instance_stop_outcome",
    "released_failure_outcome",
]

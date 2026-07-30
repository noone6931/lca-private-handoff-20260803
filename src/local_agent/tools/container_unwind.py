from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ..execution.container_cleanup import ContainerCleanupHandle
from ..execution.container_instance import ContainerReleasedExecution
from ..execution.container_instance import VerifiedContainerExecution
from ..execution.container_termination import ContainerUserOutput
from ..execution.container_termination import build_container_termination_plan
from ..execution.container_termination import parse_container_termination_logs_result
from ..execution.container_termination import parse_container_termination_signal_result
from ..execution.container_termination import parse_container_termination_wait_result
from ..execution.container_types import container_command_id
from ..execution.contracts import AppliedIsolationProof
from ..protocol.cancellation import RunCancelled
from .container_outcome import ContainerCleanupSummary
from .container_outcome import ContainerExecutionOutcome
from .container_process import ContainerCommandObservation
from .container_process import ContainerCommandRunner
from .process_output import CapturedText
from .process_output import ProcessOutputCapture
from .process_output import StreamCaptureSummary


_UNWIND_BUDGET_SECONDS = 30.0
_TERMINATION_GRACE_SECONDS = 2.0
_TERMINATION_KILL_WAIT_SECONDS = 5.0


@dataclass(frozen=True)
class ContainerUnwindSummary:
    reason_code: str
    output_reason_code: str
    user_output: ProcessOutputCapture | None = None

    def __post_init__(self) -> None:
        if not self.reason_code.strip() or not self.output_reason_code.strip():
            raise ValueError("container unwind summary is invalid")


def unwind_released_container(
    runner: ContainerCommandRunner,
    execution: VerifiedContainerExecution | ContainerReleasedExecution,
    *,
    invoke: Callable[..., ContainerCommandObservation | None],
) -> ContainerUnwindSummary:
    plan = build_container_termination_plan(execution)
    deadline = time.monotonic() + _UNWIND_BUDGET_SECONDS
    termination_reason = "terminate_not_run"
    observed = invoke(
        runner,
        step="terminate",
        argv=plan.terminate_argv,
        command_id=container_command_id(runner.attempt_id, "terminate"),
        deadline=deadline,
        cancel_event=None,
    )
    if observed is not None:
        termination_reason = parse_container_termination_signal_result(
            plan,
            observed.result,
            step="terminate",
        ).reason_code
    stopped = False
    observed = invoke(
        runner,
        step="termination_wait",
        argv=plan.wait_argv,
        command_id=container_command_id(runner.attempt_id, "termination_wait"),
        deadline=deadline,
        cancel_event=None,
        timeout_limit=_TERMINATION_GRACE_SECONDS,
    )
    if observed is not None:
        waited = parse_container_termination_wait_result(
            plan,
            observed.result,
            step="termination_wait",
        )
        stopped = waited.stopped
        termination_reason = waited.reason_code
    if not stopped:
        observed = invoke(
            runner,
            step="kill",
            argv=plan.kill_argv,
            command_id=container_command_id(runner.attempt_id, "kill"),
            deadline=deadline,
            cancel_event=None,
        )
        if observed is not None:
            termination_reason = parse_container_termination_signal_result(
                plan,
                observed.result,
                step="kill",
            ).reason_code
        observed = invoke(
            runner,
            step="kill_wait",
            argv=plan.wait_argv,
            command_id=container_command_id(runner.attempt_id, "kill_wait"),
            deadline=deadline,
            cancel_event=None,
            timeout_limit=_TERMINATION_KILL_WAIT_SECONDS,
        )
        if observed is not None:
            waited = parse_container_termination_wait_result(
                plan,
                observed.result,
                step="kill_wait",
            )
            termination_reason = waited.reason_code
    observed = invoke(
        runner,
        step="termination_logs",
        argv=plan.logs_argv,
        command_id=container_command_id(runner.attempt_id, "termination_logs"),
        deadline=deadline,
        cancel_event=None,
    )
    if observed is None:
        return ContainerUnwindSummary(
            termination_reason,
            "termination_logs_not_run",
        )
    logs = parse_container_termination_logs_result(plan, observed.result)
    return ContainerUnwindSummary(
        termination_reason,
        logs.reason_code,
        _process_capture(logs.output) if logs.output is not None else None,
    )


def _process_capture(output: ContainerUserOutput) -> ProcessOutputCapture:
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
        stream(output.stdout, output.capture.stdout),
        stream(output.stderr, output.capture.stderr),
    )


def close_container_after_exception(
    error: BaseException,
    *,
    attempt_id: str,
    runner: ContainerCommandRunner,
    cleanup_handle: ContainerCleanupHandle,
    proof: AppliedIsolationProof | None,
    release_attempted: VerifiedContainerExecution | None,
    release: ContainerReleasedExecution | None,
    invoke: Callable[..., ContainerCommandObservation | None],
    cleanup: Callable[
        [ContainerCommandRunner, ContainerCleanupHandle],
        ContainerCleanupSummary,
    ],
) -> ContainerExecutionOutcome:
    unwind: ContainerUnwindSummary | None = None
    if release_attempted is not None:
        try:
            unwind = unwind_released_container(
                runner,
                release or release_attempted,
                invoke=invoke,
            )
        except BaseException:
            unwind = ContainerUnwindSummary(
                "unwind_exception",
                "termination_logs_unavailable",
            )
    try:
        cleanup_summary = cleanup(runner, cleanup_handle)
    except BaseException:
        cleanup_summary = ContainerCleanupSummary(
            "cleanup_exception",
            False,
            True,
        )
    release_verified = release is not None
    cancellation = error if isinstance(error, RunCancelled) else None
    outcome = ContainerExecutionOutcome(
        "container_runtime_exception",
        attempt_id,
        proof=proof,
        cleanup=cleanup_summary,
        cancellation=cancellation,
        command_release_state=(
            "verified"
            if release_verified
            else "ambiguous"
            if release_attempted is not None
            else "not_attempted"
        ),
        execution_outcome=(
            "cancelled"
            if cancellation is not None
            else "indeterminate"
            if release_attempted is not None
            else "not_run"
        ),
        user_output=(
            unwind.user_output
            if release_verified and unwind is not None
            else None
        ),
        termination_reason_code=(
            unwind.reason_code if unwind is not None else None
        ),
        user_output_reason_code=(
            unwind.output_reason_code
            if release_verified and unwind is not None
            else None
        ),
    )
    return outcome


def attach_container_outcome_to_control_error(
    error: BaseException,
    outcome: ContainerExecutionOutcome,
) -> None:
    """Preserve typed cleanup facts only for non-model control-flow exceptions."""

    try:
        error.execution_started = outcome.command_release_state != "not_attempted"
        error.execution_outcome = outcome.execution_outcome
        error.returncode = (
            outcome.completed.returncode if outcome.completed is not None else None
        )
        error.isolation_metadata = outcome.metadata()
        if outcome.user_output is not None:
            error.output_capture = outcome.user_output
            error.stdout = outcome.user_output.stdout.text
            error.stderr = outcome.user_output.stderr.text
    except (AttributeError, TypeError):
        pass


__all__ = [
    "ContainerUnwindSummary",
    "attach_container_outcome_to_control_error",
    "close_container_after_exception",
    "unwind_released_container",
]

from __future__ import annotations

import time
from collections.abc import Callable

from ..execution.container_cleanup import ContainerCleanupHandle
from ..execution.container_instance import ContainerCreatedInstance
from ..execution.container_instance import ContainerReleasedExecution
from ..execution.container_instance import VerifiedContainerExecution
from ..execution.container_instance import parse_container_final_inspect_result
from ..execution.container_instance import parse_container_gate_ready_result
from ..execution.container_instance import parse_container_inspect_result
from ..execution.container_instance import parse_container_logs_result
from ..execution.container_instance import parse_container_mount_proof_result
from ..execution.container_instance import parse_container_release_result
from ..execution.container_instance import parse_container_stage_proof_result
from ..execution.container_instance import parse_container_start_result
from ..execution.container_instance import parse_container_wait_result
from ..execution.container_plan import ContainerExecutionPlan
from ..execution.container_volume import ContainerVolumePreparationPlan
from ..execution.contracts import AppliedIsolationProof
from ..protocol.cancellation import CancellationSignal
from ..protocol.cancellation import RunCancelled
from .container_outcome import ContainerCleanupSummary
from .container_outcome import ContainerExecutionOutcome
from .container_process import ContainerCommandObservation
from .container_process import ContainerCommandRunner
from .container_projection import completed_process
from .container_projection import instance_failure_outcome
from .container_projection import released_failure_outcome
from .container_unwind import attach_container_outcome_to_control_error
from .container_unwind import close_container_after_exception
from .container_unwind import unwind_released_container
from .container_volume_runtime import ContainerVolumeRuntime


CleanupCallback = Callable[
    [ContainerCommandRunner, ContainerCleanupHandle],
    ContainerCleanupSummary,
]


def run_created_container_protocol(
    *,
    attempt_id: str,
    plan: ContainerExecutionPlan,
    created: ContainerCreatedInstance,
    runner: ContainerCommandRunner,
    volume_runtime: ContainerVolumeRuntime,
    volume_plan: ContainerVolumePreparationPlan,
    deadline: float,
    cancel_event: CancellationSignal | None,
    invoke: Callable[..., ContainerCommandObservation | None],
    invoke_instance_step: Callable[..., ContainerCommandObservation | None],
    cleanup: CleanupCallback,
    close_volumes: Callable[
        [ContainerExecutionOutcome],
        ContainerExecutionOutcome,
    ],
    cleanup_budget_seconds: float,
) -> ContainerExecutionOutcome:
    cancellation: RunCancelled | None = None
    proof: AppliedIsolationProof | None = None
    release_attempted: VerifiedContainerExecution | None = None
    release: ContainerReleasedExecution | None = None
    try:
        observed = invoke_instance_step(
            runner, "start", created.start_argv, deadline, cancel_event
        )
        if observed is None:
            return close_volumes(
                _instance_failure(
                    "start_not_run",
                    attempt_id,
                    runner,
                    created.cleanup,
                    cancel_event,
                    cleanup=cleanup,
                )
            )
        cancellation = observed.cancellation
        started_result = parse_container_start_result(created, observed.result)
        if started_result.started is None:
            return close_volumes(
                _instance_failure(
                    started_result.reason_code,
                    attempt_id,
                    runner,
                    created.cleanup,
                    cancellation,
                    cleanup=cleanup,
                )
            )
        started = started_result.started
        observed = invoke_instance_step(
            runner,
            "gate_ready",
            started.ready_argv,
            deadline,
            cancel_event,
            timeout_limit=started.ready_timeout_seconds,
        )
        if observed is None:
            return close_volumes(
                _instance_failure(
                    "gate_ready_not_run",
                    attempt_id,
                    runner,
                    created.cleanup,
                    cancel_event,
                    cleanup=cleanup,
                )
            )
        cancellation = observed.cancellation
        ready_result = parse_container_gate_ready_result(
            started,
            observed.result,
        )
        if ready_result.ready is None:
            return close_volumes(
                _instance_failure(
                    ready_result.reason_code,
                    attempt_id,
                    runner,
                    created.cleanup,
                    cancellation,
                    cleanup=cleanup,
                )
            )
        ready = ready_result.ready
        if plan.workspace_transport == "direct-bind":
            proof_step = "mount_proof"
            proof_argv = ready.mount_proof_argv
            proof_parser = parse_container_mount_proof_result
        else:
            proof_step = "stage_proof"
            proof_argv = ready.stage_proof_argv
            proof_parser = parse_container_stage_proof_result
        if proof_argv is None:
            return close_volumes(
                _instance_failure(
                    "workspace_proof_invalid",
                    attempt_id,
                    runner,
                    created.cleanup,
                    cancel_event,
                    cleanup=cleanup,
                )
            )
        observed = invoke_instance_step(
            runner,
            proof_step,
            proof_argv,
            deadline,
            cancel_event,
        )
        if observed is None:
            return close_volumes(
                _instance_failure(
                    f"{proof_step}_not_run",
                    attempt_id,
                    runner,
                    created.cleanup,
                    cancel_event,
                    cleanup=cleanup,
                )
            )
        cancellation = observed.cancellation
        mount_result = proof_parser(ready, observed.result)
        if mount_result.mounted is None:
            return close_volumes(
                _instance_failure(
                    mount_result.reason_code,
                    attempt_id,
                    runner,
                    created.cleanup,
                    cancellation,
                    cleanup=cleanup,
                )
            )
        mounted = mount_result.mounted
        observed = invoke_instance_step(
            runner,
            "inspect",
            mounted.inspect_argv,
            deadline,
            cancel_event,
        )
        if observed is None:
            return close_volumes(
                _instance_failure(
                    "inspect_not_run",
                    attempt_id,
                    runner,
                    created.cleanup,
                    cancel_event,
                    cleanup=cleanup,
                )
            )
        cancellation = observed.cancellation
        inspect_result = parse_container_inspect_result(
            mounted,
            observed.result,
        )
        if inspect_result.verified is None:
            return close_volumes(
                _instance_failure(
                    inspect_result.reason_code,
                    attempt_id,
                    runner,
                    created.cleanup,
                    cancellation,
                    cleanup=cleanup,
                )
            )
        verified = inspect_result.verified
        proof = verified.proof
        release_attempted = verified
        observed = invoke_instance_step(
            runner,
            "release",
            verified.release_argv,
            deadline,
            cancel_event,
        )
        if observed is None:
            release_attempted = None
            return close_volumes(
                _instance_failure(
                    "release_not_run",
                    attempt_id,
                    runner,
                    created.cleanup,
                    cancel_event,
                    proof=proof,
                    cleanup=cleanup,
                )
            )
        cancellation = observed.cancellation
        release_result = parse_container_release_result(
            verified,
            observed.result,
        )
        if release_result.released is None:
            return close_volumes(
                _released_failure(
                    release_result.reason_code,
                    attempt_id,
                    runner,
                    verified,
                    created.cleanup,
                    cancellation,
                    proof=proof,
                    release_verified=False,
                    invoke=invoke,
                    cleanup=cleanup,
                )
            )
        release = release_result.released
        observed = invoke_instance_step(
            runner,
            "wait",
            release.wait_argv,
            deadline,
            cancel_event,
        )
        if observed is None:
            return close_volumes(
                _released_failure(
                    "wait_not_run",
                    attempt_id,
                    runner,
                    release,
                    created.cleanup,
                    cancel_event,
                    proof=proof,
                    release_verified=True,
                    invoke=invoke,
                    cleanup=cleanup,
                )
            )
        cancellation = observed.cancellation
        wait_result = parse_container_wait_result(release, observed.result)
        if wait_result.waited is None:
            return close_volumes(
                _released_failure(
                    wait_result.reason_code,
                    attempt_id,
                    runner,
                    release,
                    created.cleanup,
                    cancellation,
                    proof=proof,
                    release_verified=True,
                    invoke=invoke,
                    cleanup=cleanup,
                )
            )
        waited = wait_result.waited
        observed = invoke_instance_step(
            runner,
            "final_inspect",
            waited.final_inspect_argv,
            deadline,
            cancel_event,
        )
        if observed is None:
            return close_volumes(
                _released_failure(
                    "final_inspect_not_run",
                    attempt_id,
                    runner,
                    release,
                    created.cleanup,
                    cancel_event,
                    proof=proof,
                    release_verified=True,
                    invoke=invoke,
                    cleanup=cleanup,
                )
            )
        cancellation = observed.cancellation
        final_result = parse_container_final_inspect_result(
            waited,
            observed.result,
        )
        if final_result.exited is None:
            return close_volumes(
                _released_failure(
                    final_result.reason_code,
                    attempt_id,
                    runner,
                    release,
                    created.cleanup,
                    cancellation,
                    proof=proof,
                    release_verified=True,
                    invoke=invoke,
                    cleanup=cleanup,
                )
            )
        exited = final_result.exited
        observed = invoke_instance_step(
            runner,
            "logs",
            exited.logs_argv,
            deadline,
            cancel_event,
        )
        if observed is None:
            return close_volumes(
                _released_failure(
                    "logs_not_run",
                    attempt_id,
                    runner,
                    release,
                    created.cleanup,
                    cancel_event,
                    proof=proof,
                    release_verified=True,
                    invoke=invoke,
                    cleanup=cleanup,
                )
            )
        cancellation = observed.cancellation
        logs_result = parse_container_logs_result(exited, observed.result)
        if logs_result.captured is None:
            return close_volumes(
                _released_failure(
                    logs_result.reason_code,
                    attempt_id,
                    runner,
                    release,
                    created.cleanup,
                    cancellation,
                    proof=proof,
                    release_verified=True,
                    invoke=invoke,
                    cleanup=cleanup,
                )
            )
        captured = logs_result.captured
    except BaseException as error:
        outcome = close_container_after_exception(
            error,
            attempt_id=attempt_id,
            runner=runner,
            cleanup_handle=created.cleanup,
            proof=proof,
            release_attempted=release_attempted,
            release=release,
            invoke=invoke,
            cleanup=cleanup,
        )
        outcome = close_volumes(outcome)
        if isinstance(error, RunCancelled) or not isinstance(error, Exception):
            attach_container_outcome_to_control_error(error, outcome)
            raise
        return outcome
    output_captured = volume_runtime.export_output(
        runner=runner,
        plan=volume_plan,
        execution_container_id=created.container_id,
        deadline=time.monotonic() + cleanup_budget_seconds,
        cancel_event=cancel_event,
    )
    container_cleanup = cleanup(runner, created.cleanup)
    completed = completed_process(captured)
    if not container_cleanup.verified:
        return close_volumes(
            ContainerExecutionOutcome(
                "cleanup_unverified",
                attempt_id,
                completed=completed,
                proof=proof,
                cleanup=container_cleanup,
                cancellation=cancellation,
                command_release_state="verified",
                execution_outcome="exited",
                workspace_output_captured=output_captured,
            )
        )
    return close_volumes(
        ContainerExecutionOutcome(
            (
                "container_execution_completed"
                if output_captured
                else "workspace_output_export_failed"
            ),
            attempt_id,
            completed=completed,
            proof=proof,
            cleanup=container_cleanup,
            cancellation=cancellation,
            command_release_state="verified",
            execution_outcome="exited",
            workspace_output_captured=output_captured,
        )
    )


def _instance_failure(
    reason_code: str,
    attempt_id: str,
    runner: ContainerCommandRunner,
    cleanup_handle: ContainerCleanupHandle,
    cancellation: RunCancelled | CancellationSignal | None,
    *,
    cleanup: CleanupCallback,
    proof: AppliedIsolationProof | None = None,
) -> ContainerExecutionOutcome:
    return instance_failure_outcome(
        reason_code,
        attempt_id,
        cleanup(runner, cleanup_handle),
        cancellation,
        proof=proof,
    )


def _released_failure(
    reason_code: str,
    attempt_id: str,
    runner: ContainerCommandRunner,
    execution: VerifiedContainerExecution | ContainerReleasedExecution,
    cleanup_handle: ContainerCleanupHandle,
    cancellation: RunCancelled | CancellationSignal | None,
    *,
    proof: AppliedIsolationProof,
    release_verified: bool,
    invoke: Callable[..., ContainerCommandObservation | None],
    cleanup: CleanupCallback,
) -> ContainerExecutionOutcome:
    unwind = unwind_released_container(
        runner,
        execution,
        invoke=invoke,
    )
    return released_failure_outcome(
        reason_code,
        attempt_id,
        cleanup(runner, cleanup_handle),
        cancellation,
        unwind,
        proof=proof,
        release_verified=release_verified,
    )


__all__ = ["run_created_container_protocol"]

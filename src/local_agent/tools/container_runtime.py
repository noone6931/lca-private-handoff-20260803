from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..execution.container_cleanup import ContainerCleanupHandle
from ..execution.container_cleanup import parse_container_removal_check_result
from ..execution.container_cleanup import parse_container_remove_result
from ..execution.container_instance import parse_container_create_result
from ..execution.container_plan import build_container_execution_draft
from ..execution.container_plan import parse_container_image_result
from ..execution.container_probe import build_docker_server_probe
from ..execution.container_probe import parse_container_probe_result
from ..execution.container_recovery import ContainerRecoveryObligation
from ..execution.container_recovery import build_container_recovery_obligation
from ..execution.container_staging import ContainerStagingAttempt
from ..execution.container_staging import ContainerStagingError
from ..execution.container_staging import recover_staging_authority
from ..execution.container_staging import record_staging_create_possible
from ..execution.container_staging import record_staging_container_absent
from ..execution.container_staging import record_staging_execution_absent
from ..execution.container_staging import (
    record_staging_execution_create_possible,
)
from ..execution.container_staging import run_staged_workspace_operation
from ..execution.container_staging_recovery import (
    build_staging_container_binding,
)
from ..execution.container_types import container_command_id
from ..execution.container_volume import ContainerVolumePreparationPlan
from ..execution.contracts import ContainerBackendAuthority
from ..execution.contracts import IsolationRequest
from ..protocol.cancellation import CancellationSignal
from ..protocol.cancellation import RunCancelled
from .container_process import ContainerCommandObservation
from .container_process import ContainerCommandRunner
from .container_outcome import ContainerCleanupSummary
from .container_outcome import ContainerExecutionOutcome
from .container_instance_runtime import run_created_container_protocol
from .container_projection import cleanup_summary
from .container_projection import container_closure_verified
from .container_projection import create_failure_outcome
from .container_projection import finalize_staged_outcome
from .container_projection import pre_instance_stop_outcome
from .container_recovery_runtime import recover_created_container
from .container_recovery_runtime import recover_durable_staging_container
from .container_unwind import ContainerUnwindSummary
from .container_volume_runtime import ContainerVolumeRuntime
from .process_runtime import run_process


_CONTROL_STEP_TIMEOUT_SECONDS = 30.0
_CLEANUP_BUDGET_SECONDS = 30.0


class ContainerExecutionRuntime:
    """Sequence Docker protocol facts through the existing process owner."""

    def __init__(
        self,
        authority: ContainerBackendAuthority,
        *,
        control_environment: Mapping[str, str],
        process_runner: Callable[..., Any] = run_process,
        attempt_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._authority = authority
        self._control_environment = control_environment
        self._process_runner = process_runner
        self._attempt_id_factory = attempt_id_factory
        self._sleeper = sleeper

    def execute(
        self,
        *,
        request: IsolationRequest,
        workspace_roots: tuple[Path, ...],
        workspace_roots_revision: int,
        working_directory: Path,
        command_argv: tuple[str, ...],
        timeout: float,
        cancel_event: CancellationSignal | None,
        forbidden_snapshot_directory_identities: frozenset[
            tuple[int, int]
        ] = frozenset(),
    ) -> ContainerExecutionOutcome:
        attempt_id = self._attempt_id_factory()
        if self._authority.workspace_transport == "direct-bind":
            return ContainerExecutionOutcome(
                "direct_bind_unsupported",
                attempt_id,
                workspace_transport="direct-bind",
            )
        staging_root = self._authority.staging_root
        if staging_root is None:
            return ContainerExecutionOutcome(
                "staging_authority_unconfigured",
                attempt_id,
                workspace_transport="staged-copy",
            )
        recovery = recover_staging_authority(
            staging_root=staging_root,
            workspace_roots=workspace_roots,
            workspace_roots_revision=workspace_roots_revision,
            recover_container=lambda binding, staging_state: (
                recover_durable_staging_container(
                    authority=self._authority,
                    binding=binding,
                    staging_state=staging_state,
                    workspace_roots=workspace_roots,
                    workspace_roots_revision=workspace_roots_revision,
                    control_environment=self._control_environment,
                    process_runner=self._process_runner,
                    invoke=self._invoke,
                    cleanup=self._cleanup,
                    sleeper=self._sleeper,
                )
            ),
        )
        if not recovery.ready:
            return ContainerExecutionOutcome(
                recovery.reason_code,
                attempt_id,
                staging_cleanup=ContainerCleanupSummary(
                    recovery.reason_code,
                    False,
                    True,
                ),
                workspace_transport="staged-copy",
                recovery_unresolved=True,
            )
        try:
            staged = run_staged_workspace_operation(
                staging_root=staging_root,
                workspace_roots=workspace_roots,
                workspace_roots_revision=workspace_roots_revision,
                attempt_id=attempt_id,
                profile=request.profile,
                operation=lambda staging: self._execute_transport(
                    attempt_id=attempt_id,
                    staging=staging,
                    request=request,
                    workspace_roots=workspace_roots,
                    workspace_roots_revision=workspace_roots_revision,
                    working_directory=working_directory,
                    command_argv=command_argv,
                    timeout=timeout,
                    cancel_event=cancel_event,
                ),
                cleanup_authorized=container_closure_verified,
                output_captured=lambda outcome: (
                    outcome.workspace_output_captured
                ),
                forbidden_directory_identities=(
                    forbidden_snapshot_directory_identities
                ),
            )
        except ContainerStagingError as exc:
            staging_cleanup = ContainerCleanupSummary(
                (
                    "staging_cleanup_verified_absent"
                    if exc.cleanup_verified
                    else "staging_cleanup_unverified"
                ),
                exc.cleanup_verified,
                not exc.cleanup_verified,
            )
            return ContainerExecutionOutcome(
                exc.kind,
                attempt_id,
                staging_cleanup=staging_cleanup,
                workspace_transport="staged-copy",
            )
        outcome = staged.value
        return finalize_staged_outcome(outcome, staged.output, staged.cleanup)

    def _execute_transport(
        self,
        *,
        attempt_id: str,
        staging: ContainerStagingAttempt | None,
        request: IsolationRequest,
        workspace_roots: tuple[Path, ...],
        workspace_roots_revision: int,
        working_directory: Path,
        command_argv: tuple[str, ...],
        timeout: float,
        cancel_event: CancellationSignal | None,
    ) -> ContainerExecutionOutcome:
        deadline = time.monotonic() + timeout
        try:
            probe_plan = build_docker_server_probe(
                attempt_id=attempt_id,
                workspace_roots=workspace_roots,
                workspace_roots_revision=workspace_roots_revision,
                executable=self._authority.executable,
                executable_sha256=self._authority.executable_sha256,
                socket_path=self._authority.socket_path,
                client_config_directory=self._authority.client_config_directory,
                gate_image=self._authority.gate_image,
            )
        except (OSError, ValueError):
            return ContainerExecutionOutcome("container_authority_invalid", attempt_id)
        runner = ContainerCommandRunner(
            attempt_id=attempt_id,
            workspace_roots=probe_plan.workspace_authority.roots,
            workspace_roots_revision=workspace_roots_revision,
            control_working_directory=probe_plan.endpoint.client_config_directory,
            control_environment=self._control_environment,
            process_runner=self._process_runner,
        )
        observed = self._invoke(
            runner,
            step="server",
            argv=probe_plan.argv,
            command_id=container_command_id(attempt_id, "server"),
            deadline=deadline,
            cancel_event=cancel_event,
            timeout_limit=probe_plan.timeout_seconds,
        )
        if observed is None:
            return pre_instance_stop_outcome(attempt_id, cancel_event)
        probe = parse_container_probe_result(probe_plan, observed.result)
        if probe.identity is None:
            return ContainerExecutionOutcome(
                probe.reason_code,
                attempt_id,
                cancellation=observed.cancellation,
                execution_outcome=(
                    "cancelled" if observed.cancellation is not None else "not_run"
                ),
            )
        try:
            draft = build_container_execution_draft(
                probe.identity,
                request,
                attempt_id=attempt_id,
                working_directory=working_directory,
                command_argv=command_argv,
                user_id=os.getuid(),
                group_id=os.getgid(),
                staging=staging,
            )
        except (OSError, ValueError):
            return ContainerExecutionOutcome("container_plan_invalid", attempt_id)
        observed = self._invoke(
            runner,
            step="image",
            argv=draft.image_inspect_argv,
            command_id=container_command_id(attempt_id, "image"),
            deadline=deadline,
            cancel_event=cancel_event,
        )
        if observed is None:
            return pre_instance_stop_outcome(attempt_id, cancel_event)
        image = parse_container_image_result(draft, observed.result)
        if image.plan is None:
            return ContainerExecutionOutcome(
                image.reason_code,
                attempt_id,
                cancellation=observed.cancellation,
                execution_outcome=(
                    "cancelled" if observed.cancellation is not None else "not_run"
                ),
            )
        plan = image.plan
        if staging is not None:
            transition = record_staging_create_possible(
                staging,
                build_staging_container_binding(plan),
            )
            if not transition.verified:
                return ContainerExecutionOutcome(
                    transition.reason_code,
                    attempt_id,
                    workspace_transport="staged-copy",
                )
        volume_runtime = ContainerVolumeRuntime(
            invoke=self._invoke,
            cleanup_container=self._cleanup,
        )
        preparation = volume_runtime.prepare(
            runner=runner,
            execution_plan=plan,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        if preparation.prepared is None:
            return ContainerExecutionOutcome(
                preparation.reason_code,
                attempt_id,
                cleanup=preparation.cleanup,
                recovery_unresolved=preparation.cleanup.unresolved,
                workspace_transport="staged-copy",
            )
        volume_plan = preparation.prepared
        assert staging is not None
        transition = record_staging_execution_create_possible(staging)
        if not transition.verified:
            volume_cleanup = volume_runtime.cleanup_volumes(
                runner=runner,
                plan=volume_plan,
                deadline=time.monotonic() + _CLEANUP_BUDGET_SECONDS,
            )
            unresolved_reason = (
                transition.reason_code
                if volume_cleanup.verified
                else volume_cleanup.reason_code
            )
            return ContainerExecutionOutcome(
                transition.reason_code,
                attempt_id,
                cleanup=ContainerCleanupSummary(
                    unresolved_reason,
                    False,
                    True,
                ),
                recovery_unresolved=True,
                workspace_transport="staged-copy",
            )

        def close_volumes(
            outcome: ContainerExecutionOutcome,
        ) -> ContainerExecutionOutcome:
            return self._close_volume_outcome(
                outcome,
                runner=runner,
                volume_runtime=volume_runtime,
                volume_plan=volume_plan,
                staging=staging,
            )

        observed = self._invoke(
            runner,
            step="create",
            argv=plan.create_argv,
            command_id=container_command_id(attempt_id, "create"),
            deadline=deadline,
            cancel_event=cancel_event,
        )
        if observed is not None and observed.parent_error is not None:
            return close_volumes(
                self._create_failure(
                    "create_parent_exception",
                    attempt_id,
                    runner,
                    build_container_recovery_obligation(plan, observed.result),
                    None,
                    unresolved_without_correlation=False,
                )
            )
        if observed is None:
            return close_volumes(
                pre_instance_stop_outcome(attempt_id, cancel_event)
            )
        try:
            created_result = parse_container_create_result(plan, observed.result)
        except (OSError, TypeError, ValueError):
            try:
                obligation = build_container_recovery_obligation(
                    plan,
                    observed.result,
                )
            except ValueError:
                obligation = None
            return close_volumes(
                self._create_failure(
                    "create_result_invalid",
                    attempt_id,
                    runner,
                    obligation,
                    observed.cancellation,
                    unresolved_without_correlation=obligation is None,
                )
            )
        if created_result.created is None:
            return close_volumes(
                self._create_failure(
                    created_result.reason_code,
                    attempt_id,
                    runner,
                    created_result.recovery,
                    observed.cancellation,
                    unresolved_without_correlation=(
                        created_result.unresolved_without_correlation
                    ),
                )
            )
        return run_created_container_protocol(
            attempt_id=attempt_id,
            plan=plan,
            created=created_result.created,
            runner=runner,
            volume_runtime=volume_runtime,
            volume_plan=volume_plan,
            deadline=deadline,
            cancel_event=cancel_event,
            invoke=self._invoke,
            invoke_instance_step=self._invoke_instance_step,
            cleanup=self._cleanup,
            close_volumes=close_volumes,
            cleanup_budget_seconds=_CLEANUP_BUDGET_SECONDS,
        )

    def _close_volume_outcome(
        self,
        outcome: ContainerExecutionOutcome,
        *,
        runner: ContainerCommandRunner,
        volume_runtime: ContainerVolumeRuntime,
        volume_plan: ContainerVolumePreparationPlan,
        staging: ContainerStagingAttempt,
    ) -> ContainerExecutionOutcome:
        container_cleanup = outcome.cleanup
        if (
            outcome.recovery_unresolved
            or container_cleanup is not None
            and not container_cleanup.verified
        ):
            return replace(outcome, recovery_unresolved=True)
        execution_absent = record_staging_execution_absent(staging)
        if not execution_absent.verified:
            return replace(
                outcome,
                reason_code=(
                    "cleanup_unverified"
                    if outcome.reason_code
                    == "container_execution_completed"
                    else outcome.reason_code
                ),
                cleanup=ContainerCleanupSummary(
                    execution_absent.reason_code,
                    False,
                    True,
                ),
                recovery_unresolved=True,
            )
        volume_cleanup = volume_runtime.cleanup_volumes(
            runner=runner,
            plan=volume_plan,
            deadline=time.monotonic() + _CLEANUP_BUDGET_SECONDS,
        )
        if not volume_cleanup.verified:
            return replace(
                outcome,
                reason_code=(
                    "cleanup_unverified"
                    if outcome.reason_code
                    == "container_execution_completed"
                    else outcome.reason_code
                ),
                cleanup=volume_cleanup,
                recovery_unresolved=True,
            )
        resources_absent = record_staging_container_absent(staging)
        if not resources_absent.verified:
            return replace(
                outcome,
                reason_code=(
                    "cleanup_unverified"
                    if outcome.reason_code
                    == "container_execution_completed"
                    else outcome.reason_code
                ),
                cleanup=ContainerCleanupSummary(
                    resources_absent.reason_code,
                    False,
                    True,
                ),
                recovery_unresolved=True,
            )
        return replace(
            outcome,
            cleanup=ContainerCleanupSummary(
                "container_resources_cleanup_verified",
                True,
                False,
            ),
        )

    def _invoke_instance_step(
        self,
        runner: ContainerCommandRunner,
        step,
        argv: tuple[str, ...],
        deadline: float,
        cancel_event: CancellationSignal | None,
        *,
        timeout_limit: float = _CONTROL_STEP_TIMEOUT_SECONDS,
    ) -> ContainerCommandObservation | None:
        observed = self._invoke(
            runner,
            step=step,
            argv=argv,
            command_id=container_command_id(runner.attempt_id, step),
            deadline=deadline,
            cancel_event=cancel_event,
            timeout_limit=timeout_limit,
        )
        if observed is not None and observed.parent_error is not None:
            raise observed.parent_error
        return observed

    def _invoke(
        self,
        runner: ContainerCommandRunner,
        *,
        step,
        argv: tuple[str, ...],
        command_id: str,
        deadline: float,
        cancel_event: CancellationSignal | None,
        timeout_limit: float = _CONTROL_STEP_TIMEOUT_SECONDS,
    ) -> ContainerCommandObservation | None:
        if cancel_event is not None and cancel_event.is_set():
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        return runner.run(
            command_id=command_id,
            step=step,
            argv=argv,
            timeout=min(timeout_limit, remaining),
            cancel_event=cancel_event,
        )

    def _create_failure(
        self,
        reason_code: str,
        attempt_id: str,
        runner: ContainerCommandRunner,
        obligation: ContainerRecoveryObligation | None,
        cancellation: RunCancelled | None,
        *,
        unresolved_without_correlation: bool,
    ) -> ContainerExecutionOutcome:
        recovery = recover_created_container(
            runner=runner,
            obligation=obligation,
            deadline=time.monotonic() + _CLEANUP_BUDGET_SECONDS,
            invoke=self._invoke,
            cleanup=self._cleanup,
            sleeper=self._sleeper,
        )
        return create_failure_outcome(
            reason_code,
            attempt_id,
            recovery,
            cancellation,
            unresolved_without_correlation=unresolved_without_correlation,
        )

    def _cleanup(
        self,
        runner: ContainerCommandRunner,
        cleanup: ContainerCleanupHandle,
    ) -> ContainerCleanupSummary:
        deadline = time.monotonic() + _CLEANUP_BUDGET_SECONDS
        remove = self._invoke(
            runner,
            step="remove",
            argv=cleanup.remove_argv,
            command_id=container_command_id(
                cleanup.plan.attempt_id,
                "remove",
                ordinal=cleanup.command_ordinal,
            ),
            deadline=deadline,
            cancel_event=None,
        )
        if remove is None:
            return ContainerCleanupSummary("remove_not_run", False, True)
        try:
            removed = parse_container_remove_result(cleanup, remove.result)
        except (OSError, TypeError, ValueError):
            return ContainerCleanupSummary(
                "remove_result_invalid",
                False,
                True,
            )
        checked = self._invoke(
            runner,
            step="removal_check",
            argv=removed.removal_check_argv,
            command_id=container_command_id(
                cleanup.plan.attempt_id,
                "removal_check",
                ordinal=cleanup.command_ordinal,
            ),
            deadline=deadline,
            cancel_event=None,
        )
        if checked is None:
            return ContainerCleanupSummary("cleanup_check_not_run", False, True)
        try:
            parsed = parse_container_removal_check_result(removed, checked.result)
            return cleanup_summary(
                parsed.reason_code,
                parsed.cleanup_verified,
                parsed.unresolved is not None,
            )
        except (OSError, TypeError, ValueError):
            return ContainerCleanupSummary(
                "cleanup_check_result_invalid",
                False,
                True,
            )

__all__ = [
    "ContainerCleanupSummary",
    "ContainerExecutionOutcome",
    "ContainerExecutionRuntime",
    "ContainerUnwindSummary",
]

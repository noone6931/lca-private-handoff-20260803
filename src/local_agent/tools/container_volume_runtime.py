from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..execution.container_staging import staged_input_snapshots_are_current
from ..execution.container_types import container_command_id
from ..execution.container_types import root_identity_matches
from ..execution.container_volume import ContainerVolumePreparationPlan
from ..execution.container_volume import build_volume_preparation_plan
from ..execution.container_volume import parse_copy_result
from ..execution.container_volume import parse_prep_create_result
from ..execution.container_volume import parse_prep_inspect_result
from ..execution.container_volume import parse_volume_absence_result
from ..execution.container_volume import parse_volume_create_result
from ..execution.container_volume import parse_volume_inspect_result
from ..execution.container_volume import parse_volume_remove_result
from ..protocol.cancellation import CancellationSignal
from .container_outcome import ContainerCleanupSummary
from .container_process import ContainerCommandObservation
from .container_process import ContainerCommandRunner


@dataclass(frozen=True)
class ContainerVolumePreparationResult:
    reason_code: str
    prepared: ContainerVolumePreparationPlan | None
    cleanup: ContainerCleanupSummary

    def __post_init__(self) -> None:
        if (
            not self.reason_code
            or (self.reason_code == "workspace_volumes_prepared")
            != (self.prepared is not None)
        ):
            raise ValueError("container volume preparation result is invalid")
        if self.prepared is not None and not self.cleanup.verified:
            raise ValueError("prepared volumes require closed prep resources")


class ContainerVolumeRuntime:
    """Compose Docker volume commands through the runtime's process owner."""

    def __init__(
        self,
        *,
        invoke: Callable[..., ContainerCommandObservation | None],
        cleanup_container: Callable[..., ContainerCleanupSummary],
    ) -> None:
        self._invoke = invoke
        self._cleanup_container = cleanup_container

    def prepare(
        self,
        *,
        runner: ContainerCommandRunner,
        execution_plan,
        deadline: float,
        cancel_event: CancellationSignal | None,
    ) -> ContainerVolumePreparationResult:
        plan = build_volume_preparation_plan(execution_plan)
        if not staged_input_snapshots_are_current(plan.staging):
            return self._failed(
                "stage_source_snapshot_changed",
                ContainerCleanupSummary(
                    "volume_resources_not_created",
                    True,
                    False,
                ),
            )
        created_roots = []
        for root in plan.roots:
            observed = self._run(
                runner,
                plan,
                step="volume_create",
                ordinal=root.ordinal + 1,
                argv=plan.volume_create_argv(root),
                deadline=deadline,
                cancel_event=cancel_event,
            )
            if observed is None:
                return self._failed(
                    "volume_create_not_run",
                    self._unknown_cleanup("volume_create_unresolved"),
                )
            created = parse_volume_create_result(plan, root, observed.result)
            if not created.verified:
                return self._failed(
                    created.reason_code,
                    self._unknown_cleanup("volume_create_unresolved"),
                )
            created_roots.append(root)
            inspected = self._run(
                runner,
                plan,
                step="volume_inspect",
                ordinal=root.ordinal + 1,
                argv=plan.volume_inspect_argv(root),
                deadline=deadline,
                cancel_event=cancel_event,
            )
            if inspected is None:
                return self._failed(
                    "volume_inspect_not_run",
                    self._cleanup_volumes(
                        runner,
                        plan,
                        tuple(created_roots),
                        deadline,
                    ),
                )
            proof = parse_volume_inspect_result(
                plan,
                root,
                inspected.result,
                command_ordinal=root.ordinal + 1,
            )
            if not proof.verified:
                return self._failed(
                    proof.reason_code,
                    self._unknown_cleanup("volume_ownership_unresolved"),
                )
        prep = self._run(
            runner,
            plan,
            step="prep_create",
            ordinal=1,
            argv=plan.prep_create_argv,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        if prep is None:
            return self._failed(
                "prep_create_not_run",
                self._unknown_cleanup("prep_create_unresolved"),
            )
        created_prep = parse_prep_create_result(plan, prep.result)
        if created_prep.container_id is None:
            return self._failed(
                created_prep.reason_code,
                self._unknown_cleanup("prep_create_unresolved"),
            )
        prep_id = created_prep.container_id
        inspected = self._run(
            runner,
            plan,
            step="prep_inspect",
            ordinal=1,
            argv=plan.prep_inspect_argv(prep_id),
            deadline=deadline,
            cancel_event=cancel_event,
        )
        if inspected is None:
            return self._failed(
                "prep_inspect_not_run",
                self._close_prep_and_volumes(
                    runner,
                    plan,
                    prep_id,
                    deadline,
                ),
            )
        prep_proof = parse_prep_inspect_result(plan, prep_id, inspected.result)
        if not prep_proof.verified:
            return self._failed(
                prep_proof.reason_code,
                self._close_prep_and_volumes(
                    runner,
                    plan,
                    prep_id,
                    deadline,
                ),
            )
        for root in plan.roots:
            argv = plan.stage_copy_argv(root, prep_id)
            copied = self._run(
                runner,
                plan,
                step="stage_copy",
                ordinal=root.ordinal + 1,
                argv=argv,
                deadline=deadline,
                cancel_event=cancel_event,
            )
            if copied is None:
                return self._failed(
                    "stage_copy_not_run",
                    self._close_prep_and_volumes(
                        runner,
                        plan,
                        prep_id,
                        deadline,
                    ),
                )
            result = parse_copy_result(
                plan,
                root,
                step="stage_copy",
                command_ordinal=root.ordinal + 1,
                argv=argv,
                result=copied.result,
            )
            if not result.verified:
                return self._failed(
                    result.reason_code,
                    self._close_prep_and_volumes(
                        runner,
                        plan,
                        prep_id,
                        deadline,
                    ),
                )
        if not staged_input_snapshots_are_current(plan.staging):
            return self._failed(
                "stage_source_snapshot_changed",
                self._close_prep_and_volumes(
                    runner,
                    plan,
                    prep_id,
                    deadline,
                ),
            )
        prep_cleanup = self._cleanup_container(
            runner,
            plan.prep_cleanup(prep_id),
        )
        if not prep_cleanup.verified:
            return self._failed(
                "prep_cleanup_unverified",
                prep_cleanup,
            )
        return ContainerVolumePreparationResult(
            "workspace_volumes_prepared",
            plan,
            prep_cleanup,
        )

    def export_output(
        self,
        *,
        runner: ContainerCommandRunner,
        plan: ContainerVolumePreparationPlan,
        execution_container_id: str,
        deadline: float,
        cancel_event: CancellationSignal | None,
    ) -> bool:
        for root in plan.roots:
            if not root_identity_matches(root.output_path, root.output_identity):
                return False
            argv = plan.output_copy_argv(root, execution_container_id)
            observed = self._run(
                runner,
                plan,
                step="output_copy",
                ordinal=root.ordinal + 1,
                argv=argv,
                deadline=deadline,
                cancel_event=cancel_event,
            )
            if observed is None:
                return False
            copied = parse_copy_result(
                plan,
                root,
                step="output_copy",
                command_ordinal=root.ordinal + 1,
                argv=argv,
                result=observed.result,
            )
            if (
                not copied.verified
                or not root_identity_matches(
                    root.output_path,
                    root.output_identity,
                )
            ):
                return False
        return plan.staging.authority_is_current()

    def cleanup_volumes(
        self,
        *,
        runner: ContainerCommandRunner,
        plan: ContainerVolumePreparationPlan,
        deadline: float,
    ) -> ContainerCleanupSummary:
        return self._cleanup_volumes(
            runner,
            plan,
            plan.roots,
            deadline,
        )

    def _close_prep_and_volumes(
        self,
        runner: ContainerCommandRunner,
        plan: ContainerVolumePreparationPlan,
        prep_id: str,
        deadline: float,
    ) -> ContainerCleanupSummary:
        prep_cleanup = self._cleanup_container(
            runner,
            plan.prep_cleanup(prep_id),
        )
        if not prep_cleanup.verified:
            return prep_cleanup
        return self._cleanup_volumes(
            runner,
            plan,
            plan.roots,
            deadline,
        )

    def _cleanup_volumes(
        self,
        runner: ContainerCommandRunner,
        plan: ContainerVolumePreparationPlan,
        roots,
        deadline: float,
    ) -> ContainerCleanupSummary:
        for root in reversed(tuple(roots)):
            inspect_ordinal = len(plan.roots) + root.ordinal + 1
            observed = self._run(
                runner,
                plan,
                step="volume_inspect",
                ordinal=inspect_ordinal,
                argv=plan.volume_inspect_argv(root),
                deadline=deadline,
                cancel_event=None,
            )
            if observed is None:
                return self._unknown_cleanup("volume_cleanup_inspect_not_run")
            inspected = parse_volume_inspect_result(
                plan,
                root,
                observed.result,
                command_ordinal=inspect_ordinal,
            )
            if not inspected.verified:
                return self._unknown_cleanup(inspected.reason_code)
            removed = self._run(
                runner,
                plan,
                step="volume_remove",
                ordinal=root.ordinal + 1,
                argv=plan.volume_remove_argv(root),
                deadline=deadline,
                cancel_event=None,
            )
            if removed is None:
                return self._unknown_cleanup("volume_remove_not_run")
            attempted = parse_volume_remove_result(
                plan,
                root,
                removed.result,
            )
            checked = self._run(
                runner,
                plan,
                step="volume_removal_check",
                ordinal=root.ordinal + 1,
                argv=plan.volume_absence_argv(root),
                deadline=deadline,
                cancel_event=None,
            )
            if checked is None:
                return self._unknown_cleanup(
                    "volume_removal_check_not_run"
                )
            absent = parse_volume_absence_result(
                plan,
                root,
                checked.result,
            )
            if not absent.verified:
                return self._unknown_cleanup(
                    (
                        absent.reason_code
                        if attempted.verified
                        else attempted.reason_code
                    )
                )
        return ContainerCleanupSummary(
            "workspace_volumes_cleanup_verified",
            True,
            False,
        )

    def _run(
        self,
        runner: ContainerCommandRunner,
        plan: ContainerVolumePreparationPlan,
        *,
        step: str,
        ordinal: int,
        argv: tuple[str, ...],
        deadline: float,
        cancel_event: CancellationSignal | None,
    ) -> ContainerCommandObservation | None:
        return self._invoke(
            runner,
            step=step,
            argv=argv,
            command_id=container_command_id(
                plan.attempt_id,
                step,
                ordinal=ordinal,
            ),
            deadline=deadline,
            cancel_event=cancel_event,
        )

    @staticmethod
    def _failed(
        reason_code: str,
        cleanup: ContainerCleanupSummary,
    ) -> ContainerVolumePreparationResult:
        return ContainerVolumePreparationResult(
            reason_code,
            None,
            cleanup,
        )

    @staticmethod
    def _unknown_cleanup(reason_code: str) -> ContainerCleanupSummary:
        return ContainerCleanupSummary(reason_code, False, True)


__all__ = [
    "ContainerVolumePreparationResult",
    "ContainerVolumeRuntime",
]

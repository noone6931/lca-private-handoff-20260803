from __future__ import annotations

from collections.abc import Callable

from ..execution.container_staging_contracts import (
    ContainerStagingContainerBinding,
)
from ..execution.container_types import ContainerEngineIdentity
from ..execution.container_types import container_command_id
from ..execution.container_volume_recovery import (
    build_durable_volume_recovery_plan,
)
from ..execution.container_volume_recovery import (
    parse_durable_volume_absence_result,
)
from ..execution.container_volume_recovery import (
    parse_durable_volume_recovery_inspect_result,
)
from ..execution.container_volume_recovery import (
    parse_durable_volume_recovery_query_result,
)
from ..execution.container_volume_recovery import (
    parse_durable_volume_remove_result,
)
from .container_outcome import ContainerCleanupSummary
from .container_process import ContainerCommandObservation
from .container_process import ContainerCommandRunner


def recover_durable_volume_resource(
    *,
    binding: ContainerStagingContainerBinding,
    identity: ContainerEngineIdentity,
    root_ordinal: int,
    absence_allowed: bool,
    runner: ContainerCommandRunner,
    deadline: float,
    invoke: Callable[..., ContainerCommandObservation | None],
    sleeper: Callable[[float], None],
) -> ContainerCleanupSummary:
    try:
        plan = build_durable_volume_recovery_plan(
            binding,
            identity,
            root_ordinal=root_ordinal,
            absence_allowed=absence_allowed,
        )
    except ValueError:
        return ContainerCleanupSummary(
            "volume_recovery_authority_changed",
            False,
            True,
        )
    while True:
        if plan.retry_after_seconds:
            sleeper(plan.retry_after_seconds)
        observed = invoke(
            runner,
            step="resource_recovery_query",
            argv=plan.query_argv,
            command_id=plan.query_command_id,
            deadline=deadline,
            cancel_event=None,
        )
        if observed is None:
            return ContainerCleanupSummary(
                "volume_recovery_query_not_run",
                False,
                True,
            )
        try:
            query = parse_durable_volume_recovery_query_result(
                plan,
                observed.result,
            )
        except (OSError, TypeError, ValueError):
            return ContainerCleanupSummary(
                "volume_recovery_query_result_invalid",
                False,
                True,
            )
        if query.retry is not None:
            plan = query.retry
            continue
        if query.absent_verified:
            return ContainerCleanupSummary(
                query.reason_code,
                True,
                False,
            )
        if query.unresolved:
            return ContainerCleanupSummary(query.reason_code, False, True)
        assert query.candidate is not None
        candidate = query.candidate
        observed = invoke(
            runner,
            step="resource_recovery_inspect",
            argv=candidate.inspect_argv,
            command_id=candidate.inspect_command_id,
            deadline=deadline,
            cancel_event=None,
        )
        if observed is None:
            return ContainerCleanupSummary(
                "volume_recovery_inspect_not_run",
                False,
                True,
            )
        try:
            inspected = parse_durable_volume_recovery_inspect_result(
                candidate,
                observed.result,
            )
        except (OSError, TypeError, ValueError):
            return ContainerCleanupSummary(
                "volume_recovery_inspect_result_invalid",
                False,
                True,
            )
        if inspected.retry is not None:
            plan = inspected.retry
            continue
        if inspected.unresolved:
            return ContainerCleanupSummary(
                inspected.reason_code,
                False,
                True,
            )
        assert inspected.cleanup is not None
        cleanup_handle = inspected.cleanup
        removed = invoke(
            runner,
            step="volume_remove",
            argv=cleanup_handle.remove_argv,
            command_id=container_command_id(
                plan.attempt_id,
                "volume_remove",
                ordinal=root_ordinal + 1,
            ),
            deadline=deadline,
            cancel_event=None,
        )
        if removed is None:
            return ContainerCleanupSummary(
                "volume_remove_not_run",
                False,
                True,
            )
        try:
            attempted = parse_durable_volume_remove_result(
                cleanup_handle,
                removed.result,
            )
        except (OSError, TypeError, ValueError):
            return ContainerCleanupSummary(
                "volume_remove_result_invalid",
                False,
                True,
            )
        checked = invoke(
            runner,
            step="volume_removal_check",
            argv=cleanup_handle.absence_argv,
            command_id=container_command_id(
                plan.attempt_id,
                "volume_removal_check",
                ordinal=root_ordinal + 1,
            ),
            deadline=deadline,
            cancel_event=None,
        )
        if checked is None:
            return ContainerCleanupSummary(
                "volume_removal_check_not_run",
                False,
                True,
            )
        try:
            absent = parse_durable_volume_absence_result(
                cleanup_handle,
                checked.result,
            )
        except (OSError, TypeError, ValueError):
            return ContainerCleanupSummary(
                "volume_removal_check_result_invalid",
                False,
                True,
            )
        if absent.verified:
            return ContainerCleanupSummary(
                absent.reason_code,
                True,
                False,
            )
        return ContainerCleanupSummary(
            (
                absent.reason_code
                if attempted.verified
                else attempted.reason_code
            ),
            False,
            True,
        )


__all__ = ["recover_durable_volume_resource"]

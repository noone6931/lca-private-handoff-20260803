from __future__ import annotations

from collections.abc import Callable

from ..execution.container_cleanup import ContainerCleanupHandle
from ..execution.container_durable_recovery import (
    build_durable_container_recovery_plan,
)
from ..execution.container_durable_recovery import (
    parse_durable_recovery_inspect_result,
)
from ..execution.container_durable_recovery import (
    parse_durable_recovery_query_result,
)
from ..execution.container_staging_contracts import (
    ContainerStagingContainerBinding,
)
from ..execution.container_types import ContainerEngineIdentity
from .container_outcome import ContainerCleanupSummary
from .container_process import ContainerCommandObservation
from .container_process import ContainerCommandRunner


def recover_durable_container_resource(
    *,
    binding: ContainerStagingContainerBinding,
    identity: ContainerEngineIdentity,
    resource: str,
    runner: ContainerCommandRunner,
    deadline: float,
    invoke: Callable[..., ContainerCommandObservation | None],
    cleanup: Callable[
        [ContainerCommandRunner, ContainerCleanupHandle],
        ContainerCleanupSummary,
    ],
    sleeper: Callable[[float], None],
) -> ContainerCleanupSummary:
    try:
        plan = build_durable_container_recovery_plan(
            binding,
            identity,
            resource=resource,
        )
    except ValueError:
        return ContainerCleanupSummary(
            "staging_recovery_authority_changed",
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
                "staging_recovery_query_not_run",
                False,
                True,
            )
        try:
            query = parse_durable_recovery_query_result(
                plan,
                observed.result,
            )
        except (OSError, TypeError, ValueError):
            return ContainerCleanupSummary(
                "staging_recovery_query_result_invalid",
                False,
                True,
            )
        if query.retry is not None:
            plan = query.retry
            continue
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
                "staging_recovery_inspect_not_run",
                False,
                True,
            )
        try:
            inspected = parse_durable_recovery_inspect_result(
                candidate,
                observed.result,
            )
        except (OSError, TypeError, ValueError):
            return ContainerCleanupSummary(
                "staging_recovery_inspect_result_invalid",
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
        return cleanup(runner, inspected.cleanup)


__all__ = ["recover_durable_container_resource"]

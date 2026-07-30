from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..execution.container_cleanup import ContainerCleanupHandle
from ..execution.container_probe import build_docker_server_probe
from ..execution.container_probe import parse_container_probe_result
from ..execution.container_recovery import ContainerRecoveryObligation
from ..execution.container_recovery import (
    parse_container_recovery_inspect_result,
)
from ..execution.container_recovery import (
    parse_container_recovery_query_result,
)
from ..execution.container_staging_contracts import (
    ContainerStagingContainerBinding,
)
from ..execution.container_staging_contracts import (
    ContainerStagingContainerRecoveryResult,
)
from ..execution.container_types import container_command_id
from ..execution.contracts import ContainerBackendAuthority
from .container_outcome import ContainerCleanupSummary
from .container_process import ContainerCommandObservation
from .container_process import ContainerCommandRunner
from .container_resource_recovery_runtime import (
    recover_durable_container_resource,
)
from .container_volume_recovery_runtime import (
    recover_durable_volume_resource,
)


_RECOVERY_BUDGET_SECONDS = 30.0


def recover_created_container(
    *,
    runner: ContainerCommandRunner,
    obligation: ContainerRecoveryObligation | None,
    deadline: float,
    invoke: Callable[..., ContainerCommandObservation | None],
    cleanup: Callable[
        [ContainerCommandRunner, ContainerCleanupHandle],
        ContainerCleanupSummary,
    ],
    sleeper: Callable[[float], None],
) -> ContainerCleanupSummary | None:
    while obligation is not None:
        if obligation.retry_after_seconds:
            sleeper(obligation.retry_after_seconds)
        observed = invoke(
            runner,
            step="recovery_query",
            argv=obligation.query_argv,
            command_id=obligation.query_command_id,
            deadline=deadline,
            cancel_event=None,
        )
        if observed is None:
            return ContainerCleanupSummary(
                "recovery_query_not_run",
                False,
                True,
            )
        try:
            query = parse_container_recovery_query_result(
                obligation,
                observed.result,
            )
        except (OSError, TypeError, ValueError):
            return ContainerCleanupSummary(
                "recovery_query_result_invalid",
                False,
                True,
            )
        if query.retry is not None:
            obligation = query.retry
            continue
        if query.unresolved is not None:
            return ContainerCleanupSummary(query.reason_code, False, True)
        assert query.candidate is not None
        candidate = query.candidate
        observed = invoke(
            runner,
            step="recovery_inspect",
            argv=candidate.inspect_argv,
            command_id=candidate.inspect_command_id,
            deadline=deadline,
            cancel_event=None,
        )
        if observed is None:
            return ContainerCleanupSummary(
                "recovery_inspect_not_run",
                False,
                True,
            )
        try:
            inspected = parse_container_recovery_inspect_result(
                candidate,
                observed.result,
            )
        except (OSError, TypeError, ValueError):
            return ContainerCleanupSummary(
                "recovery_inspect_result_invalid",
                False,
                True,
            )
        if inspected.retry is not None:
            obligation = inspected.retry
            continue
        if inspected.unresolved is not None:
            return ContainerCleanupSummary(inspected.reason_code, False, True)
        assert inspected.cleanup is not None
        return cleanup(runner, inspected.cleanup)
    return None


def recover_durable_staging_container(
    *,
    authority: ContainerBackendAuthority,
    binding: ContainerStagingContainerBinding,
    staging_state: str,
    workspace_roots: tuple[Path, ...],
    workspace_roots_revision: int,
    control_environment: Mapping[str, str],
    process_runner: Callable[..., Any],
    invoke: Callable[..., ContainerCommandObservation | None],
    cleanup: Callable[
        [ContainerCommandRunner, ContainerCleanupHandle],
        ContainerCleanupSummary,
    ],
    sleeper: Callable[[float], None],
) -> ContainerStagingContainerRecoveryResult:
    attempt_id = binding.instance_name.removeprefix("lca-")
    try:
        probe_plan = build_docker_server_probe(
            attempt_id=attempt_id,
            workspace_roots=workspace_roots,
            workspace_roots_revision=workspace_roots_revision,
            executable=authority.executable,
            executable_sha256=authority.executable_sha256,
            socket_path=authority.socket_path,
            client_config_directory=authority.client_config_directory,
            gate_image=authority.gate_image,
        )
    except (OSError, ValueError):
        return _unresolved("staging_recovery_authority_invalid")
    runner = ContainerCommandRunner(
        attempt_id=attempt_id,
        workspace_roots=probe_plan.workspace_authority.roots,
        workspace_roots_revision=workspace_roots_revision,
        control_working_directory=probe_plan.endpoint.client_config_directory,
        control_environment=control_environment,
        process_runner=process_runner,
    )
    deadline = time.monotonic() + _RECOVERY_BUDGET_SECONDS
    observed = invoke(
        runner,
        step="server",
        argv=probe_plan.argv,
        command_id=container_command_id(attempt_id, "server"),
        deadline=deadline,
        cancel_event=None,
        timeout_limit=probe_plan.timeout_seconds,
    )
    if observed is None:
        return _unresolved("staging_recovery_probe_not_run")
    probe = parse_container_probe_result(probe_plan, observed.result)
    if probe.identity is None:
        return _unresolved(probe.reason_code)
    identity = probe.identity
    if staging_state == "create_possible":
        container_resource = "prep"
        absence_allowed = False
    elif staging_state == "execution_create_possible":
        container_resource = "execution"
        absence_allowed = False
    elif staging_state == "execution_absent":
        container_resource = None
        absence_allowed = True
    else:
        return _unresolved("staging_recovery_state_invalid")
    if container_resource is not None:
        cleaned = recover_durable_container_resource(
            binding=binding,
            identity=identity,
            resource=container_resource,
            runner=runner,
            deadline=deadline,
            invoke=invoke,
            cleanup=cleanup,
            sleeper=sleeper,
        )
        if not cleaned.verified:
            return ContainerStagingContainerRecoveryResult(
                cleaned.reason_code,
                False,
                True,
            )
    for root_ordinal in range(len(binding.volume_names)):
        cleaned = recover_durable_volume_resource(
            binding=binding,
            identity=identity,
            root_ordinal=root_ordinal,
            absence_allowed=absence_allowed,
            runner=runner,
            deadline=deadline,
            invoke=invoke,
            sleeper=sleeper,
        )
        if not cleaned.verified:
            return ContainerStagingContainerRecoveryResult(
                cleaned.reason_code,
                False,
                True,
            )
    return ContainerStagingContainerRecoveryResult(
        "staging_resources_cleanup_verified",
        True,
        False,
    )


def _unresolved(reason_code: str) -> ContainerStagingContainerRecoveryResult:
    return ContainerStagingContainerRecoveryResult(
        reason_code,
        False,
        True,
    )


__all__ = [
    "recover_created_container",
    "recover_durable_staging_container",
]

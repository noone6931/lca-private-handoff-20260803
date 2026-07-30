from __future__ import annotations

import json
from dataclasses import dataclass

from .container_plan import CONTAINER_INSTANCE_LABEL
from .container_plan import CONTAINER_RESOURCE_LABEL
from .container_recovery import RECOVERY_QUERY_ATTEMPT_LIMIT
from .container_recovery import RECOVERY_RETRY_AFTER_SECONDS
from .container_staging_contracts import ContainerStagingContainerBinding
from .container_staging_recovery import staging_container_binding_matches
from .container_types import ContainerCommandResult
from .container_types import ContainerEngineIdentity
from .container_types import command_output_is_complete
from .container_types import command_workspace_authority_matches
from .container_types import container_command_id
from .container_types import validate_attempt_id
from .container_volume import CONTAINER_VOLUME_RESOURCE_PREFIX
from .container_volume import ContainerResourceResult
from .container_volume import parse_exact_volume_absence
from .container_volume import volume_inspect_argv
from .container_volume import volume_payload_matches
from .container_volume import volume_query_argv


_MAX_RECOVERY_OUTPUT_CHARS = 16_384
_RETRY_AFTER_NS = int(RECOVERY_RETRY_AFTER_SECONDS * 1_000_000_000)


@dataclass(frozen=True)
class DurableVolumeRecoveryPlan:
    binding: ContainerStagingContainerBinding
    identity: ContainerEngineIdentity
    attempt_id: str
    root_ordinal: int
    absence_allowed: bool
    query_ordinal: int
    query_command_id: str
    minimum_event_sequence: int
    not_before_monotonic_ns: int
    query_argv: tuple[str, ...]
    retry_after_seconds: float

    def __post_init__(self) -> None:
        validate_attempt_id(self.attempt_id)
        if (
            self.binding.instance_name != f"lca-{self.attempt_id}"
            or not staging_container_binding_matches(
                self.binding,
                self.identity,
            )
            or not 0 <= self.root_ordinal < len(self.binding.volume_names)
            or not isinstance(self.absence_allowed, bool)
            or not 1
            <= self.query_ordinal
            <= RECOVERY_QUERY_ATTEMPT_LIMIT
            or self.query_command_id
            != container_command_id(
                self.attempt_id,
                "resource_recovery_query",
                ordinal=self.command_ordinal,
            )
            or self.minimum_event_sequence < 1
            or self.not_before_monotonic_ns < 0
            or self.query_argv
            != volume_query_argv(self.identity, self.volume_name)
        ):
            raise ValueError("durable volume recovery plan is invalid")
        expected_delay = (
            0.0
            if self.query_ordinal == 1
            else RECOVERY_RETRY_AFTER_SECONDS
        )
        if self.retry_after_seconds != expected_delay:
            raise ValueError("durable volume recovery delay is invalid")

    @property
    def volume_name(self) -> str:
        return self.binding.volume_names[self.root_ordinal]

    @property
    def resource_label(self) -> str:
        return (
            f"{CONTAINER_VOLUME_RESOURCE_PREFIX}{self.root_ordinal:04d}"
        )

    @property
    def command_ordinal(self) -> int:
        return (
            (2 + self.root_ordinal) * RECOVERY_QUERY_ATTEMPT_LIMIT
            + self.query_ordinal
        )


@dataclass(frozen=True)
class DurableVolumeRecoveryCandidate:
    plan: DurableVolumeRecoveryPlan
    inspect_command_id: str
    minimum_event_sequence: int
    not_before_monotonic_ns: int
    inspect_argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            self.inspect_command_id
            != container_command_id(
                self.plan.attempt_id,
                "resource_recovery_inspect",
                ordinal=self.plan.command_ordinal,
            )
            or self.minimum_event_sequence
            < self.plan.minimum_event_sequence
            or self.not_before_monotonic_ns
            < self.plan.not_before_monotonic_ns
            or self.inspect_argv
            != volume_inspect_argv(
                self.plan.identity,
                self.plan.volume_name,
            )
        ):
            raise ValueError(
                "durable volume recovery candidate is invalid"
            )


@dataclass(frozen=True)
class DurableVolumeCleanupHandle:
    plan: DurableVolumeRecoveryPlan
    remove_argv: tuple[str, ...]
    absence_argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            self.remove_argv != _volume_remove_argv(self.plan)
            or self.absence_argv
            != volume_query_argv(
                self.plan.identity,
                self.plan.volume_name,
            )
        ):
            raise ValueError("durable volume cleanup handle is invalid")


@dataclass(frozen=True)
class DurableVolumeRecoveryQueryResult:
    reason_code: str
    retry: DurableVolumeRecoveryPlan | None = None
    candidate: DurableVolumeRecoveryCandidate | None = None
    absent_verified: bool = False
    unresolved: bool = False

    def __post_init__(self) -> None:
        if (
            not self.reason_code
            or sum(
                (
                    self.retry is not None,
                    self.candidate is not None,
                    self.absent_verified,
                    self.unresolved,
                )
            )
            != 1
        ):
            raise ValueError(
                "durable volume recovery query result is invalid"
            )


@dataclass(frozen=True)
class DurableVolumeRecoveryInspectResult:
    reason_code: str
    cleanup: DurableVolumeCleanupHandle | None = None
    retry: DurableVolumeRecoveryPlan | None = None
    unresolved: bool = False

    def __post_init__(self) -> None:
        if (
            not self.reason_code
            or sum(
                (
                    self.cleanup is not None,
                    self.retry is not None,
                    self.unresolved,
                )
            )
            != 1
        ):
            raise ValueError(
                "durable volume recovery inspect result is invalid"
            )


def build_durable_volume_recovery_plan(
    binding: ContainerStagingContainerBinding,
    identity: ContainerEngineIdentity,
    *,
    root_ordinal: int,
    absence_allowed: bool = False,
) -> DurableVolumeRecoveryPlan:
    attempt_id = binding.instance_name.removeprefix("lca-")
    validate_attempt_id(attempt_id)
    if not staging_container_binding_matches(binding, identity):
        raise ValueError("durable volume recovery authority changed")
    if not 0 <= root_ordinal < len(binding.volume_names):
        raise ValueError("durable volume recovery root is invalid")
    return DurableVolumeRecoveryPlan(
        binding=binding,
        identity=identity,
        attempt_id=attempt_id,
        root_ordinal=root_ordinal,
        absence_allowed=absence_allowed,
        query_ordinal=1,
        query_command_id=container_command_id(
            attempt_id,
            "resource_recovery_query",
            ordinal=(
                (2 + root_ordinal) * RECOVERY_QUERY_ATTEMPT_LIMIT + 1
            ),
        ),
        minimum_event_sequence=1,
        not_before_monotonic_ns=0,
        query_argv=volume_query_argv(
            identity,
            binding.volume_names[root_ordinal],
        ),
        retry_after_seconds=0.0,
    )


def parse_durable_volume_recovery_query_result(
    plan: DurableVolumeRecoveryPlan,
    result: ContainerCommandResult,
) -> DurableVolumeRecoveryQueryResult:
    failure = _result_failure(
        plan,
        step="resource_recovery_query",
        command_id=plan.query_command_id,
        argv=plan.query_argv,
        minimum_event_sequence=plan.minimum_event_sequence,
        not_before_monotonic_ns=plan.not_before_monotonic_ns,
        result=result,
    )
    if failure is not None:
        if failure in {
            "failed",
            "spawn_failed",
            "parent_failed",
            "timed_out",
            "cancelled",
        }:
            return _query_retry(
                plan,
                result,
                f"volume_recovery_query_{failure}",
            )
        return DurableVolumeRecoveryQueryResult(
            f"volume_recovery_query_{failure}",
            unresolved=True,
        )
    if result.stderr or len(result.stdout) > _MAX_RECOVERY_OUTPUT_CHARS:
        return DurableVolumeRecoveryQueryResult(
            "volume_recovery_query_output_invalid",
            unresolved=True,
        )
    if not result.stdout:
        if plan.absence_allowed:
            return DurableVolumeRecoveryQueryResult(
                "volume_recovery_absence_verified",
                absent_verified=True,
            )
        return _query_retry(
            plan,
            result,
            "volume_recovery_absence_unverified",
        )
    try:
        names = tuple(json.loads(line) for line in result.stdout.splitlines())
    except json.JSONDecodeError:
        return DurableVolumeRecoveryQueryResult(
            "volume_recovery_query_output_invalid",
            unresolved=True,
        )
    if names != (plan.volume_name,):
        return DurableVolumeRecoveryQueryResult(
            "volume_recovery_query_ambiguous",
            unresolved=True,
        )
    return DurableVolumeRecoveryQueryResult(
        "volume_recovery_candidate_observed",
        candidate=DurableVolumeRecoveryCandidate(
            plan=plan,
            inspect_command_id=container_command_id(
                plan.attempt_id,
                "resource_recovery_inspect",
                ordinal=plan.command_ordinal,
            ),
            minimum_event_sequence=result.event_sequence + 1,
            not_before_monotonic_ns=result.finished_monotonic_ns,
            inspect_argv=volume_inspect_argv(
                plan.identity,
                plan.volume_name,
            ),
        ),
    )


def parse_durable_volume_recovery_inspect_result(
    candidate: DurableVolumeRecoveryCandidate,
    result: ContainerCommandResult,
) -> DurableVolumeRecoveryInspectResult:
    plan = candidate.plan
    failure = _result_failure(
        plan,
        step="resource_recovery_inspect",
        command_id=candidate.inspect_command_id,
        argv=candidate.inspect_argv,
        minimum_event_sequence=candidate.minimum_event_sequence,
        not_before_monotonic_ns=candidate.not_before_monotonic_ns,
        result=result,
    )
    if failure is not None:
        if failure in {
            "failed",
            "spawn_failed",
            "parent_failed",
            "timed_out",
            "cancelled",
        }:
            return _inspect_retry(
                candidate,
                result,
                f"volume_recovery_inspect_{failure}",
            )
        return DurableVolumeRecoveryInspectResult(
            f"volume_recovery_inspect_{failure}",
            unresolved=True,
        )
    if result.stderr or len(result.stdout) > _MAX_RECOVERY_OUTPUT_CHARS:
        return DurableVolumeRecoveryInspectResult(
            "volume_recovery_inspect_output_invalid",
            unresolved=True,
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return DurableVolumeRecoveryInspectResult(
            "volume_recovery_inspect_output_invalid",
            unresolved=True,
        )
    if not volume_payload_matches(
        name=plan.volume_name,
        attempt_id=plan.attempt_id,
        resource_label=plan.resource_label,
        payload=payload,
    ):
        return DurableVolumeRecoveryInspectResult(
            "volume_recovery_not_owned",
            unresolved=True,
        )
    return DurableVolumeRecoveryInspectResult(
        "volume_recovered_for_cleanup",
        cleanup=DurableVolumeCleanupHandle(
            plan=plan,
            remove_argv=_volume_remove_argv(plan),
            absence_argv=volume_query_argv(
                plan.identity,
                plan.volume_name,
            ),
        ),
    )


def parse_durable_volume_remove_result(
    cleanup: DurableVolumeCleanupHandle,
    result: ContainerCommandResult,
) -> ContainerResourceResult:
    plan = cleanup.plan
    failure = _result_failure(
        plan,
        step="volume_remove",
        command_id=container_command_id(
            plan.attempt_id,
            "volume_remove",
            ordinal=plan.root_ordinal + 1,
        ),
        argv=cleanup.remove_argv,
        minimum_event_sequence=1,
        not_before_monotonic_ns=0,
        result=result,
        require_success=False,
    )
    if failure is not None:
        return ContainerResourceResult(f"volume_remove_{failure}", False)
    return ContainerResourceResult("volume_remove_attempted", True)


def parse_durable_volume_absence_result(
    cleanup: DurableVolumeCleanupHandle,
    result: ContainerCommandResult,
) -> ContainerResourceResult:
    plan = cleanup.plan
    failure = _result_failure(
        plan,
        step="volume_removal_check",
        command_id=container_command_id(
            plan.attempt_id,
            "volume_removal_check",
            ordinal=plan.root_ordinal + 1,
        ),
        argv=cleanup.absence_argv,
        minimum_event_sequence=1,
        not_before_monotonic_ns=0,
        result=result,
    )
    if failure is not None:
        return ContainerResourceResult(
            f"volume_removal_check_{failure}",
            False,
        )
    return parse_exact_volume_absence(plan.volume_name, result)


def _query_retry(
    plan: DurableVolumeRecoveryPlan,
    result: ContainerCommandResult,
    reason_code: str,
) -> DurableVolumeRecoveryQueryResult:
    if plan.query_ordinal == RECOVERY_QUERY_ATTEMPT_LIMIT:
        return DurableVolumeRecoveryQueryResult(
            f"{reason_code}_exhausted",
            unresolved=True,
        )
    return DurableVolumeRecoveryQueryResult(
        f"{reason_code}_retry",
        retry=_next_plan(plan, result),
    )


def _inspect_retry(
    candidate: DurableVolumeRecoveryCandidate,
    result: ContainerCommandResult,
    reason_code: str,
) -> DurableVolumeRecoveryInspectResult:
    if candidate.plan.query_ordinal == RECOVERY_QUERY_ATTEMPT_LIMIT:
        return DurableVolumeRecoveryInspectResult(
            f"{reason_code}_exhausted",
            unresolved=True,
        )
    return DurableVolumeRecoveryInspectResult(
        f"{reason_code}_retry",
        retry=_next_plan(candidate.plan, result),
    )


def _next_plan(
    plan: DurableVolumeRecoveryPlan,
    result: ContainerCommandResult,
) -> DurableVolumeRecoveryPlan:
    ordinal = plan.query_ordinal + 1
    return DurableVolumeRecoveryPlan(
        binding=plan.binding,
        identity=plan.identity,
        attempt_id=plan.attempt_id,
        root_ordinal=plan.root_ordinal,
        absence_allowed=plan.absence_allowed,
        query_ordinal=ordinal,
        query_command_id=container_command_id(
            plan.attempt_id,
            "resource_recovery_query",
            ordinal=(
                (2 + plan.root_ordinal) * RECOVERY_QUERY_ATTEMPT_LIMIT
                + ordinal
            ),
        ),
        minimum_event_sequence=result.event_sequence + 1,
        not_before_monotonic_ns=(
            result.finished_monotonic_ns + _RETRY_AFTER_NS
        ),
        query_argv=plan.query_argv,
        retry_after_seconds=RECOVERY_RETRY_AFTER_SECONDS,
    )


def _result_failure(
    plan: DurableVolumeRecoveryPlan,
    *,
    step: str,
    command_id: str,
    argv: tuple[str, ...],
    minimum_event_sequence: int,
    not_before_monotonic_ns: int,
    result: ContainerCommandResult,
    require_success: bool = True,
) -> str | None:
    if (
        result.attempt_id != plan.attempt_id
        or result.command_id != command_id
        or result.step != step
        or result.argv != argv
    ):
        return "correlation_mismatch"
    if result.event_sequence < minimum_event_sequence:
        return "event_sequence_mismatch"
    if result.started_monotonic_ns < not_before_monotonic_ns:
        return "retry_too_early"
    if not command_workspace_authority_matches(
        plan.identity.workspace_authority,
        result,
    ):
        return "workspace_authority_changed"
    if result.outcome != "exited":
        return result.outcome
    if require_success and result.exit_code != 0:
        return "failed"
    if not command_output_is_complete(result):
        return "output_incomplete"
    if not plan.identity.control_authority_is_current():
        return "engine_changed"
    return None


def _volume_remove_argv(
    plan: DurableVolumeRecoveryPlan,
) -> tuple[str, ...]:
    return plan.identity.command("volume", "rm", plan.volume_name)


__all__ = [
    "DurableVolumeCleanupHandle",
    "DurableVolumeRecoveryCandidate",
    "DurableVolumeRecoveryInspectResult",
    "DurableVolumeRecoveryPlan",
    "DurableVolumeRecoveryQueryResult",
    "build_durable_volume_recovery_plan",
    "parse_durable_volume_absence_result",
    "parse_durable_volume_recovery_inspect_result",
    "parse_durable_volume_recovery_query_result",
    "parse_durable_volume_remove_result",
]

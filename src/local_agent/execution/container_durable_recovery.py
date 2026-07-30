from __future__ import annotations

import json
from dataclasses import dataclass

from .container_cleanup import ContainerCleanupHandle
from .container_cleanup import build_durable_container_cleanup_handle
from .container_inspect_schema import build_container_inspect_argv
from .container_plan import CONTAINER_EXECUTION_RESOURCE
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
from .container_verification import normalized_container_id
from .container_verification import parse_container_inspect_payload
from .container_volume import CONTAINER_PREP_RESOURCE


_MAX_RECOVERY_OUTPUT_CHARS = 4_096
_RETRY_AFTER_NS = int(RECOVERY_RETRY_AFTER_SECONDS * 1_000_000_000)


@dataclass(frozen=True)
class DurableContainerRecoveryPlan:
    binding: ContainerStagingContainerBinding
    identity: ContainerEngineIdentity
    attempt_id: str
    resource_name: str
    resource_label: str
    cleanup_ordinal: int
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
            or (
                self.resource_name,
                self.resource_label,
                self.cleanup_ordinal,
            )
            not in {
                (
                    self.binding.instance_name,
                    CONTAINER_EXECUTION_RESOURCE,
                    1,
                ),
                (
                    self.binding.prep_instance_name,
                    CONTAINER_PREP_RESOURCE,
                    2,
                ),
            }
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
            != _build_container_resource_query_argv(
                self.identity,
                self.resource_name,
            )
        ):
            raise ValueError("durable container recovery plan is invalid")
        expected_delay = (
            0.0
            if self.query_ordinal == 1
            else RECOVERY_RETRY_AFTER_SECONDS
        )
        if self.retry_after_seconds != expected_delay:
            raise ValueError("durable container recovery delay is invalid")

    @property
    def command_ordinal(self) -> int:
        return (
            (self.cleanup_ordinal - 1) * RECOVERY_QUERY_ATTEMPT_LIMIT
            + self.query_ordinal
        )


@dataclass(frozen=True)
class DurableContainerRecoveryCandidate:
    plan: DurableContainerRecoveryPlan
    container_id: str
    inspect_command_id: str
    minimum_event_sequence: int
    not_before_monotonic_ns: int
    inspect_argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            normalized_container_id(self.container_id) != self.container_id
            or self.inspect_command_id
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
            != build_container_inspect_argv(
                self.plan.identity,
                self.container_id,
            )
        ):
            raise ValueError(
                "durable container recovery candidate is invalid"
            )


@dataclass(frozen=True)
class DurableContainerRecoveryQueryResult:
    reason_code: str
    retry: DurableContainerRecoveryPlan | None = None
    candidate: DurableContainerRecoveryCandidate | None = None
    unresolved: bool = False

    def __post_init__(self) -> None:
        if (
            not self.reason_code
            or sum(
                (
                    self.retry is not None,
                    self.candidate is not None,
                    self.unresolved,
                )
            )
            != 1
        ):
            raise ValueError(
                "durable container recovery query result is invalid"
            )


@dataclass(frozen=True)
class DurableContainerRecoveryInspectResult:
    reason_code: str
    cleanup: ContainerCleanupHandle | None = None
    retry: DurableContainerRecoveryPlan | None = None
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
                "durable container recovery inspect result is invalid"
            )


def build_durable_container_recovery_plan(
    binding: ContainerStagingContainerBinding,
    identity: ContainerEngineIdentity,
    *,
    resource: str = "execution",
) -> DurableContainerRecoveryPlan:
    attempt_id = binding.instance_name.removeprefix("lca-")
    validate_attempt_id(attempt_id)
    if not staging_container_binding_matches(binding, identity):
        raise ValueError("durable container recovery authority changed")
    if resource == "execution":
        resource_name = binding.instance_name
        resource_label = CONTAINER_EXECUTION_RESOURCE
        cleanup_ordinal = 1
    elif resource == "prep":
        resource_name = binding.prep_instance_name
        resource_label = CONTAINER_PREP_RESOURCE
        cleanup_ordinal = 2
    else:
        raise ValueError("durable container recovery resource is invalid")
    return DurableContainerRecoveryPlan(
        binding=binding,
        identity=identity,
        attempt_id=attempt_id,
        resource_name=resource_name,
        resource_label=resource_label,
        cleanup_ordinal=cleanup_ordinal,
        query_ordinal=1,
        query_command_id=container_command_id(
            attempt_id,
            "resource_recovery_query",
            ordinal=(cleanup_ordinal - 1) * RECOVERY_QUERY_ATTEMPT_LIMIT + 1,
        ),
        minimum_event_sequence=1,
        not_before_monotonic_ns=0,
        query_argv=_build_container_resource_query_argv(
            identity,
            resource_name,
        ),
        retry_after_seconds=0.0,
    )


def parse_durable_recovery_query_result(
    plan: DurableContainerRecoveryPlan,
    result: ContainerCommandResult,
) -> DurableContainerRecoveryQueryResult:
    failure = _result_failure(
        plan,
        "resource_recovery_query",
        plan.query_argv,
        plan.query_command_id,
        plan.minimum_event_sequence,
        plan.not_before_monotonic_ns,
        result,
    )
    if failure is not None:
        if failure in {
            "failed",
            "spawn_failed",
            "parent_failed",
            "timed_out",
            "cancelled",
        }:
            return _query_retry_or_unresolved(
                plan,
                result,
                f"staging_recovery_query_{failure}",
            )
        return DurableContainerRecoveryQueryResult(
            f"staging_recovery_query_{failure}",
            unresolved=True,
        )
    if result.stderr:
        return DurableContainerRecoveryQueryResult(
            "staging_recovery_query_unexpected_stderr",
            unresolved=True,
        )
    if len(result.stdout) > _MAX_RECOVERY_OUTPUT_CHARS:
        return DurableContainerRecoveryQueryResult(
            "staging_recovery_query_output_too_large",
            unresolved=True,
        )
    if not result.stdout.strip():
        return _query_retry_or_unresolved(
            plan,
            result,
            "staging_recovery_absence_unverified",
        )
    observed: list[str] = []
    try:
        for line in result.stdout.splitlines():
            container_id = normalized_container_id(json.loads(line))
            if container_id is None:
                raise ValueError("recovery query identity is invalid")
            observed.append(container_id)
    except (json.JSONDecodeError, ValueError):
        return DurableContainerRecoveryQueryResult(
            "staging_recovery_query_invalid_output",
            unresolved=True,
        )
    if len(observed) != 1:
        return DurableContainerRecoveryQueryResult(
            "staging_recovery_query_ambiguous",
            unresolved=True,
        )
    container_id = observed[0]
    return DurableContainerRecoveryQueryResult(
        "staging_recovery_candidate_observed",
        candidate=DurableContainerRecoveryCandidate(
            plan=plan,
            container_id=container_id,
            inspect_command_id=container_command_id(
                plan.attempt_id,
                "resource_recovery_inspect",
                ordinal=plan.command_ordinal,
            ),
            minimum_event_sequence=result.event_sequence + 1,
            not_before_monotonic_ns=result.finished_monotonic_ns,
            inspect_argv=build_container_inspect_argv(
                plan.identity,
                container_id,
            ),
        ),
    )


def parse_durable_recovery_inspect_result(
    candidate: DurableContainerRecoveryCandidate,
    result: ContainerCommandResult,
) -> DurableContainerRecoveryInspectResult:
    plan = candidate.plan
    failure = _result_failure(
        plan,
        "resource_recovery_inspect",
        candidate.inspect_argv,
        candidate.inspect_command_id,
        candidate.minimum_event_sequence,
        candidate.not_before_monotonic_ns,
        result,
    )
    if failure is not None:
        if failure in {
            "failed",
            "spawn_failed",
            "parent_failed",
            "timed_out",
            "cancelled",
        }:
            return _inspect_retry_or_unresolved(
                candidate,
                result,
                f"staging_recovery_inspect_{failure}",
            )
        return DurableContainerRecoveryInspectResult(
            f"staging_recovery_inspect_{failure}",
            unresolved=True,
        )
    if result.stderr:
        return DurableContainerRecoveryInspectResult(
            "staging_recovery_inspect_unexpected_stderr",
            unresolved=True,
        )
    payload, failure = parse_container_inspect_payload(result.stdout)
    if failure is not None:
        return DurableContainerRecoveryInspectResult(
            f"staging_recovery_{failure.removeprefix('inspect_')}",
            unresolved=True,
        )
    assert payload is not None
    if not _durable_ownership_matches(candidate, payload):
        return DurableContainerRecoveryInspectResult(
            "staging_recovery_not_owned",
            unresolved=True,
        )
    return DurableContainerRecoveryInspectResult(
        "staging_container_recovered_for_cleanup",
        cleanup=build_durable_container_cleanup_handle(
            plan.identity,
            plan.attempt_id,
            candidate.container_id,
            command_ordinal=plan.cleanup_ordinal,
        ),
    )


def _query_retry_or_unresolved(
    plan: DurableContainerRecoveryPlan,
    result: ContainerCommandResult,
    reason: str,
) -> DurableContainerRecoveryQueryResult:
    if plan.query_ordinal == RECOVERY_QUERY_ATTEMPT_LIMIT:
        return DurableContainerRecoveryQueryResult(
            f"{reason}_exhausted",
            unresolved=True,
        )
    return DurableContainerRecoveryQueryResult(
        f"{reason}_retry",
        retry=_next_plan(plan, result),
    )


def _inspect_retry_or_unresolved(
    candidate: DurableContainerRecoveryCandidate,
    result: ContainerCommandResult,
    reason: str,
) -> DurableContainerRecoveryInspectResult:
    if candidate.plan.query_ordinal == RECOVERY_QUERY_ATTEMPT_LIMIT:
        return DurableContainerRecoveryInspectResult(
            f"{reason}_exhausted",
            unresolved=True,
        )
    return DurableContainerRecoveryInspectResult(
        f"{reason}_retry",
        retry=_next_plan(candidate.plan, result),
    )


def _next_plan(
    plan: DurableContainerRecoveryPlan,
    result: ContainerCommandResult,
) -> DurableContainerRecoveryPlan:
    ordinal = plan.query_ordinal + 1
    return DurableContainerRecoveryPlan(
        binding=plan.binding,
        identity=plan.identity,
        attempt_id=plan.attempt_id,
        resource_name=plan.resource_name,
        resource_label=plan.resource_label,
        cleanup_ordinal=plan.cleanup_ordinal,
        query_ordinal=ordinal,
        query_command_id=container_command_id(
            plan.attempt_id,
            "resource_recovery_query",
            ordinal=(
                (plan.cleanup_ordinal - 1)
                * RECOVERY_QUERY_ATTEMPT_LIMIT
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
    plan: DurableContainerRecoveryPlan,
    step: str,
    argv: tuple[str, ...],
    command_id: str,
    minimum_event_sequence: int,
    not_before_monotonic_ns: int,
    result: ContainerCommandResult,
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
    if result.outcome == "spawn_failed":
        return "spawn_failed"
    if result.outcome == "parent_failed":
        return "parent_failed"
    if result.outcome == "timed_out":
        return "timed_out"
    if result.outcome == "cancelled":
        return "cancelled"
    if result.exit_code != 0:
        return "failed"
    if not command_output_is_complete(result):
        return "output_incomplete"
    if not plan.identity.control_authority_is_current():
        return "engine_changed"
    return None


def _durable_ownership_matches(
    candidate: DurableContainerRecoveryCandidate,
    payload: dict[object, object],
) -> bool:
    raw_name = payload.get("name")
    normalized_name = (
        raw_name[1:]
        if isinstance(raw_name, str) and raw_name.startswith("/")
        else raw_name
    )
    return (
        normalized_container_id(payload.get("id"))
        == candidate.container_id
        and normalized_name == candidate.plan.resource_name
        and payload.get("instance_label") == candidate.plan.attempt_id
        and payload.get("resource_label") == candidate.plan.resource_label
        and payload.get("config_image") == candidate.plan.binding.runtime_image
        and normalized_container_id(payload.get("image_id"))
        == candidate.plan.binding.runtime_image.removeprefix("sha256:")
    )


def _build_container_resource_query_argv(
    identity: ContainerEngineIdentity,
    resource_name: str,
) -> tuple[str, ...]:
    if not resource_name or "\0" in resource_name:
        raise ValueError("container recovery resource name is invalid")
    return identity.command(
        "ps",
        "--all",
        "--no-trunc",
        "--filter",
        f"name={resource_name}",
        "--format",
        "{{json .ID}}",
    )


__all__ = [
    "DurableContainerRecoveryCandidate",
    "DurableContainerRecoveryInspectResult",
    "DurableContainerRecoveryPlan",
    "DurableContainerRecoveryQueryResult",
    "build_durable_container_recovery_plan",
    "parse_durable_recovery_inspect_result",
    "parse_durable_recovery_query_result",
]

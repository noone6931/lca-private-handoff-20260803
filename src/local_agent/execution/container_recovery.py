from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .container_cleanup import ContainerCleanupHandle
from .container_cleanup import build_container_cleanup_handle
from .container_inspect_schema import build_container_inspect_argv
from .container_plan import ContainerExecutionPlan
from .container_plan import build_container_recovery_query_argv
from .container_types import ContainerCommandResult
from .container_types import command_output_is_complete
from .container_types import command_workspace_authority_matches
from .container_types import container_command_id
from .container_verification import normalized_container_id
from .container_verification import ownership_matches
from .container_verification import parse_container_inspect_payload
from .container_verification import static_container_mismatch_reason


RECOVERY_QUERY_ATTEMPT_LIMIT = 10
RECOVERY_RETRY_AFTER_SECONDS = 0.5
_RECOVERY_RETRY_AFTER_NS = int(RECOVERY_RETRY_AFTER_SECONDS * 1_000_000_000)
_MAX_RECOVERY_OUTPUT_CHARS = 4_096


@dataclass(frozen=True)
class ContainerRecoveryObligation:
    plan: ContainerExecutionPlan
    origin_command_id: str
    origin_event_sequence: int
    origin_finished_monotonic_ns: int
    query_ordinal: int
    query_command_id: str
    minimum_event_sequence: int
    not_before_monotonic_ns: int
    query_argv: tuple[str, ...]
    retry_after_seconds: float

    def __post_init__(self) -> None:
        expected_origin = container_command_id(self.plan.attempt_id, "create")
        if (
            self.origin_command_id != expected_origin
            or self.origin_event_sequence < 1
            or self.origin_finished_monotonic_ns < 0
        ):
            raise ValueError("container recovery origin provenance is invalid")
        if not 1 <= self.query_ordinal <= RECOVERY_QUERY_ATTEMPT_LIMIT:
            raise ValueError("container recovery query ordinal is invalid")
        if self.query_command_id != container_command_id(
            self.plan.attempt_id,
            "recovery_query",
            ordinal=self.query_ordinal,
        ):
            raise ValueError("container recovery query correlation is invalid")
        if (
            self.minimum_event_sequence <= self.origin_event_sequence
            or self.not_before_monotonic_ns < self.origin_finished_monotonic_ns
        ):
            raise ValueError("container recovery query boundary is invalid")
        if self.query_argv != build_container_recovery_query_argv(
            self.plan.identity,
            self.plan.attempt_id,
        ):
            raise ValueError("container recovery query does not match its plan")
        expected_delay = (
            0.0 if self.query_ordinal == 1 else RECOVERY_RETRY_AFTER_SECONDS
        )
        if self.retry_after_seconds != expected_delay:
            raise ValueError("container recovery delay does not match its ordinal")


@dataclass(frozen=True)
class ContainerRecoveryCandidate:
    obligation: ContainerRecoveryObligation
    container_id: str
    inspect_command_id: str
    minimum_event_sequence: int
    not_before_monotonic_ns: int
    inspect_argv: tuple[str, ...]

    @property
    def plan(self) -> ContainerExecutionPlan:
        return self.obligation.plan

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.container_id):
            raise ValueError("container recovery candidate identity is invalid")
        if self.inspect_command_id != container_command_id(
            self.plan.attempt_id,
            "recovery_inspect",
            ordinal=self.obligation.query_ordinal,
        ):
            raise ValueError("container recovery inspect correlation is invalid")
        if (
            self.minimum_event_sequence < self.obligation.minimum_event_sequence
            or self.not_before_monotonic_ns < self.obligation.not_before_monotonic_ns
        ):
            raise ValueError("container recovery inspect boundary is invalid")
        if self.inspect_argv != build_container_inspect_argv(
            self.plan.identity,
            self.container_id,
        ):
            raise ValueError("container recovery inspect does not match its candidate")


@dataclass(frozen=True)
class ContainerRecoveryQueryResult:
    reason_code: str
    retry: ContainerRecoveryObligation | None = None
    candidate: ContainerRecoveryCandidate | None = None
    unresolved: ContainerRecoveryObligation | None = None

    def __post_init__(self) -> None:
        if not self.reason_code.strip():
            raise ValueError("container recovery query reason must not be empty")
        if sum(
            (
                self.retry is not None,
                self.candidate is not None,
                self.unresolved is not None,
            )
        ) != 1:
            raise ValueError("container recovery query result must retain one outcome")
        if (self.candidate is not None) != (
            self.reason_code == "recovery_candidate_observed"
        ):
            raise ValueError("only an observed candidate may be inspected")
        if (self.retry is not None) != self.reason_code.endswith("_retry"):
            raise ValueError("only a bounded recovery retry may retain an obligation")
        if self.unresolved is not None and (
            self.reason_code.endswith("_retry")
            or self.reason_code == "recovery_candidate_observed"
        ):
            raise ValueError("an unresolved recovery reason cannot claim progress")


@dataclass(frozen=True)
class ContainerRecoveryInspectResult:
    reason_code: str
    cleanup: ContainerCleanupHandle | None = None
    retry: ContainerRecoveryObligation | None = None
    unresolved: ContainerRecoveryObligation | None = None

    def __post_init__(self) -> None:
        if not self.reason_code.strip():
            raise ValueError("container recovery inspect reason must not be empty")
        owned = self.reason_code == "container_recovered_for_cleanup" or (
            self.reason_code.startswith("recovery_owned_")
        )
        if owned != (self.cleanup is not None):
            raise ValueError("only an owned recovery candidate may expose cleanup")
        if (self.retry is not None) != self.reason_code.endswith("_retry"):
            raise ValueError("only a bounded inspect retry may retain recovery")
        if sum(
            (
                self.cleanup is not None,
                self.retry is not None,
                self.unresolved is not None,
            )
        ) != 1:
            raise ValueError("container recovery inspect must retain one outcome")
        if self.unresolved is not None and self.reason_code.endswith("_retry"):
            raise ValueError("an unresolved inspect reason cannot claim a retry")


def build_container_recovery_obligation(
    plan: ContainerExecutionPlan,
    origin: ContainerCommandResult,
) -> ContainerRecoveryObligation:
    expected_command_id = container_command_id(plan.attempt_id, "create")
    if (
        origin.attempt_id != plan.attempt_id
        or origin.command_id != expected_command_id
        or origin.step != "create"
        or origin.argv != plan.create_argv
    ):
        raise ValueError(
            "container recovery requires the exact correlated create result"
        )
    return ContainerRecoveryObligation(
        plan=plan,
        origin_command_id=origin.command_id,
        origin_event_sequence=origin.event_sequence,
        origin_finished_monotonic_ns=origin.finished_monotonic_ns,
        query_ordinal=1,
        query_command_id=container_command_id(
            plan.attempt_id,
            "recovery_query",
        ),
        minimum_event_sequence=origin.event_sequence + 1,
        not_before_monotonic_ns=origin.finished_monotonic_ns,
        query_argv=plan.recovery_query_argv,
        retry_after_seconds=0.0,
    )


def parse_container_recovery_query_result(
    obligation: ContainerRecoveryObligation,
    result: ContainerCommandResult,
) -> ContainerRecoveryQueryResult:
    failure = _query_result_failure(obligation, result)
    if failure is not None:
        reason = f"recovery_query_{failure}"
        if failure in {
            "failed",
            "spawn_failed",
            "parent_failed",
            "timed_out",
            "cancelled",
        }:
            return _retry_or_exhausted(obligation, result, reason)
        return ContainerRecoveryQueryResult(reason, unresolved=obligation)
    if result.stderr:
        return ContainerRecoveryQueryResult(
            "recovery_query_unexpected_stderr",
            unresolved=obligation,
        )
    if len(result.stdout) > _MAX_RECOVERY_OUTPUT_CHARS:
        return ContainerRecoveryQueryResult(
            "recovery_query_output_too_large",
            unresolved=obligation,
        )
    if not result.stdout.strip():
        return _retry_or_exhausted(
            obligation,
            result,
            "recovery_absence_unverified",
        )

    observed: list[str] = []
    try:
        for line in result.stdout.splitlines():
            value = json.loads(line)
            container_id = normalized_container_id(value)
            if container_id is None:
                raise ValueError("recovery query identity is invalid")
            observed.append(container_id)
    except (json.JSONDecodeError, ValueError):
        return ContainerRecoveryQueryResult(
            "recovery_query_invalid_output",
            unresolved=obligation,
        )
    if len(observed) != 1:
        return ContainerRecoveryQueryResult(
            "recovery_query_ambiguous",
            unresolved=obligation,
        )
    return ContainerRecoveryQueryResult(
        "recovery_candidate_observed",
        candidate=ContainerRecoveryCandidate(
            obligation=obligation,
            container_id=observed[0],
            inspect_command_id=container_command_id(
                obligation.plan.attempt_id,
                "recovery_inspect",
                ordinal=obligation.query_ordinal,
            ),
            minimum_event_sequence=result.event_sequence + 1,
            not_before_monotonic_ns=result.finished_monotonic_ns,
            inspect_argv=build_container_inspect_argv(
                obligation.plan.identity,
                observed[0],
            ),
        ),
    )


def parse_container_recovery_inspect_result(
    candidate: ContainerRecoveryCandidate,
    result: ContainerCommandResult,
) -> ContainerRecoveryInspectResult:
    plan = candidate.plan
    failure = _inspect_result_failure(candidate, result)
    if failure is not None:
        if failure in {
            "failed",
            "spawn_failed",
            "parent_failed",
            "timed_out",
            "cancelled",
        }:
            return _inspect_retry_or_exhausted(
                candidate,
                result,
                f"recovery_inspect_{failure}",
            )
        return ContainerRecoveryInspectResult(
            f"recovery_inspect_{failure}",
            unresolved=candidate.obligation,
        )
    if result.stderr:
        return ContainerRecoveryInspectResult(
            "recovery_inspect_unexpected_stderr",
            unresolved=candidate.obligation,
        )
    payload, failure = parse_container_inspect_payload(result.stdout)
    if failure is not None:
        return ContainerRecoveryInspectResult(
            f"recovery_{failure.removeprefix('inspect_')}",
            unresolved=candidate.obligation,
        )
    assert payload is not None
    if not ownership_matches(plan, candidate.container_id, payload):
        return ContainerRecoveryInspectResult(
            "recovery_not_owned",
            unresolved=candidate.obligation,
        )
    cleanup = build_container_cleanup_handle(plan, candidate.container_id)
    mismatch = static_container_mismatch_reason(
        plan,
        candidate.container_id,
        payload,
    )
    if mismatch is not None:
        return ContainerRecoveryInspectResult(
            f"recovery_owned_{mismatch.removeprefix('inspect_')}",
            cleanup,
        )
    return ContainerRecoveryInspectResult("container_recovered_for_cleanup", cleanup)


def _retry_or_exhausted(
    obligation: ContainerRecoveryObligation,
    result: ContainerCommandResult,
    reason_prefix: str,
) -> ContainerRecoveryQueryResult:
    if obligation.query_ordinal == RECOVERY_QUERY_ATTEMPT_LIMIT:
        return ContainerRecoveryQueryResult(
            f"{reason_prefix}_exhausted",
            unresolved=obligation,
        )
    return ContainerRecoveryQueryResult(
        f"{reason_prefix}_retry",
        retry=_next_obligation(obligation, result),
    )


def _inspect_retry_or_exhausted(
    candidate: ContainerRecoveryCandidate,
    result: ContainerCommandResult,
    reason_prefix: str,
) -> ContainerRecoveryInspectResult:
    obligation = candidate.obligation
    if obligation.query_ordinal == RECOVERY_QUERY_ATTEMPT_LIMIT:
        return ContainerRecoveryInspectResult(
            f"{reason_prefix}_exhausted",
            unresolved=obligation,
        )
    return ContainerRecoveryInspectResult(
        f"{reason_prefix}_retry",
        retry=_next_obligation(obligation, result),
    )


def _next_obligation(
    obligation: ContainerRecoveryObligation,
    result: ContainerCommandResult,
) -> ContainerRecoveryObligation:
    ordinal = obligation.query_ordinal + 1
    return ContainerRecoveryObligation(
        plan=obligation.plan,
        origin_command_id=obligation.origin_command_id,
        origin_event_sequence=obligation.origin_event_sequence,
        origin_finished_monotonic_ns=obligation.origin_finished_monotonic_ns,
        query_ordinal=ordinal,
        query_command_id=container_command_id(
            obligation.plan.attempt_id,
            "recovery_query",
            ordinal=ordinal,
        ),
        minimum_event_sequence=result.event_sequence + 1,
        not_before_monotonic_ns=(
            result.finished_monotonic_ns + _RECOVERY_RETRY_AFTER_NS
        ),
        query_argv=obligation.query_argv,
        retry_after_seconds=RECOVERY_RETRY_AFTER_SECONDS,
    )


def _query_result_failure(
    obligation: ContainerRecoveryObligation,
    result: ContainerCommandResult,
) -> str | None:
    return _result_failure(
        obligation.plan,
        "recovery_query",
        obligation.query_argv,
        obligation.query_command_id,
        obligation.minimum_event_sequence,
        obligation.not_before_monotonic_ns,
        result,
    )


def _inspect_result_failure(
    candidate: ContainerRecoveryCandidate,
    result: ContainerCommandResult,
) -> str | None:
    return _result_failure(
        candidate.plan,
        "recovery_inspect",
        candidate.inspect_argv,
        candidate.inspect_command_id,
        candidate.minimum_event_sequence,
        candidate.not_before_monotonic_ns,
        result,
    )


def _result_failure(
    plan: ContainerExecutionPlan,
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

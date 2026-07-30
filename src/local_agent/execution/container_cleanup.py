from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .container_plan import ContainerExecutionPlan
from .container_types import ContainerCommandResult
from .container_types import ContainerEngineIdentity
from .container_types import command_output_is_complete
from .container_types import container_command_id
from .container_types import command_workspace_authority_matches
from .container_types import validate_attempt_id


_MAX_CONTROL_OUTPUT_CHARS = 4_096


@dataclass(frozen=True)
class ContainerCleanupPlan:
    identity: ContainerEngineIdentity
    attempt_id: str

    def __post_init__(self) -> None:
        validate_attempt_id(self.attempt_id)
        if not self.identity.control_authority_is_current():
            raise ValueError("container cleanup authority is stale")


@dataclass(frozen=True)
class ContainerCleanupHandle:
    plan: ContainerExecutionPlan | ContainerCleanupPlan
    container_id: str
    remove_argv: tuple[str, ...]
    removal_check_argv: tuple[str, ...]
    command_ordinal: int = 1

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.container_id):
            raise ValueError("container cleanup identity must contain 64 lowercase hex characters")
        if self.command_ordinal < 1:
            raise ValueError("container cleanup command ordinal is invalid")
        expected_remove = self.plan.identity.command(
            "rm",
            "--force",
            "--volumes",
            self.container_id,
        )
        if self.remove_argv != expected_remove:
            raise ValueError("container remove argv does not match its instance")
        expected_check = self.plan.identity.command(
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            f"id={self.container_id}",
            "--format",
            "{{json .ID}}",
        )
        if self.removal_check_argv != expected_check:
            raise ValueError("container removal check does not match its instance")


@dataclass(frozen=True)
class ContainerRemoveResult:
    reason_code: str
    cleanup: ContainerCleanupHandle

    def __post_init__(self) -> None:
        if not self.reason_code.strip():
            raise ValueError("container remove result must retain its cleanup obligation")

    @property
    def removal_check_argv(self) -> tuple[str, ...]:
        return self.cleanup.removal_check_argv


@dataclass(frozen=True)
class ContainerRemovalCheckResult:
    reason_code: str
    cleanup_verified: bool
    unresolved: ContainerCleanupHandle | None

    def __post_init__(self) -> None:
        if not self.reason_code.strip():
            raise ValueError("container removal check reason_code must not be empty")
        if self.cleanup_verified != (self.reason_code == "cleanup_verified_absent"):
            raise ValueError("only exact absence may verify container cleanup")
        if self.cleanup_verified == (self.unresolved is not None):
            raise ValueError(
                "every unverified removal check must retain its cleanup obligation"
            )


def build_container_cleanup_handle(
    plan: ContainerExecutionPlan,
    container_id: str,
) -> ContainerCleanupHandle:
    return ContainerCleanupHandle(
        plan=plan,
        container_id=container_id,
        remove_argv=plan.identity.command(
            "rm",
            "--force",
            "--volumes",
            container_id,
        ),
        removal_check_argv=plan.identity.command(
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            f"id={container_id}",
            "--format",
            "{{json .ID}}",
        ),
    )


def build_durable_container_cleanup_handle(
    identity: ContainerEngineIdentity,
    attempt_id: str,
    container_id: str,
    *,
    command_ordinal: int = 1,
) -> ContainerCleanupHandle:
    plan = ContainerCleanupPlan(identity, attempt_id)
    return ContainerCleanupHandle(
        plan=plan,
        container_id=container_id,
        remove_argv=identity.command(
            "rm",
            "--force",
            "--volumes",
            container_id,
        ),
        removal_check_argv=identity.command(
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            f"id={container_id}",
            "--format",
            "{{json .ID}}",
        ),
        command_ordinal=command_ordinal,
    )


def parse_container_remove_result(
    cleanup: ContainerCleanupHandle,
    result: ContainerCommandResult,
) -> ContainerRemoveResult:
    failure = _result_failure(
        cleanup,
        "remove",
        cleanup.remove_argv,
        result,
    )
    reason = "remove_requested" if failure is None else f"remove_{failure}"
    return ContainerRemoveResult(reason, cleanup)


def parse_container_removal_check_result(
    removed: ContainerRemoveResult,
    result: ContainerCommandResult,
) -> ContainerRemovalCheckResult:
    if not isinstance(removed, ContainerRemoveResult):
        raise TypeError("container absence check requires a remove attempt")
    cleanup = removed.cleanup
    failure = _result_failure(
        cleanup,
        "removal_check",
        cleanup.removal_check_argv,
        result,
    )
    if failure is not None:
        return ContainerRemovalCheckResult(
            f"cleanup_check_{failure}",
            False,
            cleanup,
        )
    if len(result.stdout) > _MAX_CONTROL_OUTPUT_CHARS:
        return ContainerRemovalCheckResult(
            "cleanup_check_output_too_large",
            False,
            cleanup,
        )
    if result.stdout == "":
        return ContainerRemovalCheckResult("cleanup_verified_absent", True, None)
    observed: list[str] = []
    try:
        for line in result.stdout.splitlines():
            value = json.loads(line)
            if not isinstance(value, str):
                raise ValueError("container removal output is not a string")
            observed.append(value)
    except (json.JSONDecodeError, ValueError):
        return ContainerRemovalCheckResult(
            "cleanup_check_invalid_output",
            False,
            cleanup,
        )
    if cleanup.container_id in observed:
        return ContainerRemovalCheckResult(
            "cleanup_container_still_present",
            False,
            cleanup,
        )
    return ContainerRemovalCheckResult(
        "cleanup_check_unexpected_identity",
        False,
        cleanup,
    )


def _result_failure(
    cleanup: ContainerCleanupHandle,
    step: str,
    argv: tuple[str, ...],
    result: ContainerCommandResult,
) -> str | None:
    if (
        result.attempt_id != cleanup.plan.attempt_id
        or result.command_id
        != container_command_id(
            cleanup.plan.attempt_id,
            step,
            ordinal=cleanup.command_ordinal,
        )
        or result.step != step
        or result.argv != argv
    ):
        return "correlation_mismatch"
    if not command_workspace_authority_matches(
        cleanup.plan.identity.workspace_authority,
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
    if result.stderr:
        return "unexpected_stderr"
    if not cleanup.plan.identity.control_authority_is_current():
        return "engine_changed"
    return None

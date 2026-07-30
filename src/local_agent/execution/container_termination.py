from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .container_instance import ContainerReleasedExecution
from .container_instance import VerifiedContainerExecution
from .container_types import ContainerCommandResult
from .container_types import ContainerOutputCapture
from .container_types import command_workspace_authority_matches
from .container_types import container_command_id


TerminationSignalStep = Literal["terminate", "kill"]
TerminationWaitStep = Literal["termination_wait", "kill_wait"]


@dataclass(frozen=True)
class ContainerTerminationPlan:
    verified: VerifiedContainerExecution
    terminate_argv: tuple[str, ...]
    wait_argv: tuple[str, ...]
    kill_argv: tuple[str, ...]
    logs_argv: tuple[str, ...]

    def __post_init__(self) -> None:
        identity = self.verified.plan.identity
        container_id = self.verified.container_id
        if self.terminate_argv != identity.command(
            "kill",
            "--signal=TERM",
            container_id,
        ):
            raise ValueError("container termination argv does not match its instance")
        if self.wait_argv != identity.command("wait", container_id):
            raise ValueError("container termination wait argv does not match its instance")
        if self.kill_argv != identity.command(
            "kill",
            "--signal=KILL",
            container_id,
        ):
            raise ValueError("container kill argv does not match its instance")
        if self.logs_argv != identity.command("logs", container_id):
            raise ValueError("container termination logs argv does not match its instance")


@dataclass(frozen=True)
class ContainerTerminationStepResult:
    reason_code: str
    succeeded: bool

    def __post_init__(self) -> None:
        if not self.reason_code.strip():
            raise ValueError("container termination step reason must not be empty")


@dataclass(frozen=True)
class ContainerTerminationWaitResult:
    reason_code: str
    stopped: bool
    exit_code: int | None = None

    def __post_init__(self) -> None:
        if not self.reason_code.strip() or self.stopped != (self.exit_code is not None):
            raise ValueError("container termination wait result is inconsistent")


@dataclass(frozen=True)
class ContainerUserOutput:
    stdout: str
    stderr: str
    capture: ContainerOutputCapture


@dataclass(frozen=True)
class ContainerTerminationLogsResult:
    reason_code: str
    output: ContainerUserOutput | None = None

    def __post_init__(self) -> None:
        if not self.reason_code.strip():
            raise ValueError("container termination logs reason must not be empty")
        carries_output = self.reason_code in {
            "termination_logs_captured",
            "termination_logs_partial_cancelled",
            "termination_logs_partial_timed_out",
        }
        if carries_output != (self.output is not None):
            raise ValueError("container termination logs result is inconsistent")


def build_container_termination_plan(
    execution: VerifiedContainerExecution | ContainerReleasedExecution,
) -> ContainerTerminationPlan:
    verified = (
        execution.verified
        if isinstance(execution, ContainerReleasedExecution)
        else execution
    )
    if not isinstance(verified, VerifiedContainerExecution):
        raise TypeError("container termination requires a verified instance")
    identity = verified.plan.identity
    container_id = verified.container_id
    return ContainerTerminationPlan(
        verified=verified,
        terminate_argv=identity.command("kill", "--signal=TERM", container_id),
        wait_argv=identity.command("wait", container_id),
        kill_argv=identity.command("kill", "--signal=KILL", container_id),
        logs_argv=identity.command("logs", container_id),
    )


def parse_container_termination_signal_result(
    plan: ContainerTerminationPlan,
    result: ContainerCommandResult,
    *,
    step: TerminationSignalStep,
) -> ContainerTerminationStepResult:
    argv = plan.terminate_argv if step == "terminate" else plan.kill_argv
    failure = _result_failure(plan, result, step=step, argv=argv)
    return ContainerTerminationStepResult(
        f"{step}_{failure}" if failure is not None else f"{step}_sent",
        failure is None,
    )


def parse_container_termination_wait_result(
    plan: ContainerTerminationPlan,
    result: ContainerCommandResult,
    *,
    step: TerminationWaitStep,
) -> ContainerTerminationWaitResult:
    failure = _result_failure(plan, result, step=step, argv=plan.wait_argv)
    if failure is not None:
        return ContainerTerminationWaitResult(f"{step}_{failure}", False)
    rendered = result.stdout.strip()
    if result.stderr or not re.fullmatch(r"[0-9]{1,3}", rendered):
        return ContainerTerminationWaitResult(f"{step}_invalid_output", False)
    exit_code = int(rendered)
    if not 0 <= exit_code <= 255:
        return ContainerTerminationWaitResult(f"{step}_invalid_exit", False)
    return ContainerTerminationWaitResult(f"{step}_stopped", True, exit_code)


def parse_container_termination_logs_result(
    plan: ContainerTerminationPlan,
    result: ContainerCommandResult,
) -> ContainerTerminationLogsResult:
    failure = _result_failure(
        plan,
        result,
        step="termination_logs",
        argv=plan.logs_argv,
        require_complete=False,
    )
    if failure is not None:
        observed = (
            result.output_capture.stdout.observed_bytes
            + result.output_capture.stderr.observed_bytes
        )
        if failure in {"cancelled", "timed_out"} and observed:
            return ContainerTerminationLogsResult(
                f"termination_logs_partial_{failure}",
                ContainerUserOutput(
                    result.stdout,
                    result.stderr,
                    result.output_capture,
                ),
            )
        return ContainerTerminationLogsResult(f"termination_logs_{failure}")
    return ContainerTerminationLogsResult(
        "termination_logs_captured",
        ContainerUserOutput(result.stdout, result.stderr, result.output_capture),
    )


def _result_failure(
    plan: ContainerTerminationPlan,
    result: ContainerCommandResult,
    *,
    step: str,
    argv: tuple[str, ...],
    require_complete: bool = True,
) -> str | None:
    execution = plan.verified
    if (
        result.attempt_id != execution.plan.attempt_id
        or result.command_id != container_command_id(execution.plan.attempt_id, step)
        or result.step != step
        or result.argv != argv
    ):
        return "correlation_mismatch"
    if not command_workspace_authority_matches(
        execution.plan.identity.workspace_authority,
        result,
    ):
        return "workspace_authority_changed"
    if not execution.plan.identity.control_authority_is_current():
        return "engine_changed"
    if result.outcome != "exited":
        return result.outcome
    if result.exit_code != 0:
        return "failed"
    if require_complete and not result.output_capture.complete:
        return "output_incomplete"
    return None


__all__ = [
    "ContainerTerminationLogsResult",
    "ContainerTerminationPlan",
    "ContainerTerminationStepResult",
    "ContainerTerminationWaitResult",
    "ContainerUserOutput",
    "build_container_termination_plan",
    "parse_container_termination_logs_result",
    "parse_container_termination_signal_result",
    "parse_container_termination_wait_result",
]

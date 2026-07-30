from __future__ import annotations

import re
from dataclasses import dataclass

from .container_cleanup import ContainerCleanupHandle
from .container_cleanup import build_container_cleanup_handle
from .container_inspect_schema import build_container_inspect_argv
from .container_plan import GATE_READY_ATTEMPTS
from .container_plan import GATE_READY_CHECK
from .container_plan import GATE_READY_DELAY_SECONDS
from .container_plan import GATE_READY_TIMEOUT_SECONDS
from .container_plan import GATE_RELEASE_SIGNAL
from .container_plan import GATE_MOUNT_PROOF
from .container_plan import GATE_STAGE_PROOF
from .container_plan import ContainerDirectoryIdentity
from .container_plan import ContainerExecutionPlan
from .container_plan import mount_sources_unchanged
from .container_recovery import ContainerRecoveryObligation
from .container_recovery import build_container_recovery_obligation
from .container_types import ContainerCommandResult
from .container_types import ContainerOutputCapture
from .container_types import command_output_is_complete
from .container_types import container_command_id
from .container_types import command_workspace_authority_matches
from .contracts import AppliedIsolationProof
from .container_verification import normalized_container_id
from .container_verification import parse_container_inspect_payload
from .container_verification import running_state_mismatch
from .container_verification import static_container_mismatch_reason


_MAX_CREATE_OUTPUT_CHARS = 256
_MAX_CONTROL_OUTPUT_CHARS = 4_096


@dataclass(frozen=True)
class ContainerCreatedInstance:
    plan: ContainerExecutionPlan
    container_id: str
    start_argv: tuple[str, ...]
    cleanup: ContainerCleanupHandle

    def __post_init__(self) -> None:
        _validate_instance(self.plan, self.container_id, self.cleanup)
        if self.start_argv != self.plan.identity.command("start", self.container_id):
            raise ValueError("container start argv does not match its instance")


@dataclass(frozen=True)
class ContainerStartedGate:
    plan: ContainerExecutionPlan
    container_id: str
    ready_argv: tuple[str, ...]
    ready_timeout_seconds: int
    cleanup: ContainerCleanupHandle

    def __post_init__(self) -> None:
        _validate_instance(self.plan, self.container_id, self.cleanup)
        if self.ready_argv != self.plan.identity.command(
            "exec",
            self.container_id,
            GATE_READY_CHECK,
            "--attempt-id",
            self.plan.attempt_id,
            "--attempts",
            str(GATE_READY_ATTEMPTS),
            "--delay-seconds",
            GATE_READY_DELAY_SECONDS,
        ):
            raise ValueError("container readiness argv does not match its started gate")
        if self.ready_timeout_seconds != GATE_READY_TIMEOUT_SECONDS:
            raise ValueError("container readiness timeout does not match its protocol")


@dataclass(frozen=True)
class ContainerReadyGate:
    plan: ContainerExecutionPlan
    container_id: str
    mount_proof_argv: tuple[str, ...] | None
    stage_proof_argv: tuple[str, ...] | None
    cleanup: ContainerCleanupHandle

    def __post_init__(self) -> None:
        _validate_instance(self.plan, self.container_id, self.cleanup)
        if self.plan.workspace_transport == "direct-bind":
            expected = self.plan.identity.command(
                "exec",
                self.container_id,
                GATE_MOUNT_PROOF,
                *(str(mount.destination) for mount in self.plan.mounts),
            )
            if self.mount_proof_argv != expected or self.stage_proof_argv is not None:
                raise ValueError(
                    "container mount proof argv does not match its ready gate"
                )
        else:
            expected = self.plan.identity.command(
                "exec",
                self.container_id,
                GATE_STAGE_PROOF,
                *self.plan.stage_proof_arguments,
            )
            if self.stage_proof_argv != expected or self.mount_proof_argv is not None:
                raise ValueError(
                    "container staged proof argv does not match its ready gate"
                )


@dataclass(frozen=True)
class ContainerMountVerifiedGate:
    plan: ContainerExecutionPlan
    container_id: str
    proof_kind: str
    mount_identities: tuple[ContainerDirectoryIdentity, ...]
    manifest_digests: tuple[str, ...]
    inspect_argv: tuple[str, ...]
    cleanup: ContainerCleanupHandle

    def __post_init__(self) -> None:
        _validate_instance(self.plan, self.container_id, self.cleanup)
        if self.plan.workspace_transport == "direct-bind":
            if (
                self.proof_kind != "mount-object-identity"
                or self.mount_identities
                != tuple(mount.source_identity for mount in self.plan.mounts)
                or self.manifest_digests
            ):
                raise ValueError("container mount proof does not match its plan")
        elif (
            self.proof_kind != "staged-manifest"
            or self.mount_identities
            or self.manifest_digests != self.plan.staged_manifest_digests
        ):
            raise ValueError("container staged proof does not match its plan")
        if self.inspect_argv != build_container_inspect_argv(
            self.plan.identity,
            self.container_id,
        ):
            raise ValueError("container inspect argv does not match its mount proof")


@dataclass(frozen=True)
class VerifiedContainerExecution:
    plan: ContainerExecutionPlan
    container_id: str
    proof: AppliedIsolationProof
    release_argv: tuple[str, ...]
    cleanup: ContainerCleanupHandle

    def __post_init__(self) -> None:
        _validate_instance(self.plan, self.container_id, self.cleanup)
        expected_release = self.plan.identity.command(
            "kill",
            f"--signal={GATE_RELEASE_SIGNAL}",
            self.container_id,
        )
        if self.release_argv != expected_release:
            raise ValueError("container release argv does not match its verified instance")
        if self.proof.backend_instance_id != self.container_id:
            raise ValueError("container proof identity does not match its instance")
        if (
            self.proof.backend != "container"
            or self.proof.profile != self.plan.request.profile
            or self.proof.network_policy != self.plan.request.network_policy
            or self.proof.workspace != self.plan.workspace
            or self.proof.readable_roots != self.plan.readable_roots
            or self.proof.writable_roots != self.plan.writable_roots
            or self.proof.image_digest != self.plan.image_digest
        ):
            raise ValueError("container proof does not match the verified plan")


@dataclass(frozen=True)
class ContainerReleasedExecution:
    verified: VerifiedContainerExecution
    wait_argv: tuple[str, ...]

    def __post_init__(self) -> None:
        expected = self.verified.plan.identity.command(
            "wait",
            self.verified.container_id,
        )
        if self.wait_argv != expected:
            raise ValueError("container wait argv does not match its released instance")


@dataclass(frozen=True)
class ContainerWaitedExecution:
    released: ContainerReleasedExecution
    command_exit_code: int
    final_inspect_argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 0 <= self.command_exit_code <= 255:
            raise ValueError("container command exit code must be between 0 and 255")
        expected = build_container_inspect_argv(
            self.released.verified.plan.identity,
            self.released.verified.container_id,
        )
        if self.final_inspect_argv != expected:
            raise ValueError("container final inspect argv does not match its instance")


@dataclass(frozen=True)
class ContainerExitedExecution:
    waited: ContainerWaitedExecution
    oom_killed: bool
    logs_argv: tuple[str, ...]

    def __post_init__(self) -> None:
        expected = self.waited.released.verified.plan.identity.command(
            "logs",
            self.waited.released.verified.container_id,
        )
        if self.logs_argv != expected:
            raise ValueError("container logs argv does not match its exited instance")

    @property
    def cleanup(self) -> ContainerCleanupHandle:
        return self.waited.released.verified.cleanup

    @property
    def command_exit_code(self) -> int:
        return self.waited.command_exit_code


@dataclass(frozen=True)
class ContainerCapturedExecution:
    exited: ContainerExitedExecution
    stdout: str
    stderr: str
    output_capture: ContainerOutputCapture

    @property
    def cleanup(self) -> ContainerCleanupHandle:
        return self.exited.cleanup

    @property
    def command_exit_code(self) -> int:
        return self.exited.command_exit_code


@dataclass(frozen=True)
class ContainerCreateResult:
    reason_code: str
    created: ContainerCreatedInstance | None = None
    recovery: ContainerRecoveryObligation | None = None
    unresolved_without_correlation: bool = False

    def __post_init__(self) -> None:
        success = self.reason_code == "container_created"
        if not self.reason_code.strip() or success != (self.created is not None):
            raise ValueError("only container_created may expose a created instance")
        uncorrelated = self.reason_code == "create_correlation_mismatch"
        if uncorrelated != self.unresolved_without_correlation:
            raise ValueError(
                "only a correlation mismatch may be unresolved without recovery"
            )
        if sum(
            (
                self.created is not None,
                self.recovery is not None,
                self.unresolved_without_correlation,
            )
        ) != 1:
            raise ValueError("container create result must retain exactly one outcome")


@dataclass(frozen=True)
class ContainerStartResult:
    reason_code: str
    started: ContainerStartedGate | None = None

    def __post_init__(self) -> None:
        _validate_optional_result(self.reason_code, "gate_started", self.started)


@dataclass(frozen=True)
class ContainerGateReadyResult:
    reason_code: str
    ready: ContainerReadyGate | None = None

    def __post_init__(self) -> None:
        _validate_optional_result(self.reason_code, "gate_ready", self.ready)


@dataclass(frozen=True)
class ContainerMountProofResult:
    reason_code: str
    mounted: ContainerMountVerifiedGate | None = None

    def __post_init__(self) -> None:
        if not self.reason_code.strip():
            raise ValueError("container result reason_code must not be empty")
        success = self.reason_code in {
            "mount_object_identity_verified",
            "staged_manifest_verified",
        }
        if success != (self.mounted is not None):
            raise ValueError("only a workspace proof may expose a verified mount")
        if self.mounted is not None:
            expected = (
                "mount_object_identity_verified"
                if self.mounted.proof_kind == "mount-object-identity"
                else "staged_manifest_verified"
            )
            if self.reason_code != expected:
                raise ValueError("container workspace proof result is inconsistent")


@dataclass(frozen=True)
class ContainerInspectResult:
    reason_code: str
    verified: VerifiedContainerExecution | None = None

    def __post_init__(self) -> None:
        _validate_optional_result(self.reason_code, "inspect_applied", self.verified)


@dataclass(frozen=True)
class ContainerReleaseResult:
    reason_code: str
    released: ContainerReleasedExecution | None = None

    def __post_init__(self) -> None:
        _validate_optional_result(self.reason_code, "gate_released", self.released)


@dataclass(frozen=True)
class ContainerWaitResult:
    reason_code: str
    waited: ContainerWaitedExecution | None = None

    def __post_init__(self) -> None:
        _validate_optional_result(self.reason_code, "container_waited", self.waited)


@dataclass(frozen=True)
class ContainerFinalInspectResult:
    reason_code: str
    exited: ContainerExitedExecution | None = None

    def __post_init__(self) -> None:
        _validate_optional_result(self.reason_code, "final_state_verified", self.exited)


@dataclass(frozen=True)
class ContainerLogsResult:
    reason_code: str
    captured: ContainerCapturedExecution | None = None

    def __post_init__(self) -> None:
        _validate_optional_result(self.reason_code, "logs_captured", self.captured)


def parse_container_create_result(
    plan: ContainerExecutionPlan,
    result: ContainerCommandResult,
) -> ContainerCreateResult:
    failure = _result_failure(plan, "create", plan.create_argv, result)
    if failure == "correlation_mismatch":
        return ContainerCreateResult(
            "create_correlation_mismatch",
            unresolved_without_correlation=True,
        )
    if failure is not None:
        return ContainerCreateResult(
            f"create_{failure}",
            recovery=build_container_recovery_obligation(plan, result),
        )
    if result.stderr:
        return ContainerCreateResult(
            "create_unexpected_stderr",
            recovery=build_container_recovery_obligation(plan, result),
        )
    if len(result.stdout) > _MAX_CREATE_OUTPUT_CHARS:
        return ContainerCreateResult(
            "create_invalid_identity",
            recovery=build_container_recovery_obligation(plan, result),
        )
    container_id = normalized_container_id(result.stdout.strip())
    if container_id is None:
        return ContainerCreateResult(
            "create_invalid_identity",
            recovery=build_container_recovery_obligation(plan, result),
        )
    cleanup = build_container_cleanup_handle(plan, container_id)
    return ContainerCreateResult(
        "container_created",
        ContainerCreatedInstance(
            plan=plan,
            container_id=container_id,
            start_argv=plan.identity.command("start", container_id),
            cleanup=cleanup,
        ),
    )


def parse_container_start_result(
    created: ContainerCreatedInstance,
    result: ContainerCommandResult,
) -> ContainerStartResult:
    failure = _result_failure(
        created.plan,
        "start",
        created.start_argv,
        result,
    )
    if failure is not None:
        return ContainerStartResult(f"start_{failure}")
    if result.stderr:
        return ContainerStartResult("start_unexpected_stderr")
    if len(result.stdout) > _MAX_CONTROL_OUTPUT_CHARS:
        return ContainerStartResult("start_output_too_large")
    return ContainerStartResult(
        "gate_started",
        ContainerStartedGate(
            plan=created.plan,
            container_id=created.container_id,
            ready_argv=created.plan.identity.command(
                "exec",
                created.container_id,
                GATE_READY_CHECK,
                "--attempt-id",
                created.plan.attempt_id,
                "--attempts",
                str(GATE_READY_ATTEMPTS),
                "--delay-seconds",
                GATE_READY_DELAY_SECONDS,
            ),
            ready_timeout_seconds=GATE_READY_TIMEOUT_SECONDS,
            cleanup=created.cleanup,
        ),
    )


def parse_container_gate_ready_result(
    started: ContainerStartedGate,
    result: ContainerCommandResult,
) -> ContainerGateReadyResult:
    failure = _result_failure(
        started.plan,
        "gate_ready",
        started.ready_argv,
        result,
    )
    if failure is not None:
        return ContainerGateReadyResult(f"gate_ready_{failure}")
    if result.stdout or result.stderr:
        return ContainerGateReadyResult("gate_ready_unexpected_output")
    return ContainerGateReadyResult(
        "gate_ready",
        ContainerReadyGate(
            plan=started.plan,
            container_id=started.container_id,
            mount_proof_argv=(
                started.plan.identity.command(
                    "exec",
                    started.container_id,
                    GATE_MOUNT_PROOF,
                    *(str(mount.destination) for mount in started.plan.mounts),
                )
                if started.plan.workspace_transport == "direct-bind"
                else None
            ),
            stage_proof_argv=(
                started.plan.identity.command(
                    "exec",
                    started.container_id,
                    GATE_STAGE_PROOF,
                    *started.plan.stage_proof_arguments,
                )
                if started.plan.workspace_transport == "staged-copy"
                else None
            ),
            cleanup=started.cleanup,
        ),
    )


def parse_container_mount_proof_result(
    ready: ContainerReadyGate,
    result: ContainerCommandResult,
) -> ContainerMountProofResult:
    if not isinstance(ready, ContainerReadyGate):
        raise TypeError("mount-object proof requires a ready gate")
    if ready.plan.workspace_transport != "direct-bind" or ready.mount_proof_argv is None:
        raise TypeError("mount-object proof requires a direct-bind plan")
    failure = _result_failure(
        ready.plan,
        "mount_proof",
        ready.mount_proof_argv,
        result,
    )
    if failure is not None:
        return ContainerMountProofResult(f"mount_proof_{failure}")
    if result.stderr:
        return ContainerMountProofResult("mount_proof_unexpected_stderr")
    if len(result.stdout) > _MAX_CONTROL_OUTPUT_CHARS:
        return ContainerMountProofResult("mount_proof_output_too_large")
    try:
        observed = tuple(
            _parse_directory_identity(line)
            for line in result.stdout.splitlines()
        )
    except ValueError:
        return ContainerMountProofResult("mount_proof_invalid_output")
    expected = tuple(mount.source_identity for mount in ready.plan.mounts)
    if observed != expected:
        return ContainerMountProofResult("mount_object_identity_mismatch")
    if not mount_sources_unchanged(ready.plan.mounts):
        return ContainerMountProofResult("mount_source_identity_changed")
    return ContainerMountProofResult(
        "mount_object_identity_verified",
        ContainerMountVerifiedGate(
            plan=ready.plan,
            container_id=ready.container_id,
            proof_kind="mount-object-identity",
            mount_identities=observed,
            manifest_digests=(),
            inspect_argv=build_container_inspect_argv(
                ready.plan.identity,
                ready.container_id,
            ),
            cleanup=ready.cleanup,
        ),
    )


def parse_container_stage_proof_result(
    ready: ContainerReadyGate,
    result: ContainerCommandResult,
) -> ContainerMountProofResult:
    if not isinstance(ready, ContainerReadyGate):
        raise TypeError("staged manifest proof requires a ready gate")
    if ready.plan.workspace_transport != "staged-copy" or ready.stage_proof_argv is None:
        raise TypeError("staged manifest proof requires a staged-copy plan")
    failure = _result_failure(
        ready.plan,
        "stage_proof",
        ready.stage_proof_argv,
        result,
    )
    if failure is not None:
        return ContainerMountProofResult(f"stage_proof_{failure}")
    if result.stdout or result.stderr:
        return ContainerMountProofResult("stage_proof_unexpected_output")
    staging = ready.plan.staging
    if staging is None or not staging.authority_is_current():
        return ContainerMountProofResult("stage_authority_changed")
    return ContainerMountProofResult(
        "staged_manifest_verified",
        ContainerMountVerifiedGate(
            plan=ready.plan,
            container_id=ready.container_id,
            proof_kind="staged-manifest",
            mount_identities=(),
            manifest_digests=ready.plan.staged_manifest_digests,
            inspect_argv=build_container_inspect_argv(
                ready.plan.identity,
                ready.container_id,
            ),
            cleanup=ready.cleanup,
        ),
    )


def parse_container_inspect_result(
    ready: ContainerMountVerifiedGate,
    result: ContainerCommandResult,
) -> ContainerInspectResult:
    if not isinstance(ready, ContainerMountVerifiedGate):
        raise TypeError("applied inspect requires a workspace proof")
    failure = _result_failure(
        ready.plan,
        "inspect",
        ready.inspect_argv,
        result,
    )
    if failure is not None:
        return ContainerInspectResult(f"inspect_{failure}")
    if result.stderr:
        return ContainerInspectResult("inspect_unexpected_stderr")
    payload, failure = parse_container_inspect_payload(result.stdout)
    if failure is not None:
        return ContainerInspectResult(failure)
    assert payload is not None
    mismatch = static_container_mismatch_reason(ready.plan, ready.container_id, payload)
    if mismatch is None:
        mismatch = running_state_mismatch(payload)
    if mismatch is not None:
        return ContainerInspectResult(mismatch)
    plan = ready.plan
    proof = AppliedIsolationProof(
        backend="container",
        backend_instance_id=ready.container_id,
        profile=plan.request.profile,
        network_policy=plan.request.network_policy,
        workspace=plan.workspace,
        readable_roots=plan.readable_roots,
        writable_roots=plan.writable_roots,
        image_digest=plan.image_digest,
    )
    return ContainerInspectResult(
        "inspect_applied",
        VerifiedContainerExecution(
            plan=plan,
            container_id=ready.container_id,
            proof=proof,
            release_argv=plan.identity.command(
                "kill",
                f"--signal={GATE_RELEASE_SIGNAL}",
                ready.container_id,
            ),
            cleanup=ready.cleanup,
        ),
    )


def parse_container_release_result(
    verified: VerifiedContainerExecution,
    result: ContainerCommandResult,
) -> ContainerReleaseResult:
    failure = _result_failure(
        verified.plan,
        "release",
        verified.release_argv,
        result,
    )
    if failure is not None:
        return ContainerReleaseResult(f"release_{failure}")
    if result.stderr:
        return ContainerReleaseResult("release_unexpected_stderr")
    if len(result.stdout) > _MAX_CONTROL_OUTPUT_CHARS:
        return ContainerReleaseResult("release_output_too_large")
    return ContainerReleaseResult(
        "gate_released",
        ContainerReleasedExecution(
            verified=verified,
            wait_argv=verified.plan.identity.command("wait", verified.container_id),
        ),
    )


def parse_container_wait_result(
    released: ContainerReleasedExecution,
    result: ContainerCommandResult,
) -> ContainerWaitResult:
    plan = released.verified.plan
    failure = _result_failure(plan, "wait", released.wait_argv, result)
    if failure is not None:
        return ContainerWaitResult(f"wait_{failure}")
    if result.stderr:
        return ContainerWaitResult("wait_unexpected_stderr")
    if len(result.stdout) > _MAX_CONTROL_OUTPUT_CHARS:
        return ContainerWaitResult("wait_output_too_large")
    rendered = result.stdout.strip()
    if not re.fullmatch(r"[0-9]{1,3}", rendered):
        return ContainerWaitResult("wait_invalid_exit")
    exit_code = int(rendered)
    if not 0 <= exit_code <= 255:
        return ContainerWaitResult("wait_invalid_exit")
    return ContainerWaitResult(
        "container_waited",
        ContainerWaitedExecution(
            released=released,
            command_exit_code=exit_code,
            final_inspect_argv=build_container_inspect_argv(
                plan.identity,
                released.verified.container_id,
            ),
        ),
    )


def parse_container_final_inspect_result(
    waited: ContainerWaitedExecution,
    result: ContainerCommandResult,
) -> ContainerFinalInspectResult:
    plan = waited.released.verified.plan
    failure = _result_failure(plan, "final_inspect", waited.final_inspect_argv, result)
    if failure is not None:
        return ContainerFinalInspectResult(f"final_inspect_{failure}")
    if result.stderr:
        return ContainerFinalInspectResult("final_inspect_unexpected_stderr")
    payload, failure = parse_container_inspect_payload(result.stdout)
    if failure is not None:
        return ContainerFinalInspectResult(
            f"final_{failure.removeprefix('inspect_')}"
        )
    assert payload is not None
    mismatch = static_container_mismatch_reason(
        plan,
        waited.released.verified.container_id,
        payload,
        require_source_path_identity=False,
    )
    if mismatch is not None:
        return ContainerFinalInspectResult(f"final_{mismatch.removeprefix('inspect_')}")
    if (
        payload.get("state_status") != "exited"
        or payload.get("state_running") is not False
        or payload.get("state_exit_code") != waited.command_exit_code
    ):
        return ContainerFinalInspectResult("final_state_mismatch")
    state_error = payload.get("state_error")
    if not isinstance(state_error, str) or state_error:
        return ContainerFinalInspectResult("final_state_error")
    oom_killed = payload.get("state_oom_killed")
    if not isinstance(oom_killed, bool):
        return ContainerFinalInspectResult("final_oom_state_invalid")
    return ContainerFinalInspectResult(
        "final_state_verified",
        ContainerExitedExecution(
            waited=waited,
            oom_killed=oom_killed,
            logs_argv=plan.identity.command(
                "logs",
                waited.released.verified.container_id,
            ),
        ),
    )


def parse_container_logs_result(
    exited: ContainerExitedExecution,
    result: ContainerCommandResult,
) -> ContainerLogsResult:
    plan = exited.waited.released.verified.plan
    failure = _result_failure(
        plan,
        "logs",
        exited.logs_argv,
        result,
        require_complete_output=False,
    )
    if failure is not None:
        return ContainerLogsResult(f"logs_{failure}")
    return ContainerLogsResult(
        "logs_captured",
        ContainerCapturedExecution(
            exited=exited,
            stdout=result.stdout,
            stderr=result.stderr,
            output_capture=result.output_capture,
        ),
    )


def _result_failure(
    plan: ContainerExecutionPlan,
    step: str,
    argv: tuple[str, ...],
    result: ContainerCommandResult,
    *,
    require_engine_current: bool = True,
    require_complete_output: bool = True,
) -> str | None:
    if (
        result.attempt_id != plan.attempt_id
        or result.command_id != container_command_id(plan.attempt_id, step)
        or result.step != step
        or result.argv != argv
    ):
        return "correlation_mismatch"
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
    if require_complete_output and not command_output_is_complete(result):
        return "output_incomplete"
    if require_engine_current and not plan.identity.control_authority_is_current():
        return "engine_changed"
    return None


def _validate_instance(
    plan: ContainerExecutionPlan,
    container_id: str,
    cleanup: ContainerCleanupHandle,
) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise ValueError("container identity must contain 64 lowercase hex characters")
    if cleanup.plan != plan or cleanup.container_id != container_id:
        raise ValueError("container cleanup handle does not match its instance")


def _validate_optional_result(reason_code: str, success: str, value: object | None) -> None:
    if not reason_code.strip():
        raise ValueError("container result reason_code must not be empty")
    if (reason_code == success) != (value is not None):
        raise ValueError(f"only {success} may expose a successful value")


def _parse_directory_identity(raw: str) -> ContainerDirectoryIdentity:
    match = re.fullmatch(r"([0-9]+):([0-9]+)", raw)
    if match is None:
        raise ValueError("container mount identity output is invalid")
    return ContainerDirectoryIdentity(int(match.group(1)), int(match.group(2)))

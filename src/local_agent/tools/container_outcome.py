from __future__ import annotations

from dataclasses import dataclass

from ..execution.contracts import AppliedIsolationProof
from ..protocol.cancellation import RunCancelled
from ..workspace.snapshot_delta import WorkspaceTextMutationPlan
from .process_output import CapturedCompletedProcess
from .process_output import ProcessOutputCapture


@dataclass(frozen=True)
class ContainerCleanupSummary:
    reason_code: str
    verified: bool
    unresolved: bool

    def __post_init__(self) -> None:
        if not self.reason_code.strip() or self.verified == self.unresolved:
            raise ValueError("container cleanup summary is invalid")


@dataclass(frozen=True)
class ContainerExecutionOutcome:
    reason_code: str
    attempt_id: str
    completed: CapturedCompletedProcess | None = None
    proof: AppliedIsolationProof | None = None
    cleanup: ContainerCleanupSummary | None = None
    staging_cleanup: ContainerCleanupSummary | None = None
    workspace_transport: str = "direct-bind"
    workspace_output_plan: WorkspaceTextMutationPlan | None = None
    workspace_output_captured: bool = False
    cancellation: RunCancelled | None = None
    command_release_state: str = "not_attempted"
    recovery_unresolved: bool = False
    execution_outcome: str = "not_run"
    user_output: ProcessOutputCapture | None = None
    termination_reason_code: str | None = None
    user_output_reason_code: str | None = None

    def __post_init__(self) -> None:
        if not self.reason_code.strip():
            raise ValueError("container execution reason_code must not be empty")
        if self.workspace_transport not in {"direct-bind", "staged-copy"}:
            raise ValueError("container workspace transport is invalid")
        if (
            self.workspace_output_plan is not None
            and self.workspace_transport != "staged-copy"
        ):
            raise ValueError("container workspace output requires staged-copy")
        if (
            self.workspace_output_captured
            and self.execution_outcome != "exited"
        ):
            raise ValueError(
                "container workspace output capture requires normal exit"
            )
        if self.execution_outcome not in {
            "exited",
            "timed_out",
            "cancelled",
            "not_run",
            "spawn_failed",
            "indeterminate",
        }:
            raise ValueError("container execution outcome is invalid")
        if self.command_release_state not in {
            "not_attempted",
            "ambiguous",
            "verified",
        }:
            raise ValueError("container command release state is invalid")
        success = self.reason_code == "container_execution_completed"
        if success and not (
            self.completed is not None
            and self.proof is not None
            and self.cleanup is not None
            and self.cleanup.verified
            and (
                self.workspace_transport == "direct-bind"
                or (
                    self.staging_cleanup is not None
                    and self.staging_cleanup.verified
                )
            )
            and self.execution_outcome == "exited"
        ):
            raise ValueError("container execution success fields are incomplete")
        if self.completed is not None and not self.command_released:
            raise ValueError("container output requires a released command")
        if self.user_output is not None and not self.command_released:
            raise ValueError("container user output requires a released command")
        if self.user_output is not None and self.user_output_reason_code is None:
            raise ValueError("container user output provenance is inconsistent")
        if (
            self.user_output_reason_code is not None
            and not self.user_output_reason_code.strip()
        ):
            raise ValueError("container user output reason must not be empty")
        if self.cancellation is not None and self.execution_outcome != "cancelled":
            raise ValueError("container cancellation outcome is inconsistent")

    @property
    def command_released(self) -> bool:
        return self.command_release_state == "verified"

    def metadata(self) -> dict[str, object]:
        proof = self.proof
        cleanup = self.cleanup
        staging_cleanup = self.staging_cleanup
        workspace_output = self.workspace_output_plan
        cleanup_verified = (
            cleanup is not None
            and cleanup.verified
            and (
                self.workspace_transport == "direct-bind"
                or (
                    staging_cleanup is not None
                    and staging_cleanup.verified
                )
            )
        )
        return {
            "sandboxed": proof is not None,
            "isolation": {
                "backend": "container",
                "reason_code": self.reason_code,
                "attempt_id": self.attempt_id,
                "applied": proof is not None,
                "profile": proof.profile if proof is not None else None,
                "network_policy": (
                    proof.network_policy if proof is not None else None
                ),
                "image_digest": (
                    proof.image_digest if proof is not None else None
                ),
                "cleanup": (
                    cleanup.reason_code
                    if cleanup is not None
                    else "not_applicable"
                ),
                "cleanup_verified": (
                    cleanup_verified
                ),
                "workspace_transport": self.workspace_transport,
                "staging_cleanup": (
                    staging_cleanup.reason_code
                    if staging_cleanup is not None
                    else "not_applicable"
                ),
                "staging_cleanup_verified": (
                    staging_cleanup.verified
                    if staging_cleanup is not None
                    else self.workspace_transport == "direct-bind"
                ),
                "workspace_output": (
                    {
                        "planned": True,
                        "captured": self.workspace_output_captured,
                        "file_count": len(workspace_output.changes),
                        "before_manifest_sha256": (
                            workspace_output.before.manifest_sha256
                        ),
                        "after_manifest_sha256": (
                            workspace_output.after.manifest_sha256
                        ),
                    }
                    if workspace_output is not None
                    else {
                        "planned": False,
                        "captured": self.workspace_output_captured,
                        "file_count": 0,
                    }
                ),
                "recovery_unresolved": self.recovery_unresolved,
                "execution_outcome": self.execution_outcome,
                "command_release_state": self.command_release_state,
                "termination_reason_code": self.termination_reason_code,
                "user_output_reason_code": self.user_output_reason_code,
                "user_output_capture": (
                    self.user_output.to_metadata()
                    if self.user_output is not None
                    else None
                ),
            },
        }


__all__ = ["ContainerCleanupSummary", "ContainerExecutionOutcome"]

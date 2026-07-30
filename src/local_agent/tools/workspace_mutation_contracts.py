from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


WorkspaceMutationState = Literal[
    "committed",
    "stale",
    "restored",
    "indeterminate",
]


@dataclass(frozen=True)
class ContainerMutationProvenance:
    attempt_id: str
    image_digest: str | None
    profile: str
    workspace_transport: str

    def __post_init__(self) -> None:
        if (
            not self.attempt_id.strip()
            or self.profile != "workspace-write"
            or self.workspace_transport != "staged-copy"
        ):
            raise ValueError("container mutation provenance is invalid")


@dataclass(frozen=True)
class WorkspaceMutationCommitResult:
    state: WorkspaceMutationState
    workspace_changed: bool
    transaction_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    transaction_id: str | None = None
    error_kind: str | None = None
    before_manifest_sha256: str | None = None
    after_manifest_sha256: str | None = None

    @property
    def committed(self) -> bool:
        return self.state == "committed"

    def metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "workspace_changed": self.workspace_changed,
            "changed_paths": list(self.changed_paths),
            "effective_changed_paths": list(self.changed_paths),
            "transaction_paths": list(self.transaction_paths),
            "transaction_status": self.state,
            "workspace_state": self.state,
            "error_kind": self.error_kind,
            "workspace_transaction_id": self.transaction_id,
            "before_manifest_sha256": self.before_manifest_sha256,
            "after_manifest_sha256": self.after_manifest_sha256,
            "workspace_mutation_source": "container_staged_copy",
        }
        if self.transaction_id is not None:
            metadata["transaction_id"] = self.transaction_id
        return metadata


__all__ = [
    "ContainerMutationProvenance",
    "WorkspaceMutationCommitResult",
    "WorkspaceMutationState",
]

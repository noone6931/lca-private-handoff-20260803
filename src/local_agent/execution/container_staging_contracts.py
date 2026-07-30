from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .container_types import ContainerDirectoryPathIdentity
from .container_types import ContainerFileIdentity
from .container_types import ContainerRootIdentity
from .container_types import validate_attempt_id


STAGING_JOURNAL_SCHEMA = "lca.container_staging_attempt.v3"
STAGING_RECORD_STATES = frozenset(
    {
        "reserved",
        "allocated",
        "prepared",
        "create_possible",
        "execution_create_possible",
        "execution_absent",
        "container_absent",
        "cleanup_started",
    }
)


class ContainerStagingError(RuntimeError):
    """A typed fail-closed result for private container staging."""

    def __init__(self, kind: str, *, cleanup_verified: bool = False) -> None:
        super().__init__(kind)
        self.kind = kind
        self.cleanup_verified = cleanup_verified


@dataclass(frozen=True)
class ContainerStagingLifecycleResult:
    reason_code: str
    verified: bool
    unresolved: bool

    def __post_init__(self) -> None:
        if not self.reason_code or self.verified == self.unresolved:
            raise ValueError("container staging lifecycle result is invalid")


@dataclass(frozen=True)
class ContainerStagingRecoveryResult:
    reason_code: str
    ready: bool
    unresolved: bool
    recovered_attempts: int = 0

    def __post_init__(self) -> None:
        if (
            not self.reason_code
            or self.ready == self.unresolved
            or self.recovered_attempts < 0
        ):
            raise ValueError("container staging recovery result is invalid")


@dataclass(frozen=True)
class ContainerStagingContainerRecoveryResult:
    reason_code: str
    absent_verified: bool
    unresolved: bool

    def __post_init__(self) -> None:
        if not self.reason_code or self.absent_verified == self.unresolved:
            raise ValueError(
                "container staging recovery outcome is invalid"
            )


@dataclass(frozen=True)
class ContainerStagingWorkspaceBinding:
    path: Path
    device: int
    inode: int
    manifest_sha256: str

    def __post_init__(self) -> None:
        if (
            not self.path.is_absolute()
            or self.device < 0
            or self.inode <= 0
            or len(self.manifest_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.manifest_sha256
            )
        ):
            raise ValueError("container staging workspace binding is invalid")


@dataclass(frozen=True)
class ContainerStagingContainerBinding:
    instance_name: str
    prep_instance_name: str
    volume_names: tuple[str, ...]
    runtime_image: str
    executable: Path
    executable_sha256: str
    socket_path: Path
    socket_identity: ContainerFileIdentity
    client_config_directory: Path
    client_config_identity: ContainerFileIdentity
    gate_image_reference: str
    gate_image_digest: str

    def __post_init__(self) -> None:
        for path in (
            self.executable,
            self.socket_path,
            self.client_config_directory,
        ):
            if not path.is_absolute():
                raise ValueError(
                    "container staging backend path must be absolute"
                )
        attempt_id = self.instance_name.removeprefix("lca-")
        validate_attempt_id(attempt_id)
        if (
            self.instance_name != f"lca-{attempt_id}"
            or self.prep_instance_name != f"lca-{attempt_id}-prep"
            or not self.volume_names
            or self.volume_names
            != tuple(
                f"lca-{attempt_id}-root-{ordinal:04d}"
                for ordinal in range(len(self.volume_names))
            )
            or not self.runtime_image.startswith("sha256:")
            or len(self.runtime_image) != 71
            or any(
                character not in "0123456789abcdef"
                for character in self.runtime_image.removeprefix("sha256:")
            )
            or len(self.executable_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.executable_sha256
            )
            or not self.gate_image_reference
            or not self.gate_image_digest.startswith("sha256:")
            or len(self.gate_image_digest) != 71
            or any(
                character not in "0123456789abcdef"
                for character in self.gate_image_digest.removeprefix("sha256:")
            )
        ):
            raise ValueError(
                "container staging backend binding is invalid"
            )


@dataclass(frozen=True)
class ContainerStagingJournalRecord:
    attempt_id: str
    state: str
    authority_root: Path
    authority_identity: ContainerRootIdentity
    authority_path_identity: ContainerDirectoryPathIdentity
    attempt_identity: ContainerRootIdentity | None
    attempt_path_identity: ContainerDirectoryPathIdentity | None
    workspace_roots_revision: int
    workspace_roots: tuple[ContainerStagingWorkspaceBinding, ...]
    container: ContainerStagingContainerBinding | None = None

    def __post_init__(self) -> None:
        validate_attempt_id(self.attempt_id)
        if (
            self.state not in STAGING_RECORD_STATES
            or not self.authority_root.is_absolute()
            or self.workspace_roots_revision < 0
            or not self.workspace_roots
            or len({binding.path for binding in self.workspace_roots})
            != len(self.workspace_roots)
        ):
            raise ValueError("container staging journal record is invalid")
        if (self.authority_identity.device, self.authority_identity.inode) != (
            self.authority_path_identity.device,
            self.authority_path_identity.inode,
        ):
            raise ValueError("container staging journal identity is invalid")
        attempt_present = (
            self.attempt_identity is not None
            and self.attempt_path_identity is not None
        )
        if attempt_present and (
            self.attempt_identity.device,
            self.attempt_identity.inode,
        ) != (
            self.attempt_path_identity.device,
            self.attempt_path_identity.inode,
        ):
            raise ValueError("container staging attempt identity is invalid")
        if (self.attempt_identity is None) != (
            self.attempt_path_identity is None
        ):
            raise ValueError("container staging attempt identity is incomplete")
        if self.state == "reserved" and (
            attempt_present or self.container is not None
        ):
            raise ValueError("reserved staging record is invalid")
        if self.state != "reserved" and not attempt_present:
            raise ValueError("staging attempt identity is required")
        if (
            self.state
            in {
                "create_possible",
                "execution_create_possible",
                "execution_absent",
            }
            and self.container is None
        ) or (
            self.state in {"reserved", "allocated", "prepared"}
            and self.container is not None
        ):
            raise ValueError("staging container binding is invalid")
        if self.state in {
            "execution_create_possible",
            "execution_absent",
            "container_absent",
            "cleanup_started",
        } and (
            self.container is not None
            and self.container.instance_name != f"lca-{self.attempt_id}"
        ):
            raise ValueError("staging container correlation is invalid")
        if self.container is not None and (
            self.container.instance_name != f"lca-{self.attempt_id}"
        ):
            raise ValueError("staging container correlation is invalid")

    @property
    def attempt_path(self) -> Path:
        return self.authority_root / self.attempt_id

    def payload(self) -> dict[str, object]:
        return {
            "schema": STAGING_JOURNAL_SCHEMA,
            "attempt_id": self.attempt_id,
            "state": self.state,
            "authority_root": str(self.authority_root),
            "authority_identity": _root_payload(self.authority_identity),
            "authority_path_identity": _directory_payload(
                self.authority_path_identity
            ),
            "attempt_identity": (
                _root_payload(self.attempt_identity)
                if self.attempt_identity is not None
                else None
            ),
            "attempt_path_identity": (
                _directory_payload(self.attempt_path_identity)
                if self.attempt_path_identity is not None
                else None
            ),
            "workspace_roots_revision": self.workspace_roots_revision,
            "workspace_roots": [
                {
                    "path": str(binding.path),
                    "device": binding.device,
                    "inode": binding.inode,
                    "manifest_sha256": binding.manifest_sha256,
                }
                for binding in self.workspace_roots
            ],
            "container": (
                _container_payload(self.container)
                if self.container is not None
                else None
            ),
        }


def staging_record_transition_is_valid(
    before: ContainerStagingJournalRecord,
    after: ContainerStagingJournalRecord,
) -> bool:
    allowed = {
        ("reserved", "allocated"),
        ("allocated", "prepared"),
        ("allocated", "container_absent"),
        ("prepared", "create_possible"),
        ("prepared", "container_absent"),
        ("create_possible", "execution_create_possible"),
        ("create_possible", "container_absent"),
        ("execution_create_possible", "execution_absent"),
        ("execution_absent", "container_absent"),
        ("container_absent", "cleanup_started"),
    }
    if (before.state, after.state) not in allowed:
        return False
    if (
        before.attempt_id != after.attempt_id
        or before.authority_root != after.authority_root
        or before.authority_identity != after.authority_identity
        or before.workspace_roots_revision != after.workspace_roots_revision
        or before.workspace_roots != after.workspace_roots
        or not _same_directory_object(
            before.authority_path_identity,
            after.authority_path_identity,
        )
    ):
        return False
    if before.state == "reserved":
        return (
            before.attempt_identity is None
            and after.attempt_identity is not None
            and after.attempt_path_identity is not None
            and after.container is None
        )
    if (
        before.attempt_identity != after.attempt_identity
        or before.attempt_path_identity is None
        or after.attempt_path_identity is None
        or not _same_directory_object(
            before.attempt_path_identity,
            after.attempt_path_identity,
        )
    ):
        return False
    if before.state == "allocated":
        return after.container is None
    if before.state == "prepared" and after.state == "create_possible":
        return before.container is None and after.container is not None
    return before.container == after.container


def _root_payload(identity: ContainerRootIdentity) -> dict[str, int]:
    return {
        "device": identity.device,
        "inode": identity.inode,
        "mode": identity.mode,
    }


def _directory_payload(
    identity: ContainerDirectoryPathIdentity,
) -> dict[str, int]:
    return {
        "device": identity.device,
        "inode": identity.inode,
        "mode": identity.mode,
        "changed_ns": identity.changed_ns,
    }


def _container_payload(
    binding: ContainerStagingContainerBinding,
) -> dict[str, object]:
    return {
        "instance_name": binding.instance_name,
        "prep_instance_name": binding.prep_instance_name,
        "volume_names": list(binding.volume_names),
        "runtime_image": binding.runtime_image,
        "executable": str(binding.executable),
        "executable_sha256": binding.executable_sha256,
        "socket_path": str(binding.socket_path),
        "socket_identity": _file_payload(binding.socket_identity),
        "client_config_directory": str(binding.client_config_directory),
        "client_config_identity": _file_payload(
            binding.client_config_identity
        ),
        "gate_image_reference": binding.gate_image_reference,
        "gate_image_digest": binding.gate_image_digest,
    }


def _file_payload(identity: ContainerFileIdentity) -> dict[str, int]:
    return {
        "device": identity.device,
        "inode": identity.inode,
        "mode": identity.mode,
        "size": identity.size,
        "modified_ns": identity.modified_ns,
        "changed_ns": identity.changed_ns,
    }


def _same_directory_object(
    before: ContainerDirectoryPathIdentity,
    after: ContainerDirectoryPathIdentity,
) -> bool:
    return (
        (before.device, before.inode, before.mode)
        == (after.device, after.inode, after.mode)
        and after.changed_ns >= before.changed_ns
    )


__all__ = [
    "ContainerStagingContainerBinding",
    "ContainerStagingContainerRecoveryResult",
    "ContainerStagingError",
    "ContainerStagingJournalRecord",
    "ContainerStagingLifecycleResult",
    "ContainerStagingRecoveryResult",
    "ContainerStagingWorkspaceBinding",
    "STAGING_JOURNAL_SCHEMA",
    "STAGING_RECORD_STATES",
    "staging_record_transition_is_valid",
]

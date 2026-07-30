from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


ContainerEngine = Literal["docker"]
ContainerGateImageKind = Literal["repository-digest", "local-image-id"]
ContainerProcessOutcome = Literal[
    "exited",
    "timed_out",
    "spawn_failed",
    "cancelled",
    "parent_failed",
]
ContainerProcessStep = Literal[
    "server",
    "image",
    "volume_create",
    "volume_inspect",
    "prep_create",
    "prep_inspect",
    "stage_copy",
    "prep_remove",
    "prep_removal_check",
    "create",
    "recovery_query",
    "recovery_inspect",
    "start",
    "gate_ready",
    "mount_proof",
    "stage_proof",
    "inspect",
    "release",
    "wait",
    "terminate",
    "termination_wait",
    "kill",
    "kill_wait",
    "termination_logs",
    "logs",
    "output_copy",
    "volume_remove",
    "volume_removal_check",
    "resource_recovery_query",
    "resource_recovery_inspect",
    "final_inspect",
    "remove",
    "removal_check",
]
CONTAINER_ENGINES = ("docker",)
CONTAINER_PROFILES = frozenset({"read-only", "workspace-write"})
CONTAINER_NETWORK_POLICIES = frozenset({"deny", "allow"})
_PROCESS_OUTCOMES = frozenset(
    {"exited", "timed_out", "spawn_failed", "cancelled", "parent_failed"}
)
_PROCESS_STEPS = frozenset(
    {
        "server",
        "image",
        "volume_create",
        "volume_inspect",
        "prep_create",
        "prep_inspect",
        "stage_copy",
        "prep_remove",
        "prep_removal_check",
        "create",
        "recovery_query",
        "recovery_inspect",
        "start",
        "gate_ready",
        "mount_proof",
        "stage_proof",
        "inspect",
        "release",
        "wait",
        "terminate",
        "termination_wait",
        "kill",
        "kill_wait",
        "termination_logs",
        "logs",
        "output_copy",
        "volume_remove",
        "volume_removal_check",
        "resource_recovery_query",
        "resource_recovery_inspect",
        "final_inspect",
        "remove",
        "removal_check",
    }
)
_ATTEMPT_ID = re.compile(r"[0-9a-f]{32}")
_COMMAND_ID = re.compile(r"[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LOCAL_IMAGE_ID = re.compile(r"sha256:(?P<digest>[0-9a-f]{64})")
_REPOSITORY_IMAGE_DIGEST = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._:/-]*)@sha256:(?P<digest>[0-9a-f]{64})"
)
_FILE_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ContainerFileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int

    def __post_init__(self) -> None:
        if (
            self.device < 0
            or self.inode <= 0
            or self.size < 0
            or self.modified_ns < 0
            or self.changed_ns < 0
        ):
            raise ValueError("container file identity is invalid")

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> ContainerFileIdentity:
        return cls(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            mode=metadata.st_mode,
            size=metadata.st_size,
            modified_ns=metadata.st_mtime_ns,
            changed_ns=metadata.st_ctime_ns,
        )


@dataclass(frozen=True)
class ContainerRootIdentity:
    device: int
    inode: int
    mode: int

    def __post_init__(self) -> None:
        if self.device < 0 or self.inode <= 0 or not stat.S_ISDIR(self.mode):
            raise ValueError("container workspace root identity is invalid")

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> ContainerRootIdentity:
        return cls(metadata.st_dev, metadata.st_ino, metadata.st_mode)


@dataclass(frozen=True)
class ContainerDirectoryPathIdentity:
    device: int
    inode: int
    mode: int
    changed_ns: int

    def __post_init__(self) -> None:
        if (
            self.device < 0
            or self.inode <= 0
            or not stat.S_ISDIR(self.mode)
            or self.changed_ns < 0
        ):
            raise ValueError("container directory path identity is invalid")

    @classmethod
    def from_stat(
        cls,
        metadata: os.stat_result,
    ) -> ContainerDirectoryPathIdentity:
        return cls(
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_ctime_ns,
        )


@dataclass(frozen=True)
class ContainerWorkspaceAuthority:
    roots: tuple[Path, ...]
    revision: int
    root_identities: tuple[ContainerRootIdentity, ...]

    def __post_init__(self) -> None:
        if (
            not self.roots
            or len(set(self.roots)) != len(self.roots)
            or len(self.roots) != len(self.root_identities)
            or self.revision < 0
        ):
            raise ValueError("container workspace authority is invalid")
        if any(not root.is_absolute() for root in self.roots):
            raise ValueError("container workspace authority roots must be absolute")

    def is_current(self) -> bool:
        return all(
            _root_identity_matches(path, identity)
            for path, identity in zip(
                self.roots,
                self.root_identities,
                strict=True,
            )
        )

    def matches(
        self,
        roots: tuple[Path, ...],
        revision: int,
    ) -> bool:
        return (
            roots == self.roots
            and revision == self.revision
            and self.is_current()
        )


@dataclass(frozen=True)
class ContainerExecutableIdentity:
    file: ContainerFileIdentity
    sha256: str

    def __post_init__(self) -> None:
        if not stat.S_ISREG(self.file.mode):
            raise ValueError("container executable identity must be regular")
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("container executable identity requires a sha256 digest")


@dataclass(frozen=True)
class ContainerGateImageAuthority:
    reference: str
    digest: str
    kind: ContainerGateImageKind

    def __post_init__(self) -> None:
        local_match = _LOCAL_IMAGE_ID.fullmatch(self.reference)
        repository_match = _REPOSITORY_IMAGE_DIGEST.fullmatch(self.reference)
        if self.kind == "local-image-id":
            match = local_match
        elif self.kind == "repository-digest":
            match = repository_match
        else:
            raise ValueError("container gate image authority kind is invalid")
        if match is None or self.digest != f"sha256:{match.group('digest')}":
            raise ValueError("container gate image authority is not digest-pinned")

    def event_payload(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class ContainerEndpointIdentity:
    uri: str
    command_prefix: tuple[str, ...]
    socket_path: Path
    socket_identity: ContainerFileIdentity
    client_config_directory: Path
    client_config_identity: ContainerFileIdentity

    def __post_init__(self) -> None:
        if not self.uri.startswith("unix://") or any(
            character in self.uri for character in ("\0", "\n", "\r")
        ):
            raise ValueError("Docker endpoint must be a local Unix URI")
        if self.command_prefix != (
            "--config",
            str(self.client_config_directory),
            "--host",
            self.uri,
        ):
            raise ValueError("Docker endpoint command prefix is incomplete")
        if self.uri != f"unix://{self.socket_path}":
            raise ValueError("Docker endpoint URI does not match its socket")

    def is_current(self) -> bool:
        return identity_matches(self.socket_path, self.socket_identity) and identity_matches(
            self.client_config_directory,
            self.client_config_identity,
        )

    def event_payload(self) -> dict[str, object]:
        return {
            "kind": "docker-unix",
            "endpoint_digest": hashlib.sha256(self.uri.encode("utf-8")).hexdigest(),
        }


@dataclass(frozen=True)
class ContainerEngineIdentity:
    engine: ContainerEngine
    executable: Path
    executable_identity: ContainerExecutableIdentity
    endpoint: ContainerEndpointIdentity
    gate_image: ContainerGateImageAuthority
    workspace_authority: ContainerWorkspaceAuthority
    client_version: str
    client_api_version: str
    server_version: str
    server_api_version: str
    server_os: str
    server_arch: str

    def __post_init__(self) -> None:
        if self.engine != "docker":
            raise ValueError("T-273 Phase 1 only supports local Docker")
        if not self.executable.is_absolute():
            raise ValueError("container engine executable must be absolute")
        for label, value in (
            ("client version", self.client_version),
            ("client API version", self.client_api_version),
            ("server version", self.server_version),
            ("server API version", self.server_api_version),
            ("server architecture", self.server_arch),
        ):
            if not value.strip():
                raise ValueError(f"container {label} must not be empty")
        if self.server_os.strip().lower() != "linux":
            raise ValueError("container isolation requires a Linux server")
        if not self.workspace_authority.is_current():
            raise ValueError("container workspace authority changed")

    def command(self, *arguments: str) -> tuple[str, ...]:
        if not arguments or any(not argument or "\0" in argument for argument in arguments):
            raise ValueError("container engine command arguments must be non-empty")
        return (str(self.executable), *self.endpoint.command_prefix, *arguments)

    def is_current(self) -> bool:
        return self.control_authority_is_current() and self.workspace_authority.is_current()

    def control_authority_is_current(self) -> bool:
        return executable_identity_matches(
            self.executable,
            self.executable_identity,
        ) and self.endpoint.is_current()

    def event_payload(self) -> dict[str, object]:
        return {
            "engine": self.engine,
            "executable_digest": self.executable_identity.sha256,
            "client_version": self.client_version,
            "client_api_version": self.client_api_version,
            "server_version": self.server_version,
            "server_api_version": self.server_api_version,
            "server_os": self.server_os,
            "server_arch": self.server_arch,
            "endpoint": self.endpoint.event_payload(),
            "gate_image": self.gate_image.event_payload(),
            "workspace_roots_revision": self.workspace_authority.revision,
            "workspace_roots_digest": hashlib.sha256(
                "\0".join(str(root) for root in self.workspace_authority.roots).encode(
                    "utf-8"
                )
            ).hexdigest(),
        }


@dataclass(frozen=True)
class ContainerStreamCapture:
    observed_bytes: int
    captured_bytes: int
    dropped_bytes: int
    truncated: bool
    text_sha256: str

    def __post_init__(self) -> None:
        if min(self.observed_bytes, self.captured_bytes, self.dropped_bytes) < 0:
            raise ValueError("container stream capture byte counts must not be negative")
        if self.observed_bytes != self.captured_bytes + self.dropped_bytes:
            raise ValueError("container stream capture byte counts are inconsistent")
        if self.truncated != (self.dropped_bytes > 0):
            raise ValueError("container stream capture truncation is inconsistent")
        if not _SHA256.fullmatch(self.text_sha256):
            raise ValueError("container stream capture requires a text sha256 digest")

    @property
    def complete(self) -> bool:
        return not self.truncated


@dataclass(frozen=True)
class ContainerOutputCapture:
    stdout: ContainerStreamCapture
    stderr: ContainerStreamCapture

    @property
    def complete(self) -> bool:
        return self.stdout.complete and self.stderr.complete


@dataclass(frozen=True)
class ContainerCommandResult:
    attempt_id: str
    command_id: str
    event_sequence: int
    step: ContainerProcessStep
    argv: tuple[str, ...]
    outcome: ContainerProcessOutcome
    exit_code: int | None
    stdout: str
    stderr: str
    output_capture: ContainerOutputCapture
    started_monotonic_ns: int
    finished_monotonic_ns: int
    workspace_roots: tuple[Path, ...]
    workspace_roots_revision: int

    def __post_init__(self) -> None:
        validate_attempt_id(self.attempt_id)
        if not _COMMAND_ID.fullmatch(self.command_id):
            raise ValueError("container command id must contain 32 lowercase hex characters")
        if self.event_sequence < 1:
            raise ValueError("container command event sequence must be positive")
        if self.step not in _PROCESS_STEPS:
            raise ValueError("container command result step is invalid")
        if self.outcome not in _PROCESS_OUTCOMES:
            raise ValueError("container command result outcome is invalid")
        if (
            not self.workspace_roots
            or len(set(self.workspace_roots)) != len(self.workspace_roots)
            or any(not root.is_absolute() for root in self.workspace_roots)
            or self.workspace_roots_revision < 0
        ):
            raise ValueError("container command result workspace authority is invalid")
        if (
            not self.argv
            or not self.argv[0]
            or any(not isinstance(argument, str) or "\0" in argument for argument in self.argv)
        ):
            raise ValueError("container command result argv is invalid")
        if self.outcome == "exited":
            if not isinstance(self.exit_code, int):
                raise ValueError("exited container command requires an exit code")
        elif self.exit_code is not None:
            raise ValueError("non-exited container command cannot expose an exit code")
        if (
            self.started_monotonic_ns < 0
            or self.finished_monotonic_ns < self.started_monotonic_ns
        ):
            raise ValueError("container command timing is invalid")
        _validate_stream_text("stdout", self.stdout, self.output_capture.stdout)
        _validate_stream_text("stderr", self.stderr, self.output_capture.stderr)


def validate_attempt_id(attempt_id: str) -> None:
    if not _ATTEMPT_ID.fullmatch(attempt_id):
        raise ValueError("container attempt id must contain 32 lowercase hex characters")


def container_command_id(
    attempt_id: str,
    step: ContainerProcessStep,
    *,
    ordinal: int = 1,
) -> str:
    validate_attempt_id(attempt_id)
    if step not in _PROCESS_STEPS or ordinal < 1:
        raise ValueError("container command correlation input is invalid")
    payload = f"container-command-v1\0{attempt_id}\0{step}\0{ordinal}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def parse_gate_image_authority(reference: str) -> ContainerGateImageAuthority:
    local_match = _LOCAL_IMAGE_ID.fullmatch(reference)
    if local_match is not None:
        return ContainerGateImageAuthority(
            reference=reference,
            digest=f"sha256:{local_match.group('digest')}",
            kind="local-image-id",
        )
    repository_match = _REPOSITORY_IMAGE_DIGEST.fullmatch(reference)
    if repository_match is not None:
        return ContainerGateImageAuthority(
            reference=reference,
            digest=f"sha256:{repository_match.group('digest')}",
            kind="repository-digest",
        )
    raise ValueError(
        "container gate image must be an exact local image id or repository digest"
    )


def capture_workspace_authority(
    roots: tuple[Path, ...],
    *,
    revision: int,
) -> ContainerWorkspaceAuthority:
    canonical = _canonical_roots(roots)
    identities = tuple(
        ContainerRootIdentity.from_stat(os.stat(root, follow_symlinks=False))
        for root in canonical
    )
    return ContainerWorkspaceAuthority(canonical, revision, identities)


def capture_trusted_executable(
    path: Path,
    *,
    expected_sha256: str,
    workspace_roots: tuple[Path, ...],
) -> tuple[Path, ContainerExecutableIdentity]:
    if not _SHA256.fullmatch(expected_sha256):
        raise ValueError("container engine executable requires an expected sha256 digest")
    canonical = _resolve_existing(path, "container engine executable")
    canonical_roots = _canonical_roots(workspace_roots)
    _validate_trusted_resource_path(
        canonical,
        workspace_roots=canonical_roots,
        label="container engine executable",
    )
    metadata = os.stat(canonical, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or not os.access(canonical, os.X_OK):
        raise ValueError("container engine executable must be an executable regular file")
    before = ContainerFileIdentity.from_stat(metadata)
    observed_sha256 = _sha256_regular_file(canonical)
    after = ContainerFileIdentity.from_stat(os.stat(canonical, follow_symlinks=False))
    if before != after:
        raise ValueError("container engine executable changed while hashing")
    if observed_sha256 != expected_sha256:
        raise ValueError("container engine executable digest mismatch")
    return canonical, ContainerExecutableIdentity(after, observed_sha256)


def capture_empty_directory(
    path: Path,
    *,
    workspace_roots: tuple[Path, ...],
) -> tuple[Path, ContainerFileIdentity]:
    canonical = _resolve_existing(path, "container client config directory")
    canonical_roots = _canonical_roots(workspace_roots)
    _validate_trusted_resource_path(
        canonical,
        workspace_roots=canonical_roots,
        label="container client config directory",
    )
    metadata = os.stat(canonical, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("container client config path must be a directory")
    if metadata.st_uid != os.getuid() or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("container client config directory must be private and user-owned")
    if any(canonical.iterdir()):
        raise ValueError("container client config directory must be empty")
    return canonical, ContainerFileIdentity.from_stat(metadata)


def capture_private_directory(
    path: Path,
    *,
    workspace_roots: tuple[Path, ...],
    label: str,
) -> tuple[Path, ContainerRootIdentity]:
    canonical = _resolve_existing(path, label)
    canonical_roots = _canonical_roots(workspace_roots)
    _validate_trusted_resource_path(
        canonical,
        workspace_roots=canonical_roots,
        label=label,
    )
    metadata = os.stat(canonical, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be a directory")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError(f"{label} must be user-owned with mode 0700")
    return canonical, ContainerRootIdentity.from_stat(metadata)


def capture_unix_socket(
    path: Path,
    *,
    workspace_roots: tuple[Path, ...],
) -> tuple[Path, ContainerFileIdentity]:
    canonical = _resolve_existing(path, "container endpoint socket")
    canonical_roots = _canonical_roots(workspace_roots)
    _validate_trusted_resource_path(
        canonical,
        workspace_roots=canonical_roots,
        label="container endpoint socket",
    )
    metadata = os.stat(canonical, follow_symlinks=False)
    if not stat.S_ISSOCK(metadata.st_mode):
        raise ValueError("container endpoint must resolve to a Unix socket")
    return canonical, ContainerFileIdentity.from_stat(metadata)


def identity_matches(path: Path, expected: ContainerFileIdentity) -> bool:
    try:
        return ContainerFileIdentity.from_stat(os.stat(path, follow_symlinks=False)) == expected
    except OSError:
        return False


def root_identity_matches(path: Path, expected: ContainerRootIdentity) -> bool:
    return _root_identity_matches(path, expected)


def directory_path_identity_matches(
    path: Path,
    expected: ContainerDirectoryPathIdentity,
) -> bool:
    try:
        observed = ContainerDirectoryPathIdentity.from_stat(
            os.stat(path, follow_symlinks=False)
        )
    except (OSError, ValueError):
        return False
    return observed == expected


def executable_identity_matches(
    path: Path,
    expected: ContainerExecutableIdentity,
) -> bool:
    return identity_matches(path, expected.file)


def command_workspace_authority_matches(
    authority: ContainerWorkspaceAuthority,
    result: ContainerCommandResult,
) -> bool:
    return authority.matches(
        result.workspace_roots,
        result.workspace_roots_revision,
    )


def command_output_is_complete(result: ContainerCommandResult) -> bool:
    return result.output_capture.complete


def _validate_stream_text(
    label: str,
    text: str,
    capture: ContainerStreamCapture,
) -> None:
    encoded = text.encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != capture.text_sha256:
        raise ValueError(f"container {label} text digest does not match its capture")
    if bool(encoded) != bool(capture.captured_bytes):
        raise ValueError(f"container {label} text does not match captured-byte presence")
    if capture.complete and len(encoded) != capture.captured_bytes:
        raise ValueError(
            f"container {label} complete text does not match captured-byte count"
        )


def _sha256_regular_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("container engine executable must remain regular")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, _FILE_HASH_CHUNK_BYTES)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
    finally:
        os.close(descriptor)


def _resolve_existing(path: Path, label: str) -> Path:
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} must resolve to an existing path") from exc


def _root_identity_matches(path: Path, expected: ContainerRootIdentity) -> bool:
    try:
        observed = ContainerRootIdentity.from_stat(
            os.stat(path, follow_symlinks=False)
        )
    except (OSError, ValueError):
        return False
    return observed == expected


def _validate_trusted_resource_path(
    path: Path,
    *,
    workspace_roots: tuple[Path, ...],
    label: str,
) -> None:
    if any(path == root or path.is_relative_to(root) for root in workspace_roots):
        raise ValueError(f"{label} must not be inside an authorized workspace root")
    effective_groups = set(os.getgroups()) | {os.getegid()}
    for current in (path, *path.parents):
        metadata = os.stat(current, follow_symlinks=False)
        if current == path and metadata.st_uid not in {0, os.getuid()}:
            raise ValueError(f"{label} must be owned by the current user or root")
        if metadata.st_mode & stat.S_IWOTH and not metadata.st_mode & stat.S_ISVTX:
            raise ValueError(f"{label} must not have a world-writable path component")
        if (
            metadata.st_mode & stat.S_IWGRP
            and metadata.st_gid in effective_groups
        ):
            raise ValueError(f"{label} must not have a group-writable path component")


def _canonical_roots(roots: tuple[Path, ...]) -> tuple[Path, ...]:
    if not roots:
        raise ValueError("container authority requires at least one workspace root")
    canonical = tuple(_resolve_existing(root, "workspace root") for root in roots)
    if len(set(canonical)) != len(canonical):
        raise ValueError("container authority workspace roots must be unique")
    if any(not root.is_dir() for root in canonical):
        raise ValueError("container authority workspace roots must be directories")
    return canonical

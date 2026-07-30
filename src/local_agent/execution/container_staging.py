from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Generic, TypeVar

from ..workspace.snapshot import WorkspaceSnapshot
from ..workspace.snapshot import WorkspaceSnapshotError
from ..workspace.snapshot import capture_workspace_snapshot
from ..workspace.snapshot_delta import WorkspaceSnapshotDeltaError
from ..workspace.snapshot_delta import WorkspaceTextMutationPlan
from ..workspace.snapshot_delta import build_workspace_text_mutation_plan
from ..workspace.snapshot_delta import snapshots_match
from .container_types import ContainerDirectoryPathIdentity
from .container_types import ContainerRootIdentity
from .container_types import directory_path_identity_matches
from .container_types import root_identity_matches
from .container_types import validate_attempt_id
from .container_staging_lifecycle import ContainerStagingError
from .container_staging_lifecycle import ContainerStagingContainerBinding
from .container_staging_lifecycle import ContainerStagingLifecycleHandle
from .container_staging_lifecycle import (
    ContainerStagingLifecycleResult as ContainerStagingCleanupResult,
)
from .container_staging_lifecycle import ContainerStagingRecoveryResult
from .container_staging_lifecycle import ContainerStagingWorkspaceBinding
from .container_staging_lifecycle import acquire_staging_authority
from .container_staging_lifecycle import cleanup_staging_lifecycle
from .container_staging_lifecycle import discard_unreleased_staging_attempt
from .container_staging_lifecycle import mark_staging_allocated
from .container_staging_lifecycle import mark_staging_container_absent
from .container_staging_lifecycle import mark_staging_create_possible
from .container_staging_lifecycle import mark_staging_execution_absent
from .container_staging_lifecycle import (
    mark_staging_execution_create_possible,
)
from .container_staging_lifecycle import mark_staging_prepared
from .container_staging_lifecycle import recover_staging_authority
from .container_staging_lifecycle import reserve_staging_lifecycle
from .container_staging_lifecycle import require_empty_staging_authority
from .container_staging_lifecycle import staging_lifecycle_is_current


_T = TypeVar("_T")


@dataclass(frozen=True)
class StagedWorkspaceRoot:
    ordinal: int
    snapshot: WorkspaceSnapshot
    source_root: Path
    source_identity: tuple[int, int]
    destination_root: Path
    staging_path: Path
    staging_identity: ContainerRootIdentity
    output_path: Path
    output_identity: ContainerRootIdentity
    volume_name: str
    manifest_sha256: str
    roots_revision: int

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise ValueError("staged workspace root ordinal is invalid")
        if (
            self.snapshot.root != self.source_root
            or self.snapshot.root_identity != self.source_identity
            or self.snapshot.roots_revision != self.roots_revision
            or self.snapshot.manifest_sha256 != self.manifest_sha256
        ):
            raise ValueError("staged workspace snapshot provenance is invalid")
        if min(self.source_identity) < 0 or self.source_identity[1] <= 0:
            raise ValueError("staged workspace source identity is invalid")
        for path in (
            self.source_root,
            self.destination_root,
            self.staging_path,
            self.output_path,
        ):
            if not path.is_absolute():
                raise ValueError("staged workspace paths must be absolute")
        if self.staging_path == self.output_path:
            raise ValueError("staged workspace input and output must be distinct")
        if not self.volume_name:
            raise ValueError("staged workspace volume name is invalid")
        if len(self.manifest_sha256) != 64:
            raise ValueError("staged workspace manifest digest is invalid")
        if self.roots_revision < 0:
            raise ValueError("staged workspace revision is invalid")


@dataclass(frozen=True)
class ContainerStagingAttempt:
    attempt_id: str
    authority_root: Path
    authority_identity: ContainerRootIdentity
    authority_path_identity: ContainerDirectoryPathIdentity
    attempt_path: Path
    attempt_identity: ContainerRootIdentity
    attempt_path_identity: ContainerDirectoryPathIdentity
    roots: tuple[StagedWorkspaceRoot, ...]
    lifecycle: ContainerStagingLifecycleHandle = field(
        repr=False,
        compare=False,
    )
    forbidden_directory_identities: frozenset[tuple[int, int]] = field(
        default_factory=frozenset,
        repr=False,
    )

    def __post_init__(self) -> None:
        validate_attempt_id(self.attempt_id)
        if not self.roots:
            raise ValueError("container staging attempt requires at least one root")
        if tuple(root.ordinal for root in self.roots) != tuple(range(len(self.roots))):
            raise ValueError("container staging root ordinals are invalid")
        if self.attempt_path != self.authority_root / self.attempt_id:
            raise ValueError("container staging attempt path is invalid")
        if (
            (self.authority_path_identity.device, self.authority_path_identity.inode)
            != (self.authority_identity.device, self.authority_identity.inode)
            or (self.attempt_path_identity.device, self.attempt_path_identity.inode)
            != (self.attempt_identity.device, self.attempt_identity.inode)
        ):
            raise ValueError("container staging path provenance is invalid")
        if any(
            root.volume_name != _staging_volume_name(self.attempt_id, root.ordinal)
            for root in self.roots
        ):
            raise ValueError("container staging volume provenance is invalid")

    def authority_is_current(self) -> bool:
        return staging_lifecycle_is_current(self.lifecycle)


@dataclass(frozen=True)
class ContainerStagedOperationResult(Generic[_T]):
    value: _T
    output: ContainerStagingOutputResult
    cleanup: ContainerStagingCleanupResult


@dataclass(frozen=True)
class ContainerStagingOutputResult:
    reason_code: str
    plan: WorkspaceTextMutationPlan | None = None

    def __post_init__(self) -> None:
        if not self.reason_code.strip():
            raise ValueError("container staging output reason is invalid")
        if self.reason_code != "staging_output_verified" and self.plan is not None:
            raise ValueError("failed staging output cannot expose a mutation plan")

    @property
    def verified(self) -> bool:
        return self.reason_code == "staging_output_verified"


def run_staged_workspace_operation(
    *,
    staging_root: Path,
    workspace_roots: tuple[Path, ...],
    workspace_roots_revision: int,
    attempt_id: str,
    profile: str,
    operation: Callable[[ContainerStagingAttempt], _T],
    cleanup_authorized: Callable[[_T], bool],
    output_captured: Callable[[_T], bool] = lambda _value: False,
    forbidden_directory_identities: frozenset[tuple[int, int]] = frozenset(),
) -> ContainerStagedOperationResult[_T]:
    try:
        snapshots = tuple(
            capture_workspace_snapshot(
                root,
                roots_revision=workspace_roots_revision,
                forbidden_directory_identities=forbidden_directory_identities,
            )
            for root in workspace_roots
        )
    except WorkspaceSnapshotError as exc:
        raise ContainerStagingError(f"workspace_snapshot_{exc.kind}") from exc
    attempt = stage_workspace_snapshots(
        staging_root=staging_root,
        snapshots=snapshots,
        destinations=tuple(snapshot.root for snapshot in snapshots),
        workspace_roots=tuple(snapshot.root for snapshot in snapshots),
        attempt_id=attempt_id,
        forbidden_directory_identities=forbidden_directory_identities,
    )
    try:
        value = operation(attempt)
    except BaseException as error:
        if attempt.lifecycle.record.state in {"prepared", "container_absent"}:
            transition = (
                mark_staging_container_absent(attempt.lifecycle)
                if attempt.lifecycle.record.state == "prepared"
                else ContainerStagingCleanupResult(
                    "staging_container_absent_recorded",
                    True,
                    False,
                )
            )
            cleanup = (
                _cleanup_staging_safely(attempt)
                if transition.verified
                else transition
            )
            if not transition.verified:
                attempt.lifecycle.lease.close()
            if not cleanup.verified:
                error.add_note(
                    f"Container staging cleanup unresolved: {cleanup.reason_code}."
                )
        else:
            attempt.lifecycle.lease.close()
            error.add_note(
                "Container staging retained because container closure is unresolved."
            )
        raise
    if not cleanup_authorized(value):
        attempt.lifecycle.lease.close()
        return ContainerStagedOperationResult(
            value,
            ContainerStagingOutputResult(
                "staging_output_container_cleanup_unverified"
            ),
            ContainerStagingCleanupResult(
                "staging_container_cleanup_unverified",
                False,
                True,
            ),
        )
    transition = (
        ContainerStagingCleanupResult(
            "staging_container_absent_recorded",
            True,
            False,
        )
        if attempt.lifecycle.record.state == "container_absent"
        else mark_staging_container_absent(attempt.lifecycle)
    )
    if not transition.verified:
        attempt.lifecycle.lease.close()
        return ContainerStagedOperationResult(
            value,
            ContainerStagingOutputResult(
                "staging_output_container_close_journal_unverified"
            ),
            transition,
        )
    try:
        output = observe_staged_workspace_output(
            attempt,
            profile=profile,
            use_output_paths=output_captured(value),
        )
    except BaseException as error:
        cleanup = _cleanup_staging_safely(attempt)
        if isinstance(error, Exception):
            raise ContainerStagingError(
                "staging_output_parent_exception",
                cleanup_verified=cleanup.verified,
            ) from error
        if not cleanup.verified:
            error.add_note(
                f"Container staging cleanup unresolved: {cleanup.reason_code}."
            )
        raise
    cleanup = _cleanup_staging_safely(attempt)
    return ContainerStagedOperationResult(value, output, cleanup)


def observe_staged_workspace_output(
    attempt: ContainerStagingAttempt,
    *,
    profile: str,
    use_output_paths: bool = False,
) -> ContainerStagingOutputResult:
    try:
        plan = _capture_staged_workspace_output(
            attempt,
            profile=profile,
            use_output_paths=use_output_paths,
        )
    except ContainerStagingError as exc:
        return ContainerStagingOutputResult(exc.kind)
    return ContainerStagingOutputResult("staging_output_verified", plan)


def staged_input_snapshots_are_current(
    attempt: ContainerStagingAttempt,
) -> bool:
    """Rebind host staging content before the gate can release user code."""

    if not attempt.authority_is_current():
        return False
    try:
        observed = tuple(
            capture_workspace_snapshot(
                root.staging_path,
                roots_revision=root.roots_revision,
                expected_root_identity=(
                    root.staging_identity.device,
                    root.staging_identity.inode,
                ),
                forbidden_directory_identities=(
                    attempt.forbidden_directory_identities
                ),
            )
            for root in attempt.roots
        )
    except WorkspaceSnapshotError:
        return False
    return attempt.authority_is_current() and all(
        snapshots_match(root.snapshot, current)
        for root, current in zip(attempt.roots, observed, strict=True)
    )


def _capture_staged_workspace_output(
    attempt: ContainerStagingAttempt,
    *,
    profile: str,
    use_output_paths: bool,
) -> WorkspaceTextMutationPlan | None:
    if profile not in {"read-only", "workspace-write"}:
        raise ContainerStagingError("staging_output_profile_invalid")
    if not attempt.authority_is_current():
        raise ContainerStagingError("staging_output_authority_changed")
    observed: list[WorkspaceSnapshot] = []
    try:
        for root in attempt.roots:
            observed_path = (
                root.output_path if use_output_paths else root.staging_path
            )
            observed_identity = (
                root.output_identity
                if use_output_paths
                else root.staging_identity
            )
            if not root_identity_matches(observed_path, observed_identity):
                raise ContainerStagingError("staging_output_root_changed")
            observed.append(
                capture_workspace_snapshot(
                    observed_path,
                    roots_revision=root.roots_revision,
                    expected_root_identity=(
                        observed_identity.device,
                        observed_identity.inode,
                    ),
                    forbidden_directory_identities=(
                        attempt.forbidden_directory_identities
                    ),
                )
            )
    except WorkspaceSnapshotError as exc:
        raise ContainerStagingError(
            f"staging_output_snapshot_{exc.kind}"
        ) from exc
    if not attempt.authority_is_current():
        raise ContainerStagingError("staging_output_authority_changed")
    for root, after in zip(attempt.roots[1:], observed[1:], strict=True):
        if not snapshots_match(root.snapshot, after):
            raise ContainerStagingError("staging_readable_root_changed")
    primary = attempt.roots[0]
    if profile == "read-only":
        if not snapshots_match(primary.snapshot, observed[0]):
            raise ContainerStagingError("staging_read_only_workspace_changed")
        return None
    try:
        plan = build_workspace_text_mutation_plan(
            primary.snapshot,
            observed[0],
        )
    except WorkspaceSnapshotDeltaError as exc:
        raise ContainerStagingError(f"staging_output_{exc.kind}") from exc
    return plan if plan.changed else None


def stage_workspace_snapshots(
    *,
    staging_root: Path,
    snapshots: tuple[WorkspaceSnapshot, ...],
    destinations: tuple[Path, ...],
    workspace_roots: tuple[Path, ...],
    attempt_id: str,
    forbidden_directory_identities: frozenset[tuple[int, int]] = frozenset(),
) -> ContainerStagingAttempt:
    validate_attempt_id(attempt_id)
    if not snapshots or len(snapshots) != len(destinations):
        raise ContainerStagingError("staging_roots_invalid")
    if tuple(snapshot.root for snapshot in snapshots) != workspace_roots:
        raise ContainerStagingError("staging_snapshot_authority_mismatch")
    revisions = {snapshot.roots_revision for snapshot in snapshots}
    if len(revisions) != 1:
        raise ContainerStagingError("staging_snapshot_revision_mismatch")
    try:
        lease = acquire_staging_authority(
            staging_root,
            workspace_roots=workspace_roots,
        )
        require_empty_staging_authority(lease)
        lifecycle = reserve_staging_lifecycle(
            lease,
            attempt_id=attempt_id,
            workspace_roots_revision=next(iter(revisions)),
            workspace_roots=tuple(
                ContainerStagingWorkspaceBinding(
                    snapshot.root,
                    snapshot.root_identity[0],
                    snapshot.root_identity[1],
                    snapshot.manifest_sha256,
                )
                for snapshot in snapshots
            ),
        )
    except ContainerStagingError:
        raise

    attempt_fd = -1
    attempt_path = lease.authority_root / attempt_id
    attempt_created = False
    attempt_identity: ContainerRootIdentity | None = None
    completed = False
    try:
        os.mkdir(attempt_id, mode=0o700, dir_fd=lease.root_fd)
        attempt_created = True
        attempt_fd = os.open(
            attempt_id,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=lease.root_fd,
        )
        attempt_metadata = os.fstat(attempt_fd)
        attempt_identity = ContainerRootIdentity.from_stat(attempt_metadata)
        if attempt_metadata.st_uid != os.getuid() or stat.S_IMODE(
            attempt_metadata.st_mode
        ) != 0o700:
            raise ContainerStagingError("staging_attempt_authority_invalid")
        allocated_identity = ContainerDirectoryPathIdentity.from_stat(
            attempt_metadata
        )
        allocated = mark_staging_allocated(
            lifecycle,
            attempt_identity=attempt_identity,
            attempt_path_identity=allocated_identity,
        )
        if not allocated.verified:
            raise ContainerStagingError(allocated.reason_code)
        staged_roots: list[StagedWorkspaceRoot] = []
        for ordinal, (snapshot, destination) in enumerate(
            zip(snapshots, destinations)
        ):
            name = f"root-{ordinal:04d}"
            output_name = f"output-{ordinal:04d}"
            os.mkdir(name, mode=0o700, dir_fd=attempt_fd)
            os.mkdir(output_name, mode=0o700, dir_fd=attempt_fd)
            root_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=attempt_fd,
            )
            try:
                _copy_snapshot(root_fd, snapshot)
                staging_path = attempt_path / name
                staged_identity = ContainerRootIdentity.from_stat(os.fstat(root_fd))
            finally:
                os.close(root_fd)
            output_fd = os.open(
                output_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=attempt_fd,
            )
            try:
                output_path = attempt_path / output_name
                output_identity = ContainerRootIdentity.from_stat(
                    os.fstat(output_fd)
                )
            finally:
                os.close(output_fd)
            observed = capture_workspace_snapshot(
                staging_path,
                roots_revision=snapshot.roots_revision,
                expected_root_identity=(
                    staged_identity.device,
                    staged_identity.inode,
                ),
                forbidden_directory_identities=forbidden_directory_identities,
            )
            if (
                observed.entries != snapshot.entries
                or observed.total_bytes != snapshot.total_bytes
                or observed.manifest_sha256 != snapshot.manifest_sha256
            ):
                raise ContainerStagingError("staging_copy_verification_failed")
            staged_roots.append(
                StagedWorkspaceRoot(
                    ordinal=ordinal,
                    snapshot=snapshot,
                    source_root=snapshot.root,
                    source_identity=snapshot.root_identity,
                    destination_root=destination,
                    staging_path=staging_path,
                    staging_identity=staged_identity,
                    output_path=output_path,
                    output_identity=output_identity,
                    volume_name=_staging_volume_name(attempt_id, ordinal),
                    manifest_sha256=snapshot.manifest_sha256,
                    roots_revision=snapshot.roots_revision,
                )
            )
        os.fsync(attempt_fd)
        os.fsync(lease.root_fd)
        attempt_path_identity = ContainerDirectoryPathIdentity.from_stat(
            os.fstat(attempt_fd)
        )
        _assert_path_identity(
            attempt_path,
            attempt_fd,
            attempt_path_identity,
            "staging_attempt_changed",
        )
        prepared = mark_staging_prepared(
            lifecycle,
            attempt_path_identity=attempt_path_identity,
        )
        if not prepared.verified:
            raise ContainerStagingError(prepared.reason_code)
        attempt = ContainerStagingAttempt(
            attempt_id=attempt_id,
            authority_root=lease.authority_root,
            authority_identity=lease.authority_identity,
            authority_path_identity=lifecycle.record.authority_path_identity,
            attempt_path=attempt_path,
            attempt_identity=attempt_identity,
            attempt_path_identity=attempt_path_identity,
            roots=tuple(staged_roots),
            lifecycle=lifecycle,
            forbidden_directory_identities=forbidden_directory_identities,
        )
        completed = True
        return attempt
    except FileExistsError as exc:
        cleanup = discard_unreleased_staging_attempt(
            lifecycle,
            attempt_created=attempt_created,
            attempt_identity=attempt_identity,
        )
        raise ContainerStagingError(
            "staging_attempt_exists",
            cleanup_verified=cleanup.verified,
        ) from exc
    except ContainerStagingError as exc:
        cleanup = discard_unreleased_staging_attempt(
            lifecycle,
            attempt_created=attempt_created,
            attempt_identity=attempt_identity,
        )
        raise ContainerStagingError(
            exc.kind,
            cleanup_verified=cleanup.verified,
        ) from exc
    except (OSError, RuntimeError, ValueError, WorkspaceSnapshotError) as exc:
        cleanup = discard_unreleased_staging_attempt(
            lifecycle,
            attempt_created=attempt_created,
            attempt_identity=attempt_identity,
        )
        raise ContainerStagingError(
            "staging_failed",
            cleanup_verified=cleanup.verified,
        ) from exc
    finally:
        _close_fds(attempt_fd)
        if not completed and not lease.closed:
            lease.close()


def cleanup_staging_attempt(
    attempt: ContainerStagingAttempt,
) -> ContainerStagingCleanupResult:
    return cleanup_staging_lifecycle(attempt.lifecycle)


def record_staging_create_possible(
    attempt: ContainerStagingAttempt,
    binding: ContainerStagingContainerBinding,
) -> ContainerStagingCleanupResult:
    return mark_staging_create_possible(attempt.lifecycle, binding)


def record_staging_execution_create_possible(
    attempt: ContainerStagingAttempt,
) -> ContainerStagingCleanupResult:
    return mark_staging_execution_create_possible(attempt.lifecycle)


def record_staging_execution_absent(
    attempt: ContainerStagingAttempt,
) -> ContainerStagingCleanupResult:
    return mark_staging_execution_absent(attempt.lifecycle)


def record_staging_container_absent(
    attempt: ContainerStagingAttempt,
) -> ContainerStagingCleanupResult:
    return mark_staging_container_absent(attempt.lifecycle)


def _cleanup_staging_safely(
    attempt: ContainerStagingAttempt,
) -> ContainerStagingCleanupResult:
    try:
        return cleanup_staging_attempt(attempt)
    except BaseException:
        return ContainerStagingCleanupResult(
            "staging_cleanup_parent_exception",
            False,
            True,
        )


def _copy_snapshot(root_fd: int, snapshot: WorkspaceSnapshot) -> None:
    directories = [
        entry for entry in snapshot.entries if entry.kind == "directory"
    ]
    for entry in directories:
        parent_fd = _open_relative_directory(root_fd, entry.relative_parts[:-1])
        try:
            os.mkdir(entry.relative_parts[-1], mode=0o700, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    for entry in snapshot.entries:
        if entry.kind != "file":
            continue
        assert entry.content is not None
        parent_fd = _open_relative_directory(root_fd, entry.relative_parts[:-1])
        file_fd = -1
        try:
            file_fd = os.open(
                entry.relative_parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            _write_all(file_fd, entry.content)
            os.fchmod(file_fd, entry.mode)
            os.fsync(file_fd)
        finally:
            _close_fds(file_fd, parent_fd)
    for entry in sorted(
        directories,
        key=lambda value: len(value.relative_parts),
        reverse=True,
    ):
        parent_fd = _open_relative_directory(root_fd, entry.relative_parts[:-1])
        try:
            os.chmod(
                entry.relative_parts[-1],
                entry.mode,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        finally:
            os.close(parent_fd)
    os.fsync(root_fd)


def _open_relative_directory(root_fd: int, parts: tuple[str, ...]) -> int:
    current = os.dup(root_fd)
    try:
        for part in parts:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current,
            )
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def _assert_path_identity(
    path: Path,
    descriptor: int,
    expected: ContainerDirectoryPathIdentity,
    kind: str,
) -> None:
    if (
        ContainerDirectoryPathIdentity.from_stat(os.fstat(descriptor)) != expected
        or not directory_path_identity_matches(path, expected)
    ):
        raise ContainerStagingError(kind)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short staging write")
        offset += written


def _staging_volume_name(attempt_id: str, ordinal: int) -> str:
    validate_attempt_id(attempt_id)
    if ordinal < 0:
        raise ValueError("staging volume ordinal is invalid")
    return f"lca-{attempt_id}-root-{ordinal:04d}"


def _close_fds(*descriptors: int) -> None:
    for descriptor in descriptors:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


__all__ = [
    "ContainerStagingAttempt",
    "ContainerStagingCleanupResult",
    "ContainerStagingContainerBinding",
    "ContainerStagingError",
    "ContainerStagingRecoveryResult",
    "ContainerStagedOperationResult",
    "ContainerStagingOutputResult",
    "StagedWorkspaceRoot",
    "cleanup_staging_attempt",
    "observe_staged_workspace_output",
    "record_staging_container_absent",
    "record_staging_create_possible",
    "record_staging_execution_absent",
    "record_staging_execution_create_possible",
    "recover_staging_authority",
    "run_staged_workspace_operation",
    "staged_input_snapshots_are_current",
    "stage_workspace_snapshots",
]

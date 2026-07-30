from __future__ import annotations

import fcntl
import os
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path

from .container_staging_contracts import ContainerStagingError
from .container_staging_contracts import ContainerStagingContainerBinding
from .container_staging_contracts import (
    ContainerStagingContainerRecoveryResult,
)
from .container_staging_contracts import ContainerStagingJournalRecord
from .container_staging_contracts import ContainerStagingLifecycleResult
from .container_staging_contracts import ContainerStagingRecoveryResult
from .container_staging_contracts import ContainerStagingWorkspaceBinding
from .container_staging_files import assert_private_regular, remove_directory_contents
from .container_staging_journal import OPEN_DIRECTORY_FLAGS
from .container_staging_journal import STAGING_ATTEMPT_NAME
from .container_staging_journal import STAGING_JOURNAL_DIRECTORY_NAME
from .container_staging_journal import STAGING_LOCK_NAME
from .container_staging_journal import open_or_create_staging_journal
from .container_staging_journal import read_staging_record
from .container_staging_journal import remove_staging_record
from .container_staging_journal import scan_staging_records
from .container_staging_journal import write_staging_record
from .container_types import ContainerDirectoryPathIdentity
from .container_types import ContainerFileIdentity
from .container_types import ContainerRootIdentity
from .container_types import capture_private_directory
from .container_types import directory_path_identity_matches
from .container_types import root_identity_matches


@dataclass
class ContainerStagingAuthorityLease:
    authority_root: Path
    authority_identity: ContainerRootIdentity
    lock_identity: ContainerFileIdentity
    journal_identity: ContainerRootIdentity
    root_fd: int
    lock_fd: int
    journal_fd: int
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for descriptor in (self.journal_fd, self.lock_fd, self.root_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass

    def root_is_current(self) -> bool:
        if self.closed:
            return False
        try:
            lock_path = os.stat(
                STAGING_LOCK_NAME,
                dir_fd=self.root_fd,
                follow_symlinks=False,
            )
            journal_path = os.stat(
                STAGING_JOURNAL_DIRECTORY_NAME,
                dir_fd=self.root_fd,
                follow_symlinks=False,
            )
            return (
                ContainerRootIdentity.from_stat(os.fstat(self.root_fd))
                == self.authority_identity
                and ContainerFileIdentity.from_stat(os.fstat(self.lock_fd))
                == self.lock_identity
                and ContainerFileIdentity.from_stat(lock_path)
                == self.lock_identity
                and ContainerRootIdentity.from_stat(
                    os.fstat(self.journal_fd)
                )
                == self.journal_identity
                and ContainerRootIdentity.from_stat(journal_path)
                == self.journal_identity
                and root_identity_matches(
                    self.authority_root,
                    self.authority_identity,
                )
            )
        except (OSError, ValueError):
            return False


@dataclass
class ContainerStagingLifecycleHandle:
    lease: ContainerStagingAuthorityLease
    record: ContainerStagingJournalRecord
    journal_identity: ContainerFileIdentity


def acquire_staging_authority(
    staging_root: Path,
    *,
    workspace_roots: tuple[Path, ...],
) -> ContainerStagingAuthorityLease:
    root_fd = lock_fd = journal_fd = -1
    try:
        authority_root, authority_identity = capture_private_directory(
            staging_root,
            workspace_roots=workspace_roots,
            label="container staging root",
        )
        root_fd = os.open(authority_root, OPEN_DIRECTORY_FLAGS)
        if ContainerRootIdentity.from_stat(os.fstat(root_fd)) != authority_identity:
            raise ContainerStagingError("staging_authority_changed")
        lock_fd = os.open(
            STAGING_LOCK_NAME,
            os.O_RDWR
            | os.O_CREAT
            | os.O_NOFOLLOW
            | os.O_NONBLOCK
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=root_fd,
        )
        assert_private_regular(lock_fd, "staging_lock_invalid")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ContainerStagingError(
                "staging_authority_busy",
                cleanup_verified=True,
            ) from exc
        journal_fd = open_or_create_staging_journal(root_fd)
        os.fsync(root_fd)
        lock_identity = ContainerFileIdentity.from_stat(os.fstat(lock_fd))
        journal_identity = ContainerRootIdentity.from_stat(os.fstat(journal_fd))
        if (
            ContainerFileIdentity.from_stat(
                os.stat(
                    STAGING_LOCK_NAME,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
            )
            != lock_identity
            or ContainerRootIdentity.from_stat(
                os.stat(
                    STAGING_JOURNAL_DIRECTORY_NAME,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
            )
            != journal_identity
        ):
            raise ContainerStagingError("staging_authority_changed")
        lease = ContainerStagingAuthorityLease(
            authority_root,
            authority_identity,
            lock_identity,
            journal_identity,
            root_fd,
            lock_fd,
            journal_fd,
        )
        if not lease.root_is_current():
            raise ContainerStagingError("staging_authority_changed")
        root_fd = lock_fd = journal_fd = -1
        return lease
    except ContainerStagingError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ContainerStagingError("staging_authority_invalid") from exc
    finally:
        _close_fds(journal_fd, lock_fd, root_fd)


def reserve_staging_lifecycle(
    lease: ContainerStagingAuthorityLease,
    *,
    attempt_id: str,
    workspace_roots_revision: int,
    workspace_roots: tuple[ContainerStagingWorkspaceBinding, ...],
) -> ContainerStagingLifecycleHandle:
    if not lease.root_is_current():
        raise ContainerStagingError("staging_authority_changed")
    authority_path_identity = ContainerDirectoryPathIdentity.from_stat(
        os.fstat(lease.root_fd)
    )
    if not directory_path_identity_matches(
        lease.authority_root,
        authority_path_identity,
    ):
        raise ContainerStagingError("staging_authority_changed")
    record = ContainerStagingJournalRecord(
        attempt_id=attempt_id,
        state="reserved",
        authority_root=lease.authority_root,
        authority_identity=lease.authority_identity,
        authority_path_identity=authority_path_identity,
        attempt_identity=None,
        attempt_path_identity=None,
        workspace_roots_revision=workspace_roots_revision,
        workspace_roots=workspace_roots,
    )
    identity = write_staging_record(
        lease.journal_fd,
        record,
        expected_identity=None,
        authority_is_current=lease.root_is_current,
    )
    return ContainerStagingLifecycleHandle(lease, record, identity)


def mark_staging_allocated(
    handle: ContainerStagingLifecycleHandle,
    *,
    attempt_identity: ContainerRootIdentity,
    attempt_path_identity: ContainerDirectoryPathIdentity,
) -> ContainerStagingLifecycleResult:
    allocated = replace(
        handle.record,
        state="allocated",
        authority_path_identity=_authority_path_identity(handle.lease),
        attempt_identity=attempt_identity,
        attempt_path_identity=attempt_path_identity,
    )
    return _transition(
        handle,
        allocated,
        success="staging_attempt_allocated",
        failure="staging_allocation_journal_failed",
    )


def mark_staging_prepared(
    handle: ContainerStagingLifecycleHandle,
    *,
    attempt_path_identity: ContainerDirectoryPathIdentity,
) -> ContainerStagingLifecycleResult:
    prepared = replace(
        handle.record,
        state="prepared",
        attempt_path_identity=attempt_path_identity,
    )
    return _transition(
        handle,
        prepared,
        success="staging_attempt_prepared",
        failure="staging_prepare_journal_failed",
    )


def mark_staging_create_possible(
    handle: ContainerStagingLifecycleHandle,
    binding: ContainerStagingContainerBinding,
) -> ContainerStagingLifecycleResult:
    possible = replace(
        handle.record,
        state="create_possible",
        container=binding,
    )
    return _transition(
        handle,
        possible,
        success="staging_container_create_possible",
        failure="staging_container_create_journal_failed",
    )


def mark_staging_execution_create_possible(
    handle: ContainerStagingLifecycleHandle,
) -> ContainerStagingLifecycleResult:
    possible = replace(handle.record, state="execution_create_possible")
    return _transition(
        handle,
        possible,
        success="staging_execution_create_possible",
        failure="staging_execution_create_journal_failed",
    )


def mark_staging_execution_absent(
    handle: ContainerStagingLifecycleHandle,
) -> ContainerStagingLifecycleResult:
    absent = replace(handle.record, state="execution_absent")
    return _transition(
        handle,
        absent,
        success="staging_execution_absent_recorded",
        failure="staging_execution_absence_journal_failed",
    )


def staging_lifecycle_is_current(
    handle: ContainerStagingLifecycleHandle,
) -> bool:
    record = handle.record
    lease = handle.lease
    if (
        not lease.root_is_current()
        or not directory_path_identity_matches(
            record.authority_root,
            record.authority_path_identity,
        )
        or record.attempt_path_identity is not None
        and not directory_path_identity_matches(
            record.attempt_path,
            record.attempt_path_identity,
        )
    ):
        return False
    try:
        observed, identity = read_staging_record(
            lease.journal_fd,
            record.attempt_id,
            expected_identity=handle.journal_identity,
        )
    except ContainerStagingError:
        return False
    return observed == record and identity == handle.journal_identity


def mark_staging_container_absent(
    handle: ContainerStagingLifecycleHandle,
) -> ContainerStagingLifecycleResult:
    absent = replace(handle.record, state="container_absent")
    return _transition(
        handle,
        absent,
        success="staging_container_absent_recorded",
        failure="staging_container_absence_journal_failed",
    )


def cleanup_staging_lifecycle(
    handle: ContainerStagingLifecycleHandle,
) -> ContainerStagingLifecycleResult:
    try:
        if handle.record.state != "container_absent":
            return _unresolved("staging_container_closure_unverified")
        if not staging_lifecycle_is_current(handle):
            return _unresolved("staging_attempt_changed")
        return _cleanup_closed_record(handle)
    finally:
        handle.lease.close()


def discard_unreleased_staging_attempt(
    handle: ContainerStagingLifecycleHandle,
    *,
    attempt_created: bool,
    attempt_identity: ContainerRootIdentity | None,
) -> ContainerStagingLifecycleResult:
    """Remove a partial attempt before any container could have been created."""

    lease = handle.lease
    attempt_id = handle.record.attempt_id
    try:
        if handle.record.state not in {"reserved", "allocated", "prepared"}:
            return _unresolved("staging_container_closure_unverified")
        if attempt_identity is None:
            if attempt_created or not _attempt_is_absent(lease, attempt_id):
                return _unresolved("staging_attempt_identity_unavailable")
            remove_staging_record(
                lease.journal_fd,
                handle.record,
                handle.journal_identity,
            )
            return _verified("staging_cleanup_verified_absent")
        observed = os.stat(
            attempt_id,
            dir_fd=lease.root_fd,
            follow_symlinks=False,
        )
        if ContainerRootIdentity.from_stat(observed) != attempt_identity:
            return _unresolved("staging_attempt_changed")
        attempt_fd = os.open(
            attempt_id,
            OPEN_DIRECTORY_FLAGS,
            dir_fd=lease.root_fd,
        )
        try:
            if ContainerRootIdentity.from_stat(os.fstat(attempt_fd)) != attempt_identity:
                return _unresolved("staging_attempt_changed")
            remove_directory_contents(
                attempt_fd,
                open_directory_flags=OPEN_DIRECTORY_FLAGS,
            )
        finally:
            os.close(attempt_fd)
        os.rmdir(attempt_id, dir_fd=lease.root_fd)
        os.fsync(lease.root_fd)
        remove_staging_record(
            lease.journal_fd,
            handle.record,
            handle.journal_identity,
        )
        return (
            _verified("staging_cleanup_verified_absent")
            if _attempt_is_absent(lease, attempt_id)
            else _unresolved("staging_cleanup_still_present")
        )
    except FileNotFoundError:
        try:
            remove_staging_record(
                lease.journal_fd,
                handle.record,
                handle.journal_identity,
            )
        except (ContainerStagingError, OSError):
            return _unresolved("staging_journal_remove_failed")
        return _verified("staging_cleanup_verified_absent")
    except (ContainerStagingError, OSError, RuntimeError, ValueError):
        return _unresolved("staging_cleanup_failed")
    finally:
        lease.close()


def require_empty_staging_authority(
    lease: ContainerStagingAuthorityLease,
) -> None:
    try:
        root_names = set(os.listdir(lease.root_fd))
        journal_names = set(os.listdir(lease.journal_fd))
    except OSError as exc:
        raise ContainerStagingError("staging_recovery_scan_failed") from exc
    allowed = {STAGING_LOCK_NAME, STAGING_JOURNAL_DIRECTORY_NAME}
    if root_names - allowed or journal_names:
        raise ContainerStagingError("staging_recovery_unresolved")


def recover_staging_authority(
    *,
    staging_root: Path,
    workspace_roots: tuple[Path, ...],
    workspace_roots_revision: int,
    recover_container: Callable[
        [ContainerStagingContainerBinding, str],
        ContainerStagingContainerRecoveryResult,
    ]
    | None = None,
) -> ContainerStagingRecoveryResult:
    try:
        lease = acquire_staging_authority(
            staging_root,
            workspace_roots=workspace_roots,
        )
    except ContainerStagingError as exc:
        return ContainerStagingRecoveryResult(exc.kind, False, True)
    try:
        try:
            records = scan_staging_records(lease.journal_fd)
            attempt_names = _attempt_names(lease)
        except ContainerStagingError as exc:
            return ContainerStagingRecoveryResult(exc.kind, False, True)
        if len(records) > 1 or len(attempt_names) > 1:
            return ContainerStagingRecoveryResult(
                "staging_recovery_ambiguous",
                False,
                True,
            )
        if not records:
            if attempt_names:
                return ContainerStagingRecoveryResult(
                    "staging_recovery_orphan_attempt",
                    False,
                    True,
                )
            return ContainerStagingRecoveryResult(
                "staging_recovery_ready",
                True,
                False,
            )
        record, identity = next(iter(records.values()))
        if not _record_matches_workspace(
            record,
            workspace_roots,
            workspace_roots_revision,
        ):
            return ContainerStagingRecoveryResult(
                "staging_recovery_workspace_mismatch",
                False,
                True,
            )
        if attempt_names - {record.attempt_id}:
            return ContainerStagingRecoveryResult(
                "staging_recovery_orphan_attempt",
                False,
                True,
            )
        present = record.attempt_id in attempt_names
        handle = ContainerStagingLifecycleHandle(lease, record, identity)
        if record.state == "reserved":
            if present:
                return _recovery_unresolved(
                    "staging_recovery_reserved_attempt_unidentified"
                )
            return _remove_recovered_record(handle)
        if record.state in {"allocated", "prepared"}:
            if not present:
                return _remove_recovered_record(handle)
            strict = record.state == "prepared"
            if not _recovery_attempt_is_current(handle, strict_path=strict):
                return _recovery_unresolved("staging_attempt_changed")
            current_path = ContainerDirectoryPathIdentity.from_stat(
                os.stat(
                    record.attempt_id,
                    dir_fd=lease.root_fd,
                    follow_symlinks=False,
                )
            )
            absent = replace(
                record,
                state="container_absent",
                attempt_path_identity=current_path,
            )
            transition = _transition(
                handle,
                absent,
                success="staging_container_absent_recorded",
                failure="staging_container_absence_journal_failed",
            )
            if not transition.verified:
                return _recovery_unresolved(transition.reason_code)
        elif record.state in {
            "create_possible",
            "execution_create_possible",
            "execution_absent",
        }:
            binding = record.container
            if binding is None or recover_container is None:
                return _recovery_unresolved(
                    "staging_recovery_container_unresolved"
                )
            try:
                recovered = recover_container(binding, record.state)
            except BaseException:
                return _recovery_unresolved(
                    "staging_recovery_container_exception"
                )
            if not recovered.absent_verified:
                return _recovery_unresolved(recovered.reason_code)
            if record.state == "execution_create_possible":
                execution_absent = _transition(
                    handle,
                    replace(record, state="execution_absent"),
                    success="staging_execution_absent_recorded",
                    failure="staging_execution_absence_journal_failed",
                    allow_attempt_absent=True,
                )
                if not execution_absent.verified:
                    return _recovery_unresolved(
                        execution_absent.reason_code
                    )
            transition = (
                _transition(
                    handle,
                    replace(handle.record, state="container_absent"),
                    success="staging_container_absent_recorded",
                    failure="staging_container_absence_journal_failed",
                    allow_attempt_absent=True,
                )
                if not present
                else mark_staging_container_absent(handle)
            )
            if not transition.verified:
                return _recovery_unresolved(transition.reason_code)
        if not present:
            return _remove_recovered_record(handle)
        result = (
            _resume_cleanup_record(handle)
            if handle.record.state == "cleanup_started"
            else _cleanup_closed_record(handle)
        )
        if not result.verified:
            return _recovery_unresolved(result.reason_code)
        return ContainerStagingRecoveryResult(
            "staging_recovery_completed",
            True,
            False,
            1,
        )
    finally:
        lease.close()


def _cleanup_closed_record(
    handle: ContainerStagingLifecycleHandle,
) -> ContainerStagingLifecycleResult:
    if not staging_lifecycle_is_current(handle):
        return _unresolved("staging_attempt_changed")
    started = replace(handle.record, state="cleanup_started")
    transition = _transition(
        handle,
        started,
        success="staging_cleanup_started",
        failure="staging_cleanup_journal_failed",
    )
    if not transition.verified:
        return transition
    return _resume_cleanup_record(handle)


def _resume_cleanup_record(
    handle: ContainerStagingLifecycleHandle,
) -> ContainerStagingLifecycleResult:
    lease = handle.lease
    record = handle.record
    attempt_identity = record.attempt_identity
    if (
        record.state != "cleanup_started"
        or attempt_identity is None
        or not _journal_handle_is_current(handle)
    ):
        return _unresolved("staging_cleanup_state_invalid")
    try:
        attempt_fd = os.open(
            record.attempt_id,
            OPEN_DIRECTORY_FLAGS,
            dir_fd=lease.root_fd,
        )
        try:
            if (
                ContainerRootIdentity.from_stat(os.fstat(attempt_fd))
                != attempt_identity
            ):
                return _unresolved("staging_attempt_changed")
            remove_directory_contents(
                attempt_fd,
                open_directory_flags=OPEN_DIRECTORY_FLAGS,
            )
        finally:
            os.close(attempt_fd)
        os.rmdir(record.attempt_id, dir_fd=lease.root_fd)
        os.fsync(lease.root_fd)
        if not _attempt_is_absent(lease, record.attempt_id):
            return _unresolved("staging_cleanup_still_present")
        remove_staging_record(
            lease.journal_fd,
            record,
            handle.journal_identity,
        )
        return _verified("staging_cleanup_verified_absent")
    except (ContainerStagingError, OSError, RuntimeError, ValueError):
        return _unresolved("staging_cleanup_failed")


def _transition(
    handle: ContainerStagingLifecycleHandle,
    record: ContainerStagingJournalRecord,
    *,
    success: str,
    failure: str,
    allow_attempt_absent: bool = False,
) -> ContainerStagingLifecycleResult:
    if not _journal_handle_is_current(handle):
        return _unresolved(failure)
    if not directory_path_identity_matches(
        record.authority_root,
        record.authority_path_identity,
    ):
        return _unresolved(failure)
    if record.attempt_path_identity is not None:
        current = directory_path_identity_matches(
            record.attempt_path,
            record.attempt_path_identity,
        )
        if not current and not (
            allow_attempt_absent
            and _attempt_is_absent(handle.lease, record.attempt_id)
        ):
            return _unresolved(failure)
    try:
        identity = write_staging_record(
            handle.lease.journal_fd,
            record,
            expected_identity=handle.journal_identity,
            authority_is_current=handle.lease.root_is_current,
        )
    except ContainerStagingError:
        return _unresolved(failure)
    handle.record = record
    handle.journal_identity = identity
    return _verified(success)


def _journal_handle_is_current(
    handle: ContainerStagingLifecycleHandle,
) -> bool:
    if not handle.lease.root_is_current():
        return False
    try:
        observed, identity = read_staging_record(
            handle.lease.journal_fd,
            handle.record.attempt_id,
            expected_identity=handle.journal_identity,
        )
    except ContainerStagingError:
        return False
    return observed == handle.record and identity == handle.journal_identity


def _authority_path_identity(
    lease: ContainerStagingAuthorityLease,
) -> ContainerDirectoryPathIdentity:
    identity = ContainerDirectoryPathIdentity.from_stat(os.fstat(lease.root_fd))
    if not directory_path_identity_matches(lease.authority_root, identity):
        raise ContainerStagingError("staging_authority_changed")
    return identity


def _recovery_attempt_is_current(
    handle: ContainerStagingLifecycleHandle,
    *,
    strict_path: bool,
) -> bool:
    identity = handle.record.attempt_identity
    if identity is None or not _journal_handle_is_current(handle):
        return False
    try:
        observed = os.stat(
            handle.record.attempt_id,
            dir_fd=handle.lease.root_fd,
            follow_symlinks=False,
        )
    except OSError:
        return False
    if ContainerRootIdentity.from_stat(observed) != identity:
        return False
    return (
        not strict_path
        or handle.record.attempt_path_identity is not None
        and ContainerDirectoryPathIdentity.from_stat(observed)
        == handle.record.attempt_path_identity
    )


def _remove_recovered_record(
    handle: ContainerStagingLifecycleHandle,
) -> ContainerStagingRecoveryResult:
    try:
        remove_staging_record(
            handle.lease.journal_fd,
            handle.record,
            handle.journal_identity,
        )
    except (ContainerStagingError, OSError):
        return _recovery_unresolved(
            "staging_recovery_journal_remove_failed"
        )
    return ContainerStagingRecoveryResult(
        "staging_recovery_completed",
        True,
        False,
        1,
    )


def _recovery_unresolved(reason_code: str) -> ContainerStagingRecoveryResult:
    return ContainerStagingRecoveryResult(reason_code, False, True)


def _attempt_names(lease: ContainerStagingAuthorityLease) -> set[str]:
    try:
        names = set(os.listdir(lease.root_fd))
    except OSError as exc:
        raise ContainerStagingError("staging_recovery_scan_failed") from exc
    metadata = {STAGING_LOCK_NAME, STAGING_JOURNAL_DIRECTORY_NAME}
    attempts = names - metadata
    if any(STAGING_ATTEMPT_NAME.fullmatch(name) is None for name in attempts):
        raise ContainerStagingError("staging_recovery_authority_invalid")
    return attempts


def _record_matches_workspace(
    record: ContainerStagingJournalRecord,
    workspace_roots: tuple[Path, ...],
    workspace_roots_revision: int,
) -> bool:
    if (
        record.workspace_roots_revision != workspace_roots_revision
        or tuple(binding.path for binding in record.workspace_roots)
        != workspace_roots
    ):
        return False
    try:
        return all(
            (metadata.st_dev, metadata.st_ino) == (binding.device, binding.inode)
            for binding in record.workspace_roots
            for metadata in (os.stat(binding.path, follow_symlinks=False),)
        )
    except OSError:
        return False


def _attempt_is_absent(
    lease: ContainerStagingAuthorityLease,
    attempt_id: str,
) -> bool:
    try:
        os.stat(attempt_id, dir_fd=lease.root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _verified(reason_code: str) -> ContainerStagingLifecycleResult:
    return ContainerStagingLifecycleResult(reason_code, True, False)


def _unresolved(reason_code: str) -> ContainerStagingLifecycleResult:
    return ContainerStagingLifecycleResult(reason_code, False, True)


def _close_fds(*descriptors: int) -> None:
    for descriptor in descriptors:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


__all__ = [
    "ContainerStagingAuthorityLease",
    "ContainerStagingContainerBinding",
    "ContainerStagingContainerRecoveryResult",
    "ContainerStagingError",
    "ContainerStagingJournalRecord",
    "ContainerStagingLifecycleHandle",
    "ContainerStagingLifecycleResult",
    "ContainerStagingRecoveryResult",
    "ContainerStagingWorkspaceBinding",
    "acquire_staging_authority",
    "cleanup_staging_lifecycle",
    "discard_unreleased_staging_attempt",
    "mark_staging_allocated",
    "mark_staging_container_absent",
    "mark_staging_create_possible",
    "mark_staging_execution_absent",
    "mark_staging_execution_create_possible",
    "mark_staging_prepared",
    "recover_staging_authority",
    "reserve_staging_lifecycle",
    "require_empty_staging_authority",
    "staging_lifecycle_is_current",
]

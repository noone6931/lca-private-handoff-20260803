from __future__ import annotations

import errno
import os
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from .rooted_contracts import RootedFileError
from .rooted_exchange import atomic_exchange_at
from .rooted_exchange import atomic_noreplace_at
from .rooted_exchange import atomic_rename_supported
from .rooted_mutation_validation import assert_displaced_regular_matches_before
from .rooted_mutation_validation import assert_open_regular_unchanged
from .rooted_validation import identity


_READ_CHUNK_BYTES = 64 * 1024
_TEMPORARY_ATTEMPTS = 8
_ATOMIC_UNSUPPORTED_ERRNOS = frozenset(
    {
        errno.EINVAL,
        errno.ENOSYS,
        errno.ENOTSUP,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    }
)


@dataclass(frozen=True)
class RootedMutationCommit:
    changed: bool
    after_identity: tuple[int, int] | None


def mutate_regular_at(
    parent_fd: int,
    name: str,
    *,
    before_content: bytes | None,
    after_content: bytes | None,
    before_mode: int | None,
    after_mode: int | None,
    validate_authority: Callable[[], None],
) -> RootedMutationCommit:
    if not atomic_rename_supported():
        raise RootedFileError("atomic_mutation_unsupported")
    if before_content is None:
        assert after_content is not None and after_mode is not None
        return _create_regular(
            parent_fd,
            name,
            after_content,
            after_mode,
            validate_authority,
        )
    descriptor = -1
    try:
        descriptor, opened, observed = _open_regular_exact(
            parent_fd,
            name,
        )
        if observed != before_content or stat.S_IMODE(opened.st_mode) != before_mode:
            raise RootedFileError("stale_before_image")
        validate_authority()
        if after_content is None:
            return _delete_regular(
                parent_fd,
                name,
                descriptor,
                opened,
                before_content,
                before_mode,
                validate_authority,
            )
        assert after_mode is not None
        return _replace_regular(
            parent_fd,
            name,
            descriptor,
            opened,
            before_content,
            before_mode,
            after_content,
            after_mode,
            validate_authority,
        )
    finally:
        _close_fd(descriptor)


def _create_regular(
    parent_fd: int,
    name: str,
    content: bytes,
    mode: int,
    validate_authority: Callable[[], None],
) -> RootedMutationCommit:
    temporary, descriptor, prepared = _prepare_regular(parent_fd, content, mode)
    temporary_present = True
    committed = False
    try:
        validate_authority()
        try:
            _atomic_noreplace(parent_fd, temporary, parent_fd, name)
        except OSError as exc:
            if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                raise RootedFileError("stale_before_image") from exc
            raise
        temporary_present = False
        committed = True
        _assert_path_identity(parent_fd, name, prepared)
        os.fsync(parent_fd)
        return RootedMutationCommit(True, identity(prepared))
    except RootedFileError as exc:
        residual = (
            not _unlink_exact(parent_fd, temporary, prepared)
            if temporary_present
            else False
        )
        if committed or residual or exc.workspace_changed:
            raise RootedFileError(exc.kind, workspace_changed=True) from exc
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        residual = (
            not _unlink_exact(parent_fd, temporary, prepared)
            if temporary_present
            else False
        )
        raise RootedFileError(
            "mutation_failed",
            workspace_changed=committed or residual,
        ) from exc
    finally:
        _close_fd(descriptor)


def _replace_regular(
    parent_fd: int,
    name: str,
    original_fd: int,
    opened: os.stat_result,
    before_content: bytes,
    before_mode: int | None,
    after_content: bytes,
    after_mode: int,
    validate_authority: Callable[[], None],
) -> RootedMutationCommit:
    temporary, prepared_fd, prepared = _prepare_regular(
        parent_fd,
        after_content,
        after_mode,
    )
    exchanged = False
    temporary_contains_prepared = True
    try:
        assert_open_regular_unchanged(
            original_fd,
            parent_fd,
            name,
            opened,
            before_content,
            before_mode,
            _read_descriptor_exact,
        )
        validate_authority()
        _atomic_exchange(parent_fd, name, parent_fd, temporary)
        exchanged = True
        temporary_contains_prepared = False
        try:
            assert_displaced_regular_matches_before(
                original_fd,
                parent_fd,
                temporary,
                opened,
                before_content,
                before_mode,
                _read_descriptor_exact,
            )
        except RootedFileError as stale:
            restored = _rollback_exchange(
                parent_fd,
                name,
                temporary,
                prepared,
                opened,
            )
            temporary_contains_prepared = restored
            if restored:
                exchanged = False
                if not _unlink_exact(parent_fd, temporary, prepared):
                    raise RootedFileError(
                        "stale_before_image",
                        workspace_changed=True,
                    ) from stale
                temporary_contains_prepared = False
                raise RootedFileError("stale_before_image") from stale
            raise RootedFileError(
                "stale_before_image",
                workspace_changed=True,
            ) from stale
        _assert_path_identity(parent_fd, name, prepared)
        old = os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False)
        if identity(old) != identity(opened):
            raise RootedFileError(
                "path_identity_changed",
                workspace_changed=True,
            )
        os.unlink(temporary, dir_fd=parent_fd)
        os.fsync(parent_fd)
        temporary_contains_prepared = False
        return RootedMutationCommit(True, identity(prepared))
    except RootedFileError as exc:
        residual = False
        if temporary_contains_prepared:
            removed = _unlink_exact(parent_fd, temporary, prepared)
            temporary_contains_prepared = not removed
            residual = not removed
        if residual and not exc.workspace_changed:
            raise RootedFileError(exc.kind, workspace_changed=True) from exc
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        restored = (
            _rollback_exchange(
                parent_fd,
                name,
                temporary,
                prepared,
                opened,
            )
            if exchanged
            else False
        )
        temporary_contains_prepared = temporary_contains_prepared or restored
        residual = exchanged and not restored
        if temporary_contains_prepared:
            removed = _unlink_exact(parent_fd, temporary, prepared)
            temporary_contains_prepared = not removed
            residual = residual or not removed
        raise RootedFileError(
            "mutation_failed",
            workspace_changed=residual,
        ) from exc
    finally:
        _close_fd(prepared_fd)
        if temporary_contains_prepared:
            _unlink_exact(parent_fd, temporary, prepared)


def _delete_regular(
    parent_fd: int,
    name: str,
    original_fd: int,
    opened: os.stat_result,
    before_content: bytes,
    before_mode: int | None,
    validate_authority: Callable[[], None],
) -> RootedMutationCommit:
    backup = _unused_name(parent_fd)
    moved = False
    removed = False
    try:
        assert_open_regular_unchanged(
            original_fd,
            parent_fd,
            name,
            opened,
            before_content,
            before_mode,
            _read_descriptor_exact,
        )
        validate_authority()
        _atomic_noreplace(parent_fd, name, parent_fd, backup)
        moved = True
        try:
            assert_displaced_regular_matches_before(
                original_fd,
                parent_fd,
                backup,
                opened,
                before_content,
                before_mode,
                _read_descriptor_exact,
            )
        except RootedFileError as stale:
            if _rollback_move(parent_fd, backup, name, opened):
                moved = False
                raise RootedFileError("stale_before_image") from stale
            raise RootedFileError(
                "stale_before_image",
                workspace_changed=True,
            ) from stale
        os.unlink(backup, dir_fd=parent_fd)
        moved = False
        removed = True
        os.fsync(parent_fd)
        return RootedMutationCommit(True, None)
    except RootedFileError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        restored = (
            _rollback_move(parent_fd, backup, name, opened)
            if moved
            else False
        )
        raise RootedFileError(
            "mutation_failed",
            workspace_changed=removed or moved and not restored,
        ) from exc


def _rollback_exchange(
    parent_fd: int,
    name: str,
    temporary: str,
    prepared: os.stat_result,
    displaced_expected: os.stat_result,
) -> bool:
    try:
        current_target = os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if identity(current_target) != identity(prepared):
            return False
        displaced = os.stat(
            temporary,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if identity(displaced) != identity(displaced_expected):
            return False
        atomic_exchange_at(parent_fd, name, parent_fd, temporary)
        restored = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        leftover = os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False)
        if (
            identity(restored) == identity(displaced)
            and identity(leftover) == identity(prepared)
        ):
            os.fsync(parent_fd)
            return True
        return False
    except (OSError, RuntimeError, ValueError):
        return False


def _rollback_move(
    parent_fd: int,
    backup: str,
    name: str,
    expected: os.stat_result,
) -> bool:
    try:
        moved = os.stat(backup, dir_fd=parent_fd, follow_symlinks=False)
        if identity(moved) != identity(expected):
            return False
        atomic_noreplace_at(parent_fd, backup, parent_fd, name)
        restored = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if identity(restored) != identity(moved):
            return False
        os.fsync(parent_fd)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _prepare_regular(
    parent_fd: int,
    content: bytes,
    mode: int,
) -> tuple[str, int, os.stat_result]:
    name = _unused_name(parent_fd)
    descriptor = -1
    created = False
    prepared: os.stat_result | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_NONBLOCK
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        created = True
        _write_all(descriptor, content)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        prepared = os.fstat(descriptor)
        if not stat.S_ISREG(prepared.st_mode) or prepared.st_nlink != 1:
            raise RootedFileError("not_single_link_regular")
        return name, descriptor, prepared
    except RootedFileError as exc:
        if created and prepared is None:
            try:
                prepared = os.fstat(descriptor)
            except OSError:
                prepared = None
        residual = created and (
            prepared is None or not _unlink_exact(parent_fd, name, prepared)
        )
        _close_fd(descriptor)
        raise RootedFileError(
            exc.kind,
            workspace_changed=residual or exc.workspace_changed,
        ) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        if created and prepared is None:
            try:
                prepared = os.fstat(descriptor)
            except OSError:
                prepared = None
        residual = created and (
            prepared is None or not _unlink_exact(parent_fd, name, prepared)
        )
        _close_fd(descriptor)
        raise RootedFileError(
            "mutation_failed",
            workspace_changed=residual,
        ) from exc


def _atomic_exchange(
    source_fd: int,
    source: str,
    destination_fd: int,
    destination: str,
) -> None:
    try:
        atomic_exchange_at(
            source_fd,
            source,
            destination_fd,
            destination,
        )
    except OSError as exc:
        if exc.errno in _ATOMIC_UNSUPPORTED_ERRNOS:
            raise RootedFileError("atomic_mutation_unsupported") from exc
        raise


def _atomic_noreplace(
    source_fd: int,
    source: str,
    destination_fd: int,
    destination: str,
) -> None:
    try:
        atomic_noreplace_at(
            source_fd,
            source,
            destination_fd,
            destination,
        )
    except OSError as exc:
        if exc.errno in _ATOMIC_UNSUPPORTED_ERRNOS:
            raise RootedFileError("atomic_mutation_unsupported") from exc
        raise


def _open_regular_exact(
    parent_fd: int,
    name: str,
) -> tuple[int, os.stat_result, bytes]:
    inspected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(inspected.st_mode) or inspected.st_nlink != 1:
        raise RootedFileError("not_single_link_regular")
    descriptor = os.open(
        name,
        os.O_RDONLY
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or identity(opened) != identity(inspected)
        ):
            raise RootedFileError("file_identity_changed")
        content = _read_descriptor_exact(descriptor, opened.st_size)
        completed = os.fstat(descriptor)
        if (
            completed.st_size != opened.st_size
            or completed.st_mtime_ns != opened.st_mtime_ns
            or completed.st_ctime_ns != opened.st_ctime_ns
        ):
            raise RootedFileError("snapshot_changed")
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if identity(current) != identity(opened):
            raise RootedFileError("path_identity_changed")
        return descriptor, opened, content
    except BaseException:
        os.close(descriptor)
        raise


def _assert_path_identity(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
) -> None:
    observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or identity(observed) != identity(expected)
    ):
        raise RootedFileError("path_identity_changed", workspace_changed=True)


def _unlink_exact(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
) -> bool:
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if identity(observed) != identity(expected):
            return False
        os.unlink(name, dir_fd=parent_fd)
        return True
    except FileNotFoundError:
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _unused_name(parent_fd: int) -> str:
    for _ in range(_TEMPORARY_ATTEMPTS):
        name = f".lca-mutation-{uuid.uuid4().hex}"
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return name
        except OSError as exc:
            raise RootedFileError("temporary_path_failed") from exc
    raise RootedFileError("temporary_path_exhausted")


def _read_descriptor_exact(descriptor: int, size: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(remaining, _READ_CHUNK_BYTES))
        if not chunk:
            raise RootedFileError("short_read")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "short rooted mutation write")
        offset += written


def _close_fd(descriptor: int) -> None:
    if descriptor >= 0:
        try:
            os.close(descriptor)
        except OSError:
            pass


__all__ = ["RootedMutationCommit", "mutate_regular_at"]

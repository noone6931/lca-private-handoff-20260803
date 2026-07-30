"""Root-contained regular-file operations built on directory descriptors."""

from __future__ import annotations

import codecs
import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .rooted_contracts import RootedAppendResult
from .rooted_contracts import RootedDirectoryListing
from .rooted_contracts import RootedFileError
from .rooted_contracts import RootedMutationResult
from .rooted_contracts import RootedRegularSnapshot
from .rooted_contracts import RootedTextSnapshot
from .rooted_paths import canonical_root as _canonical_root
from .rooted_paths import creation_parent as _creation_parent
from .rooted_paths import lexical_under_root as _lexical_under_root
from .rooted_paths import resolve_existing as _resolve_existing
from .rooted_mutation_validation import assert_same_regular as _assert_same_regular
from .rooted_mutation import mutate_regular_at as _mutate_regular_at
from .rooted_validation import assert_expected_root_identity as _assert_expected_root_identity
from .rooted_validation import directory_snapshot as _directory_snapshot
from .rooted_validation import file_snapshot as _snapshot
from .rooted_validation import identity as _identity
from .rooted_validation import validate_mutation_sides as _validate_mutation_sides
from .rooted_validation import validate_relative_parts as _validate_relative_parts


_READ_CHUNK_BYTES = 64 * 1024
_HAS_ROOTED_FD_SUPPORT = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.listdir in os.supports_fd
)


@dataclass(frozen=True)
class _RootedDirectoryHandle:
    descriptor: int
    relative_parts: tuple[str, ...]
    identities: tuple[tuple[int, int], ...]


def read_rooted_utf8(
    root: Path,
    lexical_path: Path,
    *,
    expected_root_identity: tuple[int, int] | None = None,
) -> RootedTextSnapshot:
    """Read one existing regular UTF-8 file through a verified rooted fd chain."""

    _require_rooted_fd_support()
    canonical_root = _canonical_root(root, expected_root_identity)
    lexical = _lexical_under_root(canonical_root, lexical_path)
    canonical = _resolve_existing(canonical_root, lexical)
    root_fd = _open_root(canonical_root, expected_root_identity)
    parent: _RootedDirectoryHandle | None = None
    file_fd = -1
    try:
        parent, name = _open_canonical_parent(root_fd, canonical_root, canonical)
        _validate_directory_authority(canonical_root, root_fd, parent, expected_root_identity)
        inspected = _stat_regular(parent.descriptor, name)
        file_fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent.descriptor,
        )
        opened = os.fstat(file_fd)
        _assert_same_regular(inspected, opened)
        _validate_directory_authority(canonical_root, root_fd, parent, expected_root_identity)
        initial = _snapshot(opened)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        remaining = opened.st_size
        parts: list[str] = []
        while remaining:
            chunk = os.read(file_fd, min(remaining, _READ_CHUNK_BYTES))
            if not chunk:
                raise RootedFileError("short_read")
            remaining -= len(chunk)
            parts.append(decoder.decode(chunk, final=False))
        parts.append(decoder.decode(b"", final=True))
        completed = os.fstat(file_fd)
        if not stat.S_ISREG(completed.st_mode) or _snapshot(completed) != initial:
            raise RootedFileError("snapshot_changed")
        entry = os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
        if _identity(entry) != _identity(opened):
            raise RootedFileError("path_identity_changed")
        _validate_directory_authority(canonical_root, root_fd, parent, expected_root_identity)
        text = "".join(parts).replace("\r\n", "\n").replace("\r", "\n")
        return RootedTextSnapshot(
            lexical_path=lexical,
            canonical_path=canonical,
            identity=_identity(opened),
            text=text,
        )
    except RootedFileError:
        raise
    except UnicodeDecodeError as exc:
        raise RootedFileError("invalid_utf8") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise RootedFileError("read_failed") from exc
    finally:
        _close_fds(file_fd, _handle_fd(parent), root_fd)


def list_rooted_directory(
    root: Path,
    lexical_path: Path,
    *,
    expected_root_identity: tuple[int, int] | None = None,
) -> RootedDirectoryListing:
    """List one existing directory after canonical authorization and fd traversal."""

    _require_rooted_fd_support()
    canonical_root = _canonical_root(root, expected_root_identity)
    lexical = _lexical_under_root(canonical_root, lexical_path)
    canonical = _resolve_existing(canonical_root, lexical)
    root_fd = -1
    directory: _RootedDirectoryHandle | None = None
    try:
        root_fd = _open_root(canonical_root, expected_root_identity)
        directory = _open_canonical_directory(root_fd, canonical_root, canonical)
        _validate_directory_authority(canonical_root, root_fd, directory, expected_root_identity)
        before = os.fstat(directory.descriptor)
        if not stat.S_ISDIR(before.st_mode):
            raise RootedFileError("not_directory")
        names = tuple(os.listdir(directory.descriptor))
        after = os.fstat(directory.descriptor)
        if _directory_snapshot(after) != _directory_snapshot(before):
            raise RootedFileError("directory_changed")
        _validate_directory_authority(canonical_root, root_fd, directory, expected_root_identity)
        return RootedDirectoryListing(
            lexical_path=lexical,
            canonical_path=canonical,
            identity=_identity(before),
            names=names,
        )
    except RootedFileError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise RootedFileError("list_failed") from exc
    finally:
        _close_fds(_handle_fd(directory), root_fd)


def read_rooted_regular(
    root: Path,
    relative_parts: tuple[str, ...],
    *,
    expected_root_identity: tuple[int, int] | None = None,
) -> RootedRegularSnapshot:
    """Read an exact regular-file state without following any relative link."""

    _require_rooted_fd_support()
    _validate_relative_parts(relative_parts)
    canonical_root = _canonical_root(root, expected_root_identity)
    root_fd = file_fd = -1
    parent: _RootedDirectoryHandle | None = None
    try:
        root_fd = _open_root(canonical_root, expected_root_identity)
        parent = _open_directory_parts(root_fd, relative_parts[:-1])
        _validate_directory_authority(
            canonical_root, root_fd, parent, expected_root_identity
        )
        try:
            file_fd, opened, content = _open_regular_exact(
                parent.descriptor,
                relative_parts[-1],
                os.O_RDONLY,
            )
        except FileNotFoundError:
            _validate_directory_authority(
                canonical_root, root_fd, parent, expected_root_identity
            )
            return RootedRegularSnapshot(relative_parts, None, None, None)
        _validate_directory_authority(
            canonical_root, root_fd, parent, expected_root_identity
        )
        return RootedRegularSnapshot(
            relative_parts,
            content,
            stat.S_IMODE(opened.st_mode),
            _identity(opened),
        )
    except RootedFileError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise RootedFileError("read_failed") from exc
    finally:
        _close_fds(file_fd, _handle_fd(parent), root_fd)


def mutate_rooted_regular(
    root: Path,
    relative_parts: tuple[str, ...],
    *,
    before_content: bytes | None,
    after_content: bytes | None,
    before_mode: int | None,
    after_mode: int | None,
    expected_root_identity: tuple[int, int] | None = None,
) -> RootedMutationResult:
    """Apply one exact create, replace, or delete through a rooted fd chain."""

    _require_rooted_fd_support()
    _validate_relative_parts(relative_parts)
    _validate_mutation_sides(before_content, after_content, before_mode, after_mode)
    canonical_root = _canonical_root(root, expected_root_identity)
    root_fd = -1
    parent: _RootedDirectoryHandle | None = None
    changed = False
    name = relative_parts[-1]
    try:
        root_fd = _open_root(canonical_root, expected_root_identity)
        parent = _open_directory_parts(root_fd, relative_parts[:-1])
        _validate_directory_authority(
            canonical_root, root_fd, parent, expected_root_identity
        )
        committed = _mutate_regular_at(
            parent.descriptor,
            name,
            before_content=before_content,
            after_content=after_content,
            before_mode=before_mode,
            after_mode=after_mode,
            validate_authority=lambda: _validate_directory_authority(
                canonical_root,
                root_fd,
                parent,
                expected_root_identity,
            ),
        )
        changed = committed.changed
        _validate_directory_authority(
            canonical_root, root_fd, parent, expected_root_identity
        )
        observed = _read_regular_at(parent.descriptor, name, relative_parts)
        if (
            after_content is not None
            and observed.identity != committed.after_identity
        ):
            raise RootedFileError(
                "path_identity_changed",
                workspace_changed=changed,
            )
        if observed.content != after_content or observed.mode != after_mode:
            raise RootedFileError(
                "write_verification_failed",
                workspace_changed=changed,
            )
        return RootedMutationResult(relative_parts, changed)
    except RootedFileError as exc:
        if changed and not exc.workspace_changed:
            raise RootedFileError(exc.kind, workspace_changed=True) from exc
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise RootedFileError(
            "mutation_failed",
            workspace_changed=changed,
        ) from exc
    finally:
        _close_fds(_handle_fd(parent), root_fd)


def append_rooted_utf8(
    root: Path,
    lexical_path: Path,
    text: str,
    *,
    expected_root_identity: tuple[int, int] | None = None,
) -> RootedAppendResult:
    """Append UTF-8 through a verified O_APPEND fd, creating missing parents safely."""

    _require_rooted_fd_support()
    try:
        payload = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RootedFileError("invalid_utf8") from exc
    canonical_root = _canonical_root(root, expected_root_identity)
    lexical = _lexical_under_root(canonical_root, lexical_path)
    try:
        lexical.lstat()
    except FileNotFoundError:
        return _append_missing(canonical_root, lexical, payload, expected_root_identity)
    except OSError as exc:
        raise RootedFileError("path_inspection_failed") from exc
    canonical = _resolve_existing(canonical_root, lexical)
    return _append_existing(canonical_root, lexical, canonical, payload, expected_root_identity)


def _append_existing(
    canonical_root: Path,
    lexical: Path,
    canonical: Path,
    payload: bytes,
    expected_root_identity: tuple[int, int] | None,
) -> RootedAppendResult:
    root_fd = file_fd = -1
    parent: _RootedDirectoryHandle | None = None
    written = 0
    try:
        root_fd = _open_root(canonical_root, expected_root_identity)
        parent, name = _open_canonical_parent(root_fd, canonical_root, canonical)
        _validate_directory_authority(canonical_root, root_fd, parent, expected_root_identity)
        inspected = _stat_regular(parent.descriptor, name)
        file_fd = os.open(
            name,
            os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent.descriptor,
        )
        opened = os.fstat(file_fd)
        _assert_same_regular(inspected, opened)
        _validate_directory_authority(canonical_root, root_fd, parent, expected_root_identity)
        written = _write_all(file_fd, payload)
        entry = os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
        if _identity(entry) != _identity(opened):
            raise RootedFileError("path_identity_changed", workspace_changed=written > 0)
        _validate_directory_authority(canonical_root, root_fd, parent, expected_root_identity)
        return RootedAppendResult(
            lexical_path=lexical,
            canonical_path=canonical,
            identity=_identity(opened),
            bytes_written=written,
        )
    except RootedFileError as exc:
        if written and not exc.workspace_changed:
            raise RootedFileError(exc.kind, workspace_changed=True) from exc
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise RootedFileError("append_failed", workspace_changed=written > 0) from exc
    finally:
        _close_fds(file_fd, _handle_fd(parent), root_fd)


def _append_missing(
    canonical_root: Path,
    lexical: Path,
    payload: bytes,
    expected_root_identity: tuple[int, int] | None,
) -> RootedAppendResult:
    root_fd = file_fd = -1
    current: _RootedDirectoryHandle | None = None
    written = 0
    workspace_changed = False
    try:
        ancestor, missing_parents = _creation_parent(canonical_root, lexical.parent)
        root_fd = _open_root(canonical_root, expected_root_identity)
        current = _open_canonical_directory(root_fd, canonical_root, ancestor)
        for component in missing_parents:
            _validate_directory_authority(canonical_root, root_fd, current, expected_root_identity)
            try:
                os.mkdir(component, mode=0o700, dir_fd=current.descriptor)
                workspace_changed = True
            except FileExistsError:
                pass
            next_handle = _descend_directory(current, component)
            os.close(current.descriptor)
            current = next_handle
            _validate_directory_authority(canonical_root, root_fd, current, expected_root_identity)
        name = lexical.name
        _validate_directory_authority(canonical_root, root_fd, current, expected_root_identity)
        try:
            file_fd = os.open(
                name,
                os.O_WRONLY
                | os.O_APPEND
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_NONBLOCK,
                0o600,
                dir_fd=current.descriptor,
            )
            workspace_changed = True
        except FileExistsError:
            _close_fds(current.descriptor, root_fd)
            current = None
            root_fd = -1
            canonical = _resolve_existing(canonical_root, lexical)
            return _append_existing(canonical_root, lexical, canonical, payload, expected_root_identity)
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise RootedFileError("not_regular")
        _validate_directory_authority(canonical_root, root_fd, current, expected_root_identity)
        written = _write_all(file_fd, payload)
        entry = os.stat(name, dir_fd=current.descriptor, follow_symlinks=False)
        if _identity(entry) != _identity(opened):
            raise RootedFileError("path_identity_changed", workspace_changed=written > 0)
        _validate_directory_authority(canonical_root, root_fd, current, expected_root_identity)
        canonical = ancestor.joinpath(*missing_parents, name)
        return RootedAppendResult(
            lexical_path=lexical,
            canonical_path=canonical,
            identity=_identity(opened),
            bytes_written=written,
        )
    except RootedFileError as exc:
        if workspace_changed and not exc.workspace_changed:
            raise RootedFileError(exc.kind, workspace_changed=True) from exc
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise RootedFileError(
            "append_failed",
            workspace_changed=workspace_changed or written > 0,
        ) from exc
    finally:
        _close_fds(file_fd, _handle_fd(current), root_fd)


def _open_root(canonical_root: Path, expected_identity: tuple[int, int] | None) -> int:
    inspected = canonical_root.lstat()
    if not stat.S_ISDIR(inspected.st_mode):
        raise RootedFileError("root_not_directory")
    _assert_expected_root_identity(inspected, expected_identity)
    descriptor = os.open(
        canonical_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or _identity(opened) != _identity(inspected):
        os.close(descriptor)
        raise RootedFileError("root_identity_changed")
    _assert_expected_root_identity(opened, expected_identity)
    return descriptor


def _open_canonical_parent(
    root_fd: int,
    canonical_root: Path,
    canonical_path: Path,
) -> tuple[_RootedDirectoryHandle, str]:
    try:
        relative = canonical_path.relative_to(canonical_root)
    except ValueError as exc:
        raise RootedFileError("outside_root") from exc
    if not relative.parts:
        raise RootedFileError("root_is_not_file")
    parent_fd = _open_directory_parts(root_fd, relative.parts[:-1])
    return parent_fd, relative.parts[-1]


def _open_canonical_directory(
    root_fd: int,
    canonical_root: Path,
    canonical_path: Path,
) -> _RootedDirectoryHandle:
    try:
        relative = canonical_path.relative_to(canonical_root)
    except ValueError as exc:
        raise RootedFileError("outside_root") from exc
    return _open_directory_parts(root_fd, relative.parts)


def _open_directory_parts(
    root_fd: int,
    parts: tuple[str, ...],
) -> _RootedDirectoryHandle:
    current = os.dup(root_fd)
    identities = [_identity(os.fstat(root_fd))]
    try:
        for component in parts:
            next_fd, next_identity = _open_child_directory(current, component)
            os.close(current)
            current = next_fd
            identities.append(next_identity)
        return _RootedDirectoryHandle(current, parts, tuple(identities))
    except Exception:
        os.close(current)
        raise


def _open_child_directory(parent_fd: int, name: str) -> tuple[int, tuple[int, int]]:
    inspected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(inspected.st_mode):
        raise RootedFileError("not_directory")
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or _identity(opened) != _identity(inspected):
        os.close(descriptor)
        raise RootedFileError("directory_identity_changed")
    return descriptor, _identity(opened)


def _descend_directory(
    parent: _RootedDirectoryHandle,
    name: str,
) -> _RootedDirectoryHandle:
    descriptor, identity = _open_child_directory(parent.descriptor, name)
    return _RootedDirectoryHandle(
        descriptor,
        (*parent.relative_parts, name),
        (*parent.identities, identity),
    )


def _validate_directory_authority(
    canonical_root: Path,
    root_fd: int,
    directory: _RootedDirectoryHandle,
    expected_root_identity: tuple[int, int] | None,
) -> None:
    if _identity(os.fstat(root_fd)) != directory.identities[0]:
        raise RootedFileError("root_identity_changed")
    _assert_expected_root_identity(os.fstat(root_fd), expected_root_identity)
    reopened_root = -1
    downward: _RootedDirectoryHandle | None = None
    upward = -1
    try:
        reopened_root = _open_root(canonical_root, expected_root_identity)
        if _identity(os.fstat(reopened_root)) != directory.identities[0]:
            raise RootedFileError("root_identity_changed")
        downward = _open_directory_parts(root_fd, directory.relative_parts)
        if downward.identities != directory.identities:
            raise RootedFileError("directory_identity_changed")
        upward = os.dup(directory.descriptor)
        for index in range(len(directory.identities) - 1, -1, -1):
            current = os.fstat(upward)
            if not stat.S_ISDIR(current.st_mode):
                raise RootedFileError("not_directory")
            if _identity(current) != directory.identities[index]:
                raise RootedFileError("directory_ancestry_changed")
            if index:
                parent = os.open(
                    "..",
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=upward,
                )
                os.close(upward)
                upward = parent
    except RootedFileError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise RootedFileError("directory_authority_failed") from exc
    finally:
        _close_fds(upward, _handle_fd(downward), reopened_root)


def _stat_regular(parent_fd: int, name: str) -> os.stat_result:
    inspected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(inspected.st_mode):
        raise RootedFileError("not_regular")
    return inspected


def _read_regular_at(
    parent_fd: int,
    name: str,
    relative_parts: tuple[str, ...],
) -> RootedRegularSnapshot:
    descriptor = -1
    try:
        descriptor, opened, content = _open_regular_exact(
            parent_fd,
            name,
            os.O_RDONLY,
        )
        return RootedRegularSnapshot(
            relative_parts,
            content,
            stat.S_IMODE(opened.st_mode),
            _identity(opened),
        )
    except FileNotFoundError:
        return RootedRegularSnapshot(relative_parts, None, None, None)
    finally:
        _close_fds(descriptor)


def _open_regular_exact(
    parent_fd: int,
    name: str,
    flags: int,
) -> tuple[int, os.stat_result, bytes]:
    inspected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(inspected.st_mode) or inspected.st_nlink != 1:
        raise RootedFileError("not_single_link_regular")
    descriptor = os.open(
        name,
        flags | os.O_NOFOLLOW | os.O_NONBLOCK,
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _identity(opened) != _identity(inspected)
        ):
            raise RootedFileError("file_identity_changed")
        content = _read_descriptor_exact(descriptor, opened.st_size)
        completed = os.fstat(descriptor)
        if _snapshot(completed) != _snapshot(opened):
            raise RootedFileError("snapshot_changed")
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(current) != _identity(opened):
            raise RootedFileError("path_identity_changed")
        return descriptor, opened, content
    except BaseException:
        os.close(descriptor)
        raise

def _read_descriptor_exact(descriptor: int, size: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    remaining = size
    chunks: list[bytes] = []
    while remaining:
        chunk = os.read(descriptor, min(remaining, _READ_CHUNK_BYTES))
        if not chunk:
            raise RootedFileError("short_read")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(descriptor: int, payload: bytes) -> int:
    written = 0
    while written < len(payload):
        try:
            count = os.write(descriptor, payload[written:])
        except OSError as exc:
            raise RootedFileError(
                "write_failed",
                workspace_changed=written > 0,
            ) from exc
        if count <= 0:
            raise RootedFileError(
                "write_failed",
                workspace_changed=written > 0,
            ) from OSError(errno.EIO, "short write")
        written += count
    return written


def _require_rooted_fd_support() -> None:
    if not _HAS_ROOTED_FD_SUPPORT:
        raise RootedFileError("unsupported_platform")


def _handle_fd(handle: _RootedDirectoryHandle | None) -> int:
    return handle.descriptor if handle is not None else -1


def _close_fds(*descriptors: int) -> None:
    for descriptor in descriptors:
        if descriptor < 0:
            continue
        try:
            os.close(descriptor)
        except OSError:
            pass

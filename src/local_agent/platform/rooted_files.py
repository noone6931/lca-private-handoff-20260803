"""Root-contained regular-file operations built on directory descriptors."""

from __future__ import annotations

import codecs
import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path


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


class RootedFileError(RuntimeError):
    """A stable fail-closed result for a rooted file operation."""

    def __init__(self, kind: str, *, workspace_changed: bool = False) -> None:
        super().__init__(kind)
        self.kind = kind
        self.workspace_changed = workspace_changed


@dataclass(frozen=True)
class RootedTextSnapshot:
    lexical_path: Path
    canonical_path: Path
    identity: tuple[int, int]
    text: str


@dataclass(frozen=True)
class RootedDirectoryListing:
    lexical_path: Path
    canonical_path: Path
    identity: tuple[int, int]
    names: tuple[str, ...]


@dataclass(frozen=True)
class RootedAppendResult:
    lexical_path: Path
    canonical_path: Path
    identity: tuple[int, int]
    bytes_written: int


@dataclass(frozen=True)
class _RootedDirectoryHandle:
    descriptor: int
    relative_parts: tuple[str, ...]
    identities: tuple[tuple[int, int], ...]


def read_rooted_utf8(root: Path, lexical_path: Path) -> RootedTextSnapshot:
    """Read one existing regular UTF-8 file through a verified rooted fd chain."""

    _require_rooted_fd_support()
    canonical_root = _canonical_root(root)
    lexical = _lexical_under_root(canonical_root, lexical_path)
    canonical = _resolve_existing(canonical_root, lexical)
    root_fd = _open_root(canonical_root)
    parent: _RootedDirectoryHandle | None = None
    file_fd = -1
    try:
        parent, name = _open_canonical_parent(root_fd, canonical_root, canonical)
        _validate_directory_authority(canonical_root, root_fd, parent)
        inspected = _stat_regular(parent.descriptor, name)
        file_fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent.descriptor,
        )
        opened = os.fstat(file_fd)
        _assert_same_regular(inspected, opened)
        _validate_directory_authority(canonical_root, root_fd, parent)
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
        _validate_directory_authority(canonical_root, root_fd, parent)
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


def list_rooted_directory(root: Path, lexical_path: Path) -> RootedDirectoryListing:
    """List one existing directory after canonical authorization and fd traversal."""

    _require_rooted_fd_support()
    canonical_root = _canonical_root(root)
    lexical = _lexical_under_root(canonical_root, lexical_path)
    canonical = _resolve_existing(canonical_root, lexical)
    root_fd = -1
    directory: _RootedDirectoryHandle | None = None
    try:
        root_fd = _open_root(canonical_root)
        directory = _open_canonical_directory(root_fd, canonical_root, canonical)
        _validate_directory_authority(canonical_root, root_fd, directory)
        before = os.fstat(directory.descriptor)
        if not stat.S_ISDIR(before.st_mode):
            raise RootedFileError("not_directory")
        names = tuple(os.listdir(directory.descriptor))
        after = os.fstat(directory.descriptor)
        if _directory_snapshot(after) != _directory_snapshot(before):
            raise RootedFileError("directory_changed")
        _validate_directory_authority(canonical_root, root_fd, directory)
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


def append_rooted_utf8(root: Path, lexical_path: Path, text: str) -> RootedAppendResult:
    """Append UTF-8 through a verified O_APPEND fd, creating missing parents safely."""

    _require_rooted_fd_support()
    try:
        payload = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RootedFileError("invalid_utf8") from exc
    canonical_root = _canonical_root(root)
    lexical = _lexical_under_root(canonical_root, lexical_path)
    try:
        lexical.lstat()
    except FileNotFoundError:
        return _append_missing(canonical_root, lexical, payload)
    except OSError as exc:
        raise RootedFileError("path_inspection_failed") from exc
    canonical = _resolve_existing(canonical_root, lexical)
    return _append_existing(canonical_root, lexical, canonical, payload)


def _append_existing(
    canonical_root: Path,
    lexical: Path,
    canonical: Path,
    payload: bytes,
) -> RootedAppendResult:
    root_fd = file_fd = -1
    parent: _RootedDirectoryHandle | None = None
    written = 0
    try:
        root_fd = _open_root(canonical_root)
        parent, name = _open_canonical_parent(root_fd, canonical_root, canonical)
        _validate_directory_authority(canonical_root, root_fd, parent)
        inspected = _stat_regular(parent.descriptor, name)
        file_fd = os.open(
            name,
            os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent.descriptor,
        )
        opened = os.fstat(file_fd)
        _assert_same_regular(inspected, opened)
        _validate_directory_authority(canonical_root, root_fd, parent)
        written = _write_all(file_fd, payload)
        entry = os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
        if _identity(entry) != _identity(opened):
            raise RootedFileError("path_identity_changed", workspace_changed=written > 0)
        _validate_directory_authority(canonical_root, root_fd, parent)
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


def _append_missing(canonical_root: Path, lexical: Path, payload: bytes) -> RootedAppendResult:
    root_fd = file_fd = -1
    current: _RootedDirectoryHandle | None = None
    written = 0
    workspace_changed = False
    try:
        ancestor, missing_parents = _creation_parent(canonical_root, lexical.parent)
        root_fd = _open_root(canonical_root)
        current = _open_canonical_directory(root_fd, canonical_root, ancestor)
        for component in missing_parents:
            _validate_directory_authority(canonical_root, root_fd, current)
            try:
                os.mkdir(component, mode=0o700, dir_fd=current.descriptor)
                workspace_changed = True
            except FileExistsError:
                pass
            next_handle = _descend_directory(current, component)
            os.close(current.descriptor)
            current = next_handle
            _validate_directory_authority(canonical_root, root_fd, current)
        name = lexical.name
        _validate_directory_authority(canonical_root, root_fd, current)
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
            return _append_existing(canonical_root, lexical, canonical, payload)
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise RootedFileError("not_regular")
        _validate_directory_authority(canonical_root, root_fd, current)
        written = _write_all(file_fd, payload)
        entry = os.stat(name, dir_fd=current.descriptor, follow_symlinks=False)
        if _identity(entry) != _identity(opened):
            raise RootedFileError("path_identity_changed", workspace_changed=written > 0)
        _validate_directory_authority(canonical_root, root_fd, current)
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


def _creation_parent(canonical_root: Path, lexical_parent: Path) -> tuple[Path, tuple[str, ...]]:
    missing: list[str] = []
    candidate = lexical_parent
    while True:
        try:
            candidate.lstat()
        except FileNotFoundError:
            if candidate == canonical_root:
                raise RootedFileError("root_missing")
            missing.insert(0, candidate.name)
            candidate = candidate.parent
            continue
        except OSError as exc:
            raise RootedFileError("path_inspection_failed") from exc
        try:
            ancestor = candidate.resolve(strict=True)
            ancestor.relative_to(canonical_root)
        except FileNotFoundError as exc:
            raise RootedFileError("dangling_symlink") from exc
        except RuntimeError as exc:
            raise RootedFileError("symlink_loop") from exc
        except (OSError, ValueError) as exc:
            raise RootedFileError("outside_root") from exc
        if not ancestor.is_dir():
            raise RootedFileError("not_directory")
        return ancestor, tuple(missing)


def _canonical_root(root: Path) -> Path:
    try:
        canonical = root.expanduser().resolve(strict=True)
        if canonical != root:
            raise RootedFileError("root_not_canonical")
        if not canonical.is_dir():
            raise RootedFileError("root_not_directory")
        return canonical
    except RootedFileError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise RootedFileError("invalid_root") from exc


def _lexical_under_root(canonical_root: Path, lexical_path: Path) -> Path:
    try:
        lexical = Path(os.path.abspath(lexical_path))
        lexical.relative_to(canonical_root)
        if lexical.name in {"", ".", ".."}:
            raise ValueError
        return lexical
    except (OSError, RuntimeError, ValueError) as exc:
        raise RootedFileError("outside_root") from exc


def _resolve_existing(canonical_root: Path, lexical: Path) -> Path:
    try:
        canonical = lexical.resolve(strict=True)
        canonical.relative_to(canonical_root)
        return canonical
    except FileNotFoundError as exc:
        try:
            lexical.lstat()
        except FileNotFoundError:
            _creation_parent(canonical_root, lexical.parent)
            raise RootedFileError("not_found") from exc
        except OSError as inspection_error:
            raise RootedFileError("path_inspection_failed") from inspection_error
        raise RootedFileError("dangling_symlink") from exc
    except RuntimeError as exc:
        raise RootedFileError("symlink_loop") from exc
    except (OSError, ValueError) as exc:
        raise RootedFileError("outside_root") from exc


def _open_root(canonical_root: Path) -> int:
    inspected = canonical_root.lstat()
    if not stat.S_ISDIR(inspected.st_mode):
        raise RootedFileError("root_not_directory")
    descriptor = os.open(
        canonical_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or _identity(opened) != _identity(inspected):
        os.close(descriptor)
        raise RootedFileError("root_identity_changed")
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
) -> None:
    if _identity(os.fstat(root_fd)) != directory.identities[0]:
        raise RootedFileError("root_identity_changed")
    reopened_root = -1
    downward: _RootedDirectoryHandle | None = None
    upward = -1
    try:
        reopened_root = _open_root(canonical_root)
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


def _assert_same_regular(inspected: os.stat_result, opened: os.stat_result) -> None:
    if not stat.S_ISREG(opened.st_mode):
        raise RootedFileError("not_regular")
    if _identity(opened) != _identity(inspected):
        raise RootedFileError("file_identity_changed")


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


def _snapshot(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _directory_snapshot(file_stat: os.stat_result) -> tuple[int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _identity(file_stat: os.stat_result) -> tuple[int, int]:
    return (file_stat.st_dev, file_stat.st_ino)


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

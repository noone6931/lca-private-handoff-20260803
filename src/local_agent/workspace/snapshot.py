from __future__ import annotations

import base64
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


SNAPSHOT_MANIFEST_VERSION = "lca-workspace-snapshot-v1"
DEFAULT_SNAPSHOT_MAX_ENTRIES = 20_000
DEFAULT_SNAPSHOT_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_SNAPSHOT_MAX_FILE_BYTES = 64 * 1024 * 1024
DEFAULT_SNAPSHOT_MAX_DEPTH = 64
_READ_CHUNK_BYTES = 64 * 1024
_PATH_SEPARATORS = frozenset({"\0", "\n", "\r", "\t"})

SnapshotEntryKind = Literal["directory", "file"]


class WorkspaceSnapshotError(RuntimeError):
    """A typed fail-closed result for a recursive workspace snapshot."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class WorkspaceSnapshotBudget:
    max_entries: int = DEFAULT_SNAPSHOT_MAX_ENTRIES
    max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES
    max_file_bytes: int = DEFAULT_SNAPSHOT_MAX_FILE_BYTES
    max_depth: int = DEFAULT_SNAPSHOT_MAX_DEPTH

    def __post_init__(self) -> None:
        if min(
            self.max_entries,
            self.max_bytes,
            self.max_file_bytes,
            self.max_depth,
        ) < 1:
            raise ValueError("workspace snapshot budgets must be positive")
        if self.max_file_bytes > self.max_bytes:
            raise ValueError("workspace snapshot file budget exceeds total budget")


@dataclass(frozen=True)
class WorkspaceSnapshotEntry:
    relative_parts: tuple[str, ...]
    kind: SnapshotEntryKind
    mode: int
    size: int
    sha256: str | None
    content: bytes | None

    def __post_init__(self) -> None:
        _validate_relative_parts(self.relative_parts)
        if self.kind not in {"directory", "file"}:
            raise ValueError("workspace snapshot entry kind is invalid")
        if self.mode < 0 or self.mode > 0o777:
            raise ValueError("workspace snapshot entry mode is invalid")
        if self.kind == "directory":
            if self.size != 0 or self.sha256 is not None or self.content is not None:
                raise ValueError("workspace snapshot directory payload is invalid")
            return
        if self.content is None or self.size != len(self.content):
            raise ValueError("workspace snapshot file payload is invalid")
        expected = hashlib.sha256(self.content).hexdigest()
        if self.sha256 != expected:
            raise ValueError("workspace snapshot file digest is invalid")

    @property
    def relative_path(self) -> str:
        return "/".join(self.relative_parts)

    def manifest_line(self) -> bytes:
        encoded_path = base64.b64encode(
            self.relative_path.encode("utf-8")
        ).decode("ascii")
        if self.kind == "directory":
            rendered = f"d\t{self.mode:o}\t{encoded_path}\n"
        else:
            rendered = (
                f"f\t{self.mode:o}\t{self.size}\t{self.sha256}\t"
                f"{encoded_path}\n"
            )
        return rendered.encode("ascii")


@dataclass(frozen=True)
class WorkspaceSnapshot:
    root: Path
    root_identity: tuple[int, int]
    roots_revision: int
    entries: tuple[WorkspaceSnapshotEntry, ...]
    total_bytes: int
    manifest_sha256: str

    def __post_init__(self) -> None:
        if not self.root.is_absolute() or min(self.root_identity) < 0:
            raise ValueError("workspace snapshot root identity is invalid")
        if self.root_identity[1] <= 0 or self.roots_revision < 0:
            raise ValueError("workspace snapshot authority is invalid")
        if self.total_bytes != sum(
            entry.size for entry in self.entries if entry.kind == "file"
        ):
            raise ValueError("workspace snapshot byte count is invalid")
        if tuple(sorted(self.entries, key=_entry_sort_key)) != self.entries:
            raise ValueError("workspace snapshot entries are not canonical")
        if _manifest_digest(self.entries) != self.manifest_sha256:
            raise ValueError("workspace snapshot manifest digest is invalid")


def capture_workspace_snapshot(
    root: Path,
    *,
    roots_revision: int,
    expected_root_identity: tuple[int, int] | None = None,
    forbidden_directory_identities: frozenset[tuple[int, int]] = frozenset(),
    budget: WorkspaceSnapshotBudget | None = None,
) -> WorkspaceSnapshot:
    """Capture one canonical tree without following links or special files."""

    selected_budget = budget or WorkspaceSnapshotBudget()
    canonical = _canonical_root(root)
    root_fd = -1
    try:
        root_fd = os.open(
            canonical,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | os.O_DIRECTORY
            | os.O_NOFOLLOW,
        )
        opened = os.fstat(root_fd)
        root_identity = _identity(opened)
        if expected_root_identity is not None and root_identity != expected_root_identity:
            raise WorkspaceSnapshotError("root_identity_changed")
        if root_identity in forbidden_directory_identities:
            raise WorkspaceSnapshotError("forbidden_directory_identity")
        entries: list[WorkspaceSnapshotEntry] = []
        state = _SnapshotState(selected_budget)
        _walk_directory(
            root_fd,
            (),
            entries,
            state,
            forbidden_directory_identities,
        )
        completed = os.fstat(root_fd)
        if _directory_state(completed) != _directory_state(opened):
            raise WorkspaceSnapshotError("root_changed")
        current = os.stat(canonical, follow_symlinks=False)
        if _identity(current) != root_identity:
            raise WorkspaceSnapshotError("root_path_identity_changed")
        normalized = tuple(sorted(entries, key=_entry_sort_key))
        return WorkspaceSnapshot(
            root=canonical,
            root_identity=root_identity,
            roots_revision=roots_revision,
            entries=normalized,
            total_bytes=state.total_bytes,
            manifest_sha256=_manifest_digest(normalized),
        )
    except WorkspaceSnapshotError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspaceSnapshotError("snapshot_failed") from exc
    finally:
        if root_fd >= 0:
            os.close(root_fd)


@dataclass
class _SnapshotState:
    budget: WorkspaceSnapshotBudget
    entries: int = 0
    total_bytes: int = 0

    def add_entry(self) -> None:
        self.entries += 1
        if self.entries > self.budget.max_entries:
            raise WorkspaceSnapshotError("entry_budget_exceeded")

    def add_file(self, size: int) -> None:
        if size > self.budget.max_file_bytes:
            raise WorkspaceSnapshotError("file_budget_exceeded")
        self.total_bytes += size
        if self.total_bytes > self.budget.max_bytes:
            raise WorkspaceSnapshotError("byte_budget_exceeded")


def _walk_directory(
    directory_fd: int,
    parent_parts: tuple[str, ...],
    entries: list[WorkspaceSnapshotEntry],
    state: _SnapshotState,
    forbidden_directory_identities: frozenset[tuple[int, int]],
) -> None:
    if len(parent_parts) > state.budget.max_depth:
        raise WorkspaceSnapshotError("depth_budget_exceeded")
    before = os.fstat(directory_fd)
    names = _directory_names(directory_fd)
    for name in names:
        parts = (*parent_parts, name)
        state.add_entry()
        inspected = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        mode = stat.S_IMODE(inspected.st_mode)
        if mode & ~0o777:
            raise WorkspaceSnapshotError("unsupported_mode")
        if stat.S_ISDIR(inspected.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | os.O_DIRECTORY
                | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(child_fd)
                if _identity(opened) != _identity(inspected):
                    raise WorkspaceSnapshotError("directory_identity_changed")
                if _identity(opened) in forbidden_directory_identities:
                    raise WorkspaceSnapshotError("forbidden_directory_identity")
                entries.append(
                    WorkspaceSnapshotEntry(parts, "directory", mode, 0, None, None)
                )
                _walk_directory(
                    child_fd,
                    parts,
                    entries,
                    state,
                    forbidden_directory_identities,
                )
                completed = os.fstat(child_fd)
                if _directory_state(completed) != _directory_state(opened):
                    raise WorkspaceSnapshotError("directory_changed")
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(inspected.st_mode):
            raise WorkspaceSnapshotError("unsupported_entry_type")
        if inspected.st_nlink != 1:
            raise WorkspaceSnapshotError("hardlink_not_supported")
        state.add_file(inspected.st_size)
        content = _read_regular_file(directory_fd, name, inspected)
        entries.append(
            WorkspaceSnapshotEntry(
                parts,
                "file",
                mode,
                len(content),
                hashlib.sha256(content).hexdigest(),
                content,
            )
        )
    if _directory_names(directory_fd) != names:
        raise WorkspaceSnapshotError("directory_entries_changed")
    after = os.fstat(directory_fd)
    if _directory_state(after) != _directory_state(before):
        raise WorkspaceSnapshotError("directory_changed")


def _read_regular_file(
    parent_fd: int,
    name: str,
    inspected: os.stat_result,
) -> bytes:
    file_fd = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(file_fd)
        if _file_state(opened) != _file_state(inspected):
            raise WorkspaceSnapshotError("file_identity_changed")
        remaining = opened.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(file_fd, min(remaining, _READ_CHUNK_BYTES))
            if not chunk:
                raise WorkspaceSnapshotError("short_read")
            chunks.append(chunk)
            remaining -= len(chunk)
        completed = os.fstat(file_fd)
        if _file_state(completed) != _file_state(opened):
            raise WorkspaceSnapshotError("file_changed")
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(current) != _identity(opened):
            raise WorkspaceSnapshotError("file_path_identity_changed")
        return b"".join(chunks)
    finally:
        os.close(file_fd)


def _directory_names(directory_fd: int) -> tuple[str, ...]:
    names = tuple(os.listdir(directory_fd))
    for name in names:
        _validate_name(name)
    return tuple(sorted(names, key=lambda value: value.encode("utf-8")))


def _canonical_root(root: Path) -> Path:
    try:
        canonical = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceSnapshotError("root_unavailable") from exc
    if canonical != root or not canonical.is_dir():
        raise WorkspaceSnapshotError("root_must_be_canonical_directory")
    return canonical


def _validate_name(name: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or any(character in name for character in _PATH_SEPARATORS)
    ):
        raise WorkspaceSnapshotError("path_not_supported")
    try:
        name.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise WorkspaceSnapshotError("path_not_utf8") from exc


def _validate_relative_parts(parts: tuple[str, ...]) -> None:
    if not parts:
        raise ValueError("workspace snapshot entry path must not be empty")
    for name in parts:
        try:
            _validate_name(name)
        except WorkspaceSnapshotError as exc:
            raise ValueError("workspace snapshot entry path is invalid") from exc


def _entry_sort_key(entry: WorkspaceSnapshotEntry) -> bytes:
    return entry.relative_path.encode("utf-8")


def _manifest_digest(entries: tuple[WorkspaceSnapshotEntry, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(f"{SNAPSHOT_MANIFEST_VERSION}\n".encode("ascii"))
    for entry in entries:
        digest.update(entry.manifest_line())
    return digest.hexdigest()


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _directory_state(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _file_state(metadata: os.stat_result) -> tuple[int, ...]:
    return (*_directory_state(metadata), metadata.st_size)


__all__ = [
    "DEFAULT_SNAPSHOT_MAX_BYTES",
    "DEFAULT_SNAPSHOT_MAX_DEPTH",
    "DEFAULT_SNAPSHOT_MAX_ENTRIES",
    "DEFAULT_SNAPSHOT_MAX_FILE_BYTES",
    "SNAPSHOT_MANIFEST_VERSION",
    "WorkspaceSnapshot",
    "WorkspaceSnapshotBudget",
    "WorkspaceSnapshotEntry",
    "WorkspaceSnapshotError",
    "capture_workspace_snapshot",
]

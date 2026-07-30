from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from .snapshot import WorkspaceSnapshot
from .snapshot import WorkspaceSnapshotEntry


TextMutationOperation = Literal["replace", "create", "delete"]


class WorkspaceSnapshotDeltaError(RuntimeError):
    """A typed fail-closed result for unsupported staged workspace output."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class WorkspaceTextChange:
    relative_parts: tuple[str, ...]
    before_bytes: bytes | None
    after_bytes: bytes | None
    before_mode: int | None
    after_mode: int | None
    before_sha256: str | None
    after_sha256: str | None

    def __post_init__(self) -> None:
        if not self.relative_parts or self.before_bytes == self.after_bytes:
            raise ValueError("workspace text change is empty")
        if self.before_bytes is None and self.after_bytes is None:
            raise ValueError("workspace text change has no state")
        _validate_side(self.before_bytes, self.before_mode, self.before_sha256)
        _validate_side(self.after_bytes, self.after_mode, self.after_sha256)
        if (
            self.before_bytes is not None
            and self.after_bytes is not None
            and self.before_mode != self.after_mode
        ):
            raise ValueError("workspace text replacement cannot change mode")
        for content in (self.before_bytes, self.after_bytes):
            if content is not None:
                content.decode("utf-8", errors="strict")

    @property
    def relative_path(self) -> str:
        return "/".join(self.relative_parts)

    @property
    def operation(self) -> TextMutationOperation:
        if self.before_bytes is None:
            return "create"
        if self.after_bytes is None:
            return "delete"
        return "replace"


@dataclass(frozen=True)
class WorkspaceTextMutationPlan:
    before: WorkspaceSnapshot
    after: WorkspaceSnapshot
    changes: tuple[WorkspaceTextChange, ...]

    def __post_init__(self) -> None:
        if self.before.roots_revision != self.after.roots_revision:
            raise ValueError("workspace mutation snapshots have different revisions")
        if tuple(sorted(self.changes, key=lambda item: item.relative_path.encode("utf-8"))) != self.changes:
            raise ValueError("workspace text changes are not canonical")
        if len({change.relative_parts for change in self.changes}) != len(self.changes):
            raise ValueError("workspace text changes contain duplicates")
        expected = _build_changes(self.before, self.after)
        if expected != self.changes:
            raise ValueError("workspace text changes do not match their snapshots")

    @property
    def roots_revision(self) -> int:
        return self.before.roots_revision

    @property
    def changed(self) -> bool:
        return bool(self.changes)


def build_workspace_text_mutation_plan(
    before: WorkspaceSnapshot,
    after: WorkspaceSnapshot,
) -> WorkspaceTextMutationPlan:
    try:
        changes = _build_changes(before, after)
    except UnicodeDecodeError as exc:
        raise WorkspaceSnapshotDeltaError("changed_file_not_utf8") from exc
    return WorkspaceTextMutationPlan(before, after, changes)


def snapshots_match(
    expected: WorkspaceSnapshot,
    observed: WorkspaceSnapshot,
) -> bool:
    return (
        expected.roots_revision == observed.roots_revision
        and expected.entries == observed.entries
        and expected.total_bytes == observed.total_bytes
        and expected.manifest_sha256 == observed.manifest_sha256
    )


def _build_changes(
    before: WorkspaceSnapshot,
    after: WorkspaceSnapshot,
) -> tuple[WorkspaceTextChange, ...]:
    if before.roots_revision != after.roots_revision:
        raise WorkspaceSnapshotDeltaError("workspace_revision_changed")
    before_directories, before_files = _partition(before)
    after_directories, after_files = _partition(after)
    if before_directories != after_directories:
        raise WorkspaceSnapshotDeltaError("directory_change_not_supported")

    changes: list[WorkspaceTextChange] = []
    for parts in sorted(
        set(before_files) | set(after_files),
        key=lambda value: "/".join(value).encode("utf-8"),
    ):
        old = before_files.get(parts)
        new = after_files.get(parts)
        if old is not None and new is not None:
            if old.mode != new.mode:
                raise WorkspaceSnapshotDeltaError("file_mode_change_not_supported")
            if old.content == new.content:
                continue
        elif old is None:
            parent = parts[:-1]
            if parent and parent not in before_directories:
                raise WorkspaceSnapshotDeltaError(
                    "create_parent_directory_not_in_before_snapshot"
                )
        changes.append(_change(parts, old, new))
    return tuple(changes)


def _partition(
    snapshot: WorkspaceSnapshot,
) -> tuple[
    dict[tuple[str, ...], int],
    dict[tuple[str, ...], WorkspaceSnapshotEntry],
]:
    directories: dict[tuple[str, ...], int] = {}
    files: dict[tuple[str, ...], WorkspaceSnapshotEntry] = {}
    for entry in snapshot.entries:
        target = directories if entry.kind == "directory" else files
        target[entry.relative_parts] = entry.mode if entry.kind == "directory" else entry
    return directories, files


def _change(
    parts: tuple[str, ...],
    before: WorkspaceSnapshotEntry | None,
    after: WorkspaceSnapshotEntry | None,
) -> WorkspaceTextChange:
    before_bytes = before.content if before is not None else None
    after_bytes = after.content if after is not None else None
    if before_bytes is not None:
        before_bytes.decode("utf-8", errors="strict")
    if after_bytes is not None:
        after_bytes.decode("utf-8", errors="strict")
    return WorkspaceTextChange(
        relative_parts=parts,
        before_bytes=before_bytes,
        after_bytes=after_bytes,
        before_mode=before.mode if before is not None else None,
        after_mode=after.mode if after is not None else None,
        before_sha256=before.sha256 if before is not None else None,
        after_sha256=after.sha256 if after is not None else None,
    )


def _validate_side(
    content: bytes | None,
    mode: int | None,
    digest: str | None,
) -> None:
    if content is None:
        if mode is not None or digest is not None:
            raise ValueError("absent workspace text state has metadata")
        return
    if mode is None or not 0 <= mode <= 0o777:
        raise ValueError("workspace text state mode is invalid")
    if digest != hashlib.sha256(content).hexdigest():
        raise ValueError("workspace text state digest is invalid")


__all__ = [
    "WorkspaceSnapshotDeltaError",
    "WorkspaceTextChange",
    "WorkspaceTextMutationPlan",
    "build_workspace_text_mutation_plan",
    "snapshots_match",
]

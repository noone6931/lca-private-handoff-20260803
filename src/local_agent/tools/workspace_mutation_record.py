from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from ..patch.transaction_contracts import RootedTextFileChange


@dataclass(frozen=True)
class ParsedContainerWorkspaceRollback:
    changes: tuple[RootedTextFileChange, ...]
    paths: tuple[str, ...]
    root_identity: tuple[int, int]


def parse_container_workspace_rollback(
    record: dict[str, object],
    *,
    workspace_revision: int,
    workspace_identity: tuple[int, int] | None,
) -> ParsedContainerWorkspaceRollback | str:
    if record.get("source") != "container_staged_copy":
        return "Patch transaction record has an unsupported source."
    if (
        record.get("workspace_roots_revision") != workspace_revision
        or workspace_identity is None
    ):
        return "Container workspace rollback authority is stale."
    raw_identity = record.get("workspace_root_identity")
    if (
        not isinstance(raw_identity, list)
        or len(raw_identity) != 2
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in raw_identity
        )
        or tuple(raw_identity) != workspace_identity
    ):
        return "Container workspace rollback root identity is stale."
    raw_files = record.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        return "Patch transaction record is malformed."
    changes: list[RootedTextFileChange] = []
    paths: list[str] = []
    for item in raw_files:
        parsed = _rollback_change(item)
        if parsed is None:
            return "Patch transaction record is malformed."
        path, change = parsed
        paths.append(path)
        changes.append(change)
    return ParsedContainerWorkspaceRollback(
        tuple(changes),
        tuple(paths),
        workspace_identity,
    )


def _rollback_change(
    item: object,
) -> tuple[str, RootedTextFileChange] | None:
    if not isinstance(item, dict):
        return None
    raw_path = item.get("path")
    if not isinstance(raw_path, str):
        return None
    pure = PurePosixPath(raw_path)
    if pure.is_absolute() or pure.as_posix() != raw_path or not pure.parts:
        return None
    before_text = item.get("before_text")
    after_text = item.get("after_text")
    if before_text is not None and not isinstance(before_text, str):
        return None
    if after_text is not None and not isinstance(after_text, str):
        return None
    before_mode = item.get("before_mode")
    after_mode = item.get("after_mode")
    if before_mode is not None and (
        isinstance(before_mode, bool) or not isinstance(before_mode, int)
    ):
        return None
    if after_mode is not None and (
        isinstance(after_mode, bool) or not isinstance(after_mode, int)
    ):
        return None
    before_bytes = (
        before_text.encode("utf-8") if before_text is not None else None
    )
    after_bytes = (
        after_text.encode("utf-8") if after_text is not None else None
    )
    if (
        item.get("before_exists") is not (before_bytes is not None)
        or item.get("after_exists") is not (after_bytes is not None)
    ):
        return None
    change = RootedTextFileChange(
        relative_parts=tuple(pure.parts),
        before_bytes=after_bytes,
        after_bytes=before_bytes,
        before_mode=after_mode,
        after_mode=before_mode,
        before_sha256=item.get("after_sha256"),
        after_sha256=item.get("before_sha256"),
    )
    expected_operation = (
        "create"
        if before_bytes is None
        else "delete"
        if after_bytes is None
        else "replace"
    )
    if item.get("operation") != expected_operation:
        return None
    return raw_path, change


__all__ = [
    "ParsedContainerWorkspaceRollback",
    "parse_container_workspace_rollback",
]

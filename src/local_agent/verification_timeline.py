from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


WRITE_TOOL_NAMES = frozenset({"apply_patch", "rollback_patch", "write_file"})
NO_WRITE_RESULT_MARKERS = frozenset(
    {
        "dry run",
        "dry_run",
        "file not changed",
        "no file changed",
        "not changed",
        "patch preview only",
        "preview only",
        "would be",
    }
)


class TimelineToolResult(Protocol):
    name: str
    content: str
    is_error: bool
    changed: bool | None


def result_changed_workspace(result: TimelineToolResult) -> bool:
    if result.is_error:
        return False
    if result.changed is not None:
        return result.changed
    content = (result.content or "").lower()
    return not any(marker in content for marker in NO_WRITE_RESULT_MARKERS)


def last_workspace_write_index(results: Sequence[TimelineToolResult]) -> int:
    for index in range(len(results) - 1, -1, -1):
        result = results[index]
        if result.name in WRITE_TOOL_NAMES and result_changed_workspace(result):
            return index
    return -1


def workspace_write_happened(results: Sequence[TimelineToolResult]) -> bool:
    return last_workspace_write_index(results) >= 0


def successful_tool_after_last_write(
    results: Sequence[TimelineToolResult],
    name: str,
) -> bool:
    write_index = last_workspace_write_index(results)
    if write_index < 0:
        return False
    return any(
        result.name == name and not result.is_error
        for result in results[write_index + 1 :]
    )


def results_after_last_write(
    results: Sequence[TimelineToolResult],
) -> Sequence[TimelineToolResult]:
    write_index = last_workspace_write_index(results)
    return results[write_index + 1 :] if write_index >= 0 else ()

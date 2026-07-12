from __future__ import annotations

from collections.abc import Sequence
import re
from typing import Any, Mapping, Protocol


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
    useless: bool
    changed: bool | None
    path: str | None
    metadata: Mapping[str, Any]


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


def successful_nonempty_git_diff_after_last_write(
    results: Sequence[TimelineToolResult],
) -> TimelineToolResult | None:
    """Return the latest post-write diff only when it proves this run's net change."""

    effective_paths = effective_workspace_write_paths(results)
    if not effective_paths:
        return None
    for result in reversed(results_after_last_write(results)):
        if result.name != "git_diff" or result.is_error:
            continue
        if not _is_empty_diff(result.content) and _diff_mentions_any_path(result, effective_paths):
            return result
    return None


def effective_workspace_write_paths(results: Sequence[TimelineToolResult]) -> tuple[str, ...]:
    """Track known paths whose latest observed state still contains a runtime write.

    Rollbacks remove the path from this conservative set. This can occasionally
    require a fresh edit/diff after complex overlapping patches, but never lets a
    pre-existing dirty diff stand in for the agent's current delivery.
    """

    paths: dict[str, str] = {}
    for result in results:
        if result.name not in WRITE_TOOL_NAMES or not result_changed_workspace(result):
            continue
        path = _write_path(result)
        if not path:
            continue
        key = _normalize_path(path)
        if result.name == "rollback_patch":
            paths.pop(key, None)
        else:
            paths[key] = path
    return tuple(paths.values())


def code_evidence_for_effective_write(
    results: Sequence[TimelineToolResult],
    *,
    code_tool_names: frozenset[str],
) -> list[TimelineToolResult]:
    """Return only code evidence whose path is relevant to current runtime writes."""

    changed_paths = effective_workspace_write_paths(results)
    if not changed_paths:
        return []
    evidence: list[TimelineToolResult] = []
    for result in results:
        if result.is_error or result.useless:
            continue
        if result.name not in code_tool_names:
            continue
        paths = [result.path] if result.path else []
        metadata_paths = result.metadata.get("evidence_paths")
        if isinstance(metadata_paths, (list, tuple)):
            paths.extend(str(path) for path in metadata_paths if isinstance(path, str))
        if any(_paths_match(path, changed) for path in paths for changed in changed_paths):
            evidence.append(result)
    return evidence


def _is_empty_diff(content: str) -> bool:
    normalized = " ".join((content or "").strip().lower().split())
    return normalized in {"", "(empty)", "(empty diff)", "no changes", "no diff"}


def _write_path(result: TimelineToolResult) -> str | None:
    if result.path:
        return result.path
    changed_path = result.metadata.get("changed_path")
    return changed_path if isinstance(changed_path, str) and changed_path.strip() else None


def _diff_mentions_any_path(result: TimelineToolResult, paths: tuple[str, ...]) -> bool:
    metadata = result.metadata.get("patch_review")
    diff_paths: list[str] = []
    if isinstance(metadata, Mapping):
        values = metadata.get("changed_paths")
        if isinstance(values, (list, tuple)):
            diff_paths.extend(str(value) for value in values if isinstance(value, str))
    if not diff_paths:
        diff_paths.extend(
            match.group("path")
            for match in re.finditer(r"^diff --git a/.+? b/(?P<path>.+)$", result.content or "", flags=re.MULTILINE)
        )
    return any(_paths_match(diff_path, path) for diff_path in diff_paths for path in paths)


def _paths_match(left: str, right: str) -> bool:
    normalized_left = _normalize_path(left)
    normalized_right = _normalize_path(right)
    return normalized_left == normalized_right or normalized_left.endswith("/" + normalized_right) or normalized_right.endswith("/" + normalized_left)


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").strip().strip("./").lower()

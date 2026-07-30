from __future__ import annotations

from collections.abc import Sequence
import re
from typing import Any, Mapping, Protocol


WRITE_TOOL_NAMES = frozenset({"apply_patch", "apply_workspace_edit", "rollback_patch", "write_file"})
TRANSACTIONAL_EXEC_TOOL_NAMES = frozenset({"shell", "run_tests"})
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
    workspace_changed = result.metadata.get("workspace_changed")
    if isinstance(workspace_changed, bool):
        return workspace_changed
    if result.is_error:
        return False
    changed = getattr(result, "changed", None)
    if changed is not None:
        return bool(changed)
    content = (result.content or "").lower()
    return not any(marker in content for marker in NO_WRITE_RESULT_MARKERS)


def result_is_workspace_write(
    result: TimelineToolResult,
    *,
    name: str | None = None,
) -> bool:
    tool_name = name if name is not None else result.name
    if tool_name in WRITE_TOOL_NAMES:
        return result_changed_workspace(result)
    return _has_committed_exec_transaction(result, tool_name)


def last_workspace_write_index(results: Sequence[TimelineToolResult]) -> int:
    for index in range(len(results) - 1, -1, -1):
        result = results[index]
        if result_is_workspace_write(result):
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
        if not result_is_workspace_write(result):
            continue
        if result.name == "rollback_patch":
            transaction_paths = _metadata_paths(result, "transaction_paths") or result_workspace_write_paths(result)
            for path in transaction_paths:
                paths.pop(_normalize_path(path), None)
            for path in _metadata_paths(result, "effective_changed_paths"):
                paths[_normalize_path(path)] = path
        else:
            effective_paths = _metadata_paths(result, "effective_changed_paths") or result_workspace_write_paths(result)
            for path in effective_paths:
                paths[_normalize_path(path)] = path
    return tuple(paths.values())


def code_evidence_for_effective_write(
    results: Sequence[TimelineToolResult],
    *,
    code_tool_names: frozenset[str],
) -> list[TimelineToolResult]:
    """Return only code evidence whose path is relevant to current runtime writes."""

    changed_paths = effective_workspace_write_paths(results)
    return code_evidence_for_paths(results, changed_paths, code_tool_names=code_tool_names)


def code_evidence_for_paths(
    results: Sequence[TimelineToolResult],
    changed_paths: tuple[str, ...],
    *,
    code_tool_names: frozenset[str],
) -> list[TimelineToolResult]:
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


def successful_nonempty_git_diff_for_paths(
    results: Sequence[TimelineToolResult],
    paths: tuple[str, ...],
) -> TimelineToolResult | None:
    if not paths:
        return None
    for result in reversed(results):
        if result.name == "git_diff" and not result.is_error and not _is_empty_diff(result.content):
            if _diff_mentions_any_path(result, paths):
                return result
    return None


def _is_empty_diff(content: str) -> bool:
    normalized = " ".join((content or "").strip().lower().split())
    return normalized in {"", "(empty)", "(empty diff)", "no changes", "no diff"}


def _write_path(result: TimelineToolResult) -> str | None:
    path = getattr(result, "path", None)
    if path:
        return str(path)
    changed_path = result.metadata.get("changed_path")
    return changed_path if isinstance(changed_path, str) and changed_path.strip() else None


def result_workspace_write_paths(result: TimelineToolResult) -> tuple[str, ...]:
    paths = _metadata_paths(result, "changed_paths")
    if paths:
        return paths
    path = _write_path(result)
    return (path,) if path else ()


def _metadata_paths(result: TimelineToolResult, key: str) -> tuple[str, ...]:
    values = result.metadata.get(key)
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(value for value in values if isinstance(value, str) and value.strip()))


def _has_committed_exec_transaction(
    result: TimelineToolResult,
    tool_name: str,
) -> bool:
    metadata = result.metadata
    transaction_id = metadata.get("workspace_transaction_id")
    isolation = metadata.get("isolation")
    if (
        tool_name not in TRANSACTIONAL_EXEC_TOOL_NAMES
        or not isinstance(transaction_id, str)
        or not transaction_id.strip()
        or metadata.get("workspace_mutation_source") != "container_staged_copy"
        or metadata.get("workspace_changed") is not True
        or metadata.get("transaction_status") != "committed"
        or not isinstance(isolation, Mapping)
        or isolation.get("workspace_transport") != "staged-copy"
    ):
        return False
    commit = isolation.get("workspace_output_commit")
    return (
        isinstance(commit, Mapping)
        and commit.get("state") == "committed"
        and commit.get("transaction_id") == transaction_id
        and bool(result_workspace_write_paths(result))
    )


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

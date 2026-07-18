"""Typed root coverage facts for bounded read-only exploration."""
from __future__ import annotations

from dataclasses import dataclass

from ..tools.observation import ToolResultSummary
from ..tools.observation import tool_result_is_executed_attempt
from ..tools.observation import tool_result_was_not_executed


@dataclass(frozen=True)
class RootCoverage:
    root: str
    aliases: tuple[str, ...]
    search_attempts: int = 0
    successful_searches: int = 0
    no_match: int = 0
    successful_direct_reads: int = 0
    failures: int = 0
    suppressed: int = 0

    @property
    def attempted_without_direct_read(self) -> bool:
        return self.search_attempts > 0 and self.successful_direct_reads == 0

    @property
    def searched_without_direct_read(self) -> bool:
        return self.successful_searches > 0 and self.successful_direct_reads == 0


def read_only_root_coverage(results: list[ToolResultSummary] | tuple[ToolResultSummary, ...]) -> tuple[RootCoverage, ...]:
    """Aggregate read-only explore coverage once for handoff and final audit."""

    buckets: dict[str, dict[str, object]] = {}
    for result in results:
        root = _result_evidence_root(result)
        if not root:
            continue
        bucket = buckets.setdefault(
            root,
            {
                "aliases": set(),
                "search_attempts": 0,
                "successful_searches": 0,
                "no_match": 0,
                "successful_direct_reads": 0,
                "failures": 0,
                "suppressed": 0,
            },
        )
        aliases = bucket["aliases"]
        if isinstance(aliases, set):
            aliases.update(_root_aliases(root, result))
        if tool_result_was_not_executed(result):
            bucket["suppressed"] = int(bucket["suppressed"]) + 1
            continue
        if result.is_error:
            bucket["failures"] = int(bucket["failures"]) + 1
        if result.name in {"search_code", "glob_files"} or result.name.startswith("lsp_"):
            bucket["search_attempts"] = int(bucket["search_attempts"]) + 1
            if result.is_error or not tool_result_is_executed_attempt(result):
                continue
            bucket["successful_searches"] = int(bucket["successful_searches"]) + 1
            if result.useless or result.metadata.get("negative_evidence_type") in {"content_no_match", "path_no_match"}:
                bucket["no_match"] = int(bucket["no_match"]) + 1
        if result.name == "read_file" and _is_successful_direct_read_result(result):
            bucket["successful_direct_reads"] = int(bucket["successful_direct_reads"]) + 1
    coverage: list[RootCoverage] = []
    for root, bucket in sorted(buckets.items()):
        aliases_value = bucket["aliases"]
        aliases = tuple(sorted(aliases_value)) if isinstance(aliases_value, set) else ()
        coverage.append(
            RootCoverage(
                root=root,
                aliases=aliases,
                search_attempts=int(bucket["search_attempts"]),
                successful_searches=int(bucket["successful_searches"]),
                no_match=int(bucket["no_match"]),
                successful_direct_reads=int(bucket["successful_direct_reads"]),
                failures=int(bucket["failures"]),
                suppressed=int(bucket["suppressed"]),
            )
        )
    return tuple(coverage)


def _result_evidence_root(result: ToolResultSummary) -> str:
    for key in ("evidence_root", "root", "scope_root"):
        value = result.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return _normalize_root_identity(value)
    searched_roots = result.metadata.get("searched_roots")
    if isinstance(searched_roots, (list, tuple)) and len(searched_roots) == 1 and isinstance(searched_roots[0], str):
        return _normalize_root_identity(searched_roots[0])
    return ""


def _root_aliases(root: str, result: ToolResultSummary) -> set[str]:
    aliases = {root.casefold()}
    parts = _path_parts(root)
    if parts:
        aliases.add(parts[-1].casefold())
    label = result.metadata.get("evidence_root_label")
    if isinstance(label, str) and label.strip():
        aliases.add(label.strip().casefold())
    return {alias for alias in aliases if alias}


def _is_successful_direct_read_result(result: ToolResultSummary) -> bool:
    return result.name == "read_file" and not result.is_error and tool_result_is_executed_attempt(result)


def _normalize_root_identity(path: str) -> str:
    return path.strip().replace("\\", "/")


def _path_parts(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.split("/") if part and part != ".")

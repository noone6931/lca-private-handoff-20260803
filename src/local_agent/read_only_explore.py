"""Typed, bounded exploration policy for high-risk read-only profiles."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from .tool_observation import ToolResultSummary
from .tool_observation import tool_result_is_executed_attempt


ReadOnlyExploreAction = Literal["none", "precise", "finalize"]

PRECISE_EVIDENCE_TOOLS = frozenset(
    {
        "read_file",
        "search_code",
    }
)
CANDIDATE_EVIDENCE_TOOLS = PRECISE_EVIDENCE_TOOLS | frozenset(
    {
        "lsp_definition",
        "lsp_references",
        "lsp_symbols",
        "lsp_document_symbols",
        "lsp_workspace_symbols",
    }
)
OBSERVATION_TOOLS = CANDIDATE_EVIDENCE_TOOLS | {"glob_files", "list_files"}
MAX_OWNER_DESIGN_EXPLORE_CALLS = 12
SOFT_EXPLORE_CALLS_PER_ROOT = 2
HARD_EXPLORE_CALLS_PER_ROOT = 4


@dataclass(frozen=True)
class ReadOnlyExploreDecision:
    action: ReadOnlyExploreAction
    roots: tuple[str, ...] = ()
    missing_roots: tuple[str, ...] = ()
    observation_calls: int = 0
    successful_observations: int = 0
    soft_budget: int = 0
    hard_budget: int = 0
    read_candidates: tuple[str, ...] = ()
    preferred_roots: tuple[str, ...] = ()
    discovery_roots: tuple[str, ...] = ()

    @property
    def is_applicable(self) -> bool:
        return bool(self.roots)


def evaluate_read_only_explore(
    *,
    profile: str | None,
    tool_results: Iterable[ToolResultSummary],
    code_roots: Iterable[str],
) -> ReadOnlyExploreDecision:
    """Return a policy from typed profile and observations, never request text."""

    if profile not in {"owner_impact", "design"}:
        return ReadOnlyExploreDecision("none")
    roots = tuple(sorted({str(Path(root).resolve()) for root in code_roots if str(root).strip()}))
    if not roots:
        return ReadOnlyExploreDecision("none")
    results = tuple(tool_results)
    # Explore observations are evidence-tool attempts that actually reached
    # execution. Provider-schema violations, exact-tool suppressed calls, and
    # active-tool rejections remain bounded by their directive owners; counting
    # them here would let protocol noise consume the evidence budget.
    observation_calls = sum(
        1
        for result in results
        if _is_executed_explore_attempt(result)
    )
    successful_observations = sum(
        1
        for result in results
        if _is_executed_explore_attempt(result) and _is_typed_explore_progress(result)
    )
    soft_budget = max(2, len(roots) * SOFT_EXPLORE_CALLS_PER_ROOT)
    hard_budget = min(MAX_OWNER_DESIGN_EXPLORE_CALLS, max(4, len(roots) * HARD_EXPLORE_CALLS_PER_ROOT))
    covered = _covered_roots(results, roots)
    missing = tuple(root for root in roots if root not in covered)
    read_candidates = _read_candidates_for_missing_roots(results, missing)
    root_attempts = _root_attempts(results, roots)
    preferred_roots = _least_observed_roots(missing, root_attempts)
    discovery_roots = _roots_needing_fallback_discovery(results, missing, read_candidates, root_attempts)
    if not missing:
        return ReadOnlyExploreDecision(
            "finalize",
            roots=roots,
            observation_calls=observation_calls,
            successful_observations=successful_observations,
            soft_budget=soft_budget,
            hard_budget=hard_budget,
            read_candidates=read_candidates,
            preferred_roots=preferred_roots,
            discovery_roots=discovery_roots,
        )
    if observation_calls >= hard_budget:
        return ReadOnlyExploreDecision(
            "finalize",
            roots=roots,
            missing_roots=missing,
            observation_calls=observation_calls,
            successful_observations=successful_observations,
            soft_budget=soft_budget,
            hard_budget=hard_budget,
            read_candidates=read_candidates,
            preferred_roots=preferred_roots,
            discovery_roots=discovery_roots,
        )
    fallback_reserve = min(hard_budget, max(0, len(discovery_roots) * 2))
    if discovery_roots and observation_calls >= hard_budget - fallback_reserve:
        return ReadOnlyExploreDecision(
            "precise",
            roots=roots,
            missing_roots=missing,
            observation_calls=observation_calls,
            successful_observations=successful_observations,
            soft_budget=soft_budget,
            hard_budget=hard_budget,
            read_candidates=read_candidates,
            preferred_roots=preferred_roots,
            discovery_roots=discovery_roots,
        )
    return ReadOnlyExploreDecision(
        "precise",
        roots=roots,
        missing_roots=missing,
        observation_calls=observation_calls,
        successful_observations=successful_observations,
        soft_budget=soft_budget,
        hard_budget=hard_budget,
        read_candidates=read_candidates,
        preferred_roots=preferred_roots,
        discovery_roots=discovery_roots,
    )


def _is_typed_explore_progress(result: ToolResultSummary) -> bool:
    if result.is_error:
        return False
    if not result.useless:
        return True
    return result.metadata.get("negative_evidence_type") in {
        "content_no_match",
        "path_no_match",
        "exact_path_missing",
    }


def _is_executed_explore_attempt(result: ToolResultSummary) -> bool:
    if result.name not in OBSERVATION_TOOLS:
        return False
    return tool_result_is_executed_attempt(result)


def _covered_roots(results: Iterable[ToolResultSummary], roots: tuple[str, ...]) -> set[str]:
    covered: set[str] = set()
    for result in results:
        if result.name != "read_file" or result.is_error:
            continue
        path = _canonical_path(result)
        if path is None:
            continue
        for root in roots:
            if path == root or path.startswith(root + "/"):
                covered.add(root)
    return covered


def _canonical_path(result: ToolResultSummary) -> str | None:
    for value in (result.metadata.get("resolved_path"), result.path):
        if not isinstance(value, str) or not value.strip():
            continue
        path = Path(value)
        if path.is_absolute():
            try:
                return str(path.resolve())
            except OSError:
                return str(path)
    return None


def _read_candidates_for_missing_roots(
    results: Iterable[ToolResultSummary],
    missing_roots: tuple[str, ...],
) -> tuple[str, ...]:
    """Return only existing, typed search/LSP paths for the next direct read."""

    candidates: list[str] = []
    seen: set[str] = set()
    per_root: dict[str, int] = {root: 0 for root in missing_roots}
    for result in results:
        if (
            result.name not in CANDIDATE_EVIDENCE_TOOLS - {"read_file"}
            or not _is_executed_explore_attempt(result)
            or result.is_error
        ):
            continue
        metadata = result.metadata
        if metadata.get("evidence_paths_overflow"):
            continue
        values = metadata.get("evidence_paths")
        if not isinstance(values, (list, tuple)):
            continue
        for raw in values:
            if not isinstance(raw, str) or not raw.strip():
                continue
            raw_path = Path(raw)
            possible = (raw_path,) if raw_path.is_absolute() else tuple(Path(root) / raw_path for root in missing_roots)
            for path in possible:
                try:
                    resolved = path.resolve(strict=True)
                except OSError:
                    continue
                rendered = str(resolved)
                root = next((item for item in missing_roots if rendered == item or rendered.startswith(item + "/")), None)
                if root is None or per_root[root] >= 2 or rendered in seen:
                    continue
                seen.add(rendered)
                per_root[root] += 1
                candidates.append(rendered)
    for rendered, root in _discovery_read_candidates(results, missing_roots):
        if per_root[root] >= 1 or rendered in seen:
            continue
        seen.add(rendered)
        per_root[root] += 1
        candidates.append(rendered)
    return tuple(candidates)


def _discovery_read_candidates(
    results: Iterable[ToolResultSummary],
    missing_roots: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    candidates: list[tuple[str, str]] = []
    for result in results:
        if (
            result.name != "glob_files"
            or not _is_executed_explore_attempt(result)
            or result.is_error
            or result.useless
            or result.metadata.get("evidence_paths_overflow")
        ):
            continue
        files = result.metadata.get("files")
        if not isinstance(files, (list, tuple)):
            continue
        for raw in files:
            if not isinstance(raw, str) or not raw.strip():
                continue
            raw_path = Path(raw)
            possible = (raw_path,) if raw_path.is_absolute() else tuple(Path(root) / raw_path for root in missing_roots)
            for path in possible:
                try:
                    resolved = path.resolve(strict=True)
                except OSError:
                    continue
                rendered = str(resolved)
                root = next((item for item in missing_roots if rendered == item or rendered.startswith(item + "/")), None)
                if root is not None and resolved.is_file():
                    candidates.append((rendered, root))
                    break
    return tuple(candidates)


def _roots_needing_fallback_discovery(
    results: Iterable[ToolResultSummary],
    missing_roots: tuple[str, ...],
    read_candidates: tuple[str, ...],
    root_attempts: dict[str, int],
) -> tuple[str, ...]:
    if not missing_roots:
        return ()
    if any(root_attempts.get(root, 0) <= 0 for root in missing_roots):
        return ()
    candidate_roots = {
        root
        for path in read_candidates
        for root in missing_roots
        if path == root or path.startswith(root + "/")
    }
    roots = tuple(
        root
        for root in missing_roots
        if root not in candidate_roots and root_attempts.get(root, 0) > 0
        and not _fallback_discovery_attempted(results, root)
    )
    if not roots:
        return ()
    preferred = _least_observed_roots(roots, root_attempts)
    return preferred[:1]


def _fallback_discovery_attempted(results: Iterable[ToolResultSummary], root: str) -> bool:
    for result in results:
        if result.name != "glob_files" or not _is_executed_explore_attempt(result):
            continue
        if _glob_result_has_root_local_candidate(result, root):
            return True
        if _glob_result_is_complete_root_local_no_match(result, root):
            return True
    return False


def _glob_result_has_root_local_candidate(result: ToolResultSummary, root: str) -> bool:
    files = result.metadata.get("files")
    if not isinstance(files, (list, tuple)):
        return False
    for raw in files:
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = Path(raw)
        candidates = (path,) if path.is_absolute() else (Path(root) / path,)
        for candidate in candidates:
            try:
                resolved = str(candidate.resolve(strict=True))
            except OSError:
                continue
            if resolved == root or resolved.startswith(root + "/"):
                return True
    return False


def _glob_result_is_complete_root_local_no_match(result: ToolResultSummary, root: str) -> bool:
    if not result.useless:
        return False
    if result.metadata.get("truncated") or result.metadata.get("evidence_paths_overflow"):
        return False
    searched_roots = result.metadata.get("searched_roots")
    root_local = (
        isinstance(searched_roots, (list, tuple))
        and len(searched_roots) == 1
        and str(searched_roots[0]) == root
    ) or str(result.metadata.get("evidence_root") or "") == root
    if not root_local:
        return False
    return result.metadata.get("negative_evidence_type") in {"path_no_match", "content_no_match"}


def _root_attempts(results: Iterable[ToolResultSummary], roots: tuple[str, ...]) -> dict[str, int]:
    counts = {root: 0 for root in roots}
    for result in results:
        if not _is_executed_explore_attempt(result):
            continue
        root = str(result.metadata.get("evidence_root") or "")
        if root in counts:
            counts[root] += 1
    return counts


def _least_observed_roots(missing_roots: tuple[str, ...], attempts: dict[str, int]) -> tuple[str, ...]:
    if not missing_roots:
        return ()
    lowest = min(attempts.get(root, 0) for root in missing_roots)
    return tuple(root for root in missing_roots if attempts.get(root, 0) == lowest)

"""Typed, bounded exploration policy for high-risk read-only profiles."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from .tool_observation import ToolResultSummary


ReadOnlyExploreAction = Literal["none", "precise", "finalize"]

PRECISE_EVIDENCE_TOOLS = frozenset(
    {
        "read_file",
        "search_code",
        "lsp_definition",
        "lsp_references",
        "lsp_symbols",
        "lsp_document_symbols",
        "lsp_workspace_symbols",
    }
)
OBSERVATION_TOOLS = PRECISE_EVIDENCE_TOOLS | {"glob_files", "list_files"}
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
    # Attempts intentionally include errors and synthetic/schema-rejected
    # observations: they consume the hard budget so a provider cannot loop by
    # varying invalid calls. Progress is reported separately: it accepts a
    # real successful observation or a typed no-match, never an error or a
    # suppressed/not-executed result. Direct root coverage remains stricter
    # and only accepts a successful read below that root.
    observation_calls = sum(
        1
        for result in results
        if result.name in OBSERVATION_TOOLS and result.metadata.get("evidence_origin") != "session_cached"
    )
    successful_observations = sum(
        1
        for result in results
        if result.name in OBSERVATION_TOOLS
        and result.metadata.get("evidence_origin") != "session_cached"
        and _is_typed_explore_progress(result)
    )
    soft_budget = max(2, len(roots) * SOFT_EXPLORE_CALLS_PER_ROOT)
    hard_budget = min(MAX_OWNER_DESIGN_EXPLORE_CALLS, max(4, len(roots) * HARD_EXPLORE_CALLS_PER_ROOT))
    covered = _covered_roots(results, roots)
    missing = tuple(root for root in roots if root not in covered)
    read_candidates = _read_candidates_for_missing_roots(results, missing)
    if not missing:
        return ReadOnlyExploreDecision(
            "finalize",
            roots=roots,
            observation_calls=observation_calls,
            successful_observations=successful_observations,
            soft_budget=soft_budget,
            hard_budget=hard_budget,
            read_candidates=read_candidates,
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
    )


def _is_typed_explore_progress(result: ToolResultSummary) -> bool:
    if result.is_error:
        return False
    if result.metadata.get("suppressed") or "tool call was not executed" in result.content.lower():
        return False
    if not result.useless:
        return True
    return result.metadata.get("negative_evidence_type") in {
        "content_no_match",
        "path_no_match",
        "exact_path_missing",
    }


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
        if result.name not in PRECISE_EVIDENCE_TOOLS - {"read_file"} or result.is_error:
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
    return tuple(candidates)

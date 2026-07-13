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
    soft_budget: int = 0
    hard_budget: int = 0

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
    observation_calls = sum(
        1
        for result in results
        if result.name in OBSERVATION_TOOLS and result.metadata.get("evidence_origin") != "session_cached"
    )
    soft_budget = max(2, len(roots) * SOFT_EXPLORE_CALLS_PER_ROOT)
    hard_budget = min(MAX_OWNER_DESIGN_EXPLORE_CALLS, max(4, len(roots) * HARD_EXPLORE_CALLS_PER_ROOT))
    covered = _covered_roots(results, roots)
    missing = tuple(root for root in roots if root not in covered)
    if not missing:
        return ReadOnlyExploreDecision(
            "finalize",
            roots=roots,
            observation_calls=observation_calls,
            soft_budget=soft_budget,
            hard_budget=hard_budget,
        )
    if observation_calls >= hard_budget:
        return ReadOnlyExploreDecision(
            "finalize",
            roots=roots,
            missing_roots=missing,
            observation_calls=observation_calls,
            soft_budget=soft_budget,
            hard_budget=hard_budget,
        )
    return ReadOnlyExploreDecision(
        "precise",
        roots=roots,
        missing_roots=missing,
        observation_calls=observation_calls,
        soft_budget=soft_budget,
        hard_budget=hard_budget,
    )


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

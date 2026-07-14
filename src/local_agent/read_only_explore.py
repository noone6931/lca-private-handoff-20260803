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
BOUNDED_EXPLORE_TOOLS = PRECISE_EVIDENCE_TOOLS | {"glob_files"}
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
SOURCE_FILE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".scala",
        ".sql",
        ".swift",
        ".ts",
        ".tsx",
        ".vue",
    }
)


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
        if _is_code_root_executed_explore_attempt(result, roots)
    )
    successful_observations = sum(
        1
        for result in results
        if _is_code_root_executed_explore_attempt(result, roots) and _is_typed_explore_progress(result)
    )
    soft_budget = max(2, len(roots) * SOFT_EXPLORE_CALLS_PER_ROOT)
    hard_budget = min(MAX_OWNER_DESIGN_EXPLORE_CALLS, max(4, len(roots) * HARD_EXPLORE_CALLS_PER_ROOT))
    semantic_candidates = _semantic_candidate_paths_by_root(results, roots)
    inventory_paths = _inventory_paths_by_root(results, roots)
    precise_source_inventory = _precise_source_inventory_paths_by_root(results, roots)
    candidate_paths = _merge_candidate_paths(roots, semantic_candidates, precise_source_inventory)
    covered = _covered_roots(results, roots, candidate_paths, inventory_paths)
    missing = tuple(root for root in roots if root not in covered)
    read_candidates = _read_candidates_for_missing_roots(candidate_paths, missing)
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


def _is_code_root_executed_explore_attempt(result: ToolResultSummary, roots: tuple[str, ...]) -> bool:
    return _is_executed_explore_attempt(result) and _result_intersects_roots(result, roots)


def _result_intersects_roots(result: ToolResultSummary, roots: tuple[str, ...]) -> bool:
    return bool(_result_roots(result, roots))


def _result_roots(result: ToolResultSummary, roots: tuple[str, ...]) -> tuple[str, ...]:
    matched: list[str] = []
    seen: set[str] = set()

    def add(root: str | None) -> None:
        if root is not None and root in roots and root not in seen:
            seen.add(root)
            matched.append(root)

    root = str(result.metadata.get("evidence_root") or "").strip()
    add(root)
    searched_roots = result.metadata.get("searched_roots")
    if isinstance(searched_roots, (list, tuple)):
        for item in searched_roots:
            add(str(item).strip())
    path = _canonical_path(result)
    if path is not None:
        add(next((root for root in roots if path == root or path.startswith(root + "/")), None))
    evidence_paths = result.metadata.get("evidence_paths")
    if isinstance(evidence_paths, (list, tuple)):
        for _root, rendered in _scoped_evidence_paths(result, roots):
            add(next((root for root in roots if rendered == root or rendered.startswith(root + "/")), None))
    return tuple(matched)


def _covered_roots(
    results: Iterable[ToolResultSummary],
    roots: tuple[str, ...],
    semantic_candidates: dict[str, tuple[str, ...]],
    inventory_paths: dict[str, tuple[str, ...]],
) -> set[str]:
    covered: set[str] = set()
    for result in results:
        if result.name != "read_file" or result.is_error or not tool_result_is_executed_attempt(result):
            continue
        path = _canonical_path(result)
        if path is None:
            continue
        for root in roots:
            if path in semantic_candidates.get(root, ()):
                covered.add(root)
                continue
            if path in inventory_paths.get(root, ()):
                continue
            if path == root or path.startswith(root + "/"):
                covered.add(root)
    return covered


def _canonical_path(result: ToolResultSummary) -> str | None:
    for value in (result.metadata.get("resolved_path"), result.path, result.metadata.get("path")):
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
    semantic_candidates: dict[str, tuple[str, ...]],
    missing_roots: tuple[str, ...],
) -> tuple[str, ...]:
    """Return only existing, typed search/LSP paths for the next direct read."""

    candidates: list[str] = []
    seen: set[str] = set()
    per_root: dict[str, int] = {root: 0 for root in missing_roots}
    for root in missing_roots:
        for rendered in semantic_candidates.get(root, ()):
            if per_root[root] >= 2 or rendered in seen:
                continue
            seen.add(rendered)
            per_root[root] += 1
            candidates.append(rendered)
    return tuple(candidates)


def _semantic_candidate_paths_by_root(
    results: Iterable[ToolResultSummary],
    roots: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    candidates: dict[str, list[str]] = {root: [] for root in roots}
    seen: set[tuple[str, str]] = set()
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
        for root, rendered in _scoped_evidence_paths(result, roots):
            if (root, rendered) in seen:
                continue
            seen.add((root, rendered))
            candidates[root].append(rendered)
    return {root: tuple(paths) for root, paths in candidates.items()}


def _inventory_paths_by_root(
    results: Iterable[ToolResultSummary],
    roots: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    inventory: dict[str, list[str]] = {root: [] for root in roots}
    seen: set[tuple[str, str]] = set()
    for result in results:
        if result.name not in {"glob_files", "list_files"} or not _is_executed_explore_attempt(result) or result.is_error:
            continue
        files = result.metadata.get("files")
        if not isinstance(files, (list, tuple)):
            continue
        for root, rendered in _scoped_file_list_paths(result, roots, files):
            if (root, rendered) in seen:
                continue
            seen.add((root, rendered))
            inventory[root].append(rendered)
    return {root: tuple(paths) for root, paths in inventory.items()}


def _precise_source_inventory_paths_by_root(
    results: Iterable[ToolResultSummary],
    roots: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    """Promote only exact source basenames from glob output to read candidates.

    Broad extension globs, directories, and manifest names remain inventory.
    This mirrors OMP's glob-then-read workflow without treating an arbitrary
    listing as semantic owner evidence.
    """

    candidates: dict[str, list[str]] = {root: [] for root in roots}
    seen: set[tuple[str, str]] = set()
    for result in results:
        if result.name != "glob_files" or not _is_executed_explore_attempt(result) or result.is_error:
            continue
        patterns = result.metadata.get("patterns")
        files = result.metadata.get("files")
        if not isinstance(patterns, (list, tuple)) or not isinstance(files, (list, tuple)):
            continue
        exact_source_names = _exact_source_names_by_root(result, roots, patterns)
        if not any(exact_source_names.values()):
            continue
        for root, rendered in _scoped_file_list_paths(result, roots, files):
            if Path(rendered).name not in exact_source_names[root] or (root, rendered) in seen:
                continue
            seen.add((root, rendered))
            candidates[root].append(rendered)
    return {root: tuple(paths) for root, paths in candidates.items()}


def _exact_source_basename(raw_pattern: object) -> str | None:
    if not isinstance(raw_pattern, str) or not raw_pattern.strip():
        return None
    name = Path(raw_pattern.strip()).name
    if not name or any(char in name for char in "*?["):
        return None
    if Path(name).suffix.lower() not in SOURCE_FILE_SUFFIXES:
        return None
    return name


def _exact_source_names_by_root(
    result: ToolResultSummary,
    roots: tuple[str, ...],
    patterns: Iterable[object],
) -> dict[str, set[str]]:
    names: dict[str, set[str]] = {root: set() for root in roots}
    for raw in patterns:
        name = _exact_source_basename(raw)
        if name is None or not isinstance(raw, str):
            continue
        rendered = raw.strip()
        scoped_roots = tuple(
            root
            for root in roots
            if rendered == root or rendered.startswith(root.rstrip("/") + "/")
        )
        if not scoped_roots and not Path(rendered).is_absolute():
            scoped_roots = _typed_scope_roots(result, roots)
        for root in scoped_roots:
            names[root].add(name)
    return names


def _merge_candidate_paths(
    roots: tuple[str, ...],
    *sources: dict[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    return {
        root: tuple(
            dict.fromkeys(
                path
                for source in sources
                for path in source.get(root, ())
            )
        )
        for root in roots
    }


def _scoped_evidence_paths(result: ToolResultSummary, roots: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    values = result.metadata.get("evidence_paths")
    if not isinstance(values, (list, tuple)):
        return ()
    scoped_roots = _typed_scope_roots(result, roots)
    entries: list[tuple[str, str]] = []
    for raw in values:
        if not isinstance(raw, str) or not raw.strip():
            continue
        raw_path = Path(raw)
        if raw_path.is_absolute():
            try:
                rendered = str(raw_path.resolve(strict=True))
            except OSError:
                continue
            root = next((item for item in roots if rendered == item or rendered.startswith(item + "/")), None)
            if root is not None:
                entries.append((root, rendered))
            continue
        candidates = tuple(Path(root) / raw_path for root in scoped_roots)
        resolved_candidates: list[tuple[str, str]] = []
        for root, candidate in zip(scoped_roots, candidates, strict=False):
            try:
                rendered = str(candidate.resolve(strict=True))
            except OSError:
                continue
            if rendered == root or rendered.startswith(root + "/"):
                resolved_candidates.append((root, rendered))
        if len(resolved_candidates) == 1:
            entries.extend(resolved_candidates)
    return tuple(entries)


def _scoped_file_list_paths(
    result: ToolResultSummary,
    roots: tuple[str, ...],
    values: Iterable[object],
) -> tuple[tuple[str, str], ...]:
    entries: list[tuple[str, str]] = []
    for raw in values:
        if not isinstance(raw, str) or not raw.strip():
            continue
        raw_path = Path(raw)
        if raw_path.is_absolute():
            try:
                rendered = str(raw_path.resolve(strict=True))
            except OSError:
                continue
            root = next((item for item in roots if rendered == item or rendered.startswith(item + "/")), None)
            if root is not None:
                entries.append((root, rendered))
            continue
        scoped_roots = _typed_scope_roots(result, roots)
        resolved_candidates: list[tuple[str, str]] = []
        for root in scoped_roots:
            try:
                rendered = str((Path(root) / raw_path).resolve(strict=True))
            except OSError:
                continue
            if rendered == root or rendered.startswith(root + "/"):
                resolved_candidates.append((root, rendered))
        if len(resolved_candidates) == 1:
            entries.extend(resolved_candidates)
    return tuple(entries)


def _typed_scope_roots(result: ToolResultSummary, roots: tuple[str, ...]) -> tuple[str, ...]:
    root = str(result.metadata.get("evidence_root") or "").strip()
    if root in roots:
        return (root,)
    searched_roots = result.metadata.get("searched_roots")
    if isinstance(searched_roots, (list, tuple)):
        scoped = tuple(str(item).strip() for item in searched_roots if str(item).strip() in roots)
        if scoped:
            return scoped
    return roots


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
    relative_scope_is_root = _relative_glob_files_bind_to_root(result, root)
    for raw in files:
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = Path(raw)
        if not path.is_absolute() and not relative_scope_is_root:
            continue
        candidates = (path,) if path.is_absolute() else (Path(root) / path,)
        for candidate in candidates:
            try:
                resolved = str(candidate.resolve(strict=True))
            except OSError:
                continue
            if resolved == root or resolved.startswith(root + "/"):
                return True
    return False


def _relative_glob_files_bind_to_root(result: ToolResultSummary, root: str) -> bool:
    evidence_root = str(result.metadata.get("evidence_root") or "").strip()
    if evidence_root:
        return evidence_root == root
    searched_roots = result.metadata.get("searched_roots")
    return (
        isinstance(searched_roots, (list, tuple))
        and len(searched_roots) == 1
        and str(searched_roots[0]).strip() == root
    )


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
        for root in _result_roots(result, roots):
            counts[root] += 1
    return counts


def _least_observed_roots(missing_roots: tuple[str, ...], attempts: dict[str, int]) -> tuple[str, ...]:
    if not missing_roots:
        return ()
    lowest = min(attempts.get(root, 0) for root in missing_roots)
    return tuple(root for root in missing_roots if attempts.get(root, 0) == lowest)

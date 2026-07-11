from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
import time


_DESIGN_PROMPT_MARKERS = ("design", "architecture", "方案", "设计", "架构")
_CODE_ROOT_MARKERS = (
    "pom.xml",
    "package.json",
    "pyproject.toml",
    "build.gradle",
    "build.gradle.kts",
    "src",
)
_CODE_SOURCE_SUFFIXES = frozenset(
    {
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".py",
        ".ts",
        ".tsx",
        ".vue",
        ".go",
        ".rs",
        ".cs",
    }
)
# Keep enough wall-clock budget for one direct final response after cross-root
# evidence is complete. The runtime reuses this boundary before scheduling an
# optional final-answer rewrite.
FINAL_RESPONSE_RESERVE_SECONDS = 45.0
_MAX_FOLLOWUP_TOOL_CALLS = 6


@dataclass(frozen=True)
class DesignEvidenceCoverageDecision:
    kind: str
    payload: dict[str, object]
    message: str | None = None
    force_final_answer_without_tools: bool = False
    preceding_events: tuple[tuple[str, dict[str, object]], ...] = ()


class DesignEvidenceCoverageSteerer:
    """Track cross-root source coverage and bound post-coverage exploration."""

    def __init__(self) -> None:
        self._roots: tuple[str, ...] = ()
        self._covered_at_tool_count: int | None = None
        self._final_steers = 0

    @property
    def roots(self) -> tuple[str, ...]:
        return self._roots

    @property
    def final_steers(self) -> int:
        return self._final_steers

    def reset(self, roots: tuple[str, ...]) -> None:
        self._roots = roots
        self._covered_at_tool_count = None
        self._final_steers = 0

    def observe(
        self,
        *,
        queue_requires_steering: bool,
        read_paths: Iterable[str | None],
        tool_count: int,
        deadline: float | None,
        request_summary: str,
    ) -> DesignEvidenceCoverageDecision | None:
        if not self._roots or queue_requires_steering:
            return None
        if missing_design_evidence_roots(self._roots, read_paths):
            return None
        reserve_required = deadline is not None and deadline - time.monotonic() <= FINAL_RESPONSE_RESERVE_SECONDS
        covered_event: tuple[str, dict[str, object]] | None = None
        if self._covered_at_tool_count is None:
            self._covered_at_tool_count = tool_count
            covered_payload = {"roots": list(self._roots), "tool_count": tool_count}
            covered_event = ("design_evidence_covered", covered_payload)
            covered = DesignEvidenceCoverageDecision(
                kind="design_evidence_covered",
                payload=covered_payload,
            )
            if not reserve_required:
                return covered
        if self._final_steers:
            return None
        followup_limit_reached = tool_count - self._covered_at_tool_count >= _MAX_FOLLOWUP_TOOL_CALLS
        if not reserve_required and not followup_limit_reached:
            return None
        self._final_steers += 1
        stop_reason = "deadline_reserve" if reserve_required else "followup_limit"
        return DesignEvidenceCoverageDecision(
            kind="design_evidence_final",
            payload={
                "roots": list(self._roots),
                "covered_at_tool_count": self._covered_at_tool_count,
                "tool_count": tool_count,
                "followup_limit": _MAX_FOLLOWUP_TOOL_CALLS,
                "reason": stop_reason,
            },
            message=(
                "Runtime steering: the required requirement/backend/frontend evidence matrix is complete, "
                "and the bounded follow-up exploration allowance is exhausted. Do not call more tools. "
                "Produce the requested design now, separate verified facts from 推断/建议, and list remaining "
                "uncertainty instead of continuing to search."
                f"{request_summary}"
            ),
            force_final_answer_without_tools=True,
            preceding_events=(covered_event,) if covered_event is not None else (),
        )


def cross_root_design_evidence_roots(
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
    prompt: str,
) -> tuple[str, ...]:
    """Return code roots that must each contribute a source read for a cross-root design task."""
    lowered = (prompt or "").lower()
    if not any(marker in lowered for marker in _DESIGN_PROMPT_MARKERS):
        return ()
    roots: list[str] = []
    for candidate in (workspace, *allowed_dirs):
        resolved = candidate.resolve()
        rendered = str(resolved)
        if rendered not in roots and _looks_like_code_root(resolved):
            roots.append(rendered)
    return tuple(roots) if len(roots) >= 2 else ()


def missing_design_evidence_roots(
    roots: Iterable[str],
    read_paths: Iterable[str | None],
) -> tuple[str, ...]:
    source_paths = [path for path in read_paths if isinstance(path, str) and _is_code_source_path(path)]
    missing: list[str] = []
    for root in roots:
        if not any(_path_is_within(path, root) for path in source_paths):
            missing.append(str(root))
    return tuple(missing)


def _looks_like_code_root(path: Path) -> bool:
    return path.is_dir() and any((path / marker).exists() for marker in _CODE_ROOT_MARKERS)


def _is_code_source_path(path: str) -> bool:
    return Path(path).suffix.lower() in _CODE_SOURCE_SUFFIXES


def _path_is_within(path: str, root: str) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True

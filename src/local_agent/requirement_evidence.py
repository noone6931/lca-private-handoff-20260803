from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


MAX_PINNED_REQUIREMENT_SOURCES = 2
PINNED_REQUIREMENT_EVIDENCE_CHAR_LIMIT = 12000

_REQUIREMENT_PATH_MARKERS = ("requirement", "requirements", "spec", "specs", "需求", "规格")
_REQUIREMENT_FACT_MARKERS = ("需求", "需求文档", "需求原文", "业务流程", "方案概述", "v1.")


@dataclass(frozen=True)
class RequirementEvidence:
    path: str
    content: str
    root: str | None = None
    scope: str = "root_local"


def is_requirement_source_path(path: str, candidate_paths: tuple[Path, ...] = ()) -> bool:
    if path in {str(candidate) for candidate in candidate_paths}:
        return True
    name = Path(path).name.lower()
    return name.endswith(".md") and any(marker in name for marker in _REQUIREMENT_PATH_MARKERS)


def update_requirement_evidence(
    current: list[RequirementEvidence],
    *,
    path: str,
    content: str,
    root: str | None = None,
    scope: str = "root_local",
) -> list[RequirementEvidence]:
    latest = RequirementEvidence(
        path=path,
        content=content[:PINNED_REQUIREMENT_EVIDENCE_CHAR_LIMIT],
        root=root,
        scope=scope,
    )
    remaining = [item for item in current if item.path != path]
    return [*remaining, latest][-MAX_PINNED_REQUIREMENT_SOURCES:]


def render_pinned_requirement_evidence(evidence: list[RequirementEvidence]) -> str:
    if not evidence:
        return ""
    lines = [
        "[Pinned requirement evidence]",
        "These successfully read requirement documents are authoritative only within their recorded scope and outrank "
        "any compaction summary or inferred workflow.",
        "A root_local requirement or rule constrains only its source root. Do not infer that sibling roots must delete, "
        "modify, or omit code unless the user explicitly requested cross-root synthesis.",
        "Do not turn later-planning items into current scope. For every requirement fact in the final answer, cite "
        "the real requirement path and line number in the form `path:line`; label new design proposals as 推断/建议.",
    ]
    for item in evidence:
        root = item.root or "(unknown root)"
        lines.extend(["", f"Source: {item.path} [root={root}; scope={item.scope}]", item.content])
    return "\n".join(lines)


def requirement_fact_citation_issues(content: str, evidence: list[RequirementEvidence]) -> list[str]:
    if not evidence or not _mentions_requirement_facts(content):
        return []
    if any(_has_line_citation(content, item.path) for item in evidence):
        return []
    names = ", ".join(Path(item.path).name for item in evidence)
    return [f"missing_requirement_path_and_line_citation:{names}"]


def _mentions_requirement_facts(content: str) -> bool:
    lowered = content.lower()
    return any(marker in lowered for marker in _REQUIREMENT_FACT_MARKERS)


def _has_line_citation(content: str, path: str) -> bool:
    name = re.escape(Path(path).name)
    return re.search(rf"{name}(?::|#L)\d+", content, flags=re.IGNORECASE) is not None

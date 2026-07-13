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
    origin: str = "current_run"


@dataclass(frozen=True)
class DocumentLocator:
    """A path-bound locator into a human-authored document."""

    path: str
    kind: str
    value: str


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
    origin: str = "current_run",
) -> list[RequirementEvidence]:
    latest = RequirementEvidence(
        path=path,
        content=content[:PINNED_REQUIREMENT_EVIDENCE_CHAR_LIMIT],
        root=root,
        scope=scope,
        origin=origin,
    )
    remaining = [item for item in current if item.path != path]
    return [*remaining, latest][-MAX_PINNED_REQUIREMENT_SOURCES:]


def render_pinned_requirement_evidence(evidence: list[RequirementEvidence]) -> str:
    if not evidence:
        return ""
    lines = [
        "[Pinned requirement evidence]",
        "These successfully read requirement documents are authoritative only within their recorded scope and outrank "
        "any compaction summary or inferred workflow. Current user-provided task facts may refine or override scope; "
        "when they conflict with repository evidence, report both origins instead of silently choosing one.",
        "A root_local requirement or rule constrains only its source root. Do not infer that sibling roots must delete, "
        "modify, or omit code unless the user explicitly requested cross-root synthesis.",
        "Do not turn later-planning items into current scope. For every requirement fact in the final answer, cite "
        "the real requirement path plus a line number or document locator (heading/section); label new design proposals as 推断/建议.",
    ]
    for item in evidence:
        root = item.root or "(unknown root)"
        lines.extend(["", f"Source: {item.path} [root={root}; scope={item.scope}; origin={item.origin}]", item.content])
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
    return bool(parse_document_locators(content, path))


def parse_document_locators(content: str, path: str) -> tuple[DocumentLocator, ...]:
    """Find locators that are explicitly bound to one cited document path.

    A bare ``P49`` or heading is not a citation: callers must name the source
    file beside the locator so a document-only answer stays auditable.
    """

    filename = Path(path).name
    if not filename:
        return ()
    prefix = rf"`?{re.escape(filename)}`?\s*"
    patterns = (
        ("line", prefix + r"(?:#L|:)\s*(\d+)"),
        ("page", prefix + r"(?:P|page|页)\s*(\d+)"),
        ("section", prefix + r"(?:#|§|章节|章|节|section|heading)\s*([^\n`，,。;；:：]+)"),
        ("heading", prefix + r"(?:标题|title)\s*[:：]?\s*([^\n`，,。;；:：]+)"),
    )
    found: list[DocumentLocator] = []
    for kind, pattern in patterns:
        for match in re.finditer(pattern, content, flags=re.IGNORECASE):
            value = match.group(1).strip()
            if value:
                found.append(DocumentLocator(filename, kind, value))
    return tuple(found)

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


def requirement_citation_examples(evidence: list[RequirementEvidence], *, limit: int = 2) -> tuple[str, ...]:
    """Return copyable path-bound examples using real read evidence lines."""

    examples: list[str] = []
    for item in evidence:
        rows = _tagged_rows(item.content)
        if not rows:
            continue
        line_no, _ = rows[0]
        source = item.path
        examples.append(f"{source}:{line_no}")
        examples.append(f"{source}#L{line_no}")
        examples.append(f"{source}:#L{line_no}")
        if len(examples) >= limit * 3:
            break
    return tuple(examples[: limit * 3])


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

    normalized_path = Path(path).as_posix().strip()
    filename = Path(path).name
    if not filename:
        return ()
    refs = tuple(dict.fromkeys(ref for ref in sorted((normalized_path, filename), key=len, reverse=True) if ref))
    patterns: list[tuple[str, str, str]] = []
    for ref in refs:
        prefix = rf"`?{re.escape(ref)}`?\s*"
        content_tag_prefix = (
            rf"\[\s*`?{re.escape(ref)}`?#[0-9a-f]{{8,64}}\s*\]\s*[,，]?\s*"
        )
        patterns.extend(
            (
                (ref, "line", prefix + r"(?::#L|#L|:)\s*(L?\d+(?:\s*[-–]\s*L?\d+)?)"),
                (ref, "line", content_tag_prefix + r"L(\d+(?:\s*[-–]\s*L?\d+)?)"),
                (ref, "page", prefix + r"(?:P|page|页)\s*(\d+)"),
                (ref, "section", prefix + r"第\s*([\d.]+)\s*节"),
                (
                    ref,
                    "section",
                    prefix
                    + r"(?:#(?![0-9a-f]{8,64}\])|§|章节|章|节|section|heading)\s*([^\n`，,。;；:：]+)",
                ),
                (ref, "heading", prefix + r"(?:标题|title)\s*[:：]?\s*([^\n`，,。;；:：]+)"),
            )
        )
    found: list[DocumentLocator] = []
    spans: list[tuple[int, int, str, str]] = []
    for ref, kind, pattern in patterns:
        for match in re.finditer(pattern, content, flags=re.IGNORECASE):
            value = match.group(1).strip()
            if kind in {"section", "heading"} and re.fullmatch(r"L?\d+(?:\s*[-–]\s*L?\d+)?", value, flags=re.IGNORECASE):
                continue
            start, end = match.start(), match.end()
            overlaps = any(
                kind == prior_kind and value == prior_value and start < prior_end and prior_start < end
                for prior_start, prior_end, prior_kind, prior_value in spans
            )
            if value and not overlaps:
                spans.append((start, end, kind, value))
                found.append(DocumentLocator(ref, kind, value))
    return tuple(found)


def document_locator_excerpt(content: str, locator: DocumentLocator, *, context_lines: int = 2) -> str | None:
    """Return a bounded excerpt for a parsed path-bound locator.

    The caller has already read and pinned the document.  This helper never
    opens files; it only extracts visible source around the cited line, page,
    section, or heading so downstream reviewers can audit a specific citation.
    """

    if not content:
        return None
    if locator.kind == "line":
        line_range = parse_document_line_range(locator.value, max_lines=40)
        if line_range is None:
            return None
        if line_range[0] != line_range[1]:
            tagged_range = _tagged_line_range_excerpt(content, line_range[0], line_range[1])
            if tagged_range:
                return tagged_range
            lines = content.splitlines()
            if not 1 <= line_range[0] <= line_range[1] <= len(lines):
                return None
            rendered = [
                f"{locator.path}:{number}: {lines[number - 1].strip()}"
                for number in range(line_range[0], line_range[1] + 1)
                if lines[number - 1].strip()
            ]
            return "\n".join(rendered) if rendered else None
        line_no = line_range[0]
        tagged = _tagged_line_excerpt(content, line_no, context_lines=context_lines)
        if tagged:
            return tagged
        lines = content.splitlines()
        if not 1 <= line_no <= len(lines):
            return None
        start = max(1, line_no - context_lines)
        end = min(len(lines), line_no + context_lines)
        rendered = [
            f"{locator.path}:{number}: {lines[number - 1].strip()}"
            for number in range(start, end + 1)
            if lines[number - 1].strip()
        ]
        return "\n".join(rendered) if rendered else None
    value = locator.value.strip()
    if not value:
        return None
    pattern = _locator_search_pattern(locator)
    tagged = _tagged_pattern_excerpt(content, pattern, context_lines=context_lines)
    if tagged:
        return tagged
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if not pattern.search(line):
            continue
        start = max(0, index - context_lines)
        end = min(len(lines), index + context_lines + 1)
        rendered = [
            f"{locator.path}:{number + 1}: {lines[number].strip()}"
            for number in range(start, end)
            if lines[number].strip()
        ]
        return "\n".join(rendered) if rendered else None
    return None


def parse_document_line_range(value: str, *, max_lines: int | None = 40) -> tuple[int, int] | None:
    """Return a normalized inclusive line range from a path-bound locator value."""

    match = re.fullmatch(r"\s*L?(\d+)(?:\s*[-–]\s*L?(\d+))?\s*", value or "", flags=re.IGNORECASE)
    if match is None:
        return None
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if start < 1 or end < start:
        return None
    if max_lines is not None and end - start + 1 > max_lines:
        return None
    return start, end


def _locator_search_pattern(locator: DocumentLocator) -> re.Pattern[str]:
    value = re.escape(locator.value.strip())
    if locator.kind == "page":
        return re.compile(rf"(?:P|page|页)\s*{value}\b", flags=re.IGNORECASE)
    if locator.kind in {"section", "heading"}:
        return re.compile(rf"(?:^|\s|#|第){value}(?:\s|节|[.、:-])", flags=re.IGNORECASE)
    return re.compile(value, flags=re.IGNORECASE)


def _tagged_line_excerpt(content: str, line_no: int, *, context_lines: int) -> str | None:
    """Extract from read_file output that renders source as ``211: text``."""

    indexed = _tagged_rows(content)
    if not indexed or not any(number == line_no for number, _ in indexed):
        return None
    start = line_no - context_lines
    end = line_no + context_lines
    rendered = [
        f"{number}: {text}"
        for number, text in indexed
        if start <= number <= end and text
    ]
    return "\n".join(rendered) if rendered else None


def _tagged_line_range_excerpt(content: str, start_line: int, end_line: int) -> str | None:
    indexed = _tagged_rows(content)
    if not indexed:
        return None
    present = {number for number, _ in indexed}
    if start_line not in present or end_line not in present:
        return None
    rendered = [
        f"{number}: {text}"
        for number, text in indexed
        if start_line <= number <= end_line and text
    ]
    return "\n".join(rendered) if rendered else None


def _tagged_pattern_excerpt(content: str, pattern: re.Pattern[str], *, context_lines: int) -> str | None:
    indexed = _tagged_rows(content)
    if not indexed:
        return None
    match_number = next((number for number, text in indexed if pattern.search(text)), None)
    if match_number is None:
        return None
    start = match_number - context_lines
    end = match_number + context_lines
    rendered = [
        f"{number}: {text}"
        for number, text in indexed
        if start <= number <= end and text
    ]
    return "\n".join(rendered) if rendered else None


def _tagged_rows(content: str) -> list[tuple[int, str]]:
    indexed: list[tuple[int, str]] = []
    for line in content.splitlines():
        match = re.match(r"^\s*(\d{1,6})\s*[:：]\s*(.*)$", line)
        if match:
            indexed.append((int(match.group(1)), match.group(2).strip()))
    return indexed

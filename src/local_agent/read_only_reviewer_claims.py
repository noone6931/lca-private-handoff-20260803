"""Candidate claim projection and bounded Markdown structure."""
from __future__ import annotations

import re
from dataclasses import replace

from .read_only_reviewer_types import CandidateClaimProjectionIssue
from .read_only_reviewer_types import CandidateClaimRole
from .read_only_reviewer_types import CandidateClaimUnit
from .read_only_reviewer_types import MAX_CLAIM_TOTAL_CHARS
from .read_only_reviewer_types import MAX_CLAIM_UNIT_CHARS
from .read_only_reviewer_types import MAX_CLAIM_UNITS
from .read_only_reviewer_types import MAX_TRANSPORT_RESIDUAL_PRUNE_CLAIMS
from .task_contract import RequirementContract


def should_review_read_only_candidate(contract: RequirementContract | None, request: str | None) -> bool:
    """Consume the typed task-owner profile; never reclassify natural language."""

    if contract is None:
        return False
    if contract.inspection_forbidden or contract.workspace_metadata_subject:
        return False
    if contract.evidence_domain == "repository_code":
        return contract.read_only_review_profile in {"owner_impact", "design"}
    return contract.evidence_domain == "requirement_documents" and contract.read_only_review_profile == "document_consistency"


def candidate_claim_units(candidate: str) -> tuple[CandidateClaimUnit, ...]:
    return _extract_candidate_claim_units(candidate)[0]


def candidate_claim_projection_issues(candidate: str) -> tuple[CandidateClaimProjectionIssue, ...]:
    return _extract_candidate_claim_units(candidate)[1]


def prune_exact_transport_residual_claim_lines(
    candidate: str,
    claim_units: tuple[CandidateClaimUnit, ...],
    omitted_claim_ids: set[str],
) -> tuple[str, tuple[str, ...]]:
    """Delete a tiny residual only when every omitted claim is one exact list line.

    The primary model already received one bounded transport rewrite. This
    projection cannot rewrite prose or infer a replacement; it only removes
    whole unsupported list items before handoff reconstruction and review.
    """

    if not omitted_claim_ids or len(omitted_claim_ids) > MAX_TRANSPORT_RESIDUAL_PRUNE_CLAIMS:
        return candidate, ()
    omitted_units = {unit.claim_id: unit.text.strip() for unit in claim_units if unit.claim_id in omitted_claim_ids}
    if set(omitted_units) != omitted_claim_ids:
        return candidate, ()
    lines = candidate.splitlines()
    removed_indexes: set[int] = set()
    removed_ids: list[str] = []
    for claim_id in sorted(omitted_claim_ids):
        target = omitted_units[claim_id]
        if not re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)", target):
            return candidate, ()
        matches = [index for index, line in enumerate(lines) if index not in removed_indexes and line.strip() == target]
        if len(matches) != 1:
            return candidate, ()
        removed_indexes.add(matches[0])
        removed_ids.append(claim_id)
    projected = "\n".join(line for index, line in enumerate(lines) if index not in removed_indexes)
    if candidate.endswith("\n"):
        projected += "\n"
    return projected, tuple(removed_ids)


def _candidate_claim_role(section_context: str) -> CandidateClaimRole:
    context = section_context.casefold()
    if any(
        marker in context
        for marker in (
            "尚待确认",
            "待确认",
            "必须确认",
            "开放问题",
            "阻塞项",
            "未验证项",
            "未定位",
            "open question",
            "pending confirmation",
            "blocking dependenc",
            "unlocated",
            "unverified",
        )
    ):
        return "pending"
    if any(marker in context for marker in ("设计建议", "实施建议", "方案建议", "design proposal", "recommendation")):
        return "proposal"
    if any(marker in context for marker in ("需求明确事实", "需求事实", "requirement fact")):
        return "requirement_fact"
    if any(marker in context for marker in ("源码当前事实", "源码事实", "repository fact", "source fact")):
        return "source_fact"
    return "other"


def _extract_candidate_claim_units(candidate: str) -> tuple[tuple[CandidateClaimUnit, ...], tuple[CandidateClaimProjectionIssue, ...]]:
    """Index Markdown claims, then deterministically sample both head and tail.

    Structural lines are independent units. Ordinary paragraphs split on common
    sentence boundaries and then on complete punctuation-delimited pieces. If a
    factual unit cannot be transported without mid-unit clipping, the caller
    receives a projection issue and must fail closed rather than silently omit
    that claim. This is presentation-aware text segmentation, not semantic NLP.
    """

    indexed: list[CandidateClaimUnit] = []
    issues: list[CandidateClaimProjectionIssue] = []
    paragraph: list[str] = []
    locator_context = ""
    section_context = ""
    section_context_by_level: dict[int, str] = {}
    pending_structural_group: list[int] = []
    active_source_path = ""

    def record_issue(code: str, detail: str = "") -> None:
        issues.append(CandidateClaimProjectionIssue(code, detail))

    def append_unit(unit: str) -> None:
        nonlocal active_source_path
        explicit_source_path = _source_path_context(unit)
        if explicit_source_path:
            active_source_path = explicit_source_path
        effective_locator_context = locator_context
        line_range = _claim_line_range(unit)
        if active_source_path and line_range is not None:
            start, end = line_range
            effective_locator_context = f"{active_source_path}:{start}" if start == end else f"{active_source_path}:{start}-{end}"
        indexed.append(
            CandidateClaimUnit(
                f"c{len(indexed) + 1:03d}",
                unit,
                effective_locator_context,
                section_context,
                _candidate_claim_role(section_context),
            )
        )

    def apply_locator_to_pending_group(context: str) -> None:
        for item_index in pending_structural_group:
            unit = indexed[item_index]
            if not unit.locator_context:
                indexed[item_index] = replace(unit, locator_context=context)
        pending_structural_group.clear()

    def flush_paragraph() -> None:
        if not paragraph:
            paragraph.clear()
            return
        text = "\n".join(paragraph).strip()
        paragraph.clear()
        if _is_citation_only_context(text):
            apply_locator_to_pending_group(text)
            return
        pending_structural_group.clear()
        units, overflow = _paragraph_units(text)
        if overflow:
            record_issue("candidate_claim_projection_overflow", "paragraph")
        for sentence in units:
            append_unit(sentence)

    raw_lines = (candidate or "").splitlines()
    for index, raw_line in enumerate(raw_lines):
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        if _is_markdown_horizontal_rule(line):
            flush_paragraph()
            pending_structural_group.clear()
            continue
        if _is_markdown_heading(line):
            flush_paragraph()
            heading_level = _heading_level(line)
            section_context_by_level[heading_level] = _heading_context(line)
            for nested_level in tuple(level for level in section_context_by_level if level > heading_level):
                section_context_by_level.pop(nested_level, None)
            section_context = " > ".join(
                section_context_by_level[level]
                for level in sorted(section_context_by_level)
            )
            pending_structural_group.clear()
            locator_context = line if _line_has_path_bound_locator(line) else ""
            active_source_path = _source_path_context(line)
            if locator_context:
                pending_structural_group.clear()
            continue
        if _is_citation_only_context(line):
            flush_paragraph()
            apply_locator_to_pending_group(line)
            continue
        structural = line.startswith(("- ", "* ", "+ ", "> ")) or _is_ordered_list_item(line) or _is_table_row(line)
        if structural:
            flush_paragraph()
            next_line = raw_lines[index + 1].strip() if index + 1 < len(raw_lines) else ""
            if not _is_table_separator(line) and not (_is_table_row(line) and _is_table_separator(next_line)):
                if not _is_presentation_list_container_label(line):
                    units, overflow = _structural_units(line)
                    if overflow:
                        record_issue("candidate_claim_projection_overflow", "table_row" if _is_table_row(line) else "structural_line")
                    for unit in units:
                        before = len(indexed)
                        append_unit(unit)
                        pending_structural_group.append(before)
            continue
        pending_structural_group.clear()
        paragraph.append(raw_line)
    flush_paragraph()
    if not indexed and candidate.strip() and not issues:
        indexed.append(
            CandidateClaimUnit(
                "c001",
                _clip_unit(candidate),
                section_context=section_context,
                claim_role=_candidate_claim_role(section_context),
            )
        )
    return _sample_claim_units(indexed), tuple(issues)

def _clip(value: str, limit: int = 420) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "..."


def _normalize_span(value: str) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def _normalize_claim_binding(value: str) -> str:
    return re.sub(r"\s+", " ", _normalize_markdown(value)).strip()


def _normalize_markdown(value: str) -> str:
    without_presentation = re.sub(r"[`*_~#>]", "", value or "")
    return _normalize_span(without_presentation.replace("|", " "))

def _clip_unit(value: str) -> str:
    text = value.strip()
    return text if len(text) <= MAX_CLAIM_UNIT_CHARS else text[: MAX_CLAIM_UNIT_CHARS - 1].rstrip() + "..."


def _paragraph_units(value: str) -> tuple[tuple[str, ...], bool]:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?。！？])(?=\s+|$)", value) if part.strip()]
    units: list[str] = []
    overflow = False
    for sentence in sentences or [value.strip()]:
        if len(sentence) <= MAX_CLAIM_UNIT_CHARS:
            units.append(sentence)
            continue
        split_units, split_overflow = _split_long_complete_unit(sentence)
        units.extend(split_units)
        overflow = overflow or split_overflow
    return tuple(units), overflow


def _structural_units(line: str) -> tuple[tuple[str, ...], bool]:
    if _is_table_row(line):
        compact = " ".join(line.split())
        if len(compact) <= MAX_CLAIM_UNIT_CHARS:
            return (compact,), False
        cells = [cell.strip() for cell in line.strip().strip("|").split("|") if cell.strip()]
        overflow = any(len(cell) > MAX_CLAIM_UNIT_CHARS for cell in cells)
        return tuple(cell for cell in cells if len(cell) <= MAX_CLAIM_UNIT_CHARS), overflow
    compact = " ".join(line.split())
    if len(compact) <= MAX_CLAIM_UNIT_CHARS:
        return (compact,), False
    return _split_long_complete_unit(compact)


def _split_long_complete_unit(value: str) -> tuple[tuple[str, ...], bool]:
    pieces = [piece.strip() for piece in re.split(r"(?<=[;；,，])\s*", value) if piece.strip()]
    units: list[str] = []
    current = ""
    overflow = False
    for piece in pieces:
        if len(piece) > MAX_CLAIM_UNIT_CHARS:
            if current:
                units.append(current)
                current = ""
            overflow = True
            continue
        candidate = f"{current} {piece}".strip() if current else piece
        if len(candidate) <= MAX_CLAIM_UNIT_CHARS:
            current = candidate
            continue
        if current:
            units.append(current)
        current = piece
    if current:
        units.append(current)
    if not units and len((value or "").strip()) > MAX_CLAIM_UNIT_CHARS:
        overflow = True
    return tuple(units), overflow


def _is_markdown_heading(line: str) -> bool:
    return bool(re.fullmatch(r"#{1,6}\s+\S.*", line))


def _heading_context(line: str) -> str:
    value = re.sub(r"^#{1,6}\s+", "", line or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value[:180]


def _heading_level(line: str) -> int:
    match = re.match(r"^(#{1,6})\s+", line or "")
    return len(match.group(1)) if match is not None else 1


def _is_markdown_horizontal_rule(line: str) -> bool:
    return bool(re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", line.replace(" ", "")))


def _is_ordered_list_item(line: str) -> bool:
    return bool(re.match(r"^\d{1,4}[.)]\s+\S", line))


def _is_presentation_list_container_label(line: str) -> bool:
    if _is_table_row(line):
        return False
    if line.startswith(">"):
        line = line[1:].strip()
    if line.startswith(("- ", "* ", "+ ")):
        label = line[2:].strip()
    else:
        match = re.match(r"^\d{1,4}[.)]\s+(.+)$", line)
        if match is None:
            return False
        label = match.group(1).strip()
    semantic = re.sub(r"[*_`~]", "", label).strip()
    return bool(semantic) and semantic.endswith((":", "："))


def _line_has_path_bound_locator(line: str) -> bool:
    return bool(re.search(r"\.(?:md|markdown|html?|png|jpe?g|gif|webp)\s*[:#（(；;，, ]", line, flags=re.IGNORECASE))


_SOURCE_PATH_SUFFIXES = (
    "bash|c|cc|cpp|css|go|gradle|h|hpp|html?|java|js|jsx|json|kt|kts|md|markdown|png|jpe?g|gif|webp|"
    "properties|py|rs|scss|sh|sql|svelte|toml|ts|tsx|vue|xml|ya?ml|zsh"
)


def _source_path_context(text: str) -> str:
    """Return one explicit source path for nearby line-only bullets."""

    backticked = re.findall(r"`([^`\n]+)`", text or "")
    candidates = [*backticked]
    candidates.extend(
        match.group(1)
        for match in re.finditer(
            rf"((?:[A-Za-z]:)?(?:[/\\][^\s`，,；;：:]+)+\.(?:{_SOURCE_PATH_SUFFIXES}))",
            text or "",
            flags=re.IGNORECASE,
        )
    )
    for candidate in candidates:
        normalized = candidate.strip().strip("()[]{}（）【】,，;；。")
        if re.search(rf"\.(?:{_SOURCE_PATH_SUFFIXES})$", normalized, flags=re.IGNORECASE):
            return normalized
    return ""


def _claim_line_range(text: str) -> tuple[int, int] | None:
    """Parse a nearby source-line label without interpreting its semantics."""

    match = re.search(r"第\s*(\d+)\s*(?:[-–至]\s*(\d+)\s*)?行", text or "")
    if match is None:
        match = re.search(r"\blines?\s+L?(\d+)\s*(?:[-–]\s*L?(\d+)\s*)?", text or "", flags=re.IGNORECASE)
    if match is None:
        return None
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if start < 1 or end < start or end - start + 1 > 160:
        return None
    return start, end


def _is_citation_only_context(line: str) -> bool:
    if not _line_has_path_bound_locator(line):
        return False
    remainder = re.sub(
        r"`?[\w./\\ \-\u4e00-\u9fff]+?\.(?:md|markdown|html?|png|jpe?g|gif|webp)"
        r"(?:\s*[:#]\s*L?\d+(?:\s*[-–]\s*L?\d+)?|\s*第?\d+(?:\.\d+)*节)?`?",
        "",
        line,
        flags=re.IGNORECASE,
    )
    remainder = re.sub(r"[\s()[\]（）【】《》:：;；,，.。、\-–#L\d]+", "", remainder, flags=re.IGNORECASE)
    if not remainder:
        return True
    remainder = re.sub(
        r"(?:证据|引用|来源|参考|见|详见|evidence|source|sources|citation|citations|ref|reference|see|line|lines)",
        "",
        remainder,
        flags=re.IGNORECASE,
    )
    return not remainder.strip()


def _sample_claim_units(indexed: list[CandidateClaimUnit]) -> tuple[CandidateClaimUnit, ...]:
    if len(indexed) <= MAX_CLAIM_UNITS:
        selected = indexed
    else:
        selected = _bounded_high_risk_claim_sample(indexed)
    total = 0
    bounded: list[CandidateClaimUnit] = []
    for unit in selected:
        if bounded and total + len(unit.text) > MAX_CLAIM_TOTAL_CHARS:
            continue
        bounded.append(unit)
        total += len(unit.text)
    return tuple(bounded)


def _bounded_high_risk_claim_sample(indexed: list[CandidateClaimUnit]) -> list[CandidateClaimUnit]:
    """Keep risk-bearing claims and their neighbors before fair sampling.

    Reviewer output is bounded, but the cap must not randomly drop the very
    claims that adjudicate user constraints or artifact reconciliation.  Stable
    original IDs remain the address; this only changes which IDs are projected.
    """

    selected_indices: set[int] = set()
    for index, unit in enumerate(indexed):
        if not _is_high_risk_candidate_claim(unit.text):
            continue
        for neighbor in (index - 1, index, index + 1):
            if 0 <= neighbor < len(indexed):
                selected_indices.add(neighbor)
    if len(selected_indices) > MAX_CLAIM_UNITS:
        selected_indices = set(_evenly_sample_indices(sorted(selected_indices), MAX_CLAIM_UNITS))
    remaining = MAX_CLAIM_UNITS - len(selected_indices)
    if remaining > 0:
        candidates = [index for index in range(len(indexed)) if index not in selected_indices]
        selected_indices.update(_evenly_sample_indices(candidates, remaining))
    return [indexed[index] for index in sorted(selected_indices)]


def _evenly_sample_indices(indices: list[int], limit: int) -> list[int]:
    if limit <= 0 or not indices:
        return []
    if len(indices) <= limit:
        return list(indices)
    if limit == 1:
        return [indices[0]]
    return [
        indices[round(position * (len(indices) - 1) / (limit - 1))]
        for position in range(limit)
    ]


def _is_high_risk_candidate_claim(text: str) -> bool:
    compact = " ".join((text or "").split())
    return any(pattern.search(compact) for pattern in _HIGH_RISK_CLAIM_PATTERNS)


def _is_table_row(line: str) -> bool:
    return line.count("|") >= 2


def _is_table_separator(line: str) -> bool:
    return bool(re.fullmatch(r"\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?", line))


_SOURCE_PATH_SUFFIXES = (
    "bash|c|cc|cpp|css|go|gradle|h|hpp|html?|java|js|jsx|json|kt|kts|md|markdown|png|jpe?g|gif|webp|"
    "properties|py|rs|scss|sh|sql|svelte|toml|ts|tsx|vue|xml|ya?ml|zsh"
)


_HIGH_RISK_CLAIM_PATTERNS = (
    re.compile(
        r"(?:\b(?:highest\s+priority|takes\s+precedence|authoritative\s+source|source\s+of\s+truth|override[sd]?)\b|"
        r"(?:最高优先级|优先于|以.{0,24}为准|权威来源|最终依据|覆盖其他来源))",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"(?:\b(?:conflict|inconsistent|not\s+consistent|difference|discrepanc(?:y|ies)|unresolved|consistent|reconciled|resolved)\b|"
        r"(?:冲突|矛盾|不一致|差异|未消解|未解决|待确认|一致|不矛盾|调和|解决))",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"(?:\b(?:document|markdown|html|prototype|image|screenshot|artifact|source)\b|"
        r"(?:文档|图片|图像|截图|示例图|原型|资料|来源)).{0,80}"
        r"(?:\b(?:blank|empty|not\s+filled|unfilled|missing|shows?|displays?|visible|populated|value)\b|"
        r"(?:留空|空值|未填|未填写|未显示|显示|可见|有值|具体值|填入))",
        flags=re.IGNORECASE,
    ),
)

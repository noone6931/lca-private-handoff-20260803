"""Typed stance and evidence policy for multi-artifact reconciliation."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Literal, Mapping

from .document_identity import document_artifact_identity

if TYPE_CHECKING:
    from .explore_handoff import ClaimEvidenceItem, ExploreHandoff


DocumentReconciliationStance = Literal[
    "reported_unresolved",
    "conditional_reconciliation",
    "asserted_reconciled",
    "explicitly_supported_reconciliation",
]


@dataclass(frozen=True)
class DocumentConsistencyAssessment:
    """A reviewer's typed statement about a document reconciliation claim."""

    stance: DocumentReconciliationStance
    conflict_evidence_ids: tuple[str, ...] = ()
    supporting_evidence_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "stance": self.stance,
            "conflict_evidence_ids": list(self.conflict_evidence_ids),
            "supporting_evidence_ids": list(self.supporting_evidence_ids),
        }


class DocumentConsistencyValidationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


MAX_REWRITE_CONTEXT_CHARS = 4200
MAX_REWRITE_REQUEST_CHARS = 700
MAX_REWRITE_PRIMARY_ITEM_SUMMARY_CHARS = 180
MAX_REWRITE_OPTIONAL_ITEM_SUMMARY_CHARS = 220
MAX_REWRITE_PATH_CHARS = 180
MAX_REWRITE_ROOT_CHARS = 140
MAX_REWRITE_SCOPE_CHARS = 80


def parse_document_consistency_assessment(
    raw: object,
    *,
    evidence_ids: Iterable[str],
) -> DocumentConsistencyAssessment:
    """Parse a bounded reviewer stance without accepting arbitrary evidence ids."""

    if not isinstance(raw, Mapping):
        raise DocumentConsistencyValidationError("document_consistency_missing")
    expected = {"stance", "conflict_evidence_ids", "supporting_evidence_ids"}
    if set(raw) != expected:
        raise DocumentConsistencyValidationError("document_consistency_keys_invalid")
    stance = raw.get("stance")
    if stance not in {
        "reported_unresolved",
        "conditional_reconciliation",
        "asserted_reconciled",
        "explicitly_supported_reconciliation",
    }:
        raise DocumentConsistencyValidationError("document_consistency_stance_invalid")
    known = set(evidence_ids)
    conflicts = _unique_known_ids(raw.get("conflict_evidence_ids"), known, "document_conflict_evidence")
    supports = _unique_known_ids(raw.get("supporting_evidence_ids"), known, "document_supporting_evidence")
    if set(conflicts) & set(supports):
        raise DocumentConsistencyValidationError("document_consistency_evidence_roles_overlap")
    if stance != "explicitly_supported_reconciliation" and supports:
        raise DocumentConsistencyValidationError("document_consistency_support_requires_explicit_stance")
    return DocumentConsistencyAssessment(stance, conflicts, supports)


def validate_document_consistency_assessment(
    assessment: DocumentConsistencyAssessment,
    handoff: "ExploreHandoff",
    *,
    candidate: str,
    verdict: str,
) -> str | None:
    """Return a typed failure code when a pass would overstate reconciliation.

    This is intentionally narrower than semantic review.  The reviewer decides
    which candidate stance it observed; Runtime only verifies that its cited
    artifact ids exist and that an asserted reconciliation has direct,
    non-visual lifecycle/precedence evidence.
    """

    by_id = {item.evidence_id: item for item in handoff.items}
    conflicts = tuple(by_id[item_id] for item_id in assessment.conflict_evidence_ids)
    if any(not _is_document_observation(item) for item in conflicts):
        return "document_conflict_evidence_not_observation"
    candidate_stance = candidate_reconciliation_stance(candidate)
    if (assessment.conflict_evidence_ids or candidate_stance is not None) and _distinct_artifact_count(conflicts) < 2:
        return "document_conflict_evidence_insufficient"
    if assessment.stance in {"conditional_reconciliation", "asserted_reconciled", "explicitly_supported_reconciliation"}:
        if _distinct_artifact_count(conflicts) < 2:
            return "document_conflict_evidence_insufficient"
    if verdict == "pass" and assessment.conflict_evidence_ids and candidate_stance is None:
        return "document_conflict_disposition_missing"
    if verdict == "pass" and assessment.stance in {"reported_unresolved", "conditional_reconciliation"} and candidate_stance == "asserted_reconciled":
        return "document_consistency_stance_mismatch"
    if (
        verdict == "pass"
        and candidate_stance == "asserted_reconciled"
        and assessment.stance != "explicitly_supported_reconciliation"
    ):
        return "document_reconciliation_unsupported"
    if verdict != "pass" or assessment.stance != "explicitly_supported_reconciliation":
        return None
    if not assessment.supporting_evidence_ids:
        return "document_reconciliation_support_missing"
    supports = tuple(by_id[item_id] for item_id in assessment.supporting_evidence_ids)
    if not all(_is_explicit_reconciliation_support(item) for item in supports):
        return "document_reconciliation_support_invalid"
    return None


def unresolved_document_conflict_items(
    handoff: "ExploreHandoff",
    assessment: DocumentConsistencyAssessment | None,
) -> tuple["ClaimEvidenceItem", ...]:
    """Return only reviewer-addressed conflict observations for a partial report."""

    if assessment is None or assessment.stance not in {"reported_unresolved", "conditional_reconciliation", "asserted_reconciled"}:
        return ()
    by_id = {item.evidence_id: item for item in handoff.items}
    return tuple(by_id[item_id] for item_id in assessment.conflict_evidence_ids if item_id in by_id)


def document_consistency_rewrite_context(
    handoff: "ExploreHandoff",
    assessment: DocumentConsistencyAssessment,
) -> tuple[str, ...]:
    """Render bounded typed context for a document-consistency rewrite.

    Evidence ids are only meaningful within the handoff that produced the
    assessment.  This helper projects the cited observations back to the
    primary model without trusting reviewer free-text actions as facts.
    """

    by_id = {item.evidence_id: item for item in handoff.items}
    conflict_items = tuple(
        by_id[item_id]
        for item_id in assessment.conflict_evidence_ids
        if item_id in by_id and _is_document_observation(by_id[item_id])
    )
    support_items = tuple(
        by_id[item_id]
        for item_id in assessment.supporting_evidence_ids
        if item_id in by_id and _is_explicit_reconciliation_support(by_id[item_id])
    )
    lines = [
        "Typed document-consistency context from the same reviewer handoff:",
        f"- Mandatory user request excerpt: {_clip_for_rewrite(handoff.request, MAX_REWRITE_REQUEST_CHARS)}",
        "- Treat that request as a hard constraint; do not invent source precedence, artifact lifecycle, or artifact role.",
        (
            f"- Reviewer stance: {assessment.stance}; cited conflict observations={len(conflict_items)}; "
            f"valid lifecycle/precedence support observations={len(support_items)}."
        ),
    ]
    if conflict_items:
        lines.append("- Cited conflict observations:")
        lines.extend(f"  * {_format_rewrite_item(item, summary_limit=MAX_REWRITE_PRIMARY_ITEM_SUMMARY_CHARS)}" for item in conflict_items[:2])
    if support_items:
        lines.append("- Cited lifecycle/precedence support observations:")
        lines.extend(f"  * {_format_rewrite_item(item, summary_limit=MAX_REWRITE_PRIMARY_ITEM_SUMMARY_CHARS)}" for item in support_items[:2])
        lines.append("- Any reconciliation may use only the cited support observations and only within their stated scope.")
    else:
        lines.append("- No valid supporting evidence in this handoff establishes artifact lifecycle, role, or precedence.")
    if not support_items:
        lines.append(
            "- Required disposition: restate each cited side as an observation, state that artifact role/lifecycle/precedence "
            "is not established, and keep the discrepancy unresolved or pending confirmation."
        )
        lines.append(
            "- Do not describe either artifact as mockup, reference-only, example-only, historical, later, final, "
            "offline-filled, authoritative, stronger, or the source of truth unless a cited support id explicitly says so."
        )
    optional_lines: list[str] = []
    if len(conflict_items) > 2:
        optional_lines.append("- Additional cited conflict observations, if space permits:")
        optional_lines.extend(
            f"  * {_format_rewrite_item(item, summary_limit=MAX_REWRITE_OPTIONAL_ITEM_SUMMARY_CHARS)}"
            for item in conflict_items[2:]
        )
    if len(support_items) > 2:
        optional_lines.append("- Additional cited support observations, if space permits:")
        optional_lines.extend(
            f"  * {_format_rewrite_item(item, summary_limit=MAX_REWRITE_OPTIONAL_ITEM_SUMMARY_CHARS)}"
            for item in support_items[2:]
        )
    return _bounded_rewrite_lines(lines, optional_lines)


def explicit_reconciliation_excerpt(value: str) -> str | None:
    """Return a bounded visible excerpt only for explicit lifecycle/precedence text."""

    compact = " ".join((value or "").split())
    for pattern in _RECONCILIATION_SUPPORT_PATTERNS:
        match = re.search(pattern, compact, flags=re.IGNORECASE)
        if match is None:
            continue
        start = max(0, match.start() - 160)
        end = min(len(compact), match.end() + 160)
        return compact[start:end]
    return None


def candidate_reconciliation_stance(candidate: str) -> DocumentReconciliationStance | None:
    """Classify only explicit reconciliation clauses, never artifact business content."""

    clauses = _candidate_clauses(candidate)
    saw_conditional = False
    saw_unresolved = False
    for index, clause in enumerate(clauses):
        assertions = _positive_reconciliation_matches(clause)
        if not assertions:
            if _EXPLICIT_CONFLICT_MARKER.search(clause):
                saw_unresolved = True
            continue
        conditional = bool(_CONDITIONAL_MARKER.search(clause))
        unresolved = bool(_UNRESOLVED_MARKER.search(clause)) or (
            index + 1 < len(clauses) and bool(_UNRESOLVED_MARKER.search(clauses[index + 1]))
        )
        if conditional and unresolved:
            saw_conditional = True
            continue
        return "asserted_reconciled"
    if saw_conditional:
        return "conditional_reconciliation"
    return "reported_unresolved" if saw_unresolved else None


def _unique_known_ids(value: object, known: set[str], prefix: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 8:
        raise DocumentConsistencyValidationError(f"{prefix}_invalid")
    if not all(isinstance(item, str) and item in known for item in value):
        raise DocumentConsistencyValidationError(f"{prefix}_unknown")
    if len(set(value)) != len(value):
        raise DocumentConsistencyValidationError(f"{prefix}_duplicate")
    return tuple(value)


def _is_document_observation(item: ClaimEvidenceItem) -> bool:
    return item.outcome == "ok" and item.tool in {"read_file", "inspect_image"}


def _distinct_artifact_count(items: tuple[ClaimEvidenceItem, ...]) -> int:
    return len({_document_artifact_identity(item) for item in items})


def _document_artifact_identity(item: ClaimEvidenceItem) -> tuple[str, str]:
    return document_artifact_identity(root=item.root, path=item.path, identity_path=item.identity_path)


def _is_explicit_reconciliation_support(item: ClaimEvidenceItem) -> bool:
    """Accept only document text that explicitly states lifecycle or precedence.

    Image observations describe pixels, not the author's intended workflow, so
    they never establish why two artifacts should be reconciled.
    """

    if item.classification != "document_reconciliation_support" or item.tool != "read_file" or item.outcome != "ok":
        return False
    return explicit_reconciliation_excerpt(item.summary) is not None


def _format_rewrite_item(item: ClaimEvidenceItem, *, summary_limit: int) -> str:
    parts = [
        f"evidence_id={item.evidence_id}",
        f"tool={item.tool}",
        f"path={_clip_for_rewrite(item.path or '(unknown)', MAX_REWRITE_PATH_CHARS)}",
        f"root={_clip_for_rewrite(item.root or '(unknown)', MAX_REWRITE_ROOT_CHARS)}",
        f"scope={_clip_for_rewrite(item.scope or '(unknown)', MAX_REWRITE_SCOPE_CHARS)}",
        f"summary={_safe_rewrite_summary(item.summary, summary_limit)}",
    ]
    return "; ".join(parts)


def _safe_rewrite_summary(value: str, limit: int) -> str:
    compact = " ".join((value or "").split())
    if _looks_like_embedded_binary_payload(compact):
        return "[omitted data-url/base64 payload; use path/tool provenance only]"
    return _clip_for_rewrite(compact, limit)


def _looks_like_embedded_binary_payload(value: str) -> bool:
    if re.search(r"data:[^,\s;]+(?:;[^,\s]+)*;base64,", value, flags=re.IGNORECASE):
        return True
    return bool(re.search(r"\b[A-Za-z0-9+/]{180,}={0,2}\b", value))


def _clip_for_rewrite(value: str, limit: int) -> str:
    compact = " ".join((value or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "..."


def _bounded_rewrite_lines(mandatory_lines: list[str], optional_lines: list[str]) -> tuple[str, ...]:
    rendered = list(mandatory_lines)
    while _joined_context_len(rendered) > MAX_REWRITE_CONTEXT_CHARS and rendered:
        # The constants above should keep mandatory lines under budget.  If a
        # future path/root format violates that assumption, remove the least
        # important mandatory variable line rather than returning malformed
        # partial metadata.
        removable = next((index for index, line in reversed(tuple(enumerate(rendered))) if line.startswith("  * ")), None)
        if removable is None:
            break
        del rendered[removable]
    for line in optional_lines:
        if _joined_context_len((*rendered, line)) > MAX_REWRITE_CONTEXT_CHARS:
            continue
        rendered.append(line)
    return tuple(rendered)


def _joined_context_len(lines: Iterable[str]) -> int:
    items = tuple(lines)
    if not items:
        return 0
    return sum(len(line) for line in items) + len(items) - 1


_RECONCILIATION_SUPPORT_PATTERNS = (
    r"\b(after|once|upon)\b.{0,96}\b(manual completion|manual processing|completion|finali[sz]ed)\b",
    r"\b(takes precedence|supersedes|overrides|authoritative source)\b",
    r"(?:人工处理|线下处理|手工完成).{0,48}(?:后|之后).{0,48}(?:示例|截图|图|页面)",
    r"(?:示例|截图|图|页面).{0,48}(?:为准|优先于|覆盖).{0,48}(?:文档|规范|说明)",
)

_ASSERTED_RECONCILIATION_MARKER = re.compile(
    r"(?:\b(?:no\s+conflict|not\s+(?:a\s+)?conflict|consistent|reconciled|resolved|aligned)\b|"
    r"\b(?:is|are)\b.{0,48}\b(?:completed|final|demonstration|later)\s+state\b|"
    r"\b(?:highest\s+priority|takes\s+precedence|supersedes|overrides|authoritative\s+source|source\s+of\s+truth)\b|"
    r"(?:无|没有|并非).{0,12}(?:冲突|矛盾)|不矛盾|(?:整体)?一致|已解决|(?:完成态|最终态|演示态|后期完成态)|"
    r"(?:最高优先级|优先于|以.{0,24}为准|权威来源|最终依据|覆盖其他来源))",
    flags=re.IGNORECASE,
)
_SCOPED_RECONCILIATION_MARKER = re.compile(
    r"(?:\b(?:completed|final|demonstration|later)\s+state\b|"
    r"\b(?:highest\s+priority|takes\s+precedence|supersedes|overrides|authoritative\s+source|source\s+of\s+truth)\b|"
    r"(?:完成态|最终态|演示态|后期完成态|最高优先级|优先于|以.{0,24}为准|权威来源|最终依据|覆盖其他来源))",
    flags=re.IGNORECASE,
)
_INTRINSIC_RECONCILIATION_MARKER = re.compile(
    r"(?:\b(?:no\s+conflict|not\s+(?:a\s+)?conflict|consistent|reconciled)\b|"
    r"(?:无|没有|并非).{0,12}(?:冲突|矛盾)|不矛盾)",
    flags=re.IGNORECASE,
)
_RELATIONAL_RECONCILIATION_MARKER = re.compile(
    r"(?:\b(?:resolved|aligned)\b|(?:整体)?一致|已解决)",
    flags=re.IGNORECASE,
)
_TWO_ARTIFACT_RELATION_MARKER = re.compile(
    r"(?:\b(?:A\s+and\s+B|(?:both|two|the\s+two|multiple)\s+(?:artifacts|sources|documents|materials))\b|"
    r"\b(?:document|markdown|html|prototype|image|screenshot|artifact|source|spec|policy)\b"
    r".{0,24}\b(?:and|vs\.?|versus)\b.{0,24}"
    r"\b(?:document|markdown|html|prototype|image|screenshot|artifact|source|spec|policy)\b|"
    r"两份(?:资料|文档)|两个(?:资料|文档|来源)|二者|两者|"
    r"(?:文档|需求|规范|说明|资料|来源|原型|页面|图片|图像|截图|示例图|HTML|Markdown)"
    r".{0,16}(?:和|与|及|以及|、|/|对比|相比).{0,16}"
    r"(?:文档|需求|规范|说明|资料|来源|原型|页面|图片|图像|截图|示例图|HTML|Markdown))",
    flags=re.IGNORECASE,
)
_ARTIFACT_FAMILY_PATTERNS = (
    ("document", re.compile(r"\b(?:document|markdown|md|spec|policy|requirement|requirements)\b|需求文档|文档|需求|规范|说明", re.IGNORECASE)),
    ("prototype", re.compile(r"\b(?:html|prototype|page)\b|原型|页面", re.IGNORECASE)),
    ("image", re.compile(r"\b(?:image|screenshot|picture|photo|png|jpg|jpeg)\b|图片|图像|截图|示例图", re.IGNORECASE)),
)
_EXPLICIT_CONFLICT_MARKER = re.compile(
    r"(?:\b(?:not\s+consistent|in\s+conflict|inconsistent|conflict\s+remains|source\s+differen(?:ce|ces)|artifact\s+differen(?:ce|ces)|discrepanc(?:y|ies)|(?:document|image|artifact|source)s?.{0,32}\bdiffer(?:s|ent)?)\b|"
    r"不一致|存在冲突|仍.{0,8}(?:冲突|矛盾)|(?:冲突|矛盾).{0,8}(?:未消解|未解决|待确认|不明确)|"
    r"(?:资料|来源|文档|图片|图像|原型).{0,16}(?:差异|不一致)|差异.{0,16}(?:未消解|待确认|不明确))",
    flags=re.IGNORECASE,
)
_CONDITIONAL_MARKER = re.compile(r"(?:\b(?:if|may|might|could|perhaps)\b|如果|若|可能|或许|可由|可以)", flags=re.IGNORECASE)
_UNRESOLVED_MARKER = re.compile(
    r"(?:\b(?:unresolved|unclear|not\s+specified|not\s+established|pending\s+confirmation)\b|"
    r"(?:未说明|不明确|无法确认|待确认|仍.{0,8}未解决|(?:可由|需由|需要|请).{0,24}(?:确认|决定)))",
    flags=re.IGNORECASE,
)


def _candidate_clauses(candidate: str) -> tuple[str, ...]:
    return tuple(piece.strip() for piece in re.split(r"[。；;\n]+", candidate or "") if piece.strip())


def _positive_reconciliation_matches(clause: str) -> tuple[re.Match[str], ...]:
    """Keep positive reconciliation claims, excluding the negated phrase itself.

    A clause can first report an artifact conflict and then overreach by
    declaring it resolved.  Treating any conflict marker as clause-wide
    unresolved would hide that latter assertion.  Conversely, ``not
    consistent`` contains the word ``consistent`` but is only a report of the
    conflict.  Match spans let us distinguish the two without a broad
    keyword-only rule.
    """

    conflicts = tuple(_EXPLICIT_CONFLICT_MARKER.finditer(clause))
    return tuple(
        match
        for match in _ASSERTED_RECONCILIATION_MARKER.finditer(clause)
        if not any(match.start() < conflict.end() and conflict.start() < match.end() for conflict in conflicts)
        and _assertion_is_document_scoped(clause, match)
    )


def _assertion_is_document_scoped(clause: str, match: re.Match[str]) -> bool:
    if _INTRINSIC_RECONCILIATION_MARKER.search(match.group(0)):
        return True
    if (
        _SCOPED_RECONCILIATION_MARKER.search(match.group(0)) is None
        and _RELATIONAL_RECONCILIATION_MARKER.search(match.group(0)) is None
    ):
        return True
    if _EXPLICIT_CONFLICT_MARKER.search(clause):
        return True
    if _TWO_ARTIFACT_RELATION_MARKER.search(clause):
        return True
    return len(_artifact_families(clause)) >= 2


def _artifact_families(clause: str) -> set[str]:
    return {family for family, pattern in _ARTIFACT_FAMILY_PATTERNS if pattern.search(clause)}

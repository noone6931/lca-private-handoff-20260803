"""Typed stance and evidence policy for multi-artifact reconciliation."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Literal, Mapping

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
    if assessment.stance in {"conditional_reconciliation", "asserted_reconciled", "explicitly_supported_reconciliation"}:
        if len(conflicts) < 2:
            return "document_conflict_evidence_insufficient"
    candidate_stance = candidate_reconciliation_stance(candidate)
    if verdict == "pass" and assessment.stance in {"reported_unresolved", "conditional_reconciliation"} and candidate_stance == "asserted_reconciled":
        return "document_consistency_stance_mismatch"
    if verdict == "pass" and assessment.stance == "asserted_reconciled":
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


def _is_explicit_reconciliation_support(item: ClaimEvidenceItem) -> bool:
    """Accept only document text that explicitly states lifecycle or precedence.

    Image observations describe pixels, not the author's intended workflow, so
    they never establish why two artifacts should be reconciled.
    """

    if item.classification != "document_reconciliation_support" or item.tool != "read_file" or item.outcome != "ok":
        return False
    return explicit_reconciliation_excerpt(item.summary) is not None


_RECONCILIATION_SUPPORT_PATTERNS = (
    r"\b(after|once|upon)\b.{0,96}\b(manual completion|manual processing|completion|finali[sz]ed)\b",
    r"\b(takes precedence|supersedes|overrides|authoritative source)\b",
    r"(?:人工处理|线下处理|手工完成).{0,48}(?:后|之后).{0,48}(?:示例|截图|图|页面)",
    r"(?:示例|截图|图|页面).{0,48}(?:为准|优先于|覆盖).{0,48}(?:文档|规范|说明)",
)

_ASSERTED_RECONCILIATION_MARKER = re.compile(
    r"(?:\b(?:no\s+conflict|not\s+(?:a\s+)?conflict|consistent|reconciled|resolved|aligned)\b|"
    r"\b(?:is|are)\b.{0,48}\b(?:completed|final|demonstration|later)\s+state\b|"
    r"(?:无|没有|并非).{0,12}(?:冲突|矛盾)|不矛盾|整体一致|已解决|(?:完成态|最终态|演示态|后期完成态))",
    flags=re.IGNORECASE,
)
_EXPLICIT_CONFLICT_MARKER = re.compile(
    r"(?:\b(?:not\s+consistent|in\s+conflict|inconsistent|conflict\s+remains)\b|不一致|存在冲突|仍.{0,8}(?:冲突|矛盾))",
    flags=re.IGNORECASE,
)
_CONDITIONAL_MARKER = re.compile(r"(?:\b(?:if|may|might|could|perhaps)\b|如果|若|可能|或许)", flags=re.IGNORECASE)
_UNRESOLVED_MARKER = re.compile(
    r"(?:\b(?:unresolved|unclear|not\s+specified|not\s+established|pending\s+confirmation)\b|"
    r"(?:未说明|不明确|无法确认|待确认|仍.{0,8}未解决))",
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
    )

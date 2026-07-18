"""Typed stance and evidence policy for multi-artifact reconciliation."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Literal, Mapping

from ..document_identity import document_artifact_identity

if TYPE_CHECKING:
    from .handoff import ClaimEvidenceItem, ExploreHandoff


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


@dataclass(frozen=True)
class DocumentConsistencyFindingIssue:
    code: str
    claim_ids: tuple[str, ...] = ()


class DocumentConsistencyValidationError(ValueError):
    def __init__(self, code: str, diagnostics: Mapping[str, object] | None = None) -> None:
        self.code = code
        self.diagnostics = dict(diagnostics or {})
        super().__init__(code)


DOCUMENT_CONSISTENCY_KEYS = ("stance", "conflict_evidence_ids", "supporting_evidence_ids")
DOCUMENT_CONSISTENCY_STANCES = (
    "reported_unresolved",
    "conditional_reconciliation",
    "asserted_reconciled",
    "explicitly_supported_reconciliation",
)
DOCUMENT_CONSISTENCY_REJECTION_CODES = frozenset(
    {
        "document_consistency_missing",
        "document_consistency_keys_invalid",
        "document_consistency_stance_invalid",
        "document_consistency_evidence_roles_overlap",
        "document_consistency_support_requires_explicit_stance",
        "document_consistency_finding_reconciles_conflict",
        "document_conflict_evidence_invalid",
        "document_conflict_evidence_unknown",
        "document_conflict_evidence_duplicate",
        "document_conflict_evidence_not_observation",
        "document_conflict_evidence_insufficient",
        "document_conflict_disposition_missing",
        "document_consistency_stance_mismatch",
        "document_reconciliation_unsupported",
        "document_reconciliation_support_missing",
        "document_reconciliation_support_invalid",
        "document_supporting_evidence_invalid",
        "document_supporting_evidence_unknown",
        "document_supporting_evidence_duplicate",
    }
)

MAX_REWRITE_CONTEXT_CHARS = 4200
MAX_REWRITE_REQUEST_CHARS = 700
MAX_REWRITE_PRIMARY_ITEM_SUMMARY_CHARS = 180
MAX_REWRITE_OPTIONAL_ITEM_SUMMARY_CHARS = 220
MAX_REWRITE_PATH_CHARS = 180
MAX_REWRITE_ROOT_CHARS = 140
MAX_REWRITE_SCOPE_CHARS = 80


def is_document_consistency_rejection_code(code: str) -> bool:
    """Return whether a reviewer output rejection belongs to this typed owner."""

    return code in DOCUMENT_CONSISTENCY_REJECTION_CODES


def document_consistency_schema(evidence_ids: Iterable[str]) -> dict[str, Any]:
    """Return the single source of truth for final consistency payload shape."""

    evidence_id = {"type": "string", "enum": list(evidence_ids)}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "stance": {
                "type": "string",
                "enum": list(DOCUMENT_CONSISTENCY_STANCES),
                "description": (
                    "How the candidate handles differences between supplied artifacts. Use reported_unresolved when "
                    "the answer keeps artifact role/lifecycle/precedence unresolved."
                ),
            },
            "conflict_evidence_ids": {
                "type": "array",
                "maxItems": 8,
                "items": evidence_id,
                "description": (
                    "Known evidence IDs for the artifact observations being compared. When the candidate reports, "
                    "conditions, or reconciles a source difference, cite at least two sides from distinct artifacts."
                ),
            },
            "supporting_evidence_ids": {
                "type": "array",
                "maxItems": 8,
                "items": evidence_id,
                "description": (
                    "Must be empty unless stance is explicitly_supported_reconciliation; never overlap with "
                    "conflict_evidence_ids; cite only independent non-visual read_file lifecycle/precedence support."
                ),
            },
        },
        "required": list(DOCUMENT_CONSISTENCY_KEYS),
    }


def document_consistency_rejection_hint(code: str) -> str:
    """Return a sanitized, executable final-submit correction hint."""

    if code == "document_consistency_keys_invalid":
        return (
            " document_consistency must be an object with exactly these keys: "
            "stance, conflict_evidence_ids, supporting_evidence_ids."
        )
    if code == "document_conflict_evidence_insufficient":
        return (
            " Accepted findings remain recorded. Submit only the final verdict again and set "
            "document_consistency.conflict_evidence_ids to at least two known document/image observation IDs from "
            "distinct artifacts when the candidate compares or preserves an artifact difference."
        )
    if code == "document_consistency_evidence_roles_overlap":
        return " conflict_evidence_ids and supporting_evidence_ids must be disjoint."
    if code == "document_consistency_support_requires_explicit_stance":
        return " supporting_evidence_ids must be [] unless stance is explicitly_supported_reconciliation."
    return ""


def parse_document_consistency_assessment(
    raw: object,
    *,
    evidence_ids: Iterable[str],
) -> DocumentConsistencyAssessment:
    """Parse a bounded reviewer stance without accepting arbitrary evidence ids."""

    if not isinstance(raw, Mapping):
        raise DocumentConsistencyValidationError("document_consistency_missing")
    expected = set(DOCUMENT_CONSISTENCY_KEYS)
    if set(raw) != expected:
        raise DocumentConsistencyValidationError(
            "document_consistency_keys_invalid",
            {
                "document_consistency_keys": sorted(str(key)[:64] for key in raw),
                "expected_document_consistency_keys": list(DOCUMENT_CONSISTENCY_KEYS),
            },
        )
    stance = raw.get("stance")
    if stance not in DOCUMENT_CONSISTENCY_STANCES:
        raise DocumentConsistencyValidationError("document_consistency_stance_invalid")
    known = set(evidence_ids)
    conflicts = _unique_known_ids(raw.get("conflict_evidence_ids"), known, "document_conflict_evidence")
    supports = _unique_known_ids(raw.get("supporting_evidence_ids"), known, "document_supporting_evidence")
    if set(conflicts) & set(supports):
        raise DocumentConsistencyValidationError("document_consistency_evidence_roles_overlap")
    if stance != "explicitly_supported_reconciliation" and supports:
        raise DocumentConsistencyValidationError("document_consistency_support_requires_explicit_stance")
    return DocumentConsistencyAssessment(stance, conflicts, supports)


def complete_document_consistency_assessment(
    assessment: DocumentConsistencyAssessment,
    handoff: "ExploreHandoff",
    *,
    candidate: str,
    finding_claim_ids: Iterable[str] = (),
) -> DocumentConsistencyAssessment:
    """Fill omitted conflict ids from the same bounded handoff when deterministic.

    The reviewer has already yielded candidate findings incrementally.  The
    final submit should not need to reconstruct a conflict matrix that Runtime
    already owns.  This helper only completes omitted conflict_evidence_ids
    when the candidate/stance actually involves artifact reconciliation and the
    handoff contains a unique two-sided artifact set.  A generic multi-document
    handoff is not enough; otherwise Runtime would guess which artifacts are in
    conflict.
    """

    candidate_stance = candidate_reconciliation_stance(candidate)
    needs_conflict_ids = candidate_stance is not None or assessment.stance in {
        "conditional_reconciliation",
        "asserted_reconciled",
        "explicitly_supported_reconciliation",
    }
    if not needs_conflict_ids or assessment.conflict_evidence_ids:
        return assessment
    canonical_ids = _canonical_document_conflict_ids(
        handoff,
        exclude=assessment.supporting_evidence_ids,
        finding_claim_ids=finding_claim_ids,
    )
    if _distinct_artifact_count(tuple(_items_by_id(handoff)[item_id] for item_id in canonical_ids)) < 2:
        return assessment
    return DocumentConsistencyAssessment(
        assessment.stance,
        canonical_ids[:8],
        assessment.supporting_evidence_ids,
    )


def validate_document_consistency_findings(
    assessment: DocumentConsistencyAssessment,
    findings: Iterable[Mapping[str, str]],
) -> str | None:
    issue = validate_document_consistency_finding_issue(assessment, findings)
    return issue.code if issue is not None else None


def validate_document_consistency_finding_issue(
    assessment: DocumentConsistencyAssessment,
    findings: Iterable[Mapping[str, str]],
) -> DocumentConsistencyFindingIssue | None:
    """Reject reviewer actions that contradict a typed unresolved conflict.

    Findings are advisory repair instructions, not evidence.  For unresolved
    or conditional artifact conflicts, an action may remove or downgrade a bad
    candidate claim, but it must not tell the primary model to invent a source
    priority, lifecycle, historical/current role, or resolution.
    """

    if assessment.stance == "explicitly_supported_reconciliation":
        return None
    invalid_claim_ids: list[str] = []
    saw_invalid_finding = False
    for finding in findings:
        claim = str(finding.get("claim") or "")
        issue = str(finding.get("issue") or "")
        action = str(finding.get("action") or "")
        if not _finding_targets_artifact_conflict(claim, issue):
            continue
        if _finding_reconciles_unresolved_conflict(issue, action):
            saw_invalid_finding = True
            claim_id = str(finding.get("claim_id") or "").strip()
            if claim_id:
                invalid_claim_ids.append(claim_id)
    if saw_invalid_finding:
        return DocumentConsistencyFindingIssue(
            "document_consistency_finding_reconciles_conflict",
            tuple(dict.fromkeys(invalid_claim_ids)),
        )
    return None


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
    global_candidate_stance = candidate_reconciliation_stance(candidate)
    candidate_stance = (
        candidate_reconciliation_stance_for_conflict(candidate, conflicts, handoff.items)
        if conflicts
        else global_candidate_stance
    )
    if (assessment.conflict_evidence_ids or global_candidate_stance is not None) and _distinct_artifact_count(conflicts) < 2:
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

    return _candidate_reconciliation_stance(candidate, clause_filter=lambda _clause: True)


def candidate_reconciliation_stance_for_conflict(
    candidate: str,
    conflict_items: Iterable["ClaimEvidenceItem"],
    all_items: Iterable["ClaimEvidenceItem"] = (),
) -> DocumentReconciliationStance | None:
    """Classify reconciliation only when scoped to the reviewer-cited artifact pair."""

    conflicts = tuple(conflict_items)
    if _distinct_artifact_count(conflicts) < 2:
        return candidate_reconciliation_stance(candidate)
    all_observations = tuple(item for item in all_items if _is_document_observation(item))
    comparable_items = _comparable_document_observations(all_observations, conflicts)
    return _candidate_reconciliation_stance(
        candidate,
        clause_filter=lambda clause: _clause_scopes_to_conflict_pair(clause, conflicts, comparable_items),
        contextual_clause_filter=lambda clause: _context_uniquely_scopes_to_conflict_pair(
            clause,
            conflicts,
            comparable_items,
        ),
    )


def _candidate_reconciliation_stance(
    candidate: str,
    *,
    clause_filter: Any,
    contextual_clause_filter: Any | None = None,
) -> DocumentReconciliationStance | None:
    clauses = _candidate_clauses(candidate)
    saw_conditional = False
    saw_unresolved = False
    for index, clause in enumerate(clauses):
        if not clause_filter(clause):
            prior_context = "。".join(clauses[max(0, index - 2) : index])
            contextual_clause = "。".join((*clauses[max(0, index - 2) : index], clause))
            if (
                contextual_clause_filter is None
                or not _ANAPHORIC_ARTIFACT_RELATION_MARKER.search(clause)
                or not prior_context
                or not contextual_clause_filter(contextual_clause)
            ):
                continue
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
        if unresolved and _assertions_only_reconcile_remaining_items(clause, assertions):
            saw_unresolved = True
            continue
        return "asserted_reconciled"
    if saw_conditional:
        return "conditional_reconciliation"
    return "reported_unresolved" if saw_unresolved else None


def _clause_scopes_to_conflict_pair(
    clause: str,
    conflict_items: tuple["ClaimEvidenceItem", ...],
    all_items: tuple["ClaimEvidenceItem", ...],
) -> bool:
    if _broad_artifact_relation(clause):
        return True
    comparable_items = _comparable_document_observations(all_items, conflict_items)
    mentioned = tuple(item for item in conflict_items if _clause_mentions_artifact(clause, item, conflict_items))
    if _distinct_artifact_count(mentioned) >= 2:
        return True
    mentioned_comparable = tuple(
        item for item in comparable_items if _clause_mentions_artifact(clause, item, comparable_items)
    )
    if (
        not mentioned_comparable
        and _GENERIC_ARTIFACT_DIFFERENCE_MARKER.search(clause)
        and _EXPLICIT_CONFLICT_MARKER.search(clause)
        and _UNRESOLVED_MARKER.search(clause)
    ):
        # A candidate that leaves the material differences broadly unresolved
        # necessarily leaves every reviewer-cited pair unresolved. Keep this
        # one-way: a generic reconciliation still needs an explicit pair or a
        # genuinely broad all-artifact relation.
        return True
    if _TWO_ARTIFACT_RELATION_MARKER.search(clause) and _distinct_artifact_count(conflict_items) == 2:
        specific_items = mentioned_comparable
        if specific_items:
            return _distinct_artifact_count(tuple(item for item in specific_items if item in conflict_items)) >= 2
        return _distinct_artifact_count(comparable_items) == 2
    if _ANAPHORIC_ARTIFACT_RELATION_MARKER.search(clause):
        return _distinct_artifact_count(comparable_items) == 2
    return False


def _context_uniquely_scopes_to_conflict_pair(
    clause: str,
    conflict_items: tuple["ClaimEvidenceItem", ...],
    all_items: tuple["ClaimEvidenceItem", ...],
) -> bool:
    comparable_items = _comparable_document_observations(all_items, conflict_items)
    mentioned = tuple(item for item in comparable_items if _clause_mentions_artifact(clause, item, comparable_items))
    mentioned_identities = {_document_artifact_identity(item) for item in mentioned}
    conflict_identities = {_document_artifact_identity(item) for item in conflict_items}
    return len(mentioned_identities) == 2 and mentioned_identities == conflict_identities


def _comparable_document_observations(
    all_items: tuple["ClaimEvidenceItem", ...],
    conflict_items: tuple["ClaimEvidenceItem", ...],
) -> tuple["ClaimEvidenceItem", ...]:
    observations = tuple(
        item
        for item in all_items
        if _is_document_observation(item) and item.classification != "document_reconciliation_support"
    )
    return observations or conflict_items


def _broad_artifact_relation(clause: str) -> bool:
    return bool(
        re.search(
            r"(?:\b(?:all|every|all\s+three)\b.{0,40}\b(?:artifacts|sources|documents|materials)\b|"
            r"\b(?:between|among)\b.{0,24}\b(?:artifacts|sources|documents|materials)\b|"
            r"\b(?:the\s+)?(?:artifacts|sources|documents|materials)\b.{0,60}"
            r"\b(?:consistent|reconciled|resolved|aligned|no\s+conflict|not\s+(?:a\s+)?conflict)\b|"
            r"(?:所有|全部|三份|各)(?:资料|文档|来源|材料)|(?:资料|文档|来源|材料)之间)",
            clause,
            flags=re.IGNORECASE,
        )
    )


def _clause_mentions_artifact(
    clause: str,
    item: "ClaimEvidenceItem",
    peer_items: Iterable["ClaimEvidenceItem"],
) -> bool:
    compact = clause.lower()
    ambiguous_aliases = _ambiguous_artifact_aliases(tuple(peer_items))
    exact_aliases = _artifact_exact_aliases(item)
    if any(alias and alias.lower() not in ambiguous_aliases and alias.lower() in compact for alias in exact_aliases):
        return True
    family = _artifact_family(item)
    peer_families = {_artifact_family(peer) for peer in peer_items}
    if len(peer_families) <= 1:
        return False
    same_family_items = tuple(peer for peer in peer_items if _artifact_family(peer) == family)
    if _distinct_artifact_count(same_family_items) > 1:
        return False
    return bool(family and _ARTIFACT_FAMILY_MARKERS.get(family, re.compile(r"$^")).search(clause))


def _ambiguous_artifact_aliases(items: tuple["ClaimEvidenceItem", ...]) -> set[str]:
    identities_by_alias: dict[str, set[tuple[str, str]]] = {}
    for item in items:
        identity = _document_artifact_identity(item)
        for alias in _artifact_exact_aliases(item):
            key = alias.lower()
            identities_by_alias.setdefault(key, set()).add(identity)
    return {alias for alias, identities in identities_by_alias.items() if len(identities) > 1}


def _artifact_exact_aliases(item: "ClaimEvidenceItem") -> tuple[str, ...]:
    aliases: list[str] = []
    for value in (item.path, item.identity_path):
        value = (value or "").strip()
        if not value:
            continue
        aliases.append(value)
        basename = value.replace("\\", "/").rsplit("/", 1)[-1]
        if basename and basename != value:
            aliases.append(basename)
        stem = basename.rsplit(".", 1)[0] if "." in basename else ""
        if stem and len(stem) >= 4:
            aliases.append(stem)
    return tuple(dict.fromkeys(aliases))


def _artifact_family(item: "ClaimEvidenceItem") -> str:
    provenance = f"{item.classification} {item.tool} {item.path} {item.identity_path}"
    for family, pattern in _ARTIFACT_FAMILY_PATTERNS:
        if pattern.search(provenance):
            return family
    for family, pattern in _ARTIFACT_FAMILY_PATTERNS:
        if pattern.search(item.summary):
            return family
    return "artifact"


def _unique_known_ids(value: object, known: set[str], prefix: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 8:
        raise DocumentConsistencyValidationError(f"{prefix}_invalid")
    if not all(isinstance(item, str) and item in known for item in value):
        raise DocumentConsistencyValidationError(f"{prefix}_unknown")
    if len(set(value)) != len(value):
        raise DocumentConsistencyValidationError(f"{prefix}_duplicate")
    return tuple(value)


def _items_by_id(handoff: "ExploreHandoff") -> dict[str, "ClaimEvidenceItem"]:
    return {item.evidence_id: item for item in handoff.items}


def _canonical_document_conflict_ids(
    handoff: "ExploreHandoff",
    *,
    exclude: Iterable[str] = (),
    finding_claim_ids: Iterable[str] = (),
) -> tuple[str, ...]:
    excluded = {item_id for item_id in exclude if item_id}
    claim_ids = {claim_id for claim_id in finding_claim_ids if claim_id}
    candidates = tuple(
        item
        for item in handoff.items
        if item.evidence_id not in excluded and _is_document_observation(item)
    )
    if claim_ids:
        bound = tuple(item for item in candidates if _item_binds_any_claim(item, claim_ids))
        if bound:
            return _unique_two_artifact_ids(bound)
    return _unique_two_artifact_ids(candidates)


def _item_binds_any_claim(item: "ClaimEvidenceItem", claim_ids: set[str]) -> bool:
    return item.claim_id in claim_ids or bool(set(item.claim_ids) & claim_ids)


def _unique_two_artifact_ids(items: tuple["ClaimEvidenceItem", ...]) -> tuple[str, ...]:
    first_by_artifact: dict[tuple[str, str], str] = {}
    for item in items:
        first_by_artifact.setdefault(_document_artifact_identity(item), item.evidence_id)
    if len(first_by_artifact) != 2:
        return ()
    return tuple(first_by_artifact.values())


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
    r"[\w./\\ \-\u4e00-\u9fff]+\.(?:md|markdown|html?|png|jpe?g|gif|webp)"
    r".{0,24}(?:\band\b|vs\.?|versus|和|与|及|以及|、|/|对比|相比).{0,24}"
    r"[\w./\\ \-\u4e00-\u9fff]+\.(?:md|markdown|html?|png|jpe?g|gif|webp)|"
    r"\b(?:document|markdown|html|prototype|image|screenshot|artifact|source|spec|policy)\b"
    r".{0,24}\b(?:and|vs\.?|versus)\b.{0,24}"
    r"\b(?:document|markdown|html|prototype|image|screenshot|artifact|source|spec|policy)\b|"
    r"两份(?:资料|文档)|两个(?:资料|文档|来源)|二者|两者|"
    r"(?:文档|需求|规范|说明|资料|来源|原型|页面|图片|图像|截图|示例图|HTML|Markdown)"
    r".{0,16}(?:和|与|及|以及|、|/|对比|相比).{0,16}"
    r"(?:文档|需求|规范|说明|资料|来源|原型|页面|图片|图像|截图|示例图|HTML|Markdown))",
    flags=re.IGNORECASE,
)
_ANAPHORIC_ARTIFACT_RELATION_MARKER = re.compile(
    r"(?:\b(?:they|them|their|both|the\s+two|these\s+two|discrepancy|conflict|"
    r"this\s+(?:discrepancy|conflict)|the\s+(?:discrepancy|conflict))\b|"
    r"这两份|上述两份|两者|二者|差异|冲突|矛盾|该(?:差异|冲突|矛盾)|这(?:项|个)(?:差异|冲突|矛盾))",
    flags=re.IGNORECASE,
)
_GENERIC_ARTIFACT_DIFFERENCE_MARKER = re.compile(
    r"(?:\b(?:artifact|source|document|material)s?\b.{0,16}\b(?:difference|discrepancy|conflict)s?\b|"
    r"(?:资料|来源|文档|材料)(?:之间|中的)?(?:差异|冲突|矛盾))",
    flags=re.IGNORECASE,
)
_ARTIFACT_FAMILY_PATTERNS = (
    ("document", re.compile(r"\b(?:document|markdown|md|spec|policy|requirement|requirements)\b|需求文档|文档|需求|规范|说明", re.IGNORECASE)),
    ("prototype", re.compile(r"\b(?:html|prototype|page)\b|原型|页面", re.IGNORECASE)),
    ("image", re.compile(r"\b(?:image|screenshot|picture|photo|png|jpg|jpeg)\b|图片|图像|截图|示例图", re.IGNORECASE)),
)
_ARTIFACT_FAMILY_MARKERS = {family: pattern for family, pattern in _ARTIFACT_FAMILY_PATTERNS}
_EXPLICIT_CONFLICT_MARKER = re.compile(
    r"(?:\b(?:not\s+consistent|in\s+conflict|inconsistent|conflicts?\s+with|conflict\s+remains|source\s+differen(?:ce|ces)|artifact\s+differen(?:ce|ces)|discrepanc(?:y|ies)|(?:document|image|artifact|source)s?.{0,32}\bdiffer(?:s|ent)?)\b|"
    r"不一致|存在冲突|未解决.{0,4}(?:差异|冲突|矛盾)|仍.{0,8}(?:冲突|矛盾)|(?:冲突|矛盾).{0,8}(?:未消解|未解决|待确认|不明确)|"
    r"(?:资料|来源|文档|图片|图像|原型).{0,16}(?:差异|不一致)|差异.{0,16}(?:未消解|待确认|不明确))",
    flags=re.IGNORECASE,
)
_CONDITIONAL_MARKER = re.compile(r"(?:\b(?:if|may|might|could|perhaps)\b|如果|若|可能|或许|可由|可以)", flags=re.IGNORECASE)
_UNRESOLVED_MARKER = re.compile(
    r"(?:\b(?:unresolved|unclear|not\s+specified|not\s+established|pending\s+confirmation)\b|"
    r"(?:未说明|不明确|无法确认|待确认|未消解|未解决|(?:可由|需由|需要|请).{0,24}(?:确认|决定)))",
    flags=re.IGNORECASE,
)
_EPISTEMIC_UNCERTAINTY_MATCH = re.compile(
    r"(?:尚)?无法(?:判定|判断|确认|说明).{0,24}(?:是否)?.{0,12}(?:冲突|矛盾)|"
    r"(?:cannot|unable\s+to).{0,24}(?:determine|confirm|establish).{0,24}(?:conflict|consistent|reconciled)",
    flags=re.IGNORECASE,
)
_NEGATED_RELATION_PREFIX = re.compile(
    r"(?:(?:未|没有|尚未).{0,20}(?:建立|说明|明确|确认)|"
    r"\b(?:no|not)\b.{0,28}\b(?:established|specified|confirmed)\b).{0,16}$",
    flags=re.IGNORECASE,
)
_REMAINING_ITEMS_PREFIX = re.compile(
    r"(?:其余|其他|剩余|除此之外|\b(?:other|remaining|the\s+rest)\b).{0,8}$",
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
        and not _is_epistemically_negated_reconciliation(clause, match)
        and _assertion_is_document_scoped(clause, match)
    )


def _is_epistemically_negated_reconciliation(clause: str, match: re.Match[str]) -> bool:
    matched = match.group(0)
    if _EPISTEMIC_UNCERTAINTY_MATCH.fullmatch(matched):
        return True
    prefix = clause[max(0, match.start() - 56) : match.start()]
    return bool(_NEGATED_RELATION_PREFIX.search(prefix))


def _assertions_only_reconcile_remaining_items(clause: str, assertions: tuple[re.Match[str], ...]) -> bool:
    if not assertions or not _EXPLICIT_CONFLICT_MARKER.search(clause):
        return False
    return all(
        _REMAINING_ITEMS_PREFIX.search(clause[max(0, match.start() - 20) : match.start()])
        for match in assertions
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


_FINDING_RECONCILIATION_ACTION_MARKER = re.compile(
    r"(?:\b(?:treat|describe|classify|assume|use|mark|call|make|consider)\b.{0,80}"
    r"\b(?:historical|history|current|mockup|reference|example-only|preview|completed|final|later|authoritative|precedence|priority)\b|"
    r"\b(?:authoritative|source\s+of\s+truth|takes\s+precedence|supersedes|overrides)\b|"
    r"(?:视为|当作|作为|认定|判定|说明为|解释为|属于).{0,50}(?:历史|当前|参考|示意|样例|预留|完成态|最终态|后期|优先|为准)|"
    r"(?:不影响|不构成).{0,50}(?:待确认|冲突|矛盾)|"
    r"(?:以.{0,24}为准|优先于|最高优先级|权威来源|最终依据|冲突.{0,12}(?:已解决|已消解)|不(?:冲突|矛盾)|整体一致))",
    flags=re.IGNORECASE,
)
_FINDING_REMOVAL_QUALIFIER = re.compile(
    r"(?:\b(?:remove|delete|downgrade|avoid|do\s+not|must\s+not|cannot|unsupported|unverified)\b|"
    r"删除|移除|降级|不要|不能|不得|未支持|未验证|无证据)",
    flags=re.IGNORECASE,
)


def _finding_reconciles_unresolved_conflict(issue: str, action: str) -> bool:
    for clause in _finding_instruction_clauses(action):
        if _FINDING_RECONCILIATION_ACTION_MARKER.search(clause) and not _FINDING_REMOVAL_QUALIFIER.search(clause):
            return True
    # Some reviewers put the actionable instruction in the issue field and a
    # vague "fix it" in action.  Keep this narrow: only reject issue text that
    # itself has an imperative reconciliation marker.
    return any(
        _FINDING_RECONCILIATION_ACTION_MARKER.search(clause) and not _FINDING_REMOVAL_QUALIFIER.search(clause)
        for clause in _finding_instruction_clauses(issue)
    )


def _finding_instruction_clauses(value: str) -> tuple[str, ...]:
    clauses: list[str] = []
    for piece in re.split(r"[。；;，,、\n]+", " ".join((value or "").split())):
        for clause in re.split(r"\b(?:and|then)\b|(?:并且|并将|并|然后|再将)", piece):
            stripped = clause.strip()
            if stripped:
                clauses.append(stripped)
    return tuple(clauses)


def _finding_targets_artifact_conflict(claim: str, issue: str) -> bool:
    text = "。".join(part for part in (claim, issue) if part)
    if not text.strip():
        return False
    if candidate_reconciliation_stance(text) in {"reported_unresolved", "conditional_reconciliation", "asserted_reconciled"}:
        return True
    return any(
        _EXPLICIT_CONFLICT_MARKER.search(clause)
        and (_TWO_ARTIFACT_RELATION_MARKER.search(clause) or _artifact_families(clause))
        for clause in _candidate_clauses(text)
    )

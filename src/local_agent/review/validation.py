"""Typed reviewer payload parsing and semantic closure validation."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping

from .document_consistency import DocumentConsistencyValidationError
from .document_consistency import complete_document_consistency_assessment
from .document_consistency import document_consistency_rewrite_context
from .document_consistency import parse_document_consistency_assessment
from .document_consistency import validate_document_consistency_finding_issue
from .handoff import ExploreHandoff
from .readiness import ImplementationReadinessValidationError
from .readiness import parse_implementation_readiness_assessment
from .readiness import validate_implementation_readiness_assessment
from .claims import _clip
from .claims import _normalize_claim_binding
from .claims import _normalize_markdown
from .contract import _shape_diagnostics
from .types import CandidateClaimUnit
from .types import MAX_REVIEWER_FINDINGS
from .types import MAX_REVIEWER_RESPONSE_CHARS
from .types import ReviewerFinding
from .types import ReviewerResult
from .types import ReviewerValidationError


def parse_reviewer_result(
    content: object,
    *,
    claim_units: tuple[CandidateClaimUnit, ...],
    document_consistency: bool = False,
    implementation_readiness: bool = False,
    evidence_ids: tuple[str, ...] = (),
    required_candidate_claim_ids: tuple[str, ...] = (),
) -> ReviewerResult:
    if not isinstance(content, str) or not content.strip():
        raise ReviewerValidationError("missing_json")
    if len(content) > MAX_REVIEWER_RESPONSE_CHARS:
        raise ReviewerValidationError("response_too_large", {"response_chars": len(content)})
    try:
        raw = _json_object(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ReviewerValidationError("malformed_json") from None
    return parse_reviewer_payload(
        raw,
        claim_units=claim_units,
        document_consistency=document_consistency,
        implementation_readiness=implementation_readiness,
        evidence_ids=evidence_ids,
        required_candidate_claim_ids=required_candidate_claim_ids,
    )


def reviewer_rewrite_message(
    result: ReviewerResult,
    *,
    profile: str | None = None,
    handoff: ExploreHandoff | None = None,
) -> str:
    """Render a bounded runtime instruction, not the reviewer's raw transcript."""

    disposition = (
        "The reviewer could not verify the disputed factual conclusion. Still answer the original request, but "
        "state the owner or implementation as scoped and unlocated, and keep observed code only as analogous evidence."
        if result.verdict == "unverified"
        else "Apply every finding while preserving the supported parts of the answer."
    )
    lines = [
        "[Read-only evidence review]",
        "Revise the candidate answer once, without calling tools. Preserve only claims supported by the existing handoff.",
        disposition,
        "Do not call an analogous/reusable candidate the verified owner. Keep unlocated owner/DDL/template/API facts as unverified, and label new design as proposal.",
        "Reviewer issue/action text is advisory, not evidence. Never copy a reviewer-suggested class, field, state, service, source behavior, or business rule into the answer unless the handoff already supports it.",
        "Prefer deleting or narrowly downgrading a challenged claim over inventing a replacement. Keep the rewrite concise; do not add new implementation-completeness judgments or design options just to answer a finding.",
    ]
    if profile == "owner_impact":
        lines.append(
            "Do not introduce concrete class, table, path, service, endpoint, field, or numbering names that were not already supported by the handoff."
        )
    elif profile == "design":
        lines.append(
            "Design proposals may be conceptual, but every concrete repository name must remain an observed fact or be explicitly marked unverified."
        )
    if result.implementation_readiness is not None:
        readiness = result.implementation_readiness
        dimension_summary = "; ".join(
            f"{key}={readiness.dimension(key).status}:"
            f"{','.join(readiness.dimension(key).claim_ids) or 'none'}"
            for key in (
                "owner",
                "data_contract_or_source",
                "write_target",
                "test_entry",
                "rollback_boundary",
            )
        )
        lines.append(
            "Implementation-readiness disposition: "
            f"status={readiness.status}; dimensions=[{dimension_summary}]; "
            f"unsupported_identifier_claim_ids={','.join(readiness.unsupported_identifier_claim_ids) or 'none'}."
        )
        lines.append(
            "If readiness is blocked or conditional, do not present an implementation slice as selected/ready; keep it as "
            "blocked, conditional, or a follow-up investigation direction."
        )
    elif profile == "document_consistency":
        lines.append(
            "Do not call conflicting artifact observations consistent, completed, or a later state unless the handoff explicitly "
            "establishes their lifecycle, role, or precedence. Conditional explanations must keep the conflict unresolved."
        )
        stance = result.document_consistency.stance if result.document_consistency else "unknown"
        lines.append(
            "Document-consistency rewrite policy: preserve both sides of the artifact observations, state that lifecycle/role/"
            "precedence is not established when no supporting evidence id exists, and present any reconciliation only as a "
            f"conditional option. Reviewer stance={stance}. Do not apply reviewer action text as an authoritative source."
        )
        if handoff is not None and result.document_consistency is not None:
            lines.extend(document_consistency_rewrite_context(handoff, result.document_consistency))
        for finding in result.findings:
            claim = f": {finding.claim}" if finding.claim else ""
            lines.append(
                f"- Address claim {finding.claim_id}{claim}; if this claim lacks direct handoff support, remove it or downgrade it "
                "to unlocated/unverified/proposal. If it concerns an artifact conflict, restate it using the typed "
                "document-consistency disposition above. Do not invent replacement facts."
            )
        return "\n".join(lines)
    for finding in result.findings:
        claim = f": {finding.claim}" if finding.claim else ""
        lines.append(f"- Claim {finding.claim_id}{claim}; issue: {finding.issue}; action: {finding.action}")
    return "\n".join(lines)


def rewrite_complies_with_review(
    candidate: str,
    claim_units: tuple[CandidateClaimUnit, ...],
    findings: tuple[ReviewerFinding, ...],
) -> bool:
    """Reject a rewrite that leaves any addressed original claim unchanged."""

    normalized_candidate = _normalize_markdown(candidate)
    addressed = {unit.claim_id: unit for unit in claim_units}
    return all(
        finding.claim_id in addressed
        and bool(_normalize_markdown(addressed[finding.claim_id].text))
        and _normalize_markdown(addressed[finding.claim_id].text) not in normalized_candidate
        for finding in findings
    )


def parse_reviewer_finding_payload(
    raw: object,
    *,
    claim_units: tuple[CandidateClaimUnit, ...],
    document_consistency: bool = False,
    implementation_readiness: bool = False,
) -> ReviewerFinding:
    """Validate one incremental finding payload."""

    result = parse_reviewer_payload(
        {
            "verdict": "revise",
            "confidence": 1.0,
            "findings": [raw],
            "reason": "incremental finding",
        },
        claim_units=claim_units,
        allow_requirement_fact_findings=document_consistency,
        allow_implementation_readiness_proposal_findings=implementation_readiness,
    )
    return result.findings[0]


def parse_reviewer_final_payload(
    raw: object,
    *,
    findings: tuple[ReviewerFinding, ...],
    claim_units: tuple[CandidateClaimUnit, ...],
    document_consistency: bool = False,
    implementation_readiness: bool = False,
    evidence_ids: tuple[str, ...] = (),
    required_candidate_claim_ids: tuple[str, ...] = (),
    handoff: ExploreHandoff | None = None,
    candidate: str = "",
) -> ReviewerResult:
    """Validate final verdict payload after incremental findings have been collected."""

    if not isinstance(raw, Mapping):
        raise ReviewerValidationError("top_level_not_object", {"top_level_type": type(raw).__name__})
    assembled = dict(raw)
    if "findings" in assembled:
        raise ReviewerValidationError("top_level_keys_invalid", _shape_diagnostics(assembled))
    assembled["findings"] = [finding.to_dict() for finding in findings]
    return parse_reviewer_payload(
        assembled,
        claim_units=claim_units,
        document_consistency=document_consistency,
        implementation_readiness=implementation_readiness,
        evidence_ids=evidence_ids,
        required_candidate_claim_ids=required_candidate_claim_ids,
        handoff=handoff,
        candidate=candidate,
    )


def parse_reviewer_payload(
    raw: object,
    *,
    claim_units: tuple[CandidateClaimUnit, ...],
    document_consistency: bool = False,
    implementation_readiness: bool = False,
    evidence_ids: tuple[str, ...] = (),
    required_candidate_claim_ids: tuple[str, ...] = (),
    handoff: ExploreHandoff | None = None,
    candidate: str = "",
    allow_requirement_fact_findings: bool = False,
    allow_implementation_readiness_proposal_findings: bool = False,
) -> ReviewerResult:
    if not isinstance(raw, Mapping):
        raise ReviewerValidationError("top_level_not_object", {"top_level_type": type(raw).__name__})
    try:
        rendered_size = len(json.dumps(raw, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        rendered_size = MAX_REVIEWER_RESPONSE_CHARS + 1
    if rendered_size > MAX_REVIEWER_RESPONSE_CHARS:
        raise ReviewerValidationError("response_too_large", {"response_chars": rendered_size})
    diagnostics = _shape_diagnostics(raw)
    allowed_keys = {"verdict", "confidence", "findings", "reason"}
    if document_consistency:
        allowed_keys.add("document_consistency")
    if implementation_readiness:
        allowed_keys.add("implementation_readiness")
    if set(raw) != allowed_keys:
        raise ReviewerValidationError("top_level_keys_invalid", diagnostics)
    verdict = raw.get("verdict")
    if verdict not in {"pass", "revise", "unverified"}:
        raise ReviewerValidationError("verdict_invalid", diagnostics)
    confidence = raw.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
        raise ReviewerValidationError("confidence_invalid", diagnostics)
    findings_value = raw.get("findings")
    if not isinstance(findings_value, list):
        raise ReviewerValidationError("findings_not_list", diagnostics)
    if len(findings_value) > MAX_REVIEWER_FINDINGS:
        raise ReviewerValidationError("findings_too_many", diagnostics)
    findings: list[ReviewerFinding] = []
    claim_unit_by_id = {unit.claim_id: unit for unit in claim_units}
    claim_text_by_id = {claim_id: unit.text for claim_id, unit in claim_unit_by_id.items()}
    known_claim_ids = set(claim_text_by_id)
    readiness_assessment = None
    readiness_unsupported_claim_ids: set[str] = set()
    if implementation_readiness:
        try:
            readiness_assessment = parse_implementation_readiness_assessment(
                raw.get("implementation_readiness"),
                claim_ids=known_claim_ids,
            )
        except ImplementationReadinessValidationError as exc:
            raise ReviewerValidationError(exc.code, {**diagnostics, **exc.diagnostics}) from None
        readiness_unsupported_claim_ids = set(readiness_assessment.unsupported_identifier_claim_ids)
    used_claim_ids: set[str] = set()
    source_material_gap_count = 0
    candidate_defect_count = 0
    candidate_defect_claim_ids: list[str] = []
    for item in findings_value:
        if not isinstance(item, Mapping):
            raise ReviewerValidationError("finding_not_object", diagnostics)
        allowed_finding_keys = {"claim_id", "claim", "finding_scope", "issue", "action"}
        required_finding_keys = {"claim_id", "finding_scope", "issue", "action"}
        if not required_finding_keys.issubset(item) or not set(item).issubset(allowed_finding_keys):
            raise ReviewerValidationError("finding_keys_invalid", diagnostics)
        claim_id, claim, finding_scope, issue, action = (
            item.get("claim_id"),
            item.get("claim"),
            item.get("finding_scope"),
            item.get("issue"),
            item.get("action"),
        )
        if not isinstance(claim_id, str) or claim_id not in known_claim_ids:
            raise ReviewerValidationError("claim_id_unknown", {**diagnostics, "unknown_claim_id_count": 1})
        if claim_id in used_claim_ids:
            raise ReviewerValidationError("claim_id_duplicate", {**diagnostics, "duplicate_claim_id_count": 1})
        if not all(isinstance(value, str) and value.strip() for value in (issue, action)):
            raise ReviewerValidationError("finding_fields_invalid", diagnostics)
        if len(issue) > 1000 or len(action) > 1000:
            raise ReviewerValidationError("finding_fields_too_large", diagnostics)
        canonical_claim = claim_text_by_id[claim_id]
        if claim is not None:
            if not isinstance(claim, str) or not claim.strip():
                raise ReviewerValidationError("finding_claim_invalid", diagnostics)
            if _normalize_claim_binding(claim) != _normalize_claim_binding(canonical_claim):
                raise ReviewerValidationError("finding_claim_mismatch", {**diagnostics, "claim_mismatch_count": 1})
        if finding_scope not in {"candidate_defect", "source_material_gap"}:
            raise ReviewerValidationError("finding_scope_invalid", diagnostics)
        claim_role = claim_unit_by_id[claim_id].claim_role
        readiness_identifier_finding = bool(
            claim_role == "proposal"
            and (
                allow_implementation_readiness_proposal_findings
                or (implementation_readiness and claim_id in readiness_unsupported_claim_ids)
            )
        )
        role_out_of_scope = (
            claim_role in {"proposal", "pending"}
            and not readiness_identifier_finding
        ) or (
            claim_role == "requirement_fact"
            and not document_consistency
            and not allow_requirement_fact_findings
        )
        if role_out_of_scope:
            raise ReviewerValidationError(
                "claim_role_out_of_scope",
                {**diagnostics, "out_of_scope_claim_role": claim_role},
            )
        if finding_scope == "source_material_gap":
            source_material_gap_count += 1
        else:
            candidate_defect_count += 1
            candidate_defect_claim_ids.append(claim_id)
        used_claim_ids.add(claim_id)
        findings.append(ReviewerFinding(claim_id, _clip(issue), _clip(action), _clip(canonical_claim), finding_scope))
    if source_material_gap_count:
        raise ReviewerValidationError(
            "source_material_gap_finding",
            {
                **diagnostics,
                "source_material_gap_count": source_material_gap_count,
                "candidate_defect_count": candidate_defect_count,
                "required_candidate_defect_count": len(candidate_defect_claim_ids),
            },
            pending_candidate_claim_ids=tuple(candidate_defect_claim_ids),
        )
    reason = raw.get("reason")
    if not isinstance(reason, str):
        raise ReviewerValidationError("reason_invalid", diagnostics)
    if len(reason) > 1600:
        raise ReviewerValidationError("reason_too_large", diagnostics)
    if verdict == "pass" and findings:
        raise ReviewerValidationError("pass_with_findings", diagnostics)
    if verdict in {"revise", "unverified"} and not findings:
        raise ReviewerValidationError("nonpassing_without_findings", diagnostics)
    required_claims = tuple(dict.fromkeys(claim_id for claim_id in required_candidate_claim_ids if claim_id in known_claim_ids))
    missing_required = tuple(claim_id for claim_id in required_claims if claim_id not in candidate_defect_claim_ids)
    if missing_required:
        raise ReviewerValidationError(
            "candidate_defect_findings_missing",
            {
                **diagnostics,
                "required_candidate_defect_count": len(required_claims),
                "missing_candidate_defect_count": len(missing_required),
            },
            pending_candidate_claim_ids=required_claims,
        )
    assessment = None
    if document_consistency:
        try:
            assessment = parse_document_consistency_assessment(
                raw.get("document_consistency"),
                evidence_ids=evidence_ids,
            )
            if handoff is not None:
                assessment = complete_document_consistency_assessment(
                    assessment,
                    handoff,
                    candidate=candidate,
                    finding_claim_ids=tuple(finding.claim_id for finding in findings),
                )
        except DocumentConsistencyValidationError as exc:
            raise ReviewerValidationError(exc.code, {**diagnostics, **exc.diagnostics}) from None
        finding_issue = validate_document_consistency_finding_issue(
            assessment,
            (
                {
                    "claim_id": finding.claim_id,
                    "claim": finding.claim,
                    "issue": finding.issue,
                    "action": finding.action,
                    "finding_scope": finding.finding_scope,
                }
                for finding in findings
            ),
        )
        if finding_issue is not None:
            raise ReviewerValidationError(
                finding_issue.code,
                {
                    **diagnostics,
                    "invalid_document_finding_count": len(finding_issue.claim_ids),
                    "invalid_document_finding_claim_ids": list(finding_issue.claim_ids),
                },
                pending_candidate_claim_ids=finding_issue.claim_ids,
            )
    if implementation_readiness:
        assert readiness_assessment is not None
        readiness_code = validate_implementation_readiness_assessment(
            readiness_assessment,
            verdict=verdict,
            claim_units=claim_units,
            handoff=handoff,
            finding_claim_ids=tuple(finding.claim_id for finding in findings),
        )
        if readiness_code is not None:
            raise ReviewerValidationError(readiness_code, diagnostics)
    return ReviewerResult(
        verdict=verdict,
        confidence=float(confidence),
        findings=tuple(findings),
        reason=_clip(reason),
        document_consistency=assessment,
        implementation_readiness=readiness_assessment,
    )

def _json_object(content: str) -> object:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE)
    return json.loads(stripped)

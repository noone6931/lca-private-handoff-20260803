"""Isolated, bounded reviewer protocol for high-risk read-only conclusions."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .document_consistency import DocumentConsistencyAssessment
from .document_consistency import DocumentConsistencyValidationError
from .document_consistency import parse_document_consistency_assessment
from .explore_handoff import ExploreHandoff
from .task_contract import RequirementContract


ReviewerVerdict = Literal["pass", "revise", "unverified"]
ReviewerFindingScope = Literal["candidate_defect", "source_material_gap"]
MAX_REVIEWER_FINDINGS = 8
MAX_REVIEWER_RESPONSE_CHARS = 9000
MAX_INITIAL_REVIEWER_PROVIDER_CALLS = 3
MAX_REWRITE_REVIEWER_PROVIDER_CALLS = 2
REVIEWER_OUTPUT_TOOL_NAME = "submit_read_only_review"
MAX_CLAIM_UNITS = 80
MAX_CLAIM_UNIT_CHARS = 500
MAX_CLAIM_TOTAL_CHARS = 40000


@dataclass(frozen=True)
class CandidateClaimUnit:
    """A stable, bounded addressable unit from the candidate Markdown."""

    claim_id: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"claim_id": self.claim_id, "text": self.text}


@dataclass(frozen=True)
class ReviewerFinding:
    claim_id: str
    issue: str
    action: str
    claim: str
    finding_scope: ReviewerFindingScope

    def to_dict(self) -> dict[str, str]:
        return {
            "claim_id": self.claim_id,
            "claim": self.claim,
            "finding_scope": self.finding_scope,
            "issue": self.issue,
            "action": self.action,
        }


@dataclass(frozen=True)
class ReviewerResult:
    verdict: ReviewerVerdict
    confidence: float
    findings: tuple[ReviewerFinding, ...] = ()
    reason: str = ""
    document_consistency: DocumentConsistencyAssessment | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "findings": [finding.to_dict() for finding in self.findings],
            "reason": self.reason,
            "document_consistency": self.document_consistency.to_dict() if self.document_consistency else None,
        }


class ReviewerValidationError(ValueError):
    """A typed, redacted schema failure suitable for one repair request."""

    def __init__(
        self,
        code: str,
        diagnostics: Mapping[str, Any] | None = None,
        *,
        pending_candidate_claim_ids: tuple[str, ...] = (),
    ) -> None:
        self.code = code
        self.pending_candidate_claim_ids = pending_candidate_claim_ids
        self.diagnostics = {"error_code": code, **dict(diagnostics or {})}
        super().__init__(code)


@dataclass
class ReadOnlyReviewState:
    attempted: bool = False
    rewrite_requested: bool = False
    verdict: str | None = None
    reason: str | None = None
    findings: tuple[ReviewerFinding, ...] = ()
    claim_units: tuple[CandidateClaimUnit, ...] = ()
    document_consistency: DocumentConsistencyAssessment | None = None
    document_consistency_handoff_signature: tuple[tuple[str, ...], ...] = ()
    provider_attempts: int = 0
    schema_failures: int = 0
    repairs: int = 0
    repair_success: bool = False
    repair_exhausted: bool = False
    review_round: int = 0
    typed_submits: int = 0
    protocol_failures: int = 0
    safe_partial_emitted: bool = False

    def reset(self) -> None:
        self.attempted = False
        self.rewrite_requested = False
        self.verdict = None
        self.reason = None
        self.findings = ()
        self.claim_units = ()
        self.document_consistency = None
        self.document_consistency_handoff_signature = ()
        self.provider_attempts = 0
        self.schema_failures = 0
        self.repairs = 0
        self.repair_success = False
        self.repair_exhausted = False
        self.review_round = 0
        self.typed_submits = 0
        self.protocol_failures = 0
        self.safe_partial_emitted = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "rewrite_requested": self.rewrite_requested,
            "verdict": self.verdict,
            "reason": self.reason,
            "reviewed_claim_ids": [item.claim_id for item in self.findings],
            "reviewed_claim_count": len({item.claim_id for item in self.findings}),
            "document_consistency_stance": self.document_consistency.stance if self.document_consistency else None,
            "provider_attempts": self.provider_attempts,
            "schema_failures": self.schema_failures,
            "repairs": self.repairs,
            "repair_success": self.repair_success,
            "repair_exhausted": self.repair_exhausted,
            "review_round": self.review_round,
            "typed_submits": self.typed_submits,
            "protocol_failures": self.protocol_failures,
        }


@dataclass(frozen=True)
class ReviewerPhaseOutcome:
    kind: Literal["not_applicable", "pass", "rewrite", "unverified"]
    rewrite_message: str = ""
    terminal_message: str = ""
    reason: str = ""
    safe_partial_report: str = ""


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
    """Index Markdown claims, then deterministically sample both head and tail.

    Structural lines are independent units. Ordinary paragraphs split on common
    sentence boundaries and then on fixed-size chunks when one sentence is very
    long. This is presentation-aware text segmentation, not semantic NLP.
    """

    indexed: list[CandidateClaimUnit] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph:
            paragraph.clear()
            return
        text = "\n".join(paragraph).strip()
        paragraph.clear()
        for sentence in _paragraph_units(text):
            indexed.append(CandidateClaimUnit(f"c{len(indexed) + 1:03d}", sentence))

    for raw_line in (candidate or "").splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        structural = line.startswith(("#", "- ", "* ", "+ ", "> ")) or _is_table_row(line)
        if structural:
            flush_paragraph()
            if not _is_table_separator(line):
                indexed.append(CandidateClaimUnit(f"c{len(indexed) + 1:03d}", _clip_unit(line)))
            continue
        paragraph.append(raw_line)
    flush_paragraph()
    if not indexed and candidate.strip():
        indexed.append(CandidateClaimUnit("c001", _clip_unit(candidate)))
    return _sample_claim_units(indexed)


def reviewer_messages(handoff: ExploreHandoff, claim_units: tuple[CandidateClaimUnit, ...]) -> list[dict[str, str]]:
    """Return an isolated reviewer transcript with no primary conversation history."""

    system = """You are the read-only evidence reviewer for a coding agent.
Use the only available output tool exactly once to submit the review. You have no workspace tools and must never assume unseen repository facts.

Review contract:
- A direct owner is justified only by evidence that explicitly binds the requested behavior to a path, symbol, or call chain.
- Similar names, same-domain payment/order/fee capabilities, and general reusable code are analogous candidates, never verified owners.
- Missing or incomplete searches mean unlocated within their stated scope, not absent everywhere.
- Requirement facts, repository facts, proposals, and open questions must remain distinct.
- A proposal must not be worded as an existing table, class, endpoint, service, approval flow, numbering prefix, or integration unless the handoff explicitly supports it.
- When the handoff has no explicit direct binding, do not say a main owner/module judgment is correct or mostly correct. Treat same-domain code as observed or analogous and leave the owner unlocated.
- For a document-consistency review, do not resolve conflicting document or image observations with an invented workflow, scope, actor, or precedence rule. Preserve the conflict as unresolved unless the handoff explicitly reconciles it. A candidate that accurately cites both observations, explicitly keeps the conflict unresolved, and presents only labeled options or questions for later confirmation is compliant: submit `pass` with no findings. A finding must identify a candidate error such as an unsupported reconciliation, a missing cited observation, or a claim that exceeds the handoff; the source materials disagreeing by itself is not a candidate defect. If the only issue is that a source owner must decide how to update source materials, submit `pass` with no findings.

The output tool arguments use verdict, confidence, findings, and reason. The complete submission must be shorter than 9000 characters. `findings` must contain at most 8 items. A `pass` verdict requires exactly 0 findings; `revise` and `unverified` require 1 to 8 findings. Every finding must have one unique, known claim_id plus non-empty issue and action. For every finding, choose exactly one claim_id from candidate_claims, set finding_scope to `candidate_defect`, and copy that exact candidate_claims text into `claim`. The action must change the candidate answer; it must not ask to modify the requirements, images, prototypes, or source artifacts, and must not merely ask a source owner to decide. Never invent or repeat a claim_id. Do not submit `source_material_gap` findings; those are not candidate defects. Report only the highest-risk blocking findings when there are more than 8.
Choose revise when the candidate can be corrected using the handoff. Choose unverified when the candidate cannot safely make the requested factual conclusion."""
    if _is_document_consistency_review(handoff):
        system += (
            "\n\nDocument-consistency output requirement:\n"
            "- Submit document_consistency for every verdict. conflict_evidence_ids must reference the supplied artifact observations. "
            "conflict_evidence_ids and supporting_evidence_ids must never overlap. "
            "reported_unresolved means no reconciliation is asserted; conditional_reconciliation may offer a conditional "
            "explanation but must retain that artifact role/lifecycle is unresolved. asserted_reconciled identifies an "
            "unsupported candidate reconciliation and therefore cannot justify pass. For reported_unresolved, "
            "conditional_reconciliation, and asserted_reconciled, set supporting_evidence_ids to []. "
            "explicitly_supported_reconciliation is the only stance that may use non-empty supporting_evidence_ids, "
            "and only when those ids cite independent non-visual read_file observations that explicitly state lifecycle "
            "or precedence. A visual observation can show displayed values, but never establishes author intent, a typo, "
            "lifecycle, actor, or precedence."
        )
    payload = {
        "kind": "LCA_READ_ONLY_EVIDENCE_REVIEW",
        "handoff": handoff.to_dict(),
        "candidate_claims": [unit.to_dict() for unit in claim_units],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def reviewer_output_tool_schema(
    claim_units: tuple[CandidateClaimUnit, ...],
    *,
    document_consistency: bool = False,
    evidence_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return the isolated reviewer yield schema, never a workspace tool."""

    known_ids = [unit.claim_id for unit in claim_units]
    finding = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "claim_id": {"type": "string", "enum": known_ids},
            "claim": {"type": "string", "minLength": 1, "description": "Exact text copied from the selected candidate_claims item."},
            "finding_scope": {
                "type": "string",
                "enum": ["candidate_defect", "source_material_gap"],
                "description": "Findings must be candidate_defect. source_material_gap is invalid and should be removed during repair.",
            },
            "issue": {"type": "string", "minLength": 1, "maxLength": 1000},
            "action": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1000,
                "description": (
                    "Required candidate-answer change. Do not ask to modify source documents, images, prototypes, "
                    "or requirements, and do not merely ask a source owner to decide."
                ),
            },
        },
        "required": ["claim_id", "claim", "finding_scope", "issue", "action"],
    }
    properties: dict[str, Any] = {
        "verdict": {"type": "string", "enum": ["pass", "revise", "unverified"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "findings": {"type": "array", "maxItems": MAX_REVIEWER_FINDINGS, "items": finding},
        "reason": {"type": "string", "maxLength": 1600},
    }
    required = ["verdict", "confidence", "findings", "reason"]
    if document_consistency:
        evidence_id = {"type": "string", "enum": list(evidence_ids)}
        properties["document_consistency"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "stance": {
                    "type": "string",
                    "enum": [
                        "reported_unresolved",
                        "conditional_reconciliation",
                        "asserted_reconciled",
                        "explicitly_supported_reconciliation",
                    ],
                },
                "conflict_evidence_ids": {"type": "array", "maxItems": 8, "items": evidence_id},
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
            "required": ["stance", "conflict_evidence_ids", "supporting_evidence_ids"],
        }
        required.append("document_consistency")
    return {
        "type": "function",
        "function": {
            "name": REVIEWER_OUTPUT_TOOL_NAME,
            "description": "Submit the isolated read-only evidence review. This is output-only and cannot access the workspace.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
                "required": required,
            },
        },
    }


def reviewer_repair_messages(
    handoff: ExploreHandoff,
    claim_units: tuple[CandidateClaimUnit, ...],
    diagnostics: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Repeat the isolated review with sanitized schema-only feedback."""

    messages = reviewer_messages(handoff, claim_units)
    messages.append(
        {
            "role": "user",
            "content": json.dumps(
                {
                    "kind": "LCA_READ_ONLY_EVIDENCE_REVIEW_SCHEMA_REPAIR",
                    "validation": _sanitize_diagnostics(diagnostics),
                    "instruction": (
                        "Use only the original output tool and submit complete arguments that exactly follow its schema. "
                        "Use only candidate claim IDs supplied in the original payload. "
                        + _repair_shape_instruction(diagnostics)
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        }
    )
    return messages


def parse_reviewer_result(
    content: object,
    *,
    claim_units: tuple[CandidateClaimUnit, ...],
    document_consistency: bool = False,
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
        evidence_ids=evidence_ids,
        required_candidate_claim_ids=required_candidate_claim_ids,
    )


def parse_reviewer_payload(
    raw: object,
    *,
    claim_units: tuple[CandidateClaimUnit, ...],
    document_consistency: bool = False,
    evidence_ids: tuple[str, ...] = (),
    required_candidate_claim_ids: tuple[str, ...] = (),
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
    claim_text_by_id = {unit.claim_id: unit.text for unit in claim_units}
    known_claim_ids = set(claim_text_by_id)
    used_claim_ids: set[str] = set()
    source_material_gap_count = 0
    candidate_defect_count = 0
    candidate_defect_claim_ids: list[str] = []
    for item in findings_value:
        if not isinstance(item, Mapping):
            raise ReviewerValidationError("finding_not_object", diagnostics)
        allowed_finding_keys = {"claim_id", "claim", "finding_scope", "issue", "action"}
        required_finding_keys = {"claim_id", "claim", "finding_scope", "issue", "action"}
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
        if not isinstance(claim, str) or not claim.strip():
            raise ReviewerValidationError("finding_claim_invalid", diagnostics)
        if _normalize_claim_binding(claim) != _normalize_claim_binding(claim_text_by_id[claim_id]):
            raise ReviewerValidationError("finding_claim_mismatch", {**diagnostics, "claim_mismatch_count": 1})
        if finding_scope not in {"candidate_defect", "source_material_gap"}:
            raise ReviewerValidationError("finding_scope_invalid", diagnostics)
        if finding_scope == "source_material_gap":
            source_material_gap_count += 1
        else:
            candidate_defect_count += 1
            candidate_defect_claim_ids.append(claim_id)
        used_claim_ids.add(claim_id)
        findings.append(ReviewerFinding(claim_id, _clip(issue), _clip(action), _clip(claim), finding_scope))
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
        except DocumentConsistencyValidationError as exc:
            raise ReviewerValidationError(exc.code, diagnostics) from None
    return ReviewerResult(
        verdict=verdict,
        confidence=float(confidence),
        findings=tuple(findings),
        reason=_clip(reason),
        document_consistency=assessment,
    )


def reviewer_rewrite_message(result: ReviewerResult, *, profile: str | None = None) -> str:
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
    ]
    if profile == "owner_impact":
        lines.append(
            "Do not introduce concrete class, table, path, service, endpoint, field, or numbering names that were not already supported by the handoff."
        )
    elif profile == "design":
        lines.append(
            "Design proposals may be conceptual, but every concrete repository name must remain an observed fact or be explicitly marked unverified."
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
        for finding in result.findings:
            claim = f": {finding.claim}" if finding.claim else ""
            lines.append(
                f"- Address claim {finding.claim_id}{claim}; remove unsupported reconciliation or restate it as unresolved/conditional."
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
    """Reject an unchanged unsupported claim after the one permitted rewrite.

    This compares the original addressed unit, Markdown-normalized, against the
    rewritten candidate. It is not a fuzzy matcher or a business-specific
    classifier.
    """

    normalized_candidate = _normalize_markdown(candidate)
    addressed = {unit.claim_id: unit for unit in claim_units}
    return all(
        finding.claim_id in addressed
        and bool(_normalize_markdown(addressed[finding.claim_id].text))
        and _normalize_markdown(addressed[finding.claim_id].text) not in normalized_candidate
        for finding in findings
    )


def _json_object(content: str) -> object:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE)
    return json.loads(stripped)


def _shape_diagnostics(raw: Mapping[str, Any]) -> dict[str, Any]:
    findings = raw.get("findings")
    verdict = raw.get("verdict")
    return {
        "top_level_keys": sorted(str(key)[:64] for key in raw)[:16],
        "verdict": verdict if verdict in {"pass", "revise", "unverified"} else "invalid",
        "findings_type": type(findings).__name__,
        "findings_count": len(findings) if isinstance(findings, list) else None,
    }


def _sanitize_diagnostics(diagnostics: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "error_code",
        "top_level_keys",
        "verdict",
        "findings_type",
        "findings_count",
        "unknown_claim_id_count",
        "duplicate_claim_id_count",
        "claim_mismatch_count",
        "source_material_gap_count",
        "candidate_defect_count",
        "required_candidate_defect_count",
        "missing_candidate_defect_count",
        "response_chars",
        "top_level_type",
    }
    return {key: diagnostics[key] for key in allowed if key in diagnostics}


def _repair_shape_instruction(diagnostics: Mapping[str, Any]) -> str:
    """Describe the failed schema rule without repeating provider content."""

    code = str(diagnostics.get("error_code") or "")
    common = "Keep the JSON response under 9000 characters and use unique known claim IDs."
    if code == "findings_too_many":
        return "Return no more than 8 highest-risk findings. " + common
    if code == "response_too_large":
        return "Make reason, issue, and action concise; do not exceed 9000 characters. " + common
    if code == "pass_with_findings":
        return "A pass verdict must have an empty findings list; otherwise choose revise or unverified with 1 to 8 findings. " + common
    if code == "nonpassing_without_findings":
        return "A revise or unverified verdict needs 1 to 8 findings. " + common
    if code in {"finding_claim_invalid", "finding_claim_mismatch"}:
        return (
            "Every finding must copy the exact candidate_claims text for its selected claim_id into claim. "
            "If the text belongs to another claim_id, select that claim_id instead. "
            + common
        )
    if code in {"source_material_gap_finding", "finding_scope_invalid"}:
        return (
            "Do not submit source_material_gap findings. Findings must be candidate_defect items whose action changes "
            "the candidate answer. If only source materials need an owner decision and the candidate already reports that "
            "gap accurately, submit pass with an empty findings list; otherwise keep only candidate_defect findings. "
            + common
        )
    if code == "candidate_defect_findings_missing":
        return (
            "A previous repair attempt contained valid candidate_defect findings. Keep those candidate defects in this "
            "submission with their same claim_id and exact copied claim text; do not replace them with pass unless the "
            "candidate_defect findings are no longer present in the repaired output request. "
            + common
        )
    if code == "document_consistency_evidence_roles_overlap":
        return (
            "For document_consistency, conflict_evidence_ids and supporting_evidence_ids must be disjoint. "
            "For reported_unresolved, conditional_reconciliation, or asserted_reconciled, set supporting_evidence_ids to []. "
            "Only explicitly_supported_reconciliation may use non-empty supporting_evidence_ids, and only for independent "
            "non-visual read_file lifecycle or precedence support. "
            + common
        )
    if code == "document_consistency_support_requires_explicit_stance":
        return (
            "For document_consistency, set supporting_evidence_ids to [] unless stance is explicitly_supported_reconciliation. "
            "reported_unresolved, conditional_reconciliation, and asserted_reconciled must not include support ids. "
            + common
        )
    if code == "document_conflict_evidence_insufficient":
        return (
            "For document_consistency, when the candidate reports, conditions, or reconciles a conflict, "
            "conflict_evidence_ids must cite at least two known document or image observations that form the comparison. "
            "Do not cite only one side of the conflict. "
            + common
        )
    if code in {"document_supporting_evidence_invalid", "document_supporting_evidence_unknown", "document_supporting_evidence_duplicate"}:
        return (
            "For document_consistency, keep supporting_evidence_ids empty unless the stance is explicitly_supported_reconciliation "
            "and the ids cite independent non-visual read_file lifecycle or precedence support. "
            + common
        )
    return "Use a JSON findings list with at most 8 items and the verdict/finding cardinality from the original schema. " + common


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


def _is_document_consistency_review(handoff: ExploreHandoff) -> bool:
    return handoff.contract.evidence_domain == "requirement_documents" and handoff.contract.read_only_review_profile == "document_consistency"


def _clip_unit(value: str) -> str:
    text = value.strip()
    return text if len(text) <= MAX_CLAIM_UNIT_CHARS else text[: MAX_CLAIM_UNIT_CHARS - 1].rstrip() + "..."


def _paragraph_units(value: str) -> tuple[str, ...]:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?。！？])\s*", value) if part.strip()]
    units: list[str] = []
    for sentence in sentences or [value.strip()]:
        if len(sentence) <= MAX_CLAIM_UNIT_CHARS:
            units.append(sentence)
            continue
        for start in range(0, len(sentence), MAX_CLAIM_UNIT_CHARS):
            units.append(sentence[start : start + MAX_CLAIM_UNIT_CHARS])
    return tuple(units)


def _sample_claim_units(indexed: list[CandidateClaimUnit]) -> tuple[CandidateClaimUnit, ...]:
    if len(indexed) <= MAX_CLAIM_UNITS:
        selected = indexed
    else:
        # Preserve the whole answer shape when an unusually long candidate
        # exceeds the protocol cap.  Stable original IDs remain the address;
        # evenly spaced selection avoids making middle sections invisible.
        selected = [
            indexed[round(position * (len(indexed) - 1) / (MAX_CLAIM_UNITS - 1))]
            for position in range(MAX_CLAIM_UNITS)
        ]
    total = 0
    bounded: list[CandidateClaimUnit] = []
    for unit in selected:
        if bounded and total + len(unit.text) > MAX_CLAIM_TOTAL_CHARS:
            continue
        bounded.append(unit)
        total += len(unit.text)
    return tuple(bounded)


def _is_table_row(line: str) -> bool:
    return line.count("|") >= 2


def _is_table_separator(line: str) -> bool:
    return bool(re.fullmatch(r"\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?", line))

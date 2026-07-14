"""Isolated, bounded reviewer protocol for high-risk read-only conclusions."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any, Literal, Mapping

from .document_consistency import DocumentConsistencyAssessment
from .document_consistency import DocumentConsistencyValidationError
from .document_consistency import complete_document_consistency_assessment
from .document_consistency import document_consistency_rejection_hint
from .document_consistency import document_consistency_rewrite_context
from .document_consistency import document_consistency_schema
from .document_consistency import parse_document_consistency_assessment
from .document_consistency import validate_document_consistency_finding_issue
from .explore_handoff import ExploreHandoff
from .task_contract import RequirementContract


ReviewerVerdict = Literal["pass", "revise", "unverified"]
ReviewerFindingScope = Literal["candidate_defect", "source_material_gap"]
MAX_REVIEWER_FINDINGS = 8
MAX_REVIEWER_RESPONSE_CHARS = 9000
MAX_REVIEWER_SCHEMA_REPAIRS = 2
MAX_REVIEWER_OUTPUT_LIFECYCLE_ERRORS = 2
MAX_REVIEWER_CAPACITY_DIRECTIVES = 2
REVIEWER_OUTPUT_TOOL_NAME = "submit_read_only_review"
REVIEWER_FINDING_TOOL_NAME = "report_read_only_finding"
MAX_CLAIM_UNITS = 80
MAX_CLAIM_UNIT_CHARS = 500
MAX_CLAIM_TOTAL_CHARS = 40000


@dataclass(frozen=True)
class CandidateClaimUnit:
    """A stable, bounded addressable unit from the candidate Markdown."""

    claim_id: str
    text: str
    locator_context: str = ""
    section_context: str = ""

    def to_dict(self) -> dict[str, str]:
        payload = {"claim_id": self.claim_id, "text": self.text}
        if self.section_context:
            payload["section_context"] = self.section_context
        if self.locator_context:
            payload["locator_context"] = self.locator_context
        return payload


@dataclass(frozen=True)
class CandidateClaimProjectionIssue:
    code: str
    detail: str = ""


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
    transport_rewrite_requested: bool = False
    transport_rewrite_accepted: bool = False
    transport_rewrite_exhausted: bool = False
    verdict: str | None = None
    reason: str | None = None
    findings: tuple[ReviewerFinding, ...] = ()
    rewrite_closure_findings: tuple[ReviewerFinding, ...] = ()
    claim_units: tuple[CandidateClaimUnit, ...] = ()
    document_consistency: DocumentConsistencyAssessment | None = None
    document_consistency_handoff_signature: tuple[tuple[str, ...], ...] = ()
    review_handoff: ExploreHandoff | None = None
    provider_attempts: int = 0
    schema_failures: int = 0
    repairs: int = 0
    repair_success: bool = False
    repair_exhausted: bool = False
    review_round: int = 0
    typed_submits: int = 0
    protocol_failures: int = 0
    rejected_finding_submits: int = 0
    rejected_final_submits: int = 0
    finding_limit_hits: int = 0
    invalidated_finding_submits: int = 0
    output_lifecycle_exhausted: bool = False
    rewrite_accepted: bool = False
    rewrite_corrections: int = 0
    rewrite_verification_rounds: int = 0
    safe_partial_emitted: bool = False

    def reset(self) -> None:
        self.attempted = False
        self.rewrite_requested = False
        self.transport_rewrite_requested = False
        self.transport_rewrite_accepted = False
        self.transport_rewrite_exhausted = False
        self.verdict = None
        self.reason = None
        self.findings = ()
        self.rewrite_closure_findings = ()
        self.claim_units = ()
        self.document_consistency = None
        self.document_consistency_handoff_signature = ()
        self.review_handoff = None
        self.provider_attempts = 0
        self.schema_failures = 0
        self.repairs = 0
        self.repair_success = False
        self.repair_exhausted = False
        self.review_round = 0
        self.typed_submits = 0
        self.protocol_failures = 0
        self.rejected_finding_submits = 0
        self.rejected_final_submits = 0
        self.finding_limit_hits = 0
        self.invalidated_finding_submits = 0
        self.output_lifecycle_exhausted = False
        self.rewrite_accepted = False
        self.rewrite_corrections = 0
        self.rewrite_verification_rounds = 0
        self.safe_partial_emitted = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "rewrite_requested": self.rewrite_requested,
            "transport_rewrite_requested": self.transport_rewrite_requested,
            "transport_rewrite_accepted": self.transport_rewrite_accepted,
            "transport_rewrite_exhausted": self.transport_rewrite_exhausted,
            "verdict": self.verdict,
            "reason": self.reason,
            "reviewed_claim_ids": [item.claim_id for item in self.findings],
            "reviewed_claim_count": len({item.claim_id for item in self.findings}),
            "rewrite_closure_claim_ids": [item.claim_id for item in self.rewrite_closure_findings],
            "document_consistency_stance": self.document_consistency.stance if self.document_consistency else None,
            "rewrite_accepted": self.rewrite_accepted,
            "provider_attempts": self.provider_attempts,
            "provider_turns": self.provider_attempts,
            "schema_failures": self.schema_failures,
            "repairs": self.repairs,
            "repair_success": self.repair_success,
            "repair_exhausted": self.repair_exhausted,
            "review_round": self.review_round,
            "typed_submits": self.typed_submits,
            "protocol_failures": self.protocol_failures,
            "rejected_finding_submits": self.rejected_finding_submits,
            "rejected_final_submits": self.rejected_final_submits,
            "finding_limit_hits": self.finding_limit_hits,
            "invalidated_finding_submits": self.invalidated_finding_submits,
            "output_lifecycle_exhausted": self.output_lifecycle_exhausted,
            "rewrite_corrections": self.rewrite_corrections,
            "rewrite_verification_rounds": self.rewrite_verification_rounds,
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
    return _extract_candidate_claim_units(candidate)[0]


def candidate_claim_projection_issues(candidate: str) -> tuple[CandidateClaimProjectionIssue, ...]:
    return _extract_candidate_claim_units(candidate)[1]


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
    pending_structural_group: list[int] = []

    def record_issue(code: str, detail: str = "") -> None:
        issues.append(CandidateClaimProjectionIssue(code, detail))

    def append_unit(unit: str) -> None:
        indexed.append(CandidateClaimUnit(f"c{len(indexed) + 1:03d}", unit, locator_context, section_context))

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
            section_context = _heading_context(line)
            pending_structural_group.clear()
            locator_context = line if _line_has_path_bound_locator(line) else ""
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
        indexed.append(CandidateClaimUnit("c001", _clip_unit(candidate), section_context=section_context))
    return _sample_claim_units(indexed), tuple(issues)


def reviewer_messages(handoff: ExploreHandoff, claim_units: tuple[CandidateClaimUnit, ...]) -> list[dict[str, str]]:
    """Return an isolated reviewer transcript with no primary conversation history."""

    system = """You are the read-only evidence reviewer for a coding agent.
Use the output-only review tools; you have no workspace tools and must never assume unseen repository facts. For each candidate defect, call report_read_only_finding once. After all findings are reported, call submit_read_only_review exactly once.

Review contract:
- The user's exact request is mandatory context. Enforce explicit prohibitions, acceptance constraints, and requested evidence boundaries from that request; do not let the candidate invent a source priority, lifecycle, or exception that the request forbids.
- A direct owner is justified only by evidence that explicitly binds the requested behavior to a path, symbol, or call chain.
- Similar names, same-domain payment/order/fee capabilities, and general reusable code are analogous candidates, never verified owners.
- Missing or incomplete searches mean unlocated within their stated scope, not absent everywhere.
- Requirement facts, repository facts, proposals, and open questions must remain distinct.
- Some handoff claim_matrix items include claim_ids. Those are claim-scoped evidence excerpts for those candidate claims only; check that the addressed claim_id is a member of claim_ids before using the excerpt, and do not use an image observation to prove a Markdown requirement rule or vice versa.
- A proposal must not be worded as an existing table, class, endpoint, service, approval flow, numbering prefix, or integration unless the handoff explicitly supports it.
- A clearly labeled design proposal, suggested new table/class/API, candidate option, or pending-confirmation plan is allowed without proving that every old asset is impossible to reuse. It must stay labeled as proposal/pending confirmation and list the prerequisite reuse/owner checks; report a defect only when the candidate presents the proposed name as current implementation, existing fact, verified owner, or proven absence.
- When the handoff has no explicit direct binding, do not say a main owner/module judgment is correct or mostly correct. Treat same-domain code as observed or analogous and leave the owner unlocated.
- For a document-consistency review, do not resolve conflicting document or image observations with an invented workflow, scope, actor, source priority, authoritative source, or precedence rule. Preserve the conflict as unresolved unless the handoff explicitly reconciles it. A candidate that accurately cites both observations, explicitly keeps the conflict unresolved, and presents only labeled options or questions for later confirmation is compliant: submit `pass` with no reported findings. A finding must identify a candidate error such as an unsupported reconciliation, a self-contradictory candidate statement, a missing cited observation, a user-request violation, or a claim that exceeds the handoff; the source materials disagreeing by itself is not a candidate defect. If the only issue is that a source owner must decide how to update source materials, submit `pass` with no reported findings.

The incremental output contract is bounded and shallow. Report at most 8 findings total by calling report_read_only_finding once per finding; then call submit_read_only_review with verdict, confidence, reason, and any profile-required typed summary such as document_consistency. Do not repeat findings in the final submit; accepted findings are already recorded as incremental sections. Findings are capacity-limited: choose the highest-risk blocking candidate defects first. Once 8 findings are recorded, stop reporting findings and submit the final verdict. A `pass` verdict requires 0 reported findings; `revise` and `unverified` require 1 to 8 reported findings. Every finding must have one unique, known claim_id plus non-empty issue and action. For every finding, choose exactly one claim_id from candidate_claims and set finding_scope to `candidate_defect`; Runtime binds the exact candidate text by claim_id. The action must change the candidate answer; it must not ask to modify the requirements, images, prototypes, or source artifacts, and must not merely ask a source owner to decide. Never invent or repeat a claim_id. Do not submit `source_material_gap` findings; those are not candidate defects. Report only the highest-risk blocking findings when there are more than 8. Keep the complete output under 9000 characters.
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


def reviewer_rewrite_verification_messages(
    handoff: ExploreHandoff,
    claim_units: tuple[CandidateClaimUnit, ...],
    original_findings: tuple[ReviewerFinding, ...],
) -> list[dict[str, str]]:
    """Return reviewer messages for validating a rewritten candidate."""

    messages = reviewer_messages(handoff, claim_units)
    closure_payload = {
        "kind": "LCA_READ_ONLY_REWRITE_VERIFICATION",
        "instruction": (
            "This is a bounded verification of a primary rewrite after an earlier revise verdict. "
            "Submit pass only if the rewritten candidate closes the original candidate defects: each addressed original "
            "claim must now be removed, downgraded to unverified/unlocated/proposal/pending confirmation, or supported by "
            "the current handoff. Prior candidate claim IDs are intentionally omitted because they are invalid for this "
            "rewritten candidate. If any original blocking defect remains under paraphrase, report revise with findings "
            "using only IDs from the current candidate_claims list."
        ),
        "original_findings": [
            {
                "finding_ordinal": index,
                "source_round": "prior_reviewer_round",
                "claim": _clip(finding.claim, 360),
                "finding_scope": finding.finding_scope,
                "issue": _clip(finding.issue, 360),
                "action": _clip(finding.action, 360),
            }
            for index, finding in enumerate(original_findings, start=1)
        ],
    }
    messages.append({"role": "user", "content": json.dumps(closure_payload, ensure_ascii=False, sort_keys=True)})
    return messages


def reviewer_transport_rewrite_message(
    *,
    handoff: ExploreHandoff,
    omitted_claim_ids: tuple[str, ...],
) -> str:
    """Ask the primary model to compact an over-granular answer before review.

    This is a pre-review transport recovery, not a reviewer verdict rewrite:
    no findings have been accepted yet and the isolated reviewer still needs to
    run after the compact answer is produced.
    """

    omitted_count = len(tuple(dict.fromkeys(omitted_claim_ids)))
    return (
        "[Read-only evidence review: bounded transport recovery]\n"
        "The previous answer was too granular for the isolated evidence reviewer: "
        f"{omitted_count} reviewed claim(s) had cited evidence that could not fit inside the bounded claim matrix.\n\n"
        "Rewrite the same answer once, without tools, into a compact reviewable final candidate. Follow these constraints:\n"
        "- Keep only the user's high-value requested conclusions; merge repeated table rows and duplicate facts.\n"
        "- Use a small number of precise, already-observed locators per conclusion; prefer shared ranges over one citation per row.\n"
        "- Do not add new facts, paths, artifacts, owners, lifecycle explanations, source priority, or inferred workflow state.\n"
        "- Preserve direct observations exactly at their evidence boundary. Visual observations show what is visible; they do not prove author intent, lifecycle, precedence, or role.\n"
        "- If document/image/prototype observations differ and the handoff has no explicit role/lifecycle/precedence support, state that the discrepancy remains unresolved / pending confirmation. Do not describe either artifact as a mockup, reference-only example, historical version, later/final/offline-filled state, or authoritative source unless an existing handoff item explicitly supports that.\n"
        "- Suggested compact structure: current scope; later/planned items; key rules; source discrepancies/open confirmations. Use short bullets, not exhaustive row-by-row restatement.\n\n"
        f"Original user request (mandatory): {handoff.request}\n"
        "Return only the rewritten candidate answer."
    )


def reviewer_output_tool_schema(
    claim_units: tuple[CandidateClaimUnit, ...],
    *,
    document_consistency: bool = False,
    evidence_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return the isolated final-review yield schema, never a workspace tool."""

    properties: dict[str, Any] = {
        "verdict": {"type": "string", "enum": ["pass", "revise", "unverified"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {
            "type": "string",
            "maxLength": 1600,
            "description": "Brief verdict summary. Do not repeat findings here; use report_read_only_finding for each finding.",
        },
    }
    required = ["verdict", "confidence", "reason"]
    if document_consistency:
        properties["document_consistency"] = document_consistency_schema(evidence_ids)
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


def reviewer_finding_tool_schema(claim_units: tuple[CandidateClaimUnit, ...]) -> dict[str, Any]:
    """Return the isolated incremental finding-yield schema."""

    known_ids = [unit.claim_id for unit in claim_units]
    return {
        "type": "function",
        "function": {
            "name": REVIEWER_FINDING_TOOL_NAME,
            "description": (
                "Report exactly one candidate-answer defect. Use this once per finding before "
                "submit_read_only_review. This output-only tool cannot access the workspace."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "claim_id": {"type": "string", "enum": known_ids},
                    "finding_scope": {
                        "type": "string",
                        "enum": ["candidate_defect"],
                        "description": (
                            "Must be candidate_defect. source_material_gap is invalid because source-material "
                            "gaps are not candidate-answer defects."
                        ),
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
                "required": ["claim_id", "finding_scope", "issue", "action"],
            },
        },
    }


def reviewer_output_tool_schemas(
    claim_units: tuple[CandidateClaimUnit, ...],
    *,
    document_consistency: bool = False,
    evidence_ids: tuple[str, ...] = (),
    include_finding_tool: bool = True,
) -> list[dict[str, Any]]:
    """Return OMP-style incremental output tools for the isolated reviewer."""

    schemas: list[dict[str, Any]] = []
    if include_finding_tool:
        schemas.append(reviewer_finding_tool_schema(claim_units))
    schemas.append(
        reviewer_output_tool_schema(
            claim_units,
            document_consistency=document_consistency,
            evidence_ids=evidence_ids,
        )
    )
    return schemas


def reviewer_repair_messages(
    handoff: ExploreHandoff,
    claim_units: tuple[CandidateClaimUnit, ...],
    diagnostics: Mapping[str, Any],
    *,
    accepted_claim_ids: tuple[str, ...] = (),
    required_resubmit_claim_ids: tuple[str, ...] = (),
) -> list[dict[str, str]]:
    """Repeat the isolated review with sanitized schema-only feedback."""

    messages = reviewer_messages(handoff, claim_units)
    messages.append(
        reviewer_repair_message(
            diagnostics,
            accepted_claim_ids=accepted_claim_ids,
            required_resubmit_claim_ids=required_resubmit_claim_ids,
        )
    )
    return messages


def reviewer_repair_message(
    diagnostics: Mapping[str, Any],
    *,
    accepted_claim_ids: tuple[str, ...] = (),
    required_resubmit_claim_ids: tuple[str, ...] = (),
) -> dict[str, str]:
    """Return one sanitized schema-repair turn without resetting prior yield transcript."""

    accepted = tuple(dict.fromkeys(str(claim_id) for claim_id in accepted_claim_ids if str(claim_id).strip()))
    required_resubmit = tuple(
        dict.fromkeys(str(claim_id) for claim_id in required_resubmit_claim_ids if str(claim_id).strip())
    )
    return {
        "role": "user",
        "content": json.dumps(
            {
                "kind": "LCA_READ_ONLY_EVIDENCE_REVIEW_SCHEMA_REPAIR",
                "validation": _sanitize_diagnostics(diagnostics),
                "accepted_candidate_defect_claim_ids": list(accepted),
                "required_resubmit_candidate_defect_claim_ids": list(required_resubmit),
                "instruction": (
                    "Use only the original output tools and submit complete arguments that exactly follow their schemas. "
                    "Use only candidate claim IDs supplied in the original payload. "
                    "Do not repeat accepted_candidate_defect_claim_ids as new findings; those findings were already "
                    "recorded and must be preserved in the final verdict semantics. "
                    "required_resubmit_candidate_defect_claim_ids were validated in a response that could not be safely "
                    "paired, so report those findings again before submitting the final verdict. "
                    "Do not include candidate claim text; Runtime binds the exact claim by claim_id. "
                    + _repair_shape_instruction(diagnostics)
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    }


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


def parse_reviewer_finding_payload(
    raw: object,
    *,
    claim_units: tuple[CandidateClaimUnit, ...],
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
    )
    return result.findings[0]


def parse_reviewer_final_payload(
    raw: object,
    *,
    findings: tuple[ReviewerFinding, ...],
    claim_units: tuple[CandidateClaimUnit, ...],
    document_consistency: bool = False,
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
    evidence_ids: tuple[str, ...] = (),
    required_candidate_claim_ids: tuple[str, ...] = (),
    handoff: ExploreHandoff | None = None,
    candidate: str = "",
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
    return ReviewerResult(
        verdict=verdict,
        confidence=float(confidence),
        findings=tuple(findings),
        reason=_clip(reason),
        document_consistency=assessment,
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


def rewrite_changes_any_reviewed_claim(
    candidate: str,
    original_claim_units: tuple[CandidateClaimUnit, ...],
    findings: tuple[ReviewerFinding, ...],
) -> bool:
    """Require at least one addressed reviewer claim to change after the single rewrite.

    Reviewer issue/action text is advisory, so the runtime does not require
    every finding to be removed.  A rewrite that leaves every addressed claim
    Markdown-normalized identical is a deterministic no-op and must fail closed.
    """

    normalized_candidate = _normalize_markdown(candidate)
    original_by_id = {unit.claim_id: unit.text for unit in original_claim_units}
    addressed_claims = tuple(
        normalized
        for finding in findings
        for normalized in (_normalize_markdown(original_by_id.get(finding.claim_id, finding.claim)),)
        if normalized
    )
    if not addressed_claims:
        return True
    return any(claim not in normalized_candidate for claim in addressed_claims)


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
        "arguments_type",
        "json_error_category",
        "tool_name",
        "call_index",
        "tool_call_count",
        "accepted_candidate_defect_count",
        "rejected_candidate_defect_count",
        "document_consistency_keys",
        "expected_document_consistency_keys",
    }
    return {key: diagnostics[key] for key in allowed if key in diagnostics}


def _repair_shape_instruction(diagnostics: Mapping[str, Any]) -> str:
    """Describe the failed schema rule without repeating provider content."""

    code = str(diagnostics.get("error_code") or "")
    common = (
        "Use report_read_only_finding once per candidate defect, then call submit_read_only_review with "
        "verdict, confidence, and reason only. Keep the complete output under 9000 characters and use "
        "unique known claim IDs."
    )
    if code == "findings_too_many":
        return "Report no more than 8 highest-risk findings. " + common
    if code == "response_too_large":
        return "Make reason, issue, and action concise; do not exceed 9000 characters. " + common
    if code == "pass_with_findings":
        return "A pass verdict must have no reported findings; otherwise choose revise or unverified with 1 to 8 findings. " + common
    if code == "nonpassing_without_findings":
        return "A revise or unverified verdict needs 1 to 8 report_read_only_finding calls. " + common
    if code in {"finding_claim_invalid", "finding_claim_mismatch"}:
        return (
            "Do not include claim text in report_read_only_finding; Runtime binds the exact candidate_claims text "
            "from the selected claim_id. If using legacy claim text, it must exactly match that claim_id. "
            + common
        )
    if code in {"source_material_gap_finding", "finding_scope_invalid"}:
        return (
            "Do not submit source_material_gap findings. Findings must be candidate_defect items whose action changes "
            "the candidate answer. If only source materials need an owner decision and the candidate already reports that "
            "gap accurately, submit pass with no reported findings; otherwise keep only candidate_defect findings. "
            + common
        )
    if code == "candidate_defect_findings_missing":
        return (
            "A previous repair attempt contained valid candidate_defect findings. Keep those candidate defects in this "
            "submission by resubmitting the same claim_id with finding_scope=candidate_defect plus issue/action only. "
            "Do not include claim text; Runtime binds the canonical candidate text by claim_id. Do not replace them "
            "with pass unless the candidate_defect findings are no longer present in the repaired output request. "
            + common
        )
    if code in {"output_tool_arguments_type_invalid", "output_tool_arguments_json_invalid"}:
        return (
            "Tool arguments must be a JSON string matching the selected output tool schema. Do not pass a native object, "
            "plain text, or malformed JSON. "
            + common
        )
    if code == "output_tool_final_not_last":
        return (
            "Call report_read_only_finding zero or more times first; submit_read_only_review must be the final output "
            "call in the response. Do not emit findings after the final verdict. "
            + common
        )
    if code in {"output_tool_call_id_missing", "output_tool_call_id_duplicate"}:
        return (
            "Every output tool call must have one unique non-empty tool_call id so the reviewer transcript can pair "
            "assistant tool calls with tool results. "
            + common
        )
    if code == "output_tool_multiple_final_calls":
        return "Submit exactly one final verdict. " + common
    if code == "finding_limit_exceeded":
        return "Do not report more than 8 findings. Submit the final verdict after the accepted findings. " + common
    if code == "document_consistency_evidence_roles_overlap":
        return (
            "For document_consistency, conflict_evidence_ids and supporting_evidence_ids must be disjoint. "
            "For reported_unresolved, conditional_reconciliation, or asserted_reconciled, set supporting_evidence_ids to []. "
            "Only explicitly_supported_reconciliation may use non-empty supporting_evidence_ids, and only for independent "
            "non-visual read_file lifecycle or precedence support. "
            + common
        )
    if code == "document_consistency_keys_invalid":
        return document_consistency_rejection_hint(code) + " " + common
    if code == "document_consistency_support_requires_explicit_stance":
        return (
            "For document_consistency, set supporting_evidence_ids to [] unless stance is explicitly_supported_reconciliation. "
            "reported_unresolved, conditional_reconciliation, and asserted_reconciled must not include support ids. "
            + common
        )
    if code == "document_conflict_evidence_insufficient":
        return document_consistency_rejection_hint(code) + " Do not cite only one side of the conflict. " + common
    if code in {"document_supporting_evidence_invalid", "document_supporting_evidence_unknown", "document_supporting_evidence_duplicate"}:
        return (
            "For document_consistency, keep supporting_evidence_ids empty unless the stance is explicitly_supported_reconciliation "
            "and the ids cite independent non-visual read_file lifecycle or precedence support. "
            + common
        )
    if code == "document_consistency_finding_reconciles_conflict":
        return (
            "For document_consistency with unresolved or conditional conflict stance, finding issue/action must change "
            "the candidate answer without inventing artifact priority, lifecycle, historical/current role, or a resolved "
            "conflict. Keep valid unrelated candidate_defect findings, but remove or rewrite the contradictory finding. "
            + common
        )
    return "Use the shallow finding tool and final submit tool with the required verdict/finding cardinality. " + common


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

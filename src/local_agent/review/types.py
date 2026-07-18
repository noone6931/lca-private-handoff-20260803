"""Stable typed values for the isolated read-only reviewer."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .document_consistency import DocumentConsistencyAssessment
from .handoff import ExploreHandoff
from .readiness import ImplementationReadinessAssessment


ReviewerVerdict = Literal["pass", "revise", "unverified"]
ReviewerFindingScope = Literal["candidate_defect", "source_material_gap"]
CandidateClaimRole = Literal["requirement_fact", "source_fact", "proposal", "pending", "other"]
MAX_REVIEWER_FINDINGS = 8
MAX_REVIEWER_RESPONSE_CHARS = 9000
MAX_REVIEWER_SCHEMA_REPAIRS = 2
MAX_REVIEWER_OUTPUT_LIFECYCLE_ERRORS = 2
MAX_REVIEWER_CAPACITY_DIRECTIVES = 2
REVIEWER_OUTPUT_TOOL_NAME = "submit_read_only_review"
REVIEWER_FINDING_TOOL_NAME = "report_read_only_finding"
MAX_CLAIM_UNITS = 100
MAX_CLAIM_UNIT_CHARS = 500
MAX_CLAIM_TOTAL_CHARS = 40000
MAX_TRANSPORT_RESIDUAL_PRUNE_CLAIMS = 2
MAX_TRANSPORT_RESIDUAL_PROJECTION_ROUNDS = 2


@dataclass(frozen=True)
class CandidateClaimUnit:
    """A stable, bounded addressable unit from the candidate Markdown."""

    claim_id: str
    text: str
    locator_context: str = ""
    section_context: str = ""
    claim_role: CandidateClaimRole = "other"

    def to_dict(self) -> dict[str, str]:
        payload = {"claim_id": self.claim_id, "text": self.text, "claim_role": self.claim_role}
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
    implementation_readiness: ImplementationReadinessAssessment | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "findings": [finding.to_dict() for finding in self.findings],
            "reason": self.reason,
            "document_consistency": self.document_consistency.to_dict() if self.document_consistency else None,
            "implementation_readiness": (
                self.implementation_readiness.to_dict() if self.implementation_readiness else None
            ),
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
    transport_original_omitted_count: int = 0
    transport_pruned_claim_ids: tuple[str, ...] = ()
    transport_projection_rounds: int = 0
    verdict: str | None = None
    reason: str | None = None
    findings: tuple[ReviewerFinding, ...] = ()
    rewrite_closure_findings: tuple[ReviewerFinding, ...] = ()
    claim_units: tuple[CandidateClaimUnit, ...] = ()
    document_consistency: DocumentConsistencyAssessment | None = None
    document_consistency_handoff_signature: tuple[tuple[str, ...], ...] = ()
    implementation_readiness: ImplementationReadinessAssessment | None = None
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
    rewrite_closure_checks: int = 0
    rewrite_closure_acceptances: int = 0
    rewrite_verification_rounds: int = 0
    safe_partial_emitted: bool = False

    def reset(self) -> None:
        self.attempted = False
        self.rewrite_requested = False
        self.transport_rewrite_requested = False
        self.transport_rewrite_accepted = False
        self.transport_rewrite_exhausted = False
        self.transport_original_omitted_count = 0
        self.transport_pruned_claim_ids = ()
        self.transport_projection_rounds = 0
        self.verdict = None
        self.reason = None
        self.findings = ()
        self.rewrite_closure_findings = ()
        self.claim_units = ()
        self.document_consistency = None
        self.document_consistency_handoff_signature = ()
        self.implementation_readiness = None
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
        self.rewrite_closure_checks = 0
        self.rewrite_closure_acceptances = 0
        self.rewrite_verification_rounds = 0
        self.safe_partial_emitted = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "rewrite_requested": self.rewrite_requested,
            "transport_rewrite_requested": self.transport_rewrite_requested,
            "transport_rewrite_accepted": self.transport_rewrite_accepted,
            "transport_rewrite_exhausted": self.transport_rewrite_exhausted,
            "transport_original_omitted_count": self.transport_original_omitted_count,
            "transport_pruned_claim_ids": list(self.transport_pruned_claim_ids),
            "transport_projection_rounds": self.transport_projection_rounds,
            "verdict": self.verdict,
            "reason": self.reason,
            "reviewed_claim_ids": [item.claim_id for item in self.findings],
            "reviewed_claim_count": len({item.claim_id for item in self.findings}),
            "rewrite_closure_claim_ids": [item.claim_id for item in self.rewrite_closure_findings],
            "document_consistency_stance": self.document_consistency.stance if self.document_consistency else None,
            "implementation_readiness_status": (
                self.implementation_readiness.status if self.implementation_readiness else None
            ),
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
            "rewrite_closure_checks": self.rewrite_closure_checks,
            "rewrite_closure_acceptances": self.rewrite_closure_acceptances,
            "rewrite_verification_rounds": self.rewrite_verification_rounds,
        }


@dataclass(frozen=True)
class ReviewerPhaseOutcome:
    kind: Literal["not_applicable", "pass", "rewrite", "unverified"]
    rewrite_message: str = ""
    terminal_message: str = ""
    reason: str = ""
    safe_partial_report: str = ""
    final_candidate: str = ""

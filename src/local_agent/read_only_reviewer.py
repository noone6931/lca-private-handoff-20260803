"""Isolated, bounded reviewer protocol for high-risk read-only conclusions."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from .explore_handoff import ExploreHandoff
from .task_contract import RequirementContract


ReviewerVerdict = Literal["pass", "revise", "unverified"]
MAX_REVIEWER_FINDINGS = 8
MAX_REVIEWER_RESPONSE_CHARS = 9000


@dataclass(frozen=True)
class ReviewerFinding:
    claim: str
    issue: str
    action: str

    def to_dict(self) -> dict[str, str]:
        return {"claim": self.claim, "issue": self.issue, "action": self.action}


@dataclass(frozen=True)
class ReviewerResult:
    verdict: ReviewerVerdict
    confidence: float
    findings: tuple[ReviewerFinding, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "findings": [finding.to_dict() for finding in self.findings],
            "reason": self.reason,
        }


@dataclass
class ReadOnlyReviewState:
    attempted: bool = False
    rewrite_requested: bool = False
    verdict: str | None = None
    reason: str | None = None
    findings: tuple[ReviewerFinding, ...] = ()

    def reset(self) -> None:
        self.attempted = False
        self.rewrite_requested = False
        self.verdict = None
        self.reason = None
        self.findings = ()

    def snapshot(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "rewrite_requested": self.rewrite_requested,
            "verdict": self.verdict,
            "reason": self.reason,
            "findings": [item.to_dict() for item in self.findings],
        }


@dataclass(frozen=True)
class ReviewerPhaseOutcome:
    kind: Literal["not_applicable", "pass", "rewrite", "unverified"]
    rewrite_message: str = ""
    terminal_message: str = ""
    reason: str = ""


def should_review_read_only_candidate(contract: RequirementContract | None, request: str | None) -> bool:
    """Consume the typed task-owner profile; never reclassify natural language."""

    if contract is None or contract.evidence_domain != "repository_code":
        return False
    if contract.inspection_forbidden or contract.workspace_metadata_subject:
        return False
    return contract.read_only_review_profile in {"owner_impact", "design"}


def reviewer_messages(handoff: ExploreHandoff, candidate: str) -> list[dict[str, str]]:
    """Return an isolated reviewer transcript with no primary conversation history."""

    system = """You are the read-only evidence reviewer for a coding agent.
Return exactly one JSON object. You have no tools and must never assume unseen repository facts.

Review contract:
- A direct owner is justified only by evidence that explicitly binds the requested behavior to a path, symbol, or call chain.
- Similar names, same-domain payment/order/fee capabilities, and general reusable code are analogous candidates, never verified owners.
- Missing or incomplete searches mean unlocated within their stated scope, not absent everywhere.
- Requirement facts, repository facts, proposals, and open questions must remain distinct.
- A proposal must not be worded as an existing table, class, endpoint, service, approval flow, numbering prefix, or integration unless the handoff explicitly supports it.

Use schema: {"verdict":"pass|revise|unverified","confidence":0.0,"findings":[{"claim":"exact unsupported candidate excerpt","issue":"...","action":"..."}],"reason":"..."}.
Choose revise when the candidate can be corrected using the handoff. Choose unverified when the candidate cannot safely make the requested factual conclusion."""
    payload = {
        "kind": "LCA_READ_ONLY_EVIDENCE_REVIEW",
        "handoff": handoff.to_dict(),
        "candidate_draft": candidate,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def parse_reviewer_result(content: object) -> ReviewerResult:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("reviewer returned no JSON content")
    if len(content) > MAX_REVIEWER_RESPONSE_CHARS:
        raise ValueError("reviewer response exceeded the bounded output limit")
    raw = _json_object(content)
    if not isinstance(raw, Mapping):
        raise ValueError("reviewer response must be a JSON object")
    verdict = raw.get("verdict")
    if verdict not in {"pass", "revise", "unverified"}:
        raise ValueError("reviewer verdict must be pass, revise, or unverified")
    confidence = raw.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
        raise ValueError("reviewer confidence must be a number between 0 and 1")
    findings_value = raw.get("findings")
    if not isinstance(findings_value, list) or len(findings_value) > MAX_REVIEWER_FINDINGS:
        raise ValueError("reviewer findings must be a bounded list")
    findings: list[ReviewerFinding] = []
    for item in findings_value:
        if not isinstance(item, Mapping):
            raise ValueError("reviewer finding must be an object")
        claim, issue, action = (item.get("claim"), item.get("issue"), item.get("action"))
        if not all(isinstance(value, str) and value.strip() for value in (claim, issue, action)):
            raise ValueError("reviewer finding requires claim, issue, and action")
        findings.append(ReviewerFinding(_clip(claim), _clip(issue), _clip(action)))
    reason = raw.get("reason")
    if not isinstance(reason, str):
        raise ValueError("reviewer reason must be text")
    if verdict == "pass" and findings:
        raise ValueError("a passing reviewer result must not contain blocking findings")
    if verdict in {"revise", "unverified"} and not findings:
        raise ValueError("a non-passing reviewer result needs at least one finding")
    return ReviewerResult(verdict=verdict, confidence=float(confidence), findings=tuple(findings), reason=_clip(reason))


def reviewer_rewrite_message(result: ReviewerResult) -> str:
    """Render a bounded runtime instruction, not the reviewer's raw transcript."""

    lines = [
        "[Read-only evidence review]",
        "Revise the candidate answer once, without calling tools. Preserve only claims supported by the existing handoff.",
        "Do not call an analogous/reusable candidate the verified owner. Keep unlocated owner/DDL/template/API facts as unverified, and label new design as proposal.",
    ]
    for finding in result.findings:
        lines.append(f"- Claim: {finding.claim}; issue: {finding.issue}; action: {finding.action}")
    return "\n".join(lines)


def rewrite_complies_with_review(candidate: str, result: ReviewerResult) -> bool:
    """Reject an unchanged unsupported claim after the one permitted rewrite.

    This only compares exact spans supplied in the typed reviewer result.  It is
    not a second natural-language classifier and cannot turn a nearby word into
    a business-specific guard.
    """

    normalized_candidate = _normalize_span(candidate)
    return all(_normalize_span(finding.claim) not in normalized_candidate for finding in result.findings)


def _json_object(content: str) -> object:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE)
    return json.loads(stripped)


def _clip(value: str, limit: int = 420) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "..."


def _normalize_span(value: str) -> str:
    return re.sub(r"\s+", "", value or "").casefold()

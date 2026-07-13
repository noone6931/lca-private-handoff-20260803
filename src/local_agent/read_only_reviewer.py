"""Isolated, bounded reviewer protocol for high-risk read-only conclusions."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .explore_handoff import ExploreHandoff
from .task_contract import RequirementContract


ReviewerVerdict = Literal["pass", "revise", "unverified"]
MAX_REVIEWER_FINDINGS = 8
MAX_REVIEWER_RESPONSE_CHARS = 9000
MAX_CLAIM_UNITS = 20
MAX_CLAIM_UNIT_CHARS = 500
MAX_CLAIM_TOTAL_CHARS = 10000


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
    claim: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"claim_id": self.claim_id, "claim": self.claim, "issue": self.issue, "action": self.action}


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
    claim_units: tuple[CandidateClaimUnit, ...] = ()

    def reset(self) -> None:
        self.attempted = False
        self.rewrite_requested = False
        self.verdict = None
        self.reason = None
        self.findings = ()
        self.claim_units = ()

    def snapshot(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "rewrite_requested": self.rewrite_requested,
            "verdict": self.verdict,
            "reason": self.reason,
            "reviewed_claim_ids": [item.claim_id for item in self.findings],
            "reviewed_claim_count": len({item.claim_id for item in self.findings}),
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
Return exactly one JSON object. You have no tools and must never assume unseen repository facts.

Review contract:
- A direct owner is justified only by evidence that explicitly binds the requested behavior to a path, symbol, or call chain.
- Similar names, same-domain payment/order/fee capabilities, and general reusable code are analogous candidates, never verified owners.
- Missing or incomplete searches mean unlocated within their stated scope, not absent everywhere.
- Requirement facts, repository facts, proposals, and open questions must remain distinct.
- A proposal must not be worded as an existing table, class, endpoint, service, approval flow, numbering prefix, or integration unless the handoff explicitly supports it.
- When the handoff has no explicit direct binding, do not say a main owner/module judgment is correct or mostly correct. Treat same-domain code as observed or analogous and leave the owner unlocated.

Use schema: {"verdict":"pass|revise|unverified","confidence":0.0,"findings":[{"claim_id":"c001","claim":"optional human-readable summary","issue":"...","action":"..."}],"reason":"..."}.
For every finding, choose exactly one claim_id from candidate_claims. Never invent a claim_id. The optional claim field is for people, not an address.
Choose revise when the candidate can be corrected using the handoff. Choose unverified when the candidate cannot safely make the requested factual conclusion."""
    payload = {
        "kind": "LCA_READ_ONLY_EVIDENCE_REVIEW",
        "handoff": handoff.to_dict(),
        "candidate_claims": [unit.to_dict() for unit in claim_units],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def parse_reviewer_result(content: object, *, claim_units: tuple[CandidateClaimUnit, ...]) -> ReviewerResult:
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
    known_claim_ids = {unit.claim_id for unit in claim_units}
    used_claim_ids: set[str] = set()
    for item in findings_value:
        if not isinstance(item, Mapping):
            raise ValueError("reviewer finding must be an object")
        claim_id, claim, issue, action = (item.get("claim_id"), item.get("claim"), item.get("issue"), item.get("action"))
        if not isinstance(claim_id, str) or claim_id not in known_claim_ids or claim_id in used_claim_ids:
            raise ValueError("reviewer finding claim_id must be a unique candidate claim address")
        if not all(isinstance(value, str) and value.strip() for value in (issue, action)):
            raise ValueError("reviewer finding requires issue and action")
        if claim is not None and not isinstance(claim, str):
            raise ValueError("reviewer finding claim must be text when present")
        used_claim_ids.add(claim_id)
        findings.append(ReviewerFinding(claim_id, _clip(issue), _clip(action), _clip(claim or "")))
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


def _clip(value: str, limit: int = 420) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "..."


def _normalize_span(value: str) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def _normalize_markdown(value: str) -> str:
    without_presentation = re.sub(r"[`*_~#>]", "", value or "")
    return _normalize_span(without_presentation.replace("|", " "))


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
        head = MAX_CLAIM_UNITS // 2
        tail = MAX_CLAIM_UNITS - head
        selected = [*indexed[:head], *indexed[-tail:]]
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

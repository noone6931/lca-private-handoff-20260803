"""Pure collection of deterministic hard audits before semantic review."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import FinalAnswerContext
from .models import FinalAnswerSteerer
from .models import FinalAnswerSteeringSeverity
from .models import SteeringDecision


PRE_REVIEW_AUDIT_KINDS = frozenset(
    {
        "requirement_evidence",
        "source_grounded_numeric",
        "source_evidence_false_negative",
        "tool_usage_evidence",
        "negative_existence",
        "completion_audit",
    }
)


def should_aggregate_pre_review_audits(context: FinalAnswerContext) -> bool:
    return bool(
        context.requirement_contract is not None
        and context.requirement_contract.read_only_review_profile in {"owner_impact", "design"}
        and context.read_only_explore_finalized
    )


@dataclass(frozen=True)
class PreReviewAudit:
    """Sanitized hard-audit facts consumed by candidate preparation."""

    categories: tuple[str, ...]
    details: tuple[tuple[str, str], ...]

    def render(self) -> str:
        return (
            "The candidate has deterministic evidence issues. Resolve every category in this rewrite:\n"
            + "\n".join(f"- [{kind}] {detail}" for kind, detail in self.details)
            + "\nUse only collected evidence. Scope unsupported source/path/owner conclusions as unlocated or unverified."
        )


def collect_pre_review_audit(
    context: FinalAnswerContext,
    steerers: Iterable[FinalAnswerSteerer],
) -> PreReviewAudit | None:
    """Collect hard preparation facts without owning a retry lifecycle."""

    if not should_aggregate_pre_review_audits(context):
        return None
    decisions = tuple(
        decision
        for steerer in steerers
        if getattr(steerer, "kind", "") in PRE_REVIEW_AUDIT_KINDS
        if (decision := steerer.decide(context)) is not None
        and decision.severity == FinalAnswerSteeringSeverity.HARD
    )
    if len(decisions) < 2:
        return None
    return PreReviewAudit(
        categories=tuple(dict.fromkeys(decision.kind for decision in decisions)),
        details=_audit_details(decisions),
    )


def _audit_details(decisions: Iterable[SteeringDecision]) -> tuple[tuple[str, str], ...]:
    details: list[tuple[str, str]] = []
    for decision in decisions:
        payload = decision.payload
        issues = payload.get("issues") or payload.get("missing") or payload.get("unobserved_tools")
        if isinstance(issues, (list, tuple)):
            detail = "; ".join(str(item) for item in issues[:3]) or "evidence mismatch"
        elif issues:
            detail = str(issues)
        else:
            detail = "evidence mismatch"
        details.append((decision.kind, detail))
    return tuple(details)

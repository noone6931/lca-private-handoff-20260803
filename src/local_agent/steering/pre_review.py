"""Bounded aggregation of deterministic hard audits before semantic review."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
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
MAX_PRE_REVIEW_AUDIT_ROUNDS = 2


def should_aggregate_pre_review_audits(context: FinalAnswerContext) -> bool:
    return bool(
        context.requirement_contract is not None
        and context.requirement_contract.read_only_review_profile in {"owner_impact", "design"}
        and context.read_only_explore_finalized
    )


@dataclass
class PreReviewAuditCoordinator:
    """Own one bounded hard-audit lifecycle per evidence revision.

    This mirrors OMP's directive ownership: a candidate is either given one
    aggregate correction request or the directive reaches an explicit terminal
    state. Individual legacy steerer findings remain observable categories;
    they no longer take turns consuming finalization budget.
    """

    evidence_revision: str = ""
    rounds: int = 0
    exhausted: bool = False
    seen_candidates: set[str] = field(default_factory=set)
    categories: dict[str, int] = field(default_factory=dict)

    def reset(self) -> None:
        self.evidence_revision = ""
        self.rounds = 0
        self.exhausted = False
        self.seen_candidates.clear()
        self.categories.clear()

    def snapshot(self) -> dict[str, object]:
        return {
            "rounds": self.rounds,
            "categories": dict(sorted(self.categories.items())),
            "exhausted": self.exhausted,
        }

    def decide(
        self,
        context: FinalAnswerContext,
        steerers: Iterable[FinalAnswerSteerer],
    ) -> SteeringDecision | None:
        # This is the pre-review convergence owner for the high-risk read-only
        # profiles that subsequently enter the isolated semantic reviewer. The
        # existing implementation/delivery and ordinary read-only flows retain
        # their established owners and budgets.
        if not should_aggregate_pre_review_audits(context):
            return None
        decisions = tuple(
            decision
            for steerer in steerers
            if getattr(steerer, "kind", "") in PRE_REVIEW_AUDIT_KINDS
            if (decision := steerer.decide(context)) is not None
            and decision.severity == FinalAnswerSteeringSeverity.HARD
        )
        # One existing hard gate should keep its established, precise behavior.
        # Aggregation is specifically for the ping-pong condition: multiple
        # independent evidence auditors competing for the same candidate.
        if len(decisions) < 2:
            return None

        revision = _evidence_revision(context)
        candidate = _candidate_fingerprint(context.content)
        if revision != self.evidence_revision:
            self.evidence_revision = revision
            self.rounds = 0
            self.exhausted = False
            self.seen_candidates.clear()
        categories = tuple(dict.fromkeys(decision.kind for decision in decisions))
        for category in categories:
            self.categories[category] = self.categories.get(category, 0) + 1
        if self.exhausted or candidate in self.seen_candidates or self.rounds >= MAX_PRE_REVIEW_AUDIT_ROUNDS:
            self.exhausted = True
            return SteeringDecision(
                kind="pre_review_audit",
                message="",
                payload={
                    "pre_review_audit": self.snapshot(),
                    "categories": list(categories),
                },
                terminal_message=(
                    "未完成/未验证：候选答复连续未通过同一证据 revision 的合并事实审计。"
                    "为避免继续用多个最终纠偏轮流改写或释放未经审查的草稿，本次停止并保留待确认项。"
                ),
                counted_kinds=categories,
            )

        self.rounds += 1
        self.seen_candidates.add(candidate)
        allow_tools = set().union(*(decision.temporary_tool_allowlist or set() for decision in decisions))
        # The bounded owner/design exploration phase has ended. It must not be
        # reopened merely because the draft made an unsupported absence claim.
        if context.read_only_explore_finalized:
            allow_tools.clear()
        details = _audit_details(decisions)
        message = (
            "Runtime steering: the previous candidate has multiple hard evidence issues. "
            "Resolve every category below in one rewrite; do not let one correction erase another.\n"
            + "\n".join(f"- [{kind}] {detail}" for kind, detail in details)
        )
        if allow_tools:
            message += "\nUse only the bounded evidence tools already allowed by this task, then answer from observed evidence."
        else:
            message += (
                "\nDo not call tools. Use the evidence already collected. Any unsupported source/path/owner conclusion "
                "must be scoped as unlocated or unverified, not rewritten as a global absence."
            )
        return SteeringDecision(
            kind="pre_review_audit",
            message=message,
            payload={
                "pre_review_audit": self.snapshot(),
                "categories": list(categories),
                "bounded_explore": context.read_only_explore_finalized,
            },
            force_final_answer_without_tools=not allow_tools,
            temporary_tool_allowlist=allow_tools or None,
            counted_kinds=categories,
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


def _candidate_fingerprint(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8", "replace")).hexdigest()


def _evidence_revision(context: FinalAnswerContext) -> str:
    payload = {
        "tool_results": [
            {
                "name": item.name,
                "path": item.path,
                "is_error": item.is_error,
                "useless": item.useless,
                "metadata": dict(item.metadata),
            }
            for item in context.tool_results
        ],
        "source": [(item.path, hashlib.sha256(item.content.encode("utf-8", "replace")).hexdigest()) for item in context.source_evidence],
        "requirements": [(item.path, item.root, item.scope) for item in context.requirement_evidence],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8", "replace")).hexdigest()

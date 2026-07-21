"""Final-answer steering for current and attributed tool usage."""
from __future__ import annotations

from ..session.execution_evidence import trusted_prior_execution_attributions
from .evidence import _observed_tool_names, phantom_tool_evidence_claims
from .models import FinalAnswerContext, SteeringDecision, final_answer_request_summary


class ToolUsageEvidenceSteerer:
    """Keep final claims about tool evidence aligned with observed tool results."""

    kind = "tool_usage_evidence"

    def __init__(self, *, max_steers: int) -> None:
        self._max_steers = max_steers

    def decide(self, context: FinalAnswerContext) -> SteeringDecision | None:
        if context.steer_counts.get(self.kind, 0) >= self._max_steers:
            return None
        prior = trusted_prior_execution_attributions(context.messages)
        claimed_missing_tools = phantom_tool_evidence_claims(context.content, context.tool_results, prior)
        if not claimed_missing_tools:
            return None
        tools = ", ".join(claimed_missing_tools)
        steering = (
            "Runtime steering: the previous final answer claimed evidence, invocation, or an empty result from tools "
            "that did not run in this task or lacked an exact runtime-generated prior attribution. Do not call tools. "
            "Rewrite using current observed results or an exact projected prior reference; a prior reference is not "
            "current-run or current-filesystem proof.\n"
            f"- Unsupported tool-evidence claims: {tools}\n"
            f"- Tools actually observed: {', '.join(_observed_tool_names(context.tool_results)) or 'none'}"
            f"{final_answer_request_summary(context.request)}"
        )
        return SteeringDecision(
            kind=self.kind,
            message=steering,
            payload={"unobserved_tools": list(claimed_missing_tools)},
        )


__all__ = ["ToolUsageEvidenceSteerer"]

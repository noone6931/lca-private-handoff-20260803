from __future__ import annotations

from dataclasses import dataclass
import time


MAX_FORCED_FINAL_ANSWER_CONTINUATIONS = 8
MAX_FINALIZATION_ATTEMPTS = 8
FINAL_ANSWER_STEERING_HARD = "hard"
FINAL_ANSWER_STEERING_PRESENTATION = "presentation"


@dataclass(frozen=True)
class UnresolvedFinalAnswerGate:
    """A correctness gate that could not be rewritten before the run stopped."""

    kind: str
    reason: str


@dataclass(frozen=True)
class FinalizationRequestOutcome:
    accepted: bool
    reason: str | None = None


@dataclass(frozen=True)
class ForcedFinalProtocolOutcome:
    steering_kind: str
    artifact_kind: str
    suppressed_tool_calls: int


class FinalizationCoordinator:
    """Own terminal-phase final-answer rewrite state for a single run."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.pending_force_final = False
        self.continuations = 0
        self.aggregate_attempts = 0
        self.kind = "runtime_forced_final"
        self.severity = FINAL_ANSWER_STEERING_HARD
        self.unresolved_gate: UnresolvedFinalAnswerGate | None = None
        self.terminal_phase_entered = False
        self.forced_final_protocol_violations = 0

    def can_queue(self) -> bool:
        return self.continuations < MAX_FORCED_FINAL_ANSWER_CONTINUATIONS

    def request(
        self,
        *,
        kind: str,
        severity: str = FINAL_ANSWER_STEERING_HARD,
        deadline_monotonic: float | None = None,
        reserve_seconds: float = 0.0,
        now: float | None = None,
    ) -> FinalizationRequestOutcome:
        current = time.monotonic() if now is None else now
        if deadline_monotonic is not None and deadline_monotonic - current <= reserve_seconds:
            return FinalizationRequestOutcome(False, "deadline_reserve")
        if self.aggregate_attempts >= MAX_FINALIZATION_ATTEMPTS:
            return FinalizationRequestOutcome(False, "aggregate_limit")
        if not self.can_queue():
            return FinalizationRequestOutcome(False, "continuation_limit")
        self.aggregate_attempts += 1
        self.continuations += 1
        self.pending_force_final = True
        self.kind = kind
        self.severity = severity
        self.terminal_phase_entered = True
        return FinalizationRequestOutcome(True)

    def begin_forced_final_turn(self) -> bool:
        pending = self.pending_force_final
        self.pending_force_final = False
        return pending

    def clear_pending_request(self) -> None:
        self.pending_force_final = False
        self.kind = "runtime_forced_final"
        self.severity = FINAL_ANSWER_STEERING_HARD

    def allows_draft_fallback(self) -> bool:
        return self.severity == FINAL_ANSWER_STEERING_PRESENTATION

    def block_unverified(self, *, kind: str, reason: str) -> None:
        self.unresolved_gate = UnresolvedFinalAnswerGate(kind=kind, reason=reason)

    def observe_tool_progress(self) -> None:
        # A real tool step can clear the short no-tool retry window, but we keep
        # the aggregate finalization budget consumed for the run.
        self.continuations = 0
        self.clear_pending_request()

    def reject_forced_final_protocol_response(
        self,
        *,
        artifact_kind: str,
        suppressed_tool_calls: int = 0,
    ) -> ForcedFinalProtocolOutcome:
        """Close a terminal-only turn that still carried a provider tool protocol."""
        self.forced_final_protocol_violations += 1
        self.pending_force_final = False
        return ForcedFinalProtocolOutcome(
            steering_kind=self.kind,
            artifact_kind=artifact_kind,
            suppressed_tool_calls=max(0, suppressed_tool_calls),
        )

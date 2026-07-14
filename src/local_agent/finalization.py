from __future__ import annotations

from dataclasses import dataclass
import time


MAX_FORCED_FINAL_ANSWER_CONTINUATIONS = 8
MAX_FINALIZATION_ATTEMPTS = 8
MAX_NON_SUBSTANTIVE_RESPONSE_RETRIES = 3
# OMP retries invalid terminal stops up to three times.  LCA applies the same
# bounded shape to forced-final provider protocol artifacts, while still
# failing closed on exhaustion.
MAX_FORCED_FINAL_PROTOCOL_RECOVERIES = 3
FINALIZATION_REWRITE_RESERVE_SECONDS = 45.0
MIN_FINALIZATION_REWRITE_RESERVE_SECONDS = 1.0
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
    retry: bool
    attempt: int
    max_attempts: int
    reason: str | None = None


@dataclass(frozen=True)
class NonSubstantiveResponseOutcome:
    retry: bool
    attempt: int
    max_attempts: int
    forced_final: bool
    kind: str


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
        self.forced_final_protocol_recoveries = 0
        self.forced_final_protocol_recovery_exhausted = 0
        self.non_substantive_response_retries = 0
        self.non_substantive_response_exhausted = 0
        self._consecutive_non_substantive_responses = 0
        self._consecutive_forced_final_protocol_responses = 0

    def can_queue(self) -> bool:
        return self.continuations < MAX_FORCED_FINAL_ANSWER_CONTINUATIONS

    def effective_reserve_seconds(
        self,
        *,
        deadline_monotonic: float | None,
        run_started_monotonic: float | None,
    ) -> float:
        """Reserve time for a terminal rewrite without disabling short runs.

        OMP checks the actual deadline at each turn boundary. LCA retains a
        bounded correctness reserve, scaled down for short user budgets.
        """

        if deadline_monotonic is None or run_started_monotonic is None:
            return FINALIZATION_REWRITE_RESERVE_SECONDS
        total_budget = max(0.0, deadline_monotonic - run_started_monotonic)
        if total_budget <= 0:
            return FINALIZATION_REWRITE_RESERVE_SECONDS
        return min(
            FINALIZATION_REWRITE_RESERVE_SECONDS,
            max(MIN_FINALIZATION_REWRITE_RESERVE_SECONDS, total_budget * 0.2),
        )

    def rewrite_skip_reason(
        self,
        *,
        deadline_monotonic: float | None,
        run_started_monotonic: float | None,
        now: float | None = None,
    ) -> str | None:
        current = time.monotonic() if now is None else now
        reserve = self.effective_reserve_seconds(
            deadline_monotonic=deadline_monotonic,
            run_started_monotonic=run_started_monotonic,
        )
        if deadline_monotonic is not None and deadline_monotonic - current <= reserve:
            return "deadline_reserve"
        if self.aggregate_attempts >= MAX_FINALIZATION_ATTEMPTS:
            return "aggregate_limit"
        if not self.can_queue():
            return "continuation_limit"
        return None

    def request(
        self,
        *,
        kind: str,
        severity: str = FINAL_ANSWER_STEERING_HARD,
        deadline_monotonic: float | None = None,
        run_started_monotonic: float | None = None,
        now: float | None = None,
    ) -> FinalizationRequestOutcome:
        reason = self.rewrite_skip_reason(
            deadline_monotonic=deadline_monotonic,
            run_started_monotonic=run_started_monotonic,
            now=now,
        )
        if reason is not None:
            return FinalizationRequestOutcome(False, reason)
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
        self._consecutive_forced_final_protocol_responses = 0
        self.clear_pending_request()

    def reject_forced_final_protocol_response(
        self,
        *,
        artifact_kind: str,
        suppressed_tool_calls: int = 0,
        deadline_monotonic: float | None = None,
        run_started_monotonic: float | None = None,
        now: float | None = None,
    ) -> ForcedFinalProtocolOutcome:
        """Reject a terminal-only provider tool protocol and maybe re-arm finalization."""
        self.forced_final_protocol_violations += 1
        self._consecutive_forced_final_protocol_responses += 1
        attempt = self._consecutive_forced_final_protocol_responses
        current_kind = self.kind
        current_severity = self.severity
        reason = "recovery_limit" if attempt > MAX_FORCED_FINAL_PROTOCOL_RECOVERIES else None
        if reason is None:
            request = self.request(
                kind=current_kind,
                severity=current_severity,
                deadline_monotonic=deadline_monotonic,
                run_started_monotonic=run_started_monotonic,
                now=now,
            )
            if request.accepted:
                self.forced_final_protocol_recoveries += 1
                retry = True
            else:
                reason = request.reason or "finalization_rejected"
                self.forced_final_protocol_recovery_exhausted += 1
                self.pending_force_final = False
                retry = False
        else:
            self.forced_final_protocol_recovery_exhausted += 1
            self.pending_force_final = False
            retry = False
        return ForcedFinalProtocolOutcome(
            steering_kind=self.kind,
            artifact_kind=artifact_kind,
            suppressed_tool_calls=max(0, suppressed_tool_calls),
            retry=retry,
            attempt=attempt,
            max_attempts=MAX_FORCED_FINAL_PROTOCOL_RECOVERIES,
            reason=reason,
        )

    def observe_non_substantive_response(self, *, forced_final: bool, kind: str) -> NonSubstantiveResponseOutcome:
        """Bound retries for a provider response that cannot serve as an answer."""

        self._consecutive_non_substantive_responses += 1
        attempt = self._consecutive_non_substantive_responses
        if attempt <= MAX_NON_SUBSTANTIVE_RESPONSE_RETRIES:
            self.non_substantive_response_retries += 1
            if forced_final:
                self.pending_force_final = True
            return NonSubstantiveResponseOutcome(
                retry=True,
                attempt=attempt,
                max_attempts=MAX_NON_SUBSTANTIVE_RESPONSE_RETRIES,
                forced_final=forced_final,
                kind=kind,
            )
        self.non_substantive_response_exhausted += 1
        return NonSubstantiveResponseOutcome(
            retry=False,
            attempt=attempt,
            max_attempts=MAX_NON_SUBSTANTIVE_RESPONSE_RETRIES,
            forced_final=forced_final,
            kind=kind,
        )

    def observe_substantive_response(self) -> None:
        self._consecutive_non_substantive_responses = 0
        self._consecutive_forced_final_protocol_responses = 0

    def terminal_response_snapshot(self) -> dict[str, int]:
        return {
            "non_substantive_retries": self.non_substantive_response_retries,
            "non_substantive_exhausted": self.non_substantive_response_exhausted,
            "forced_final_protocol_recoveries": self.forced_final_protocol_recoveries,
            "forced_final_protocol_recovery_exhausted": self.forced_final_protocol_recovery_exhausted,
        }

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Literal


DirectiveStatus = Literal["pending", "served", "resolved", "rejected", "exhausted"]

# Match OMP's bounded soft-tool escalation principle. This is a per-source
# budget, not a global step cap: a failed discovery correction cannot keep a
# restricted schema alive for the rest of the run.
MAX_DIRECTIVE_ATTEMPTS_PER_SOURCE = 3
MAX_DIRECTIVE_TURNS_PER_SOURCE = 2


@dataclass(frozen=True)
class DirectiveTransition:
    source_kind: str
    status: DirectiveStatus
    reason: str
    allowed_tools: tuple[str, ...]
    attempts: int
    turns: int
    force_truthful_final: bool = False

    def payload(self) -> dict[str, object]:
        return {
            "source": self.source_kind,
            "status": self.status,
            "reason": self.reason,
            "allowed_tools": list(self.allowed_tools),
            "attempts": self.attempts,
            "turns": self.turns,
            "force_truthful_final": self.force_truthful_final,
        }

    def final_message(self) -> str:
        return (
            "Runtime steering: the bounded evidence-correction directive is closed. Do not call more tools. "
            "Rewrite the answer from the evidence already collected; any absence claim that was not proven by the "
            "completed discovery scope must be stated as scoped or unverified."
        )


@dataclass
class _ActiveDirective:
    source_kind: str
    allowed_tools: frozenset[str]
    turn_started: bool = False
    attempts_in_turn: int = 0
    successful_attempts: int = 0
    failed_attempts: int = 0


@dataclass
class TemporaryToolDirectiveOwner:
    """Own bounded, run-scoped temporary tool restrictions.

    The owner deliberately separates a directive's active schema projection
    from its lifecycle. A restriction is consumed by a served model turn and
    every attempted tool call counts, including error/denied/skipped calls.
    A turn is only resolved when an allowed tool produced a successful result;
    provider calls that are rejected by the active schema do not satisfy it.
    """

    _active: _ActiveDirective | None = None
    _source_attempts: dict[str, int] = field(default_factory=dict)
    _source_turns: dict[str, int] = field(default_factory=dict)
    _source_status: dict[str, DirectiveTransition] = field(default_factory=dict)

    def reset(self) -> None:
        self._active = None
        self._source_attempts.clear()
        self._source_turns.clear()
        self._source_status.clear()

    @property
    def active_allowed_tools(self) -> set[str] | None:
        if self._active is None:
            return None
        return set(self._active.allowed_tools)

    @property
    def has_active(self) -> bool:
        return self._active is not None

    def activate(self, source_kind: str, allowed_tools: set[str] | frozenset[str]) -> DirectiveTransition:
        source = source_kind or "runtime_directive"
        tools = tuple(sorted(set(allowed_tools)))
        if self._active is not None:
            self._close_active("replaced")
        if not tools:
            return self._record(source, "rejected", "empty_allowlist", (), False)
        previous = self._source_status.get(source)
        if previous is not None and previous.reason == "not_invoked":
            # The model already elected to answer without consuming this soft
            # directive. Do not keep cycling the same narrowed schema merely
            # because another final-audit pass observed that choice.
            return self._record(source, "rejected", "not_invoked", tools, False)
        if self._source_attempts.get(source, 0) >= MAX_DIRECTIVE_ATTEMPTS_PER_SOURCE:
            return self._record(source, "exhausted", "attempt_limit", tools, True)
        if self._source_turns.get(source, 0) >= MAX_DIRECTIVE_TURNS_PER_SOURCE:
            return self._record(source, "exhausted", "turn_limit", tools, True)
        self._active = _ActiveDirective(source, frozenset(tools))
        return self._record(source, "pending", "activated", tools, False)

    def begin_turn(self) -> DirectiveTransition | None:
        active = self._active
        if active is None:
            return None
        source = active.source_kind
        if self._source_turns.get(source, 0) >= MAX_DIRECTIVE_TURNS_PER_SOURCE:
            self._active = None
            return self._record(source, "exhausted", "turn_limit", tuple(active.allowed_tools), True)
        active.turn_started = True
        active.attempts_in_turn = 0
        self._source_turns[source] = self._source_turns.get(source, 0) + 1
        return self._record(source, "served", "turn_started", tuple(active.allowed_tools), False)

    def reserve_attempt(self, tool_name: str) -> DirectiveTransition | None:
        active = self._active
        if active is None:
            return None
        source = active.source_kind
        active.attempts_in_turn += 1
        self._source_attempts[source] = self._source_attempts.get(source, 0) + 1
        allowed = tool_name in active.allowed_tools
        if self._source_attempts[source] >= MAX_DIRECTIVE_ATTEMPTS_PER_SOURCE:
            # The call is already present in the active schema, so it still
            # executes. The phase queues the one truthful final rewrite after
            # its result is observed.
            self._active = None
            return self._record(source, "exhausted", "attempt_limit", tuple(active.allowed_tools), True)
        if not allowed:
            return self._record(source, "rejected", "tool_not_allowed", tuple(active.allowed_tools), False)
        return self._record(source, "served", "tool_attempt", tuple(active.allowed_tools), False)

    def record_attempt_outcome(
        self,
        transition: DirectiveTransition | None,
        *,
        is_error: bool,
    ) -> DirectiveTransition | None:
        if transition is None:
            return None
        source = transition.source_kind
        active = self._active
        allowed_attempt = transition.reason in {"tool_attempt", "attempt_limit"}
        if active is not None and active.source_kind == source:
            if is_error or not allowed_attempt:
                active.failed_attempts += 1
            else:
                active.successful_attempts += 1
        outcome = "tool_error" if is_error else ("tool_success" if allowed_attempt else "tool_rejected")
        return self._record(
            source,
            transition.status,
            outcome,
            transition.allowed_tools,
            transition.force_truthful_final,
        )

    def finish_turn(self) -> DirectiveTransition | None:
        active = self._active
        if active is None:
            return None
        # A decision can be emitted while handling the previous tool batch.
        # Its first constrained model turn starts on the next loop iteration;
        # do not consume it at the tail of the turn that created it.
        if not active.turn_started:
            return None
        if active.successful_attempts:
            return self._close_active("resolved")
        source = active.source_kind
        self._active = None
        if not active.attempts_in_turn:
            return self._record(source, "rejected", "not_invoked", tuple(active.allowed_tools), False)
        if self._source_turns.get(source, 0) >= MAX_DIRECTIVE_TURNS_PER_SOURCE:
            return self._record(source, "exhausted", "tool_error", tuple(active.allowed_tools), True)
        return self._record(
            source,
            "rejected",
            "tool_error" if active.failed_attempts else "not_invoked",
            tuple(active.allowed_tools),
            False,
        )

    def close_for_terminal(self, reason: str) -> DirectiveTransition | None:
        if self._active is None:
            return None
        return self._close_active(reason or "closed")

    def snapshot(self) -> dict[str, object]:
        sources = {
            source: {
                "attempts": self._source_attempts.get(source, 0),
                "turns": self._source_turns.get(source, 0),
                "status": transition.status,
                "reason": transition.reason,
            }
            for source, transition in sorted(self._source_status.items())
        }
        return {
            "active": self._active.source_kind if self._active is not None else None,
            "sources": sources,
        }

    def _close_active(self, reason: str) -> DirectiveTransition:
        active = self._active
        assert active is not None
        self._active = None
        return self._record(active.source_kind, "resolved" if reason == "resolved" else "rejected", reason, tuple(active.allowed_tools), False)

    def _record(
        self,
        source_kind: str,
        status: DirectiveStatus,
        reason: str,
        allowed_tools: tuple[str, ...],
        force_truthful_final: bool,
    ) -> DirectiveTransition:
        transition = DirectiveTransition(
            source_kind=source_kind,
            status=status,
            reason=reason,
            allowed_tools=tuple(sorted(allowed_tools)),
            attempts=self._source_attempts.get(source_kind, 0),
            turns=self._source_turns.get(source_kind, 0),
            force_truthful_final=force_truthful_final,
        )
        self._source_status[source_kind] = transition
        return transition

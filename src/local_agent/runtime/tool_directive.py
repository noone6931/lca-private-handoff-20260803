from __future__ import annotations

from typing import Any, Protocol

from .context import RunContext
from ..steering.models import SteeringDecision
from ..workflows.temporary_directive import DirectiveTransition


class RuntimeToolDirectivePort(Protocol):
    _run: RunContext
    _session: Any

    def _apply_final_answer_steering(self, decision: SteeringDecision) -> bool: ...


class RuntimeToolDirectivePhase:
    """Bridge bounded tool-directive ownership into the Runtime turn loop.

    This phase mirrors OMP's queue lifecycle boundary: the owner tracks state,
    while Runtime only invokes lifecycle hooks at model-turn, tool-attempt, and
    terminal boundaries.
    """

    def __init__(self, runtime: RuntimeToolDirectivePort) -> None:
        self._runtime = runtime
        self._forced_sources: set[str] = set()

    def begin_run(self) -> None:
        self._forced_sources.clear()

    def apply_steering(self, source_kind: str, allowed_tools: set[str] | None) -> bool:
        if not allowed_tools:
            self.clear("cleared")
            return False
        transition = self._runtime._run.activate_temporary_tool_directive(source_kind, allowed_tools)
        self._emit(transition)
        return self._force_truthful_final(transition)

    def before_model_turn(self) -> bool:
        transition = self._runtime._run.begin_temporary_tool_directive_turn()
        if transition is None:
            return False
        self._emit(transition)
        return self._force_truthful_final(transition)

    def before_tool_attempt(self, tool_name: str) -> DirectiveTransition | None:
        transition = self._runtime._run.reserve_temporary_tool_directive_attempt(tool_name)
        if transition is not None:
            self._emit(transition, tool=tool_name)
        return transition

    def after_tool_attempt(self, transition: DirectiveTransition | None, *, tool_name: str, is_error: bool) -> bool:
        if transition is None:
            return False
        outcome_transition = self._runtime._run.record_temporary_tool_directive_attempt(
            transition,
            is_error=is_error,
        )
        if outcome_transition is not None:
            transition = outcome_transition
        self._runtime._session.append(
            "temporary_tool_directive_attempt",
            {
                **transition.payload(),
                "tool": tool_name,
                "outcome": "error" if is_error else "success",
            },
        )
        return self._force_truthful_final(transition)

    def after_model_turn(self) -> bool:
        transition = self._runtime._run.finish_temporary_tool_directive_turn()
        if transition is None:
            return False
        self._emit(transition)
        return self._force_truthful_final(transition)

    def clear(self, reason: str) -> None:
        transition = self._runtime._run.close_temporary_tool_directive(reason)
        if transition is not None:
            self._emit(transition)

    def close_terminal(self, reason: str) -> None:
        self.clear(reason)

    def _force_truthful_final(self, transition: DirectiveTransition) -> bool:
        if not transition.force_truthful_final:
            return False
        if transition.source_kind in self._forced_sources:
            return False
        self._forced_sources.add(transition.source_kind)
        return self._runtime._apply_final_answer_steering(
            SteeringDecision(
                kind=transition.source_kind,
                message=transition.final_message(),
                payload=transition.payload(),
            )
        )

    def _emit(self, transition: DirectiveTransition, *, tool: str | None = None) -> None:
        payload = transition.payload()
        if tool:
            payload["tool"] = tool
        self._runtime._session.append("temporary_tool_directive", payload)

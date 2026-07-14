"""Bounded provider tool_choice escalation for typed tool requirements."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .tool_choice_queue import ToolChoiceDecision
from .tool_observation import ToolResultSummary


MAX_EXACT_TOOL_CHOICE_ESCALATIONS = 3


@dataclass(frozen=True)
class ToolChoiceDirectiveAction:
    kind: Literal["none", "force", "exhausted"]
    tool_name: str = ""
    reason: str = ""
    attempt: int = 0


@dataclass
class _ActiveRequirement:
    signature: tuple[str, str, tuple[str, ...], tuple[str, ...]]
    tool_name: str
    result_cursor: int
    forced_attempts: int = 0
    force_next: bool = False


class ToolChoiceDirectiveOwner:
    """Reminder-to-exact-tool lifecycle for one active tool-choice gate."""

    def __init__(self) -> None:
        self._active: _ActiveRequirement | None = None

    def reset(self) -> None:
        self._active = None

    def tool_choice_for_model(self) -> dict[str, Any] | None:
        if self._active is None or not self._active.force_next:
            return None
        tool_name = self._active.tool_name
        self._active.force_next = False
        return {"type": "function", "function": {"name": tool_name}}

    def begin_decision(
        self,
        decision: ToolChoiceDecision,
        results: list[ToolResultSummary],
    ) -> None:
        tool_name = _preferred_single_tool(decision)
        if tool_name is None:
            self._active = None
            return
        signature = _signature(decision, tool_name)
        if self._active is None or self._active.signature != signature:
            self._active = _ActiveRequirement(signature, tool_name, len(results))
            return
        if _has_required_success_since(results, self._active):
            self._active = None

    def observe_turn(self, tool_calls: list[dict[str, Any]]) -> ToolChoiceDirectiveAction:
        active = self._active
        if active is None:
            return ToolChoiceDirectiveAction("none")
        if _called_only_required_tool(tool_calls, active.tool_name):
            return ToolChoiceDirectiveAction("none")
        if active.forced_attempts >= MAX_EXACT_TOOL_CHOICE_ESCALATIONS:
            self._active = None
            return ToolChoiceDirectiveAction("exhausted", active.tool_name, "exact_tool_choice_limit", active.forced_attempts)
        active.forced_attempts += 1
        active.force_next = True
        return ToolChoiceDirectiveAction("force", active.tool_name, "noncompliant_turn", active.forced_attempts)


def _preferred_single_tool(decision: ToolChoiceDecision) -> str | None:
    if not decision.steering_required or decision.force_final_answer_without_tools:
        return None
    preferred = tuple(name for name in decision.preferred_tool_names if name in decision.allowed_tool_names)
    if len(preferred) != 1:
        return None
    if len(decision.allowed_tool_names) != 1:
        return None
    return preferred[0]


def _has_required_success_since(results: list[ToolResultSummary], active: _ActiveRequirement) -> bool:
    for result in results[active.result_cursor:]:
        if result.name == active.tool_name and not result.is_error:
            return True
    return False


def _called_only_required_tool(tool_calls: list[dict[str, Any]], tool_name: str) -> bool:
    if not tool_calls:
        return False
    for tool_call in tool_calls:
        function = tool_call.get("function") if isinstance(tool_call, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if name != tool_name:
            return False
    return True


def _signature(decision: ToolChoiceDecision, tool_name: str) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
    return (
        decision.rule_id or decision.reason,
        tool_name,
        tuple(sorted(decision.missing_requirements)),
        tuple(sorted(decision.scoped_read_paths)),
    )

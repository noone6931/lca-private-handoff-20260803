"""Bounded provider tool_choice escalation for typed tool requirements."""
from __future__ import annotations

import copy
import json
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
    read_only_unlocated_on_exhaustion: bool = False
    scoped_read_paths: tuple[str, ...] = ()


@dataclass
class _ActiveRequirement:
    signature: tuple[str, str, str, tuple[str, ...], tuple[str, ...], str]
    tool_name: str
    result_cursor: int
    required_arguments_json: str = ""
    forced_attempts: int = 0
    force_next: bool = False
    read_only_unlocated_on_exhaustion: bool = False
    scoped_read_paths: tuple[str, ...] = ()


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
    ) -> ToolChoiceDirectiveAction:
        tool_name = _preferred_single_tool(decision)
        if tool_name is None:
            self._active = None
            return ToolChoiceDirectiveAction("none")
        signature = _signature(decision, tool_name)
        if self._active is None or self._active.signature != signature:
            self._active = _ActiveRequirement(
                signature,
                tool_name,
                len(results),
                required_arguments_json=decision.required_tool_arguments_json,
                read_only_unlocated_on_exhaustion=decision.read_only_unlocated_on_exhaustion,
                scoped_read_paths=tuple(decision.scoped_read_paths),
            )
            return ToolChoiceDirectiveAction("none")
        active = self._active
        new_results = results[active.result_cursor:]
        active.result_cursor = len(results)
        if any(result.name == active.tool_name and not result.is_error for result in new_results):
            self._active = None
            return ToolChoiceDirectiveAction("none")
        if not any(result.name == active.tool_name and result.is_error for result in new_results):
            return ToolChoiceDirectiveAction("none")
        if active.forced_attempts >= MAX_EXACT_TOOL_CHOICE_ESCALATIONS - 1:
            self._active = None
            return ToolChoiceDirectiveAction(
                "exhausted",
                active.tool_name,
                "required_tool_error_limit",
                active.forced_attempts + 1,
                active.read_only_unlocated_on_exhaustion,
                active.scoped_read_paths,
            )
        active.forced_attempts += 1
        active.force_next = True
        return ToolChoiceDirectiveAction(
            "force",
            active.tool_name,
            "required_tool_error",
            active.forced_attempts,
        )

    def project_schemas(self, schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Project the active typed argument intent into the provider schema."""

        active = self._active
        if active is None or not active.required_arguments_json:
            return schemas
        try:
            required_arguments = json.loads(active.required_arguments_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return schemas
        if not isinstance(required_arguments, dict) or not required_arguments:
            return schemas
        return _project_required_arguments(schemas, active.tool_name, required_arguments)

    def observe_turn(self, tool_calls: list[dict[str, Any]]) -> ToolChoiceDirectiveAction:
        active = self._active
        if active is None:
            return ToolChoiceDirectiveAction("none")
        if _called_only_required_tool(tool_calls, active.tool_name):
            return ToolChoiceDirectiveAction("none")
        if active.forced_attempts >= MAX_EXACT_TOOL_CHOICE_ESCALATIONS:
            self._active = None
            return ToolChoiceDirectiveAction(
                "exhausted",
                active.tool_name,
                "exact_tool_choice_limit",
                active.forced_attempts,
                active.read_only_unlocated_on_exhaustion,
                active.scoped_read_paths,
            )
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


def _called_only_required_tool(tool_calls: list[dict[str, Any]], tool_name: str) -> bool:
    if not tool_calls:
        return False
    for tool_call in tool_calls:
        function = tool_call.get("function") if isinstance(tool_call, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if name != tool_name:
            return False
    return True


def _signature(decision: ToolChoiceDecision, tool_name: str) -> tuple[str, str, str, tuple[str, ...], tuple[str, ...], str]:
    return (
        decision.rule_id or decision.reason,
        decision.requirement_identity,
        tool_name,
        tuple(sorted(decision.missing_requirements)),
        tuple(sorted(decision.scoped_read_paths)),
        decision.required_tool_arguments_json,
    )


def _project_required_arguments(
    schemas: list[dict[str, Any]],
    tool_name: str,
    required_arguments: dict[str, Any],
) -> list[dict[str, Any]]:
    projected = copy.deepcopy(schemas)
    for schema in projected:
        function = schema.get("function")
        if not isinstance(function, dict) or function.get("name") != tool_name:
            continue
        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            break
        properties = parameters.get("properties")
        if not isinstance(properties, dict):
            break
        for name, value in required_arguments.items():
            existing = properties.get(name)
            if not isinstance(existing, dict):
                continue
            constraint = dict(existing)
            if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
                items = constraint.get("items")
                item_schema = dict(items) if isinstance(items, dict) else {"type": "string"}
                item_schema["enum"] = list(dict.fromkeys(value))
                constraint["items"] = item_schema
                constraint["minItems"] = 1
                constraint["maxItems"] = len(item_schema["enum"])
            else:
                constraint["enum"] = [value]
            properties[name] = constraint
        parameters["required"] = list(dict.fromkeys([*parameters.get("required", []), *required_arguments]))
        function["description"] = (
            str(function.get("description") or "").rstrip()
            + "\nCurrent runtime argument contract: use only the values allowed by this narrowed schema."
        )
        break
    return projected

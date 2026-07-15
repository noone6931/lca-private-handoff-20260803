"""Runtime facade for exact tool-choice escalation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol

from .tool_choice_directive import ToolChoiceDirectiveAction
from .tool_choice_queue import ToolChoiceDecision


@dataclass(frozen=True)
class ToolChoiceModelTurn:
    tool_choice: dict[str, Any] | None = None
    forced_tool_name: str | None = None


@dataclass(frozen=True)
class ToolChoiceTurnOutcome:
    kind: Literal["none", "force", "exhausted"] = "none"
    terminal_message: str = ""
    terminal_reason: str = ""
    append_skipped_results: bool = False
    skipped_message: str = ""
    skipped_metadata: Mapping[str, Any] | None = None
    suppressed_count: int = 0
    requeue_required: bool = False


class ToolChoiceDirectiveRuntimePort(Protocol):
    _run: Any
    _session: Any
    _read_only_explore_phase: Any


class RuntimeToolChoiceDirectivePhase:
    """Own reminder-to-exact-tool lifecycle and telemetry."""

    def __init__(self, runtime: ToolChoiceDirectiveRuntimePort) -> None:
        self._runtime = runtime

    def begin_decision(self, decision: ToolChoiceDecision) -> ToolChoiceTurnOutcome:
        action = self._runtime._run.tool_choice_directive.begin_decision(
            decision,
            self._runtime._run.tool_choice_results,
        )
        return self._outcome_for_action(action)

    def project_schemas(self, schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._runtime._run.tool_choice_directive.project_schemas(schemas)

    def before_model_turn(self) -> ToolChoiceModelTurn:
        payload = self._runtime._run.tool_choice_directive.tool_choice_for_model()
        return ToolChoiceModelTurn(tool_choice=payload, forced_tool_name=_forced_tool_name(payload))

    def after_model_turn(self, raw_tool_calls: list[dict[str, Any]]) -> ToolChoiceTurnOutcome:
        action = self._runtime._run.tool_choice_directive.observe_turn(raw_tool_calls)
        return self._outcome_for_action(action, raw_tool_calls=raw_tool_calls)

    def _outcome_for_action(
        self,
        action: ToolChoiceDirectiveAction,
        *,
        raw_tool_calls: list[dict[str, Any]] | None = None,
    ) -> ToolChoiceTurnOutcome:
        if action.kind == "none":
            return ToolChoiceTurnOutcome()
        self._record_action(action)
        suppressed_count = len(raw_tool_calls or ())
        if suppressed_count:
            self._runtime._run.collector.record_suppressed_tool_executions(suppressed_count)
        metadata = {"provider_schema_violation": True, "allowed_tools": [action.tool_name]}
        if action.kind == "force":
            return ToolChoiceTurnOutcome(
                "force",
                append_skipped_results=bool(raw_tool_calls),
                skipped_message=f"the current workflow requires `{action.tool_name}` before other tools.",
                skipped_metadata=metadata,
                suppressed_count=suppressed_count,
            )
        self._runtime._run.collector.record_tool_choice_exact_exhausted()
        if action.read_only_unlocated_on_exhaustion and self._runtime._read_only_explore_phase.mark_candidate_read_unlocated(
            action.scoped_read_paths,
            reason=action.reason,
        ):
            return ToolChoiceTurnOutcome(
                "force",
                append_skipped_results=bool(raw_tool_calls),
                skipped_message=(
                    "Skipped because the bounded candidate read requirement was exhausted; "
                    "this root is now recorded as unlocated for the read-only readiness review."
                ),
                skipped_metadata={
                    **metadata,
                    "read_only_explore_unlocated": True,
                    "candidate_read_exhausted": True,
                },
                suppressed_count=suppressed_count,
                requeue_required=True,
            )
        return ToolChoiceTurnOutcome(
            "exhausted",
            terminal_message=(
                f"未完成/未验证：模型未按当前有界证据流程调用 `{action.tool_name}`，"
                "已达到强制工具选择上限。"
            ),
            terminal_reason="tool_choice_exact_exhausted",
            append_skipped_results=bool(raw_tool_calls),
            skipped_message=f"the current workflow requires `{action.tool_name}` before other tools.",
            skipped_metadata=metadata,
            suppressed_count=suppressed_count,
        )

    def _record_action(self, action: ToolChoiceDirectiveAction) -> None:
        self._runtime._run.collector.record_tool_choice_exact_action(action.kind)
        self._runtime._session.append(
            "runtime_steering",
            {
                "kind": "tool_choice_exact_requirement",
                "action": action.kind,
                "tool": action.tool_name,
                "reason": action.reason,
                "attempt": action.attempt,
            },
        )


def _forced_tool_name(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    function = payload.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    return name if isinstance(name, str) and name else None

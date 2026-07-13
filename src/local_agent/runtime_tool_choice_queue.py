"""Runtime facade for ToolChoiceQueue projection and steering."""
from __future__ import annotations

from typing import Any, Protocol

from .tool_gateway import _tool_choice_signature_count
from .tool_gateway import _tool_choice_steering_message
from .tool_gateway import _tool_choice_steering_signature


MAX_TOOL_CHOICE_QUEUE_STEERS_PER_SIGNATURE = 1


class ToolChoiceQueueRuntimePort(Protocol):
    _messages: list[dict[str, Any]]
    _run: Any
    _session: Any
    _tool_choice_directive_phase: Any
    _tool_directive_phase: Any
    _workspace_context: Any

    def _available_registry_tool_names(self) -> tuple[str, ...]: ...
    def _final_answer_request_summary(self) -> str: ...
    def _final_answer_rewrite_skip_reason(self) -> str | None: ...
    def _queue_forced_final_answer(self, *, kind: str, severity: str = ...) -> bool: ...


class RuntimeToolChoiceQueuePhase:
    """Own queue evaluation, model steering, and queue/directive telemetry."""

    def __init__(self, runtime: ToolChoiceQueueRuntimePort) -> None:
        self._runtime = runtime

    def apply_if_needed(self, deadline: float | None = None) -> str | None:
        runtime = self._runtime
        contract = runtime._run.requirement_contract
        if contract is None:
            runtime._run.tool_choice_allowed_tool_names = None
            return None
        decision = runtime._run.tool_choice_queue.evaluate(
            task_kind=contract.task_kind,
            prompt=runtime._run.current_user_request or "",
            tool_names=runtime._run.tool_choice_tool_names,
            tool_results=runtime._run.tool_choice_results,
            available_tool_names=runtime._available_registry_tool_names(),
            design_evidence_roots=runtime._run.design_evidence_coverage.roots,
            workspace_roots=tuple(str(root) for root in runtime._workspace_context.all_roots),
            evidence_domain=contract.evidence_domain,
            read_only_review_profile=contract.read_only_review_profile,
            document_artifacts=contract.document_artifacts,
        )
        runtime._run.tool_choice_allowed_tool_names = set(decision.allowed_tool_names)
        runtime._run.update_tool_choice_read_scope(decision.scoped_read_paths, decision.scoped_read_budget)
        runtime._run.tool_choice_required_glob_roots = set(decision.required_glob_roots) or None
        runtime._tool_choice_directive_phase.begin_decision(decision)
        if decision.force_final_answer_without_tools:
            self._append_tool_choice_message(decision, force_final=True)
            if not runtime._queue_forced_final_answer(kind=decision.rule_id or "tool_choice_queue"):
                runtime._run.block_unverified_final_answer(
                    kind=decision.rule_id or "tool_choice_queue",
                    reason=runtime._final_answer_rewrite_skip_reason() or "continuation_limit",
                )
            return None
        if decision.should_stop:
            runtime._session.append(
                "runtime_steering",
                {
                    "kind": "tool_choice_queue",
                    "rule_id": decision.rule_id,
                    "reason": decision.reason,
                    "stop_message": decision.stop_message,
                },
            )
            return decision.stop_message
        stop = self._apply_design_coverage_if_needed(decision)
        if stop is not None or runtime._run.force_final_answer_without_tools:
            return stop
        if not decision.steering_required:
            return None
        signature = _tool_choice_steering_signature(decision, len(runtime._run.tool_choice_results))
        if signature in runtime._run.tool_choice_steering_signatures:
            return None
        if _tool_choice_signature_count(runtime._run.tool_choice_steering_signatures, decision.rule_id) >= (
            MAX_TOOL_CHOICE_QUEUE_STEERS_PER_SIGNATURE
        ):
            return None
        runtime._run.tool_choice_steering_signatures.add(signature)
        self._append_tool_choice_message(decision, force_final=False)
        return None

    def _append_tool_choice_message(self, decision: Any, *, force_final: bool) -> None:
        runtime = self._runtime
        content = _tool_choice_steering_message(decision, runtime._run.current_user_request)
        runtime._messages.append({"role": "user", "content": content})
        runtime._session.append(
            "runtime_steering",
            {
                "kind": "tool_choice_queue",
                "rule_id": decision.rule_id,
                "missing_requirements": list(decision.missing_requirements),
                "allowed_tool_names": [] if force_final else sorted(decision.allowed_tool_names),
                "reason": decision.reason,
                **({"force_final_answer_without_tools": True} if force_final else {}),
            },
        )

    def _apply_design_coverage_if_needed(self, decision: Any) -> str | None:
        runtime = self._runtime
        coverage = runtime._run.design_evidence_coverage.observe(
            queue_requires_steering=decision.steering_required,
            read_paths=(
                result.path
                for result in runtime._run.tool_choice_results
                if result.name == "read_file" and not result.is_error
            ),
            tool_count=len(runtime._run.tool_choice_results),
            reserve_required=runtime._run.finalization_reserve_required(),
            request_summary=runtime._final_answer_request_summary(),
        )
        if coverage is None:
            return None
        for kind, payload in coverage.preceding_events:
            runtime._session.append("runtime_steering", {"kind": kind, **payload})
        runtime._session.append("runtime_steering", {"kind": coverage.kind, **coverage.payload})
        if coverage.message is None:
            return None
        runtime._messages.append({"role": "user", "content": coverage.message})
        if not coverage.force_final_answer_without_tools:
            runtime._run.force_final_answer_without_tools = False
            runtime._tool_directive_phase.clear("coverage_final")
            return None
        if runtime._queue_forced_final_answer(kind=coverage.kind):
            runtime._run.force_final_answer_without_tools = True
            runtime._tool_directive_phase.clear("coverage_final")
            return None
        skip_reason = runtime._run.finalization_rewrite_skip_reason() or "continuation_limit"
        runtime._session.append(
            "runtime_steering",
            {
                "kind": "forced_final_answer_skipped",
                "source": coverage.kind,
                "reason": skip_reason,
            },
        )
        runtime._run.block_unverified_final_answer(kind=coverage.kind, reason=skip_reason)
        runtime._run.force_final_answer_without_tools = False
        runtime._tool_directive_phase.clear("coverage_final")
        return "Runtime could not safely schedule the required final answer rewrite."

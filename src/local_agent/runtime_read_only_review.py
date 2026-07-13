"""Runtime phase for one isolated, read-only evidence review per run."""
from __future__ import annotations

import json
from typing import Any, Protocol

from .chat_runtime import call_chat_with_timeout
from .explore_handoff import build_explore_handoff
from .llm import LlmError
from .llm import LlmTimeoutError
from .provider_protocol import classify_provider_content_artifact
from .read_only_reviewer import candidate_claim_units
from .read_only_reviewer import MAX_INITIAL_REVIEWER_PROVIDER_CALLS
from .read_only_reviewer import MAX_REWRITE_REVIEWER_PROVIDER_CALLS
from .read_only_reviewer import REVIEWER_OUTPUT_TOOL_NAME
from .read_only_reviewer import ReviewerPhaseOutcome
from .read_only_reviewer import ReviewerValidationError
from .read_only_reviewer import parse_reviewer_payload
from .read_only_reviewer import parse_reviewer_result
from .read_only_reviewer import reviewer_messages
from .read_only_reviewer import reviewer_output_tool_schema
from .read_only_reviewer import reviewer_repair_messages
from .read_only_reviewer import reviewer_rewrite_message
from .read_only_reviewer import should_review_read_only_candidate
from .safe_partial_report import build_safe_partial_report


MAX_REVIEWER_TIMEOUT_SECONDS = 20.0


class ReadOnlyReviewRuntimePort(Protocol):
    _client: Any
    _config: Any
    _events: Any
    _run: Any
    _session: Any
    _workspace_context: Any
    _provider_context_phase: Any


class ReadOnlyReviewPhase:
    """Own reviewer lifecycle, isolated context, and reviewer telemetry.

    The primary Runtime remains responsible for appending a returned rewrite
    instruction to its conversation.  The reviewer itself never receives or
    writes the primary transcript.
    """

    def __init__(self, runtime: ReadOnlyReviewRuntimePort) -> None:
        self._runtime = runtime

    def begin_run(self) -> None:
        self._runtime._run.read_only_review.reset()

    def review_candidate(self, candidate: str) -> ReviewerPhaseOutcome:
        runtime = self._runtime
        state = runtime._run.read_only_review
        contract = runtime._run.requirement_contract
        request = runtime._run.current_user_request
        if not should_review_read_only_candidate(contract, request):
            return ReviewerPhaseOutcome("not_applicable")
        if state.rewrite_requested:
            if state.review_round >= 2:
                return self._unverified("rewrite_noncompliant", "reviewer_round_limit")
            return self._review(candidate, rewrite_round=True)
        if state.attempted:
            return ReviewerPhaseOutcome("pass")
        return self._review(candidate, rewrite_round=False)

    def _review(self, candidate: str, *, rewrite_round: bool) -> ReviewerPhaseOutcome:
        runtime = self._runtime
        state = runtime._run.read_only_review
        contract = runtime._run.requirement_contract
        skip_reason = runtime._run.finalization_rewrite_skip_reason()
        if skip_reason is not None:
            return self._unverified("deadline_or_finalization_budget", skip_reason)
        handoff = build_explore_handoff(
            request=runtime._run.current_user_request or "",
            contract=contract,
            requirement_evidence=runtime._run.evidence.pinned_requirement_evidence,
            source_evidence=runtime._run.evidence.source_evidence,
            records=runtime._run.evidence.records,
            tool_results=runtime._run.tool_choice_results,
        )
        claim_units = candidate_claim_units(candidate)
        if not claim_units:
            return self._unverified("invalid_output", "candidate_has_no_addressable_claim_units")
        state.attempted = True
        state.review_round += 1
        state.claim_units = claim_units
        timeout = self._review_timeout()
        max_attempts = MAX_REWRITE_REVIEWER_PROVIDER_CALLS if rewrite_round else MAX_INITIAL_REVIEWER_PROVIDER_CALLS
        if not rewrite_round:
            runtime._run.collector.record_read_only_review_trigger()
        runtime._session.append(
            "read_only_reviewer",
            {
                "event": "triggered",
                "run_id": runtime._run.run_id,
                "items": len(handoff.items),
                "timeout_seconds": timeout,
                "review_round": state.review_round,
            },
        )
        runtime._events.emit(
            "ContextUpdated",
            {"kind": "read_only_reviewer_triggered", "items": len(handoff.items)},
        )
        messages = reviewer_messages(handoff, claim_units)
        output_schema = reviewer_output_tool_schema(claim_units)
        result = None
        saw_protocol_failure = False
        repaired_this_round = False
        for attempt in range(1, max_attempts + 1):
            state.provider_attempts = attempt
            runtime._run.collector.record_read_only_review_attempt()
            try:
                response = call_chat_with_timeout(runtime._client, messages, [output_schema], timeout=timeout)
            except LlmError as exc:
                return self._unverified(
                    "timeout" if isinstance(exc, LlmTimeoutError) else "provider_error",
                    type(exc).__name__,
                )
            message = getattr(response, "message", None)
            if not isinstance(message, dict):
                return self._unverified("protocol_error", "missing_message")
            try:
                result, typed_submit = self._parse_reviewer_response(response, message, claim_units)
            except ReviewerValidationError as exc:
                if exc.code.startswith("output_tool_") or exc.code == "provider_markup_artifact":
                    saw_protocol_failure = True
                    state.protocol_failures += 1
                    runtime._run.collector.record_read_only_review_protocol_failure()
                state.schema_failures += 1
                runtime._run.collector.record_read_only_review_schema_failure()
                diagnostic = exc.diagnostics
                if attempt >= max_attempts:
                    state.repair_exhausted = True
                    runtime._run.collector.record_read_only_review_repair_exhausted()
                    runtime._session.append(
                        "read_only_reviewer",
                        {"event": "schema_repair_exhausted", "attempts": attempt, "diagnostic": diagnostic},
                    )
                    return self._unverified("protocol_error" if saw_protocol_failure else "invalid_output", exc.code)
                if not self._has_reviewer_time_for_repair():
                    return self._unverified("deadline_or_finalization_budget", "reviewer_repair_timeout")
                state.repairs += 1
                repaired_this_round = True
                runtime._run.collector.record_read_only_review_repair()
                runtime._session.append(
                    "read_only_reviewer",
                    {"event": "schema_repair_requested", "attempt": attempt, "diagnostic": diagnostic},
                )
                runtime._events.emit(
                    "ContextUpdated",
                    {"kind": "read_only_reviewer_schema_repair", "attempt": attempt, "error_code": exc.code},
                )
                messages = reviewer_repair_messages(handoff, claim_units, diagnostic)
                timeout = self._review_timeout()
                continue
            if typed_submit:
                state.typed_submits += 1
                runtime._run.collector.record_read_only_review_typed_submit()
            break
        if result is None:
            return self._unverified("invalid_output", "schema_repair_exhausted")
        if repaired_this_round:
            state.repair_success = True
            runtime._run.collector.record_read_only_review_repair_success()
        state.verdict = result.verdict
        state.reason = result.reason
        state.findings = result.findings
        runtime._run.collector.record_read_only_review_result(result.verdict, len(result.findings))
        runtime._session.append("read_only_reviewer", {"event": "result", **result.to_dict()})
        runtime._events.emit(
            "ContextUpdated",
            {
                "kind": "read_only_reviewer_result",
                "verdict": result.verdict,
                "findings": len(result.findings),
                "review_round": state.review_round,
            },
        )
        if result.verdict == "pass":
            return ReviewerPhaseOutcome("pass")
        if rewrite_round:
            return self._unverified("second_review_nonpass", result.verdict, result=result, handoff=handoff)
        if not runtime._run.queue_finalization_rewrite(kind="read_only_reviewer"):
            return self._unverified(
                "rewrite_unavailable",
                runtime._run.finalization_rewrite_skip_reason() or "finalization_limit",
                result=result,
                handoff=handoff,
            )
        state.rewrite_requested = True
        runtime._run.collector.record_read_only_review_rewrite()
        runtime._session.append("read_only_reviewer", {"event": "rewrite_queued", "verdict": result.verdict})
        return ReviewerPhaseOutcome(
            "rewrite",
            rewrite_message=reviewer_rewrite_message(result, profile=contract.read_only_review_profile),
        )

    def _parse_reviewer_response(
        self,
        response: Any,
        message: dict[str, Any],
        claim_units: tuple[Any, ...],
    ) -> tuple[Any, bool]:
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            if len(tool_calls) != 1:
                raise ReviewerValidationError("output_tool_multiple_calls", {"tool_call_count": len(tool_calls)})
            call = tool_calls[0]
            function = call.get("function") if isinstance(call, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            if name != REVIEWER_OUTPUT_TOOL_NAME:
                raise ReviewerValidationError("output_tool_name_invalid")
            arguments = function.get("arguments") if isinstance(function, dict) else None
            if not isinstance(arguments, str):
                raise ReviewerValidationError("output_tool_arguments_invalid")
            try:
                payload = json.loads(arguments)
            except json.JSONDecodeError:
                raise ReviewerValidationError("output_tool_arguments_invalid") from None
            return parse_reviewer_payload(payload, claim_units=claim_units), True
        artifact = getattr(response, "protocol_artifact", None)
        if artifact is None:
            artifact = classify_provider_content_artifact(self._runtime._config.provider, message.get("content"))
        if artifact is not None:
            raise ReviewerValidationError("provider_markup_artifact", {"artifact_kind": artifact.kind})
        # Compatibility path for providers that cannot emit a structured output
        # call. It remains strict and is deliberately not the preferred schema.
        return parse_reviewer_result(message.get("content"), claim_units=claim_units), False

    def _review_timeout(self) -> float | None:
        remaining = self._runtime._provider_context_phase.remaining_timeout(self._runtime._run.deadline_monotonic)
        if remaining is None:
            return MAX_REVIEWER_TIMEOUT_SECONDS
        return max(0.1, min(MAX_REVIEWER_TIMEOUT_SECONDS, remaining))

    def _has_reviewer_time_for_repair(self) -> bool:
        remaining = self._runtime._provider_context_phase.remaining_timeout(self._runtime._run.deadline_monotonic)
        return remaining is None or remaining > 0.1

    def _unverified(
        self,
        reason: str,
        detail: str,
        *,
        result: Any = None,
        handoff: Any = None,
    ) -> ReviewerPhaseOutcome:
        runtime = self._runtime
        state = runtime._run.read_only_review
        state.verdict = "unverified"
        state.reason = reason
        if result is not None:
            state.findings = result.findings
        runtime._run.collector.record_read_only_review_error(reason)
        runtime._session.append(
            "read_only_reviewer",
            {"event": "unverified", "reason": reason, "detail": detail},
        )
        runtime._events.emit("ErrorEvent", {"kind": "read_only_reviewer", "reason": reason})
        safe_partial_report = ""
        if handoff is not None:
            partial = build_safe_partial_report(
                handoff,
                state.findings,
                reason=reason,
            )
            safe_partial_report = partial.content
            runtime._run.collector.record_safe_partial_report(
                observations=partial.observation_count,
                missing=partial.missing_count,
                rejected_categories=partial.rejected_categories,
            )
            runtime._session.append(
                "safe_partial_report",
                {
                    "reason": reason,
                    "observations": partial.observation_count,
                    "missing": partial.missing_count,
                    "rejected_categories": list(partial.rejected_categories),
                },
            )
        return ReviewerPhaseOutcome(
            "unverified",
            terminal_message=safe_partial_report or (
                "未完成/未验证：独立只读证据审查未能确认该候选答复。"
                f"原因：{reason}（{detail}）。未将未经审查的草稿作为最终结论返回。"
            ),
            reason=reason,
            safe_partial_report=safe_partial_report,
        )

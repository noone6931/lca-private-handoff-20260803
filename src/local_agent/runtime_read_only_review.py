"""Runtime phase for one isolated, read-only evidence review per run."""
from __future__ import annotations

from typing import Any, Protocol

from .chat_runtime import call_chat_with_timeout
from .explore_handoff import build_explore_handoff
from .llm import LlmError
from .llm import LlmTimeoutError
from .provider_protocol import classify_provider_content_artifact
from .provider_protocol import protocol_violation_payload
from .read_only_reviewer import candidate_claim_units
from .read_only_reviewer import ReviewerPhaseOutcome
from .read_only_reviewer import parse_reviewer_result
from .read_only_reviewer import reviewer_messages
from .read_only_reviewer import reviewer_rewrite_message
from .read_only_reviewer import rewrite_complies_with_review
from .read_only_reviewer import should_review_read_only_candidate


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
        if state.attempted:
            if state.rewrite_requested and state.findings:
                if not rewrite_complies_with_review(candidate, state.claim_units, state.findings):
                    return self._unverified("rewrite_noncompliant", "unsupported_claim_repeated")
            return ReviewerPhaseOutcome("pass")
        skip_reason = runtime._run.finalization_rewrite_skip_reason()
        if skip_reason is not None:
            return self._unverified("deadline_or_finalization_budget", skip_reason)
        state.attempted = True
        handoff = build_explore_handoff(
            request=request or "",
            contract=contract,
            requirement_evidence=runtime._run.evidence.pinned_requirement_evidence,
            source_evidence=runtime._run.evidence.source_evidence,
            records=runtime._run.evidence.records,
            tool_results=runtime._run.tool_choice_results,
        )
        claim_units = candidate_claim_units(candidate)
        if not claim_units:
            return self._unverified("invalid_output", "candidate_has_no_addressable_claim_units")
        state.claim_units = claim_units
        messages = reviewer_messages(handoff, claim_units)
        timeout = self._review_timeout()
        runtime._run.collector.record_read_only_review_trigger()
        runtime._session.append(
            "read_only_reviewer",
            {
                "event": "triggered",
                "run_id": runtime._run.run_id,
                "items": len(handoff.items),
                "timeout_seconds": timeout,
            },
        )
        runtime._events.emit(
            "ContextUpdated",
            {"kind": "read_only_reviewer_triggered", "items": len(handoff.items)},
        )
        try:
            response = call_chat_with_timeout(runtime._client, messages, [], timeout=timeout)
        except LlmError as exc:
            return self._unverified("timeout" if isinstance(exc, LlmTimeoutError) else "provider_error", type(exc).__name__)
        message = getattr(response, "message", None)
        if not isinstance(message, dict):
            return self._unverified("protocol_error", "missing_message")
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            return self._protocol_unverified("structured_tool_calls", tool_calls=tool_calls)
        artifact = getattr(response, "protocol_artifact", None)
        if artifact is None:
            artifact = classify_provider_content_artifact(runtime._config.provider, message.get("content"))
        if artifact is not None:
            return self._protocol_unverified(artifact.kind, artifact=artifact)
        try:
            result = parse_reviewer_result(message.get("content"), claim_units=claim_units)
        except (TypeError, ValueError) as exc:
            return self._unverified("invalid_output", str(exc))
        state.verdict = result.verdict
        state.reason = result.reason
        state.findings = result.findings
        runtime._run.collector.record_read_only_review_result(result.verdict, len(result.findings))
        runtime._session.append("read_only_reviewer", {"event": "result", **result.to_dict()})
        runtime._events.emit(
            "ContextUpdated",
            {"kind": "read_only_reviewer_result", "verdict": result.verdict, "findings": len(result.findings)},
        )
        if result.verdict == "pass":
            return ReviewerPhaseOutcome("pass")
        if not runtime._run.queue_finalization_rewrite(kind="read_only_reviewer"):
            return self._unverified(
                "rewrite_unavailable",
                runtime._run.finalization_rewrite_skip_reason() or "finalization_limit",
                result=result,
            )
        state.rewrite_requested = True
        runtime._run.collector.record_read_only_review_rewrite()
        runtime._session.append("read_only_reviewer", {"event": "rewrite_queued", "verdict": result.verdict})
        return ReviewerPhaseOutcome("rewrite", rewrite_message=reviewer_rewrite_message(result))

    def _protocol_unverified(self, artifact_kind: str, *, tool_calls: list[object] = (), artifact: Any = None) -> ReviewerPhaseOutcome:
        runtime = self._runtime
        runtime._run.collector.record_provider_protocol_violation(
            phase="read_only_reviewer",
            artifact_kind=artifact_kind,
            suppressed_tool_calls=len(tool_calls),
        )
        payload = protocol_violation_payload(
            phase="read_only_reviewer",
            artifact_kind=artifact_kind,
            tool_calls=tool_calls,
            artifact=artifact,
        )
        runtime._session.append("read_only_reviewer_protocol_violation", payload)
        runtime._events.emit("ErrorEvent", {"kind": "read_only_reviewer_protocol_violation", **payload})
        return self._unverified("protocol_error", artifact_kind)

    def _review_timeout(self) -> float | None:
        remaining = self._runtime._provider_context_phase.remaining_timeout(self._runtime._run.deadline_monotonic)
        if remaining is None:
            return MAX_REVIEWER_TIMEOUT_SECONDS
        return max(0.1, min(MAX_REVIEWER_TIMEOUT_SECONDS, remaining))

    def _unverified(self, reason: str, detail: str, *, result: Any = None) -> ReviewerPhaseOutcome:
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
        return ReviewerPhaseOutcome(
            "unverified",
            terminal_message=(
                "未完成/未验证：独立只读证据审查未能确认该候选答复。"
                f"原因：{reason}（{detail}）。未将未经审查的草稿作为最终结论返回。"
            ),
            reason=reason,
        )

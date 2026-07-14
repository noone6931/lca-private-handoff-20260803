"""Runtime facade for bounded recovery from non-substantive provider replies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .provider_protocol import ProviderProtocolArtifact
from .provider_protocol import protocol_violation_message
from .provider_protocol import protocol_violation_payload
from .provider_terminal import assess_terminal_content
from .provider_terminal import terminal_retry_message


class ProviderTerminalRuntimePort(Protocol):
    _events: Any
    _messages: list[dict[str, Any]]
    _run: Any
    _session: Any


@dataclass(frozen=True)
class ProviderTerminalOutcome:
    action: Literal["accept", "retry", "unverified"]
    terminal_message: str = ""
    terminal_reason: str = ""


class ProviderTerminalPhase:
    """Own transcript-safe terminal recovery; Runtime only dispatches its outcome."""

    def __init__(self, runtime: ProviderTerminalRuntimePort) -> None:
        self._runtime = runtime

    def handle_no_tool_response(self, content: object, *, forced_final: bool) -> ProviderTerminalOutcome:
        runtime = self._runtime
        assessment = assess_terminal_content(
            content,
            request=runtime._run.current_user_request or "",
            forced_final=forced_final,
        )
        if assessment.substantive:
            runtime._run.finalization.observe_substantive_response()
            return ProviderTerminalOutcome("accept")
        transition = runtime._run.finalization.observe_non_substantive_response(
            forced_final=forced_final,
            kind=assessment.kind,
        )
        payload = {
            "kind": assessment.kind,
            "phase": "forced_final" if forced_final else "ordinary",
            "attempt": transition.attempt,
            "max_attempts": transition.max_attempts,
            "action": "retry" if transition.retry else "unverified",
        }
        runtime._session.append("provider_terminal_response", payload)
        runtime._events.emit("ContextUpdated", {"kind": "provider_terminal_response", **payload})
        if transition.retry:
            recovery = terminal_retry_message(
                attempt=transition.attempt,
                max_attempts=transition.max_attempts,
                forced_final=forced_final,
            )
            runtime._messages.append({"role": "user", "content": recovery})
            runtime._session.append("runtime_steering", {"kind": "provider_terminal_retry", **payload})
            return ProviderTerminalOutcome("retry")
        return ProviderTerminalOutcome(
            "unverified",
            terminal_message=(
                "未完成/未验证：provider 连续返回了无法作为答复的内容，"
                "Runtime 已在有界重试后停止，未将这些内容作为最终答案展示。"
            ),
            terminal_reason="provider_non_substantive_response",
        )

    def handle_protocol_violation(
        self,
        *,
        phase: Literal["forced_final", "ordinary"],
        artifact_kind: str,
        deadline: float | None,
        forced_final_kind: str | None = None,
        tool_calls: list[object] | None = None,
        artifact: ProviderProtocolArtifact | None = None,
    ) -> ProviderTerminalOutcome:
        """Reject provider tool protocol in text/tool-call output and maybe retry forced final.

        The adapter classifier owns recognizing artifacts; this phase owns the
        bounded terminal recovery lifecycle and redacted telemetry.
        """

        runtime = self._runtime
        calls = list(tool_calls or ())
        outcome = None
        if phase == "forced_final":
            outcome = runtime._run.finalization.reject_forced_final_protocol_response(
                artifact_kind=artifact_kind,
                suppressed_tool_calls=len(calls),
                deadline_monotonic=deadline,
                run_started_monotonic=runtime._run.started_monotonic,
            )
            forced_final_kind = forced_final_kind or outcome.steering_kind
        runtime._run.collector.record_provider_protocol_violation(
            phase=phase,
            artifact_kind=artifact_kind,
            suppressed_tool_calls=len(calls),
        )
        payload = protocol_violation_payload(
            phase=phase,
            artifact_kind=artifact_kind,
            steering_kind=forced_final_kind,
            tool_calls=calls,
            artifact=artifact,
        )
        if outcome is not None:
            payload.update(
                {
                    "recovery_attempt": outcome.attempt,
                    "recovery_max_attempts": outcome.max_attempts,
                    "recovery_action": "retry" if outcome.retry else "exhausted",
                }
            )
            if outcome.reason:
                payload["recovery_reason"] = outcome.reason
        runtime._session.append("provider_protocol_violation", payload)
        reason = "forced_final_protocol_violation" if phase == "forced_final" else "provider_protocol_violation"
        runtime._events.emit("ErrorEvent", {"kind": reason, **payload})
        if outcome is not None and outcome.retry:
            runtime._run.collector.record_forced_final_protocol_recovery(exhausted=False)
            recovery = forced_final_protocol_recovery_message(
                attempt=outcome.attempt,
                max_attempts=outcome.max_attempts,
                steering_kind=forced_final_kind or "runtime_forced_final",
            )
            runtime._messages.append({"role": "user", "content": recovery})
            runtime._session.append(
                "runtime_steering",
                {
                    "kind": "forced_final_protocol_recovery",
                    "steering_kind": forced_final_kind or "runtime_forced_final",
                    "attempt": outcome.attempt,
                    "max_attempts": outcome.max_attempts,
                    "artifact_kind": artifact_kind,
                },
            )
            runtime._events.emit(
                "ContextUpdated",
                {
                    "kind": "forced_final_protocol_recovery",
                    "attempt": outcome.attempt,
                    "max_attempts": outcome.max_attempts,
                    "artifact_kind": artifact_kind,
                },
            )
            return ProviderTerminalOutcome("retry")
        if outcome is not None:
            runtime._run.collector.record_forced_final_protocol_recovery(exhausted=True)
        return ProviderTerminalOutcome(
            "unverified",
            terminal_message=protocol_violation_message(phase=phase),
            terminal_reason=reason,
        )


def forced_final_protocol_recovery_message(*, attempt: int, max_attempts: int, steering_kind: str) -> str:
    return (
        "[Runtime forced-final protocol recovery]\n"
        "Tools are unavailable in this finalization turn. The previous provider response contained tool protocol "
        "and was rejected without execution or transcript insertion. Use only the observations already present in "
        "the conversation and produce the requested substantive final answer as ordinary text. Do not emit XML, "
        f"tool calls, placeholders, or any tool protocol. Recovery attempt {attempt}/{max_attempts}; "
        f"forced-final kind: {steering_kind}."
    )

"""Runtime facade for bounded recovery from non-substantive provider replies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

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
        )

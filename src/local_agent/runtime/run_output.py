"""Typed correlation between provider messages and one Runtime delivery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .assistant_message import AssistantMessage
from .assistant_message import AssistantMessageLifecycle
from ..protocol.events import AgentEvent
from ..protocol.events import EventEmitter


OutputOrigin = Literal["provider", "runtime"]
OutputKind = Literal["provider_message", "runtime_augmented", "runtime_replaced", "runtime_only"]


@dataclass(frozen=True)
class RunOutput:
    final_message_id: str | None
    origin: OutputOrigin
    output_kind: OutputKind


class RunOutputLifecycle:
    """Own the exactly-once relation between model output and task delivery."""

    def __init__(self) -> None:
        self._latest: AssistantMessage | None = None
        self._active: AssistantMessageLifecycle | None = None
        self._finished = False

    def reset(self) -> None:
        self._latest = None
        self._active = None
        self._finished = False

    def attach(self, lifecycle: AssistantMessageLifecycle) -> None:
        if self._finished or self._active is not None:
            raise RuntimeError("Cannot attach an overlapping assistant message lifecycle.")
        self._active = lifecycle

    def observe(self, message: AssistantMessage) -> None:
        if self._finished:
            raise RuntimeError("Cannot observe an assistant message after run output finished.")
        self._active = None
        self._latest = message

    def discard(self, message_id: str) -> None:
        if self._finished:
            raise RuntimeError("Cannot discard an assistant message after run output finished.")
        self._active = None
        self._latest = None

    def abort_active(self, reason: str) -> bool:
        lifecycle = self._active
        if lifecycle is None:
            return False
        lifecycle.abort(reason)
        return True

    def finish(self, content: str) -> RunOutput:
        if self._finished:
            raise RuntimeError("Run output was already finished.")
        if self._active is not None:
            raise RuntimeError("Cannot finish run output with an active assistant message.")
        self._finished = True
        latest = self._latest
        if latest is None:
            return RunOutput(None, "runtime", "runtime_only")
        if content == latest.content:
            return RunOutput(latest.message_id, "provider", "provider_message")
        if latest.content and content.startswith(f"{latest.content.rstrip()}\n\n"):
            return RunOutput(latest.message_id, "runtime", "runtime_augmented")
        return RunOutput(latest.message_id, "runtime", "runtime_replaced")

    def emit(
        self,
        events: EventEmitter,
        *,
        content: str,
        reason: str,
        run_summary: dict[str, Any],
    ) -> AgentEvent:
        output = self.finish(content)
        return events.finish_turn(
            content=content,
            reason=reason,
            run_summary=run_summary,
            final_message_id=output.final_message_id,
            origin=output.origin,
            output_kind=output.output_kind,
        )


def emit_runtime_delivery(
    runtime: Any,
    events: EventEmitter,
    *,
    content: str,
    reason: str,
    run_summary: dict[str, Any],
) -> AgentEvent:
    """Close an optional active message and emit through the Runtime owner when available."""

    run = getattr(runtime, "_run", None)
    output = getattr(run, "output", None)
    if not isinstance(output, RunOutputLifecycle):
        return events.finish_turn(content=content, reason=reason, run_summary=run_summary)
    output.abort_active(reason)
    return output.emit(events, content=content, reason=reason, run_summary=run_summary)

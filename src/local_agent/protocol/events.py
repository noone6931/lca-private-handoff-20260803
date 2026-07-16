from __future__ import annotations

import json
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Protocol, TextIO


EVENT_TYPES = {
    "SessionStarted",
    "TurnStarted",
    "TurnFinished",
    "UserMessage",
    "AssistantDelta",
    "AssistantMessage",
    "LlmRequest",
    "ToolStarted",
    "ToolOutput",
    "ToolFinished",
    "ToolFailed",
    "ApprovalRequested",
    "ApprovalResult",
    "ExecutionPolicyEvaluated",
    "InteractionRequested",
    "InteractionResolved",
    "InteractionCancelled",
    "TodoUpdated",
    "ContextUpdated",
    "RunSummary",
    "RuntimeSteering",
    "WorkspaceRootsChanged",
    "WorkspaceMoved",
    "ErrorEvent",
}


@dataclass(frozen=True)
class AgentEvent:
    event_id: str
    session_id: str
    run_id: str | None
    seq: int
    timestamp: float
    type: str
    payload: dict[str, Any]
    command_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "command_id": self.command_id,
            "seq": self.seq,
            "timestamp": self.timestamp,
            "type": self.type,
            "payload": self.payload,
        }


class EventSink(Protocol):
    def emit(self, event: AgentEvent) -> None:
        ...


class NullEventSink:
    def emit(self, event: AgentEvent) -> None:
        return None


class ListEventSink:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)


class StderrEventSink:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stderr

    def emit(self, event: AgentEvent) -> None:
        if event.type == "SessionStarted":
            print(f"[session] {event.session_id}", file=self._stream)
        elif event.type == "ToolStarted":
            name = str(event.payload.get("name", ""))
            arguments = _shorten(event.payload.get("arguments", ""))
            print(f"[tool:start] {name} {arguments}", file=self._stream)
        elif event.type in {"ToolFinished", "ToolFailed"}:
            name = str(event.payload.get("name", ""))
            status = "error" if event.type == "ToolFailed" else "ok"
            length = event.payload.get("content_length", 0)
            print(f"[tool:end] {name} {status} ({length} chars)", file=self._stream)


class EventEmitter:
    def __init__(
        self,
        *,
        session_id: str,
        sink: EventSink | None = None,
        recorder: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self.session_id = session_id
        self.run_id: str | None = None
        self.command_id: str | None = None
        self._seq = 0
        self._sink = sink or NullEventSink()
        self._recorder = recorder
        self._run_summary_emitted = False
        self._turn_finished_payload: dict[str, Any] | None = None

    def begin_command(self, command_id: str) -> None:
        if self.command_id is not None:
            raise RuntimeError("Cannot begin a command while another command is active.")
        self.command_id = command_id
        self.run_id = None
        self._run_summary_emitted = False
        self._turn_finished_payload = None

    def start_run(self) -> str:
        if self.command_id is None:
            raise RuntimeError("Cannot start a run without an active command.")
        self.run_id = uuid.uuid4().hex
        return self.run_id

    def end_command(self, command_id: str) -> None:
        if self.command_id != command_id:
            raise RuntimeError("Cannot end a command that is not active.")
        self.command_id = None
        self.run_id = None

    @property
    def run_summary_emitted(self) -> bool:
        return self._run_summary_emitted

    @property
    def turn_finished_payload(self) -> dict[str, Any] | None:
        return dict(self._turn_finished_payload) if self._turn_finished_payload is not None else None

    def finish_turn(self, *, content: str, reason: str, run_summary: dict[str, Any]) -> AgentEvent:
        if reason == "final":
            status = "completed"
        elif reason == "interrupt":
            status = "interrupted"
        elif reason in {"provider_error", "llm_timeout", "command_error"}:
            status = "error"
        else:
            status = "stopped"
        return self.emit(
            "TurnFinished",
            {
                "content": content,
                "reason": reason,
                "status": status,
                "delivered": reason == "final",
                "run_summary": run_summary,
            },
        )

    def assistant_delta_callback(
        self,
        message_id: str,
        *,
        enabled: bool,
    ) -> Callable[[str, int], None] | None:
        if not enabled:
            return None

        def emit_delta(delta: str, delta_index: int) -> None:
            self.emit(
                "AssistantDelta",
                {
                    "message_id": message_id,
                    "delta": delta,
                    "delta_index": delta_index,
                    "provisional": True,
                },
            )

        return emit_delta

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> AgentEvent:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Unknown event type: {event_type}")
        self._seq += 1
        event = AgentEvent(
            event_id=uuid.uuid4().hex,
            session_id=self.session_id,
            run_id=self.run_id,
            seq=self._seq,
            timestamp=time.time(),
            type=event_type,
            payload=payload or {},
            command_id=self.command_id,
        )
        if event_type == "RunSummary":
            self._run_summary_emitted = True
        elif event_type == "TurnFinished":
            if self._turn_finished_payload is not None:
                raise RuntimeError("TurnFinished was already emitted for the active run.")
            self._turn_finished_payload = dict(event.payload)
        if self._recorder is not None:
            self._recorder(event)
        self._sink.emit(event)
        return event


def _shorten(value: Any, limit: int = 1000) -> str:
    if isinstance(value, str):
        rendered = value
    else:
        try:
            rendered = json.dumps(value, ensure_ascii=False, default=str)
        except TypeError:
            rendered = str(value)
    if len(rendered) > limit:
        return rendered[:limit] + "...<truncated>"
    return rendered

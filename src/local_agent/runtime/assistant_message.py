"""Authoritative assistant-message lifecycle for one provider model turn."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
import uuid

from ..protocol.events import EventEmitter
from ..session.assistant_history import annotate_provider_message


MessageIdFactory = Callable[[], str]


class AssistantMessageObserver(Protocol):
    def attach(self, lifecycle: AssistantMessageLifecycle) -> None: ...

    def observe(self, message: AssistantMessage) -> None: ...

    def discard(self, message_id: str) -> None: ...


@dataclass(frozen=True)
class AssistantMessage:
    """One finalized provider message with transport-neutral correlation."""

    message_id: str
    content: str
    message: dict[str, Any]
    finish_reason: str | None
    provider: str

    def model_message(self) -> dict[str, Any]:
        return deepcopy(self.message)

    def event_payload(self) -> dict[str, Any]:
        tool_calls = self.message.get("tool_calls") or []
        return {
            "message_id": self.message_id,
            "content": self.content,
            "finish_reason": self.finish_reason,
            "provider": self.provider,
            "origin": "provider",
            "phase": "tool_call" if tool_calls else "candidate",
            "status": "completed",
            "authoritative": True,
            "tool_calls": [
                _tool_call_event_payload(tool_call)
                for tool_call in tool_calls
                if isinstance(tool_call, Mapping)
            ],
        }


class AssistantMessageLifecycle:
    """Own delta correlation and exactly-once finalization for one model turn."""

    def __init__(
        self,
        events: EventEmitter,
        *,
        provider: str,
        stream_enabled: bool,
        message_id_factory: MessageIdFactory | None = None,
        observer: AssistantMessageObserver | None = None,
    ) -> None:
        factory = message_id_factory or (lambda: uuid.uuid4().hex)
        message_id = factory()
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("Assistant message id must be a non-empty string.")
        self.message_id = message_id
        self._events = events
        self._provider = provider
        self._stream_enabled = stream_enabled
        self._observer = observer
        self._closed = False
        if self._observer is not None:
            self._observer.attach(self)

    def delta_callback(self) -> Callable[[str, int], None] | None:
        if not self._stream_enabled:
            return None

        def emit_delta(delta: str, delta_index: int) -> None:
            if self._closed:
                return
            self._events.emit(
                "AssistantDelta",
                {
                    "message_id": self.message_id,
                    "delta": delta,
                    "delta_index": delta_index,
                    "provisional": True,
                },
            )

        return emit_delta

    def finalize(
        self,
        message: Mapping[str, Any],
        *,
        finish_reason: str | None,
    ) -> AssistantMessage:
        if self._closed:
            raise RuntimeError("Assistant message lifecycle was already closed.")
        normalized = annotate_provider_message(
            deepcopy(dict(message)),
            message_id=self.message_id,
            run_id=self._events.run_id,
        )
        normalized["role"] = "assistant"
        content_value = normalized.get("content")
        if content_value is None:
            content = ""
        elif isinstance(content_value, str):
            content = content_value
        else:
            raise TypeError("Assistant message content must be a string or null.")
        finalized = AssistantMessage(
            message_id=self.message_id,
            content=content,
            message=normalized,
            finish_reason=finish_reason,
            provider=self._provider,
        )
        self._closed = True
        if self._observer is not None:
            self._observer.observe(finalized)
        self._events.emit("AssistantMessage", finalized.event_payload())
        return finalized

    def abort(self, reason: str) -> None:
        """Close an incomplete provider message without promoting provisional text."""

        if self._closed:
            raise RuntimeError("Assistant message lifecycle was already closed.")
        if not isinstance(reason, str) or not reason:
            raise ValueError("Assistant message abort reason must be a non-empty string.")
        self._closed = True
        if self._observer is not None:
            self._observer.discard(self.message_id)
        self._events.emit(
            "AssistantMessageAborted",
            {
                "message_id": self.message_id,
                "reason": reason,
                "origin": "provider",
                "status": "aborted",
            },
        )


def _tool_call_event_payload(tool_call: Mapping[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function") or {}
    if not isinstance(function, Mapping):
        function = {}
    return {
        "id": tool_call.get("id"),
        "name": function.get("name") or "",
    }

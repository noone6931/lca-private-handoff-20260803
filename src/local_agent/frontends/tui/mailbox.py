from __future__ import annotations

from collections import deque
from threading import Condition
import time

from .messages import TuiEvent
from .messages import TuiCommandCompleted
from .messages import TuiInteractionClosed
from .messages import TuiInteractionPending
from .messages import TuiMessage
from .messages import TuiWorkerFailed


_DROPPABLE_EVENT_TYPES = frozenset(
    {
        "AssistantDelta",
        "ContextUpdated",
        "LlmRequest",
        "RuntimeSteering",
        "ToolOutput",
    }
)
_PROTECTED_EVENT_TYPES = frozenset({"SessionStarted", "TurnStarted", "TurnFinished", "ErrorEvent"})
_MAX_COALESCED_DELTA_CHARS = 64 * 1024


class TuiMailbox:
    """Bounded cross-thread mailbox with delta coalescing and typed loss accounting."""

    def __init__(self, capacity: int = 2048) -> None:
        if capacity < 8:
            raise ValueError("TUI mailbox capacity must be at least 8.")
        self._capacity = capacity
        self._items: deque[TuiMessage] = deque()
        self._condition = Condition()
        self._closed = False
        self._dropped = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def dropped_count(self) -> int:
        with self._condition:
            return self._dropped

    def put(self, message: TuiMessage) -> bool:
        """Enqueue without making the Runtime thread wait on rendering."""

        with self._condition:
            if self._closed:
                return False
            if isinstance(message, TuiEvent) and self._coalesce_delta(message):
                self._condition.notify()
                return True
            if len(self._items) >= self._capacity and not self._make_room(message):
                self._dropped += 1
                return False
            self._items.append(message)
            self._condition.notify()
            return True

    def get(self, timeout: float | None = None) -> TuiMessage | None:
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        with self._condition:
            while not self._items and not self._closed:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(remaining)
            if not self._items:
                return None
            return self._items.popleft()

    def drain(self, limit: int = 256) -> tuple[TuiMessage, ...]:
        if limit <= 0:
            return ()
        with self._condition:
            count = min(limit, len(self._items))
            return tuple(self._items.popleft() for _ in range(count))

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def _coalesce_delta(self, incoming: TuiEvent) -> bool:
        if incoming.type != "AssistantDelta" or not self._items:
            return False
        previous = self._items[-1]
        if not isinstance(previous, TuiEvent) or previous.type != "AssistantDelta":
            return False
        if previous.get("message_id") != incoming.get("message_id"):
            return False
        previous_index = previous.get("delta_index")
        previous_span = previous.get("delta_span", 1)
        incoming_index = incoming.get("delta_index")
        if type(previous_index) is not int or type(previous_span) is not int:
            return False
        if type(incoming_index) is not int or incoming_index != previous_index + previous_span:
            return False
        previous_delta = previous.get("delta", "")
        incoming_delta = incoming.get("delta", "")
        if not isinstance(previous_delta, str) or not isinstance(incoming_delta, str):
            return False
        if len(previous_delta) + len(incoming_delta) > _MAX_COALESCED_DELTA_CHARS:
            return False
        fields = tuple(
            (key, value)
            for key, value in previous.fields
            if key not in {"delta", "delta_span"}
        )
        fields += (("delta", previous_delta + incoming_delta), ("delta_span", previous_span + 1))
        self._items[-1] = TuiEvent(
            type=previous.type,
            seq=incoming.seq,
            session_id=previous.session_id,
            run_id=previous.run_id,
            command_id=previous.command_id,
            fields=fields,
        )
        return True

    def _make_room(self, incoming: TuiMessage) -> bool:
        for index, queued in enumerate(self._items):
            if isinstance(queued, TuiEvent) and queued.type in _DROPPABLE_EVENT_TYPES:
                del self._items[index]
                self._dropped += 1
                return True
        if isinstance(incoming, TuiEvent) and incoming.type in _DROPPABLE_EVENT_TYPES:
            return False
        incoming_is_protected = (
            isinstance(incoming, (TuiInteractionPending, TuiInteractionClosed, TuiCommandCompleted, TuiWorkerFailed))
            or isinstance(incoming, TuiEvent) and incoming.type in _PROTECTED_EVENT_TYPES
        )
        if incoming_is_protected:
            for index, queued in enumerate(self._items):
                queued_is_protected = (
                    isinstance(queued, (TuiInteractionPending, TuiInteractionClosed, TuiCommandCompleted, TuiWorkerFailed))
                    or isinstance(queued, TuiEvent) and queued.type in _PROTECTED_EVENT_TYPES
                )
                if not queued_is_protected:
                    del self._items[index]
                    self._dropped += 1
                    return True
        return False

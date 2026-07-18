"""Assistant-message presentation state for the plain terminal frontend."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TextIO

from ..text import sanitize_terminal_text

class TerminalAssistantPresenter:
    def __init__(
        self,
        *,
        stream: TextIO,
        render_content: Callable[[str], None],
        render_notice: Callable[[str], None],
    ) -> None:
        self._stream = stream
        self._render_content = render_content
        self._render_notice = render_notice
        self._streamed_messages: dict[str, str] = {}
        self._delta_indices: dict[str, int] = {}
        self._last_message_id: str | None = None
        self._last_message_content: str | None = None
        self._stream_line_open = False

    def start_turn(self) -> None:
        self._reset()

    def before_non_delta(self) -> None:
        if self._stream_line_open:
            print(file=self._stream, flush=True)
            self._stream_line_open = False

    def render_delta(self, payload: Mapping[str, Any]) -> None:
        message_id = payload.get("message_id")
        delta = payload.get("delta")
        delta_index = payload.get("delta_index")
        if not isinstance(message_id, str) or not message_id or not isinstance(delta, str):
            return
        delta = sanitize_terminal_text(delta)
        expected = self._delta_indices.get(message_id, 0)
        if type(delta_index) is not int or delta_index != expected:
            return
        self._delta_indices[message_id] = expected + 1
        self._streamed_messages[message_id] = self._streamed_messages.get(message_id, "") + delta
        self._last_message_id = message_id
        print(delta, end="", file=self._stream, flush=True)
        self._stream_line_open = True

    def render_message(self, payload: Mapping[str, Any]) -> None:
        message_id = payload.get("message_id")
        content = payload.get("content")
        if not isinstance(message_id, str) or not message_id or not isinstance(content, str):
            return
        content = sanitize_terminal_text(content)
        streamed = self._streamed_messages.get(message_id)
        self._last_message_id = message_id
        self._last_message_content = content
        if streamed != content:
            if streamed:
                self._render_notice("[authoritative final]")
            if content:
                self._render_content(content)
        self._streamed_messages.pop(message_id, None)
        self._delta_indices.pop(message_id, None)

    def abort_message(self, payload: Mapping[str, Any]) -> None:
        message_id = payload.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            return
        self._delta_indices.pop(message_id, None)

    def finish_turn(self, payload: Mapping[str, Any]) -> None:
        content = sanitize_terminal_text(str(payload.get("content", "")))
        streamed = self._streamed_messages.get(self._last_message_id or "")
        final_message_id = payload.get("final_message_id")
        output_kind = payload.get("output_kind")
        if (
            output_kind == "runtime_augmented"
            and final_message_id == self._last_message_id
            and self._last_message_content
        ):
            prefix = f"{self._last_message_content.rstrip()}\n\n"
            if content.startswith(prefix):
                content = content[len(prefix) :]
                self._render_notice("[runtime delivery]")
        if content != self._last_message_content and streamed != content:
            if self._last_message_content is not None or any(self._streamed_messages.values()):
                self._render_notice("[authoritative final]")
            if content:
                self._render_content(content)
        self._reset()

    def _reset(self) -> None:
        self._streamed_messages.clear()
        self._delta_indices.clear()
        self._last_message_id = None
        self._last_message_content = None
        self._stream_line_open = False

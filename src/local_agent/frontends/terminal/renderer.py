from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from ...protocol.events import AgentEvent


class TerminalEventSink:
    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        show_tools: bool = True,
        use_rich: bool = True,
    ) -> None:
        self._stream = stream or sys.stdout
        self._show_tools = show_tools
        self._console = _load_console(self._stream) if use_rich else None

    def emit(self, event: AgentEvent) -> None:
        if event.type == "SessionStarted":
            self._print_muted(f"[session] {event.session_id}")
        elif event.type == "ToolStarted" and self._show_tools:
            self._render_tool_started(event)
        elif event.type == "ToolOutput" and self._show_tools:
            self._render_tool_output(event)
        elif event.type in {"ToolFinished", "ToolFailed"} and self._show_tools:
            self._render_tool_finished(event)
        elif event.type == "ApprovalResult":
            self._render_approval_result(event)
        elif event.type == "TurnFinished":
            self._render_final(event)
        elif event.type == "ErrorEvent":
            self._print_error(str(event.payload.get("message", "Unknown error.")))

    def _render_tool_started(self, event: AgentEvent) -> None:
        name = str(event.payload.get("name", ""))
        arguments = _shorten(_render_jsonish(event.payload.get("arguments", "")), 1000)
        self._print_muted(f"[tool:start] {name} {arguments}")

    def _render_tool_output(self, event: AgentEvent) -> None:
        if not event.payload.get("is_error"):
            return
        name = str(event.payload.get("name", ""))
        preview = str(event.payload.get("content_preview", ""))
        if preview:
            self._print_error(f"[tool:error] {name}: {_shorten(preview, 1200)}")

    def _render_tool_finished(self, event: AgentEvent) -> None:
        name = str(event.payload.get("name", ""))
        status = "error" if event.type == "ToolFailed" else "ok"
        length = event.payload.get("content_length", 0)
        self._print_muted(f"[tool:end] {name} {status} ({length} chars)")

    def _render_approval_result(self, event: AgentEvent) -> None:
        tool = str(event.payload.get("tool", ""))
        decision = str(event.payload.get("decision", ""))
        allowed = bool(event.payload.get("allowed", False))
        if allowed:
            self._print_muted(f"[approval] {tool} {decision}")
        else:
            self._print_error(f"[approval] {tool} {decision}")

    def _render_final(self, event: AgentEvent) -> None:
        content = str(event.payload.get("content", ""))
        if self._console is not None:
            markdown = _load_rich_markdown(content)
            if markdown is not None:
                self._console.print(markdown)
                return
        print(content, file=self._stream)

    def _print_muted(self, text: str) -> None:
        if self._console is not None:
            self._console.print(text, style="dim", markup=False)
            return
        print(text, file=self._stream)

    def _print_error(self, text: str) -> None:
        if self._console is not None:
            self._console.print(text, style="bold red", markup=False)
            return
        print(text, file=self._stream)


def _load_console(stream: TextIO):
    try:
        from rich.console import Console
    except ImportError:
        return None
    return Console(file=stream, highlight=False)


def _load_rich_markdown(content: str):
    try:
        from rich.markdown import Markdown
    except ImportError:
        return None
    return Markdown(content)


def _render_jsonish(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def _shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "...<truncated>"

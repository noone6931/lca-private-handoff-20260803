from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
import json
from typing import Any

from ...protocol.events import AgentEvent
from .mailbox import TuiMailbox
from .messages import TuiEvent
from .text import sanitize_terminal_text


_MAX_EVENT_TEXT = 96 * 1024
_MAX_DELTA_TEXT = 16 * 1024
_MAX_TRANSCRIPT_ENTRIES = 512
_MAX_TOOL_ENTRIES = 128


@dataclass(frozen=True)
class TranscriptEntry:
    entry_id: str
    role: str
    text: str
    provisional: bool = False
    authoritative: bool = False


@dataclass(frozen=True)
class ToolEntry:
    seq: int
    name: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class TuiState:
    session_id: str = ""
    provider: str = ""
    workspace: str = ""
    run_id: str | None = None
    command_id: str | None = None
    busy: bool = False
    status: str = "ready"
    continued: bool = False
    transcript: tuple[TranscriptEntry, ...] = ()
    tools: tuple[ToolEntry, ...] = ()
    todos: tuple[str, ...] = ()
    dropped_messages: int = 0


class TuiEventSink:
    """Runtime-thread sink that emits only bounded display-safe messages."""

    def __init__(self, mailbox: TuiMailbox, *, show_tools: bool = True) -> None:
        self._mailbox = mailbox
        self._show_tools = show_tools

    def emit(self, event: AgentEvent) -> None:
        if not self._show_tools and event.type in {"ToolStarted", "ToolOutput", "ToolFinished", "ToolFailed"}:
            return
        projected = project_agent_event(event)
        if projected is not None:
            self._mailbox.put(projected)


class TuiProjector:
    """UI-thread owner of the immutable TUI view state."""

    def __init__(self, state: TuiState | None = None) -> None:
        self._state = state or TuiState()
        self._delta_indices: dict[str, int] = {}
        self._active_run_id: str | None = None
        self._candidate_message_id: str | None = None
        self._last_assistant_content: str | None = None
        self._local_entry_seq = _next_local_entry_seq(self._state.transcript)

    @property
    def state(self) -> TuiState:
        return self._state

    def append_local(self, role: str, text: str) -> TuiState:
        """Append frontend-owned help, status, or error text without forging Runtime events."""

        entry = TranscriptEntry(
            entry_id=f"local:{self._local_entry_seq}",
            role=role,
            text=_bounded_text(text),
        )
        self._local_entry_seq += 1
        self._state = replace(
            self._state,
            transcript=(*self._state.transcript, entry)[-_MAX_TRANSCRIPT_ENTRIES:],
        )
        return self._state

    def set_status(self, status: str) -> TuiState:
        self._state = replace(self._state, status=_bounded_text(status, 512))
        return self._state

    def apply(self, event: TuiEvent, *, dropped_messages: int | None = None) -> TuiState:
        state = self._state
        if event.type == "SessionStarted":
            state = replace(
                state,
                session_id=event.session_id,
                provider=str(event.get("provider", "")),
                workspace=str(event.get("workspace", "")),
                status="resumed" if event.get("continued", False) else "ready",
                continued=bool(event.get("continued", False)),
            )
        elif event.type == "TurnStarted":
            self._delta_indices.clear()
            self._active_run_id = event.run_id
            self._candidate_message_id = None
            self._last_assistant_content = None
            state = replace(
                state,
                session_id=event.session_id,
                run_id=event.run_id,
                command_id=event.command_id,
                busy=True,
                status="running",
            )
        elif event.type == "UserMessage":
            state = self._append_transcript(state, event, "user", str(event.get("content", "")))
        elif event.type == "AssistantDelta":
            if event.run_id == self._active_run_id:
                state = self._apply_delta(state, event)
        elif event.type == "AssistantMessage":
            if event.run_id == self._active_run_id:
                state = self._apply_assistant_message(state, event)
        elif event.type == "AssistantMessageAborted":
            if event.run_id == self._active_run_id:
                state = self._apply_assistant_message_aborted(state, event)
        elif event.type == "TurnFinished":
            if event.run_id == self._active_run_id:
                state = self._apply_turn_finished(state, event)
                self._active_run_id = None
                self._candidate_message_id = None
        elif event.type == "ToolStarted":
            state = self._append_tool(state, event, "running")
        elif event.type in {"ToolFinished", "ToolFailed"}:
            status = "failed" if event.type == "ToolFailed" else "completed"
            state = self._append_tool(state, event, status)
        elif event.type == "ToolOutput":
            state = self._apply_tool_output(state, event)
        elif event.type == "ApprovalRequested":
            state = replace(state, status=f"approval: {event.get('tool', '')}")
        elif event.type == "ApprovalResult":
            decision = str(event.get("decision", ""))
            state = replace(state, status=f"approval {decision}".strip())
        elif event.type == "TodoUpdated":
            todos = event.get("todos", "")
            if isinstance(todos, str):
                state = replace(state, todos=tuple(line for line in todos.splitlines() if line))
        elif event.type == "WorkspaceMoved":
            state = replace(state, status="workspace moved")
        elif event.type == "WorkspaceRootsChanged":
            state = replace(state, status="workspace roots changed")
        elif event.type == "ErrorEvent":
            state = self._append_transcript(state, event, "error", str(event.get("message", "Runtime error.")))
            state = replace(state, status="error")
        if dropped_messages is not None:
            state = replace(state, dropped_messages=dropped_messages)
        self._state = state
        return state

    def _apply_delta(self, state: TuiState, event: TuiEvent) -> TuiState:
        message_id = event.get("message_id")
        delta = event.get("delta")
        delta_index = event.get("delta_index")
        delta_span = event.get("delta_span", 1)
        if not isinstance(message_id, str) or not isinstance(delta, str):
            return state
        expected = self._delta_indices.get(message_id, 0)
        if type(delta_index) is not int or delta_index != expected or type(delta_span) is not int:
            return state
        self._delta_indices[message_id] = expected + delta_span
        entries = list(state.transcript)
        if entries and entries[-1].entry_id == message_id and entries[-1].provisional:
            entries[-1] = replace(entries[-1], text=_bounded_text(entries[-1].text + delta))
        else:
            entries.append(TranscriptEntry(message_id, "assistant", delta, provisional=True))
        return replace(state, transcript=tuple(entries[-_MAX_TRANSCRIPT_ENTRIES:]))

    def _apply_assistant_message(self, state: TuiState, event: TuiEvent) -> TuiState:
        message_id = event.get("message_id")
        content = event.get("content")
        if not isinstance(message_id, str) or not message_id or not isinstance(content, str):
            return state
        self._last_assistant_content = content
        entries = list(state.transcript)
        phase = str(event.get("phase", ""))
        if phase in {"candidate", "tool_call"}:
            if self._candidate_message_id and self._candidate_message_id != message_id:
                entries = [
                    entry
                    for entry in entries
                    if not (entry.entry_id == self._candidate_message_id and entry.provisional)
                ]
            candidate_index = next(
                (index for index in range(len(entries) - 1, -1, -1) if entries[index].entry_id == message_id),
                None,
            )
            if candidate_index is not None:
                entries[candidate_index] = replace(
                    entries[candidate_index],
                    text=content,
                    provisional=True,
                )
            elif content:
                entries.append(
                    TranscriptEntry(
                        entry_id=message_id,
                        role="assistant",
                        text=content,
                        provisional=True,
                    )
                )
            self._candidate_message_id = message_id
            self._delta_indices.pop(message_id, None)
            return replace(state, transcript=tuple(entries[-_MAX_TRANSCRIPT_ENTRIES:]))
        provisional = (
            entries[-1]
            if entries and entries[-1].entry_id == message_id and entries[-1].provisional
            else None
        )
        if provisional is not None and provisional.text == content:
            entries[-1] = replace(provisional, provisional=False)
        elif provisional is not None:
            entries[-1] = replace(provisional, text=content, provisional=False, authoritative=True)
        elif content:
            entries.append(TranscriptEntry(entry_id=message_id, role="assistant", text=content))
        self._delta_indices.pop(message_id, None)
        return replace(state, transcript=tuple(entries[-_MAX_TRANSCRIPT_ENTRIES:]))

    def _apply_turn_finished(self, state: TuiState, event: TuiEvent) -> TuiState:
        content = str(event.get("content", ""))
        entries = list(state.transcript)
        final_message_id = event.get("final_message_id")
        origin = str(event.get("origin", ""))
        if isinstance(final_message_id, str) and final_message_id:
            entries = [
                entry
                for entry in entries
                if not (entry.provisional and entry.entry_id != final_message_id)
            ]
            final_index = next(
                (index for index in range(len(entries) - 1, -1, -1) if entries[index].entry_id == final_message_id),
                None,
            )
            if final_index is not None:
                entries[final_index] = replace(
                    entries[final_index],
                    text=content or entries[final_index].text,
                    provisional=False,
                    authoritative=entries[final_index].authoritative or origin == "runtime",
                )
                content = ""
            elif content:
                entries.append(
                    TranscriptEntry(
                        entry_id=final_message_id,
                        role="assistant",
                        text=content,
                        authoritative=origin == "runtime",
                    )
                )
                content = ""
        provisional = entries[-1] if entries and entries[-1].provisional else None
        if provisional is not None and provisional.text == content:
            entries[-1] = replace(provisional, provisional=False)
        elif content and content != self._last_assistant_content:
            if provisional is not None:
                entries[-1] = replace(provisional, provisional=False)
            entries.append(
                TranscriptEntry(
                    entry_id=f"final:{event.run_id or event.seq}",
                    role="assistant",
                    text=content,
                    authoritative=self._last_assistant_content is not None or provisional is not None,
                )
            )
        reason = str(event.get("reason", ""))
        status = "ready" if reason == "final" else reason or "stopped"
        return replace(
            state,
            busy=False,
            status=status,
            transcript=tuple(entries[-_MAX_TRANSCRIPT_ENTRIES:]),
        )

    def _apply_assistant_message_aborted(self, state: TuiState, event: TuiEvent) -> TuiState:
        message_id = event.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            return state
        entries = list(state.transcript)
        if entries and entries[-1].entry_id == message_id and entries[-1].provisional:
            entries.pop()
        if self._candidate_message_id == message_id:
            self._candidate_message_id = None
        self._delta_indices.pop(message_id, None)
        return replace(state, transcript=tuple(entries[-_MAX_TRANSCRIPT_ENTRIES:]))

    @staticmethod
    def _append_transcript(state: TuiState, event: TuiEvent, role: str, text: str) -> TuiState:
        entry = TranscriptEntry(f"{event.type}:{event.seq}", role, _bounded_text(text))
        return replace(state, transcript=(*state.transcript, entry)[-_MAX_TRANSCRIPT_ENTRIES:])

    @staticmethod
    def _append_tool(state: TuiState, event: TuiEvent, status: str) -> TuiState:
        name = str(event.get("name", "tool"))
        detail = str(event.get("detail", ""))
        tool = ToolEntry(event.seq, name, status, detail)
        tools = list(state.tools)
        if status != "running":
            index = next(
                (index for index in range(len(tools) - 1, -1, -1) if tools[index].name == name and tools[index].status == "running"),
                None,
            )
            if index is not None:
                tools[index] = tool
                return replace(state, tools=tuple(tools[-_MAX_TOOL_ENTRIES:]))
        return replace(state, tools=tuple((*tools, tool)[-_MAX_TOOL_ENTRIES:]))

    @staticmethod
    def _apply_tool_output(state: TuiState, event: TuiEvent) -> TuiState:
        name = str(event.get("name", "tool"))
        detail = str(event.get("detail", ""))
        if not detail:
            return state
        tools = list(state.tools)
        index = next((index for index in range(len(tools) - 1, -1, -1) if tools[index].name == name), None)
        if index is not None:
            tools[index] = replace(tools[index], detail=detail)
        return replace(state, tools=tuple(tools))


def project_agent_event(event: AgentEvent) -> TuiEvent | None:
    fields: list[tuple[str, str | int | float | bool | None]] = []
    payload = event.payload
    if event.type == "AssistantDelta":
        fields.extend(
            (
                ("message_id", _string(payload.get("message_id"), 256)),
                ("delta", _string(payload.get("delta"), _MAX_DELTA_TEXT)),
                ("delta_index", _integer(payload.get("delta_index"))),
                ("delta_span", 1),
                ("provisional", bool(payload.get("provisional", True))),
            )
        )
    elif event.type == "AssistantMessage":
        fields.extend(
            (
                ("message_id", _string(payload.get("message_id"), 256)),
                ("content", _string(payload.get("content"), _MAX_EVENT_TEXT)),
                ("finish_reason", _string(payload.get("finish_reason"), 128)),
                ("provider", _string(payload.get("provider"), 128)),
                ("authoritative", bool(payload.get("authoritative", True))),
                ("origin", _string(payload.get("origin"), 128)),
                ("phase", _string(payload.get("phase"), 128)),
                ("status", _string(payload.get("status"), 128)),
            )
        )
    elif event.type == "AssistantMessageAborted":
        fields.extend(
            (
                ("message_id", _string(payload.get("message_id"), 256)),
                ("reason", _string(payload.get("reason"), 128)),
            )
        )
    elif event.type in {"UserMessage", "TurnFinished"}:
        fields.append(("content", _string(payload.get("content"), _MAX_EVENT_TEXT)))
        if event.type == "TurnFinished":
            fields.extend(
                (
                    ("reason", _string(payload.get("reason"), 128)),
                    ("status", _string(payload.get("status"), 128)),
                    ("delivered", bool(payload.get("delivered", False))),
                    ("final_message_id", _string(payload.get("final_message_id"), 256)),
                    ("origin", _string(payload.get("origin"), 128)),
                    ("output_kind", _string(payload.get("output_kind"), 128)),
                )
            )
    elif event.type in {"ToolStarted", "ToolFinished", "ToolFailed"}:
        fields.append(("name", _string(payload.get("name"), 256)))
        if event.type != "ToolStarted":
            length = _integer(payload.get("content_length"))
            fields.append(("detail", f"{length} chars" if length is not None else ""))
    elif event.type == "ToolOutput":
        if not payload.get("is_error"):
            return None
        fields.extend(
            (
                ("name", _string(payload.get("name"), 256)),
                ("detail", _single_line_text(payload.get("content_preview"), 2048)),
            )
        )
    elif event.type in {"ApprovalRequested", "ApprovalResult"}:
        fields.append(("tool", _string(payload.get("tool"), 256)))
        if event.type == "ApprovalResult":
            fields.extend(
                (
                    ("decision", _string(payload.get("decision"), 128)),
                    ("allowed", bool(payload.get("allowed", False))),
                )
            )
    elif event.type == "ErrorEvent":
        fields.extend(
            (
                ("kind", _string(payload.get("kind"), 128)),
                ("message", _string(payload.get("message"), 4096)),
            )
        )
    elif event.type == "TodoUpdated":
        fields.append(("todos", _todo_text(payload.get("todos"))))
    elif event.type in {
        "TurnStarted",
        "RunSummary",
        "WorkspaceRootsChanged",
        "WorkspaceMoved",
    }:
        pass
    elif event.type == "SessionStarted":
        fields.extend(
            (
                ("continued", bool(payload.get("continued", False))),
                ("provider", _string(payload.get("provider"), 128)),
                ("workspace", _string(payload.get("workspace"), 2048)),
            )
        )
    else:
        return None
    return TuiEvent(
        type=event.type,
        seq=event.seq,
        session_id=event.session_id,
        run_id=event.run_id,
        command_id=event.command_id,
        fields=tuple(fields),
    )


def _todo_text(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(_string(item, 512) for item in value[:64])
    if isinstance(value, str):
        return _bounded_text(value, 32 * 1024)
    try:
        return _bounded_text(json.dumps(value, ensure_ascii=False, default=str), 32 * 1024)
    except TypeError:
        return ""


def _string(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return _bounded_text(value, limit)


def _single_line_text(value: Any, limit: int) -> str:
    return _string(value, limit).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def _integer(value: Any) -> int | None:
    return value if type(value) is int else None


def _bounded_text(value: str, limit: int = _MAX_EVENT_TEXT) -> str:
    sanitized = sanitize_terminal_text(value)
    if len(sanitized) <= limit:
        return sanitized
    return sanitized[:limit] + "...<truncated>"


def _next_local_entry_seq(entries: tuple[TranscriptEntry, ...]) -> int:
    values: list[int] = []
    for entry in entries:
        prefix, separator, suffix = entry.entry_id.partition(":")
        if prefix == "local" and separator and suffix.isdecimal():
            values.append(int(suffix))
    return max(values, default=-1) + 1

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_RESUME_MESSAGE_LIMIT = 80


class SessionError(RuntimeError):
    """Raised when a session cannot be opened or reconstructed."""


class JsonlSessionStore:
    def __init__(
        self,
        workspace: Path,
        *,
        state_dir: Path | None = None,
        session_id: str | None = None,
        continue_recent: bool = False,
    ):
        self.state_dir = state_dir or workspace / ".local-agent"
        self.session_dir = self.state_dir / "sessions"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        if session_id:
            self.path = self._path_for_id(session_id)
            if not self.path.exists():
                raise SessionError(f"Session not found: {session_id}")
        elif continue_recent:
            self.path = self._latest_session_path()
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            self.path = self.session_dir / f"{stamp}.jsonl"
        self.session_id = self.path.stem

    def append(self, event: str, payload: dict[str, Any]) -> None:
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "payload": payload,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load_messages(self, max_messages: int = DEFAULT_RESUME_MESSAGE_LIMIT) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        messages: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SessionError(f"Malformed JSONL at {self.path}:{line_number}") from exc
                event = record.get("event")
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if event == "user":
                    messages.append({"role": "user", "content": payload.get("content", "")})
                elif event == "assistant":
                    if payload.get("role") in {None, "assistant"}:
                        messages.append({**payload, "role": "assistant"})
                elif event == "tool_result":
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": payload.get("tool_call_id"),
                            "content": payload.get("content", ""),
                        }
                    )
        return _trim_recent_messages(messages, max_messages)

    def load_latest_workspace_roots(self) -> dict[str, Any] | None:
        """Return the latest T-128 root state without replaying model messages."""

        if not self.path.exists():
            return None
        latest: dict[str, Any] | None = None
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SessionError(f"Malformed JSONL at {self.path}:{line_number}") from exc
                if record.get("event") not in {"workspace_roots_changed", "workspace_moved"}:
                    continue
                payload = record.get("payload")
                if isinstance(payload, dict):
                    latest = dict(payload)
        return latest

    def load_event_payloads(self, event: str, *, max_events: int = 0) -> list[dict[str, Any]]:
        """Load bounded typed event payloads without projecting them as chat messages."""

        if not self.path.exists():
            return []
        payloads: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SessionError(f"Malformed JSONL at {self.path}:{line_number}") from exc
                payload = record.get("payload")
                if record.get("event") == event and isinstance(payload, dict):
                    payloads.append(dict(payload))
                    if max_events > 0 and len(payloads) > max_events:
                        payloads = payloads[-max_events:]
        return payloads

    def relocate(self, state_dir: Path) -> None:
        """Point this store at an already-migrated session file in a new state dir."""

        resolved_state_dir = state_dir.expanduser().resolve()
        session_dir = resolved_state_dir / "sessions"
        path = session_dir / f"{self.session_id}.jsonl"
        if not path.exists():
            raise SessionError(f"Relocated session file not found: {path}")
        self.state_dir = resolved_state_dir
        self.session_dir = session_dir
        self.path = path

    def _path_for_id(self, session_id: str) -> Path:
        name = session_id.removesuffix(".jsonl")
        if "/" in name or "\\" in name or name in {"", ".", ".."}:
            raise SessionError(f"Invalid session id: {session_id}")
        return self.session_dir / f"{name}.jsonl"

    def _latest_session_path(self) -> Path:
        sessions = sorted(self.session_dir.glob("*.jsonl"), key=lambda path: path.stat().st_mtime)
        if not sessions:
            raise SessionError("No previous session found.")
        return sessions[-1]


def _trim_recent_messages(messages: list[dict[str, Any]], max_messages: int) -> list[dict[str, Any]]:
    recent = messages if max_messages <= 0 or len(messages) <= max_messages else messages[-max_messages:]
    while recent and recent[0].get("role") == "tool":
        recent = recent[1:]
    return _drop_trailing_unpaired_tool_calls(recent)


def _drop_trailing_unpaired_tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trimmed = list(messages)
    while trimmed:
        last_assistant_index = _last_assistant_with_tool_calls_index(trimmed)
        if last_assistant_index is None:
            return trimmed
        expected_ids = _assistant_tool_call_ids(trimmed[last_assistant_index])
        following_ids = {
            message.get("tool_call_id")
            for message in trimmed[last_assistant_index + 1 :]
            if message.get("role") == "tool"
        }
        if expected_ids.issubset(following_ids):
            return trimmed
        trimmed = trimmed[:last_assistant_index]
    return trimmed


def _last_assistant_with_tool_calls_index(messages: list[dict[str, Any]]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "assistant" and _assistant_tool_call_ids(message):
            return index
    return None


def _assistant_tool_call_ids(message: dict[str, Any]) -> set[str]:
    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        return set()
    ids = set()
    for tool_call in tool_calls:
        if isinstance(tool_call, dict) and isinstance(tool_call.get("id"), str):
            ids.add(tool_call["id"])
    return ids

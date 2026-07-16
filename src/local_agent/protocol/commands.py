from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal


SUPPORTED_COMMAND_TYPES = frozenset(
    {
        "SubmitPrompt",
        "GetStatus",
        "ListTools",
        "GetApproval",
        "SetApprovalMode",
        "SetToolApproval",
        "ResetToolApproval",
        "ListWorkspaceRoots",
        "AddWorkspaceRoot",
        "RemoveWorkspaceRoot",
        "ResetWorkspaceRoots",
        "MoveWorkspace",
    }
)
UNSUPPORTED_COMMAND_TYPES = frozenset(
    {"ApproveTool", "RejectTool", "CancelRun", "InterruptTool", "ContinueSession"}
)
COMMAND_TYPES = SUPPORTED_COMMAND_TYPES | UNSUPPORTED_COMMAND_TYPES
COMMAND_RESULT_STATUSES = frozenset({"ok", "error", "unsupported"})

_COMMAND_PAYLOAD_FIELDS: dict[str, dict[str, type]] = {
    "SubmitPrompt": {"prompt": str},
    "GetStatus": {},
    "ListTools": {},
    "GetApproval": {},
    "SetApprovalMode": {"mode": str},
    "SetToolApproval": {"tool": str, "policy": str},
    "ResetToolApproval": {"tool": str},
    "ListWorkspaceRoots": {},
    "AddWorkspaceRoot": {"path": str},
    "RemoveWorkspaceRoot": {"path": str},
    "ResetWorkspaceRoots": {},
    "MoveWorkspace": {"path": str},
}


@dataclass(frozen=True)
class AgentCommand:
    command_id: str
    session_id: str | None
    run_id: str | None
    timestamp: float
    type: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "type": self.type,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class CommandResult:
    """Transport result; task delivery remains explicit in the TurnFinished payload."""

    command_id: str
    session_id: str
    run_id: str | None
    status: Literal["ok", "error", "unsupported"]
    payload: dict[str, Any]
    error_code: str | None = None
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        error = None
        if self.error_code is not None or self.error_message is not None:
            error = {"code": self.error_code, "message": self.error_message}
        return {
            "command_id": self.command_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "status": self.status,
            "payload": self.payload,
            "error": error,
        }


def new_command(
    command_type: str,
    payload: dict[str, Any] | None = None,
    *,
    session_id: str | None = None,
    run_id: str | None = None,
) -> AgentCommand:
    command = AgentCommand(
        command_id=uuid.uuid4().hex,
        session_id=session_id,
        run_id=run_id,
        timestamp=time.time(),
        type=command_type,
        payload=dict(payload or {}),
    )
    error = command_validation_error(command)
    if error is not None:
        raise ValueError(error)
    return command


def command_validation_error(command: AgentCommand) -> str | None:
    if not isinstance(command.command_id, str) or not command.command_id.strip():
        return "command_id must be a non-empty string."
    if command.type not in COMMAND_TYPES:
        return f"Unknown command type: {command.type}"
    if command.session_id is not None and not isinstance(command.session_id, str):
        return "session_id must be a string or null."
    if command.run_id is not None and not isinstance(command.run_id, str):
        return "run_id must be a string or null."
    if isinstance(command.timestamp, bool) or not isinstance(command.timestamp, (int, float)):
        return "timestamp must be numeric."
    if not isinstance(command.payload, dict):
        return "payload must be an object."
    if command.type in UNSUPPORTED_COMMAND_TYPES:
        return None
    fields = _COMMAND_PAYLOAD_FIELDS[command.type]
    missing = sorted(set(fields) - set(command.payload))
    extra = sorted(set(command.payload) - set(fields))
    if missing:
        return f"{command.type} payload is missing fields: {', '.join(missing)}."
    if extra:
        return f"{command.type} payload has unexpected fields: {', '.join(extra)}."
    for name, expected_type in fields.items():
        value = command.payload.get(name)
        if not isinstance(value, expected_type) or (expected_type is str and not value.strip()):
            return f"{command.type} payload field '{name}' must be a non-empty {expected_type.__name__}."
    return None

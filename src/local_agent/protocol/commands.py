from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any


COMMAND_TYPES = {
    "SubmitPrompt",
    "ApproveTool",
    "RejectTool",
    "SetApprovalMode",
    "SetToolApproval",
    "CancelRun",
    "InterruptTool",
    "ContinueSession",
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


def new_command(
    command_type: str,
    payload: dict[str, Any] | None = None,
    *,
    session_id: str | None = None,
    run_id: str | None = None,
) -> AgentCommand:
    if command_type not in COMMAND_TYPES:
        raise ValueError(f"Unknown command type: {command_type}")
    return AgentCommand(
        command_id=uuid.uuid4().hex,
        session_id=session_id,
        run_id=run_id,
        timestamp=time.time(),
        type=command_type,
        payload=payload or {},
    )

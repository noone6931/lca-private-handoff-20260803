from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from ...protocol.commands import AgentCommand
from ...protocol.commands import CommandResult
from ...protocol.interactions import InteractionRequest


TuiScalar: TypeAlias = str | int | float | bool | None


@dataclass(frozen=True)
class TuiEvent:
    """A bounded, display-safe projection of one Runtime event."""

    type: str
    seq: int
    session_id: str
    run_id: str | None
    command_id: str | None
    fields: tuple[tuple[str, TuiScalar], ...] = ()

    def get(self, name: str, default: TuiScalar = None) -> TuiScalar:
        return next((value for key, value in self.fields if key == name), default)


@dataclass(frozen=True)
class TuiInteractionPending:
    request_id: str
    request: InteractionRequest


@dataclass(frozen=True)
class TuiInteractionClosed:
    request_id: str
    status: str


@dataclass(frozen=True)
class TuiCommandCompleted:
    command: AgentCommand
    result: CommandResult


@dataclass(frozen=True)
class TuiWorkerFailed:
    command: AgentCommand
    error_kind: str


TuiMessage: TypeAlias = (
    TuiEvent | TuiInteractionPending | TuiInteractionClosed | TuiCommandCompleted | TuiWorkerFailed
)

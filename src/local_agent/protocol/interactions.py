from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


InteractionKind = Literal["ask", "approval"]
InteractionStatus = Literal["answered", "cancelled", "timed_out", "eof"]


@dataclass(frozen=True)
class InteractionRequest:
    """A synchronous interaction request emitted by the Runtime boundary.

    The runtime owns the request's semantic meaning; a frontend owns focus, prompt
    rendering, keyboard handling, and returning a result. This keeps terminal input
    policy out of individual tools and leaves a replaceable boundary for a future
    asynchronous Command Bus.
    """

    kind: InteractionKind
    prompt: str
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class InteractionResult:
    status: InteractionStatus
    value: str | None = None


class InteractionHandler(Protocol):
    def request_interaction(self, request: InteractionRequest) -> InteractionResult:
        ...

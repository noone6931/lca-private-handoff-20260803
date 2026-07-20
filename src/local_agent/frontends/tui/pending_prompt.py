from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .input import MAX_INPUT_BYTES


AdmissionStatus = Literal["admitted", "empty", "too_large", "full"]


@dataclass(frozen=True)
class PendingPrompt:
    text: str
    byte_count: int


class PendingPromptQueue:
    """One in-memory next-turn prompt owned entirely by the TUI."""

    def __init__(self, *, byte_limit: int = MAX_INPUT_BYTES) -> None:
        self._byte_limit = max(byte_limit, 1)
        self._pending: PendingPrompt | None = None

    @property
    def pending(self) -> PendingPrompt | None:
        return self._pending

    def admit(self, text: str) -> AdmissionStatus:
        byte_count = len(text.encode("utf-8"))
        if not text or not text.strip():
            return "empty"
        if byte_count > self._byte_limit:
            return "too_large"
        if self._pending is not None:
            return "full"
        self._pending = PendingPrompt(text, byte_count)
        return "admitted"

    def take(self) -> PendingPrompt | None:
        pending = self._pending
        self._pending = None
        return pending

    def restore(self, pending: PendingPrompt) -> bool:
        actual_bytes = len(pending.text.encode("utf-8"))
        if (
            self._pending is not None
            or not pending.text.strip()
            or pending.byte_count != actual_bytes
            or actual_bytes > self._byte_limit
        ):
            return False
        self._pending = pending
        return True

    def clear(self) -> None:
        self._pending = None

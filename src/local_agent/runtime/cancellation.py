from __future__ import annotations

from threading import Event
from threading import Lock
from typing import Protocol


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


class RunCancelled(KeyboardInterrupt):
    """Raised at a cooperative Runtime boundary after an explicit cancel request."""


class RunCancellation:
    """Single-run cancellation owner shared by the dispatcher and blocking adapters."""

    def __init__(self) -> None:
        self._event = Event()
        self._lock = Lock()
        self._active = False
        self._pending = False

    @property
    def event(self) -> Event:
        return self._event

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def begin(self) -> None:
        with self._lock:
            if self._active:
                raise RuntimeError("A cancellable run is already active.")
            self._event.clear()
            self._active = True
            if self._pending:
                self._event.set()
                self._pending = False

    def request(self, *, include_next: bool = False) -> bool:
        with self._lock:
            if not self._active:
                if not include_next:
                    return False
                self._pending = True
                return True
            self._event.set()
            return True

    def finish(self) -> None:
        with self._lock:
            self._active = False
            self._pending = False
            self._event.clear()

    def raise_if_requested(self) -> None:
        if self._event.is_set():
            raise RunCancelled("Run cancelled by user.")


def raise_if_cancelled(signal: CancellationSignal | None) -> None:
    if signal is not None and signal.is_set():
        raise RunCancelled("Run cancelled before tool execution completed.")

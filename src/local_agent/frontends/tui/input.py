from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time

from .text import sanitize_terminal_text


BRACKETED_PASTE_START = "\x1b[200~"
BRACKETED_PASTE_END = "\x1b[201~"
MAX_INPUT_BYTES = 64 * 1024
_INCOMPLETE_PASTE_SECONDS = 2.0
_NORMAL_PREFIX_SECONDS = 0.1

_TERMINAL_KEY_SEQUENCES = {
    "\x1b[A": "UP",
    "\x1b[B": "DOWN",
    "\x1b[C": "RIGHT",
    "\x1b[D": "LEFT",
    "\x1b[H": "HOME",
    "\x1b[F": "END",
    "\x1b[1~": "HOME",
    "\x1b[3~": "DELETE",
    "\x1b[4~": "END",
    "\x1b[5~": "PAGE_UP",
    "\x1b[6~": "PAGE_DOWN",
    "\x1bOH": "HOME",
    "\x1bOF": "END",
    "\x1bOA": "UP",
    "\x1bOB": "DOWN",
    "\x1bOC": "RIGHT",
    "\x1bOD": "LEFT",
}
_NORMAL_SEQUENCES = (
    BRACKETED_PASTE_START,
    BRACKETED_PASTE_END,
    *_TERMINAL_KEY_SEQUENCES,
)


@dataclass(frozen=True)
class TuiInputEvent:
    kind: str
    value: str


class BracketedPasteDecoder:
    """Assemble terminal paste markers before any content reaches the composer."""

    def __init__(
        self,
        *,
        byte_limit: int = MAX_INPUT_BYTES,
        incomplete_seconds: float = _INCOMPLETE_PASTE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._byte_limit = max(byte_limit, 1)
        self._incomplete_seconds = max(incomplete_seconds, 0.01)
        self._clock = clock
        self._normal_pending = ""
        self._normal_started_at: float | None = None
        self._paste_parts: list[str] = []
        self._paste_marker_pending = ""
        self._paste_bytes = 0
        self._discard_pending = ""
        self._active = False
        self._discarding = False
        self._started_at: float | None = None

    @property
    def paste_active(self) -> bool:
        return self._active or self._discarding

    def feed(self, data: str) -> tuple[TuiInputEvent, ...]:
        if not data:
            return ()
        events: list[TuiInputEvent] = []
        remaining = data
        while remaining:
            if self._discarding:
                remaining = self._discard_until_end(remaining)
                continue
            if self._active:
                remaining = self._consume_paste(remaining, events)
                continue
            remaining = self._consume_normal(remaining, events)
        return tuple(events)

    def expire(self) -> tuple[TuiInputEvent, ...]:
        started_at = self._started_at
        if started_at is None or self._clock() - started_at < self._incomplete_seconds:
            return ()
        if self._active:
            self._paste_parts = []
            self._paste_marker_pending = ""
            self._paste_bytes = 0
            self._active = False
            self._discarding = True
            self._discard_pending = ""
            self._started_at = self._clock()
            return (TuiInputEvent("notice", "Incomplete paste was discarded."),)
        self._discarding = False
        self._discard_pending = ""
        self._started_at = None
        return ()

    def flush_normal(self) -> tuple[TuiInputEvent, ...]:
        if self._active or self._discarding or not self._normal_pending:
            return ()
        if self._normal_started_at is not None and self._clock() - self._normal_started_at < _NORMAL_PREFIX_SECONDS:
            return ()
        pending = self._normal_pending
        self._normal_pending = ""
        self._normal_started_at = None
        return self._key_events(pending)

    def _consume_normal(self, data: str, events: list[TuiInputEvent]) -> str:
        combined = self._normal_pending + data
        self._normal_pending = ""
        self._normal_started_at = None
        start_index = combined.find(BRACKETED_PASTE_START)
        stray_end = combined.find(BRACKETED_PASTE_END)
        if stray_end >= 0 and (start_index < 0 or stray_end < start_index):
            events.extend(self._key_events(combined[:stray_end]))
            events.append(TuiInputEvent("notice", "Unexpected paste terminator was discarded."))
            return combined[stray_end + len(BRACKETED_PASTE_END):]
        if start_index >= 0:
            events.extend(self._key_events(combined[:start_index]))
            self._active = True
            self._started_at = self._clock()
            self._paste_parts = []
            self._paste_marker_pending = ""
            self._paste_bytes = 0
            return combined[start_index + len(BRACKETED_PASTE_START):]
        suffix = _longest_sequence_prefix(combined, _NORMAL_SEQUENCES)
        if suffix:
            emitted = combined[:-len(suffix)]
            self._normal_pending = suffix
            self._normal_started_at = self._clock()
        else:
            emitted = combined
        events.extend(self._key_events(emitted))
        return ""

    def _consume_paste(self, data: str, events: list[TuiInputEvent]) -> str:
        self._started_at = self._clock()
        combined = self._paste_marker_pending + data
        self._paste_marker_pending = ""
        end_index = combined.find(BRACKETED_PASTE_END)
        if end_index >= 0:
            final_part = combined[:end_index]
            total_bytes = self._paste_bytes + len(final_part.encode("utf-8"))
            remaining = combined[end_index + len(BRACKETED_PASTE_END):]
            content = "" if total_bytes > self._byte_limit else "".join((*self._paste_parts, final_part))
            self._reset_paste()
            if total_bytes > self._byte_limit:
                events.append(TuiInputEvent("notice", "Paste exceeded the 64 KiB input limit and was discarded."))
            else:
                normalized = sanitize_terminal_text(content.replace("\r\n", "\n").replace("\r", "\n"))
                events.append(TuiInputEvent("paste", normalized))
            return remaining
        suffix = _longest_marker_prefix(combined, BRACKETED_PASTE_END)
        content = combined[:-len(suffix)] if suffix else combined
        self._paste_marker_pending = suffix
        self._paste_parts.append(content)
        self._paste_bytes += len(content.encode("utf-8"))
        if self._paste_bytes > self._byte_limit:
            self._paste_parts = []
            self._paste_marker_pending = ""
            self._paste_bytes = 0
            self._active = False
            self._discarding = True
            self._discard_pending = ""
            self._started_at = self._clock()
            events.append(TuiInputEvent("notice", "Paste exceeded the 64 KiB input limit and was discarded."))
        return ""

    def _discard_until_end(self, data: str) -> str:
        self._started_at = self._clock()
        combined = self._discard_pending + data
        end_index = combined.find(BRACKETED_PASTE_END)
        if end_index >= 0:
            remaining = combined[end_index + len(BRACKETED_PASTE_END):]
            self._discarding = False
            self._discard_pending = ""
            self._started_at = None
            return remaining
        self._discard_pending = _longest_marker_prefix(combined, BRACKETED_PASTE_END)
        return ""

    def _reset_paste(self) -> None:
        self._paste_parts = []
        self._paste_marker_pending = ""
        self._paste_bytes = 0
        self._active = False
        self._discarding = False
        self._started_at = None

    @staticmethod
    def _key_events(text: str) -> tuple[TuiInputEvent, ...]:
        normalized = text.replace("\r\n", "\n")
        result: list[TuiInputEvent] = []
        index = 0
        while index < len(normalized):
            sequence = next(
                (
                    sequence
                    for sequence in sorted(_TERMINAL_KEY_SEQUENCES, key=len, reverse=True)
                    if normalized.startswith(sequence, index)
                ),
                None,
            )
            if sequence is not None:
                result.append(TuiInputEvent("key", _TERMINAL_KEY_SEQUENCES[sequence]))
                index += len(sequence)
                continue
            character = normalized[index]
            if character == "\x1b" and index + 1 < len(normalized) and normalized[index + 1] in {"\n", "\r"}:
                result.append(TuiInputEvent("key", "ALT_ENTER"))
                index += 2
                continue
            key = {
                "\x03": "CTRL_C",
                "\x06": "CTRL_F",
                "\x10": "CTRL_P",
                "\x11": "CTRL_Q",
                "\x12": "CTRL_R",
                "\x19": "CTRL_Y",
                "\x7f": "BACKSPACE",
                "\b": "BACKSPACE",
                "\n": "ENTER",
                "\r": "ENTER",
                "\x1b": "ESC",
            }.get(character, character)
            result.append(TuiInputEvent("key", key))
            index += 1
        return tuple(result)


def _longest_marker_prefix(value: str, marker: str) -> str:
    limit = min(len(value), len(marker) - 1)
    for size in range(limit, 0, -1):
        if value.endswith(marker[:size]):
            return value[-size:]
    return ""


def _longest_sequence_prefix(value: str, sequences: tuple[str, ...]) -> str:
    best = ""
    for sequence in sequences:
        candidate = _longest_marker_prefix(value, sequence)
        if len(candidate) > len(best):
            best = candidate
    return best

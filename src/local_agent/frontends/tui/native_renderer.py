from __future__ import annotations

from dataclasses import dataclass
import os
import sys

from .model import TranscriptEntry
from .model import TuiState
from .text import cell_width
from .text import clip_cells
from .view import render_frame
from .view import render_inline_frame
from .view import transcript_lines
from .view import TuiFrame
from .view import TuiView


_CSI = "\x1b["
_ENTER_ALT_SCREEN = b"\x1b[?1049h\x1b[?1007h"
_LEAVE_ALT_SCREEN = b"\x1b[?1007l\x1b[?1049l"


@dataclass(frozen=True)
class _EntryKey:
    entry_id: str
    role: str
    text: str
    authoritative: bool


class NativeScrollbackRenderer:
    """Commit settled transcript rows and repaint only the mutable terminal tail."""

    def __init__(self, output=None) -> None:
        self._output = output or sys.stdout
        self._live_rows = 0
        self._cursor_y = 0
        self._last_frame: TuiFrame | None = None
        self._paint_width = 80
        self._committed: set[_EntryKey] = set()
        self._overlay_active = False
        self._suspended = False

    @property
    def overlay_active(self) -> bool:
        return self._overlay_active

    def render(self, state: TuiState, view: TuiView, width: int, height: int) -> None:
        if self._suspended:
            return
        if _overlay_requested(view):
            self._render_overlay(state, view, width, height)
            return
        if self._overlay_active:
            self._leave_overlay()
        self._render_normal(state, view, width, height)

    def suspend(self) -> None:
        if self._suspended:
            return
        if self._overlay_active:
            self._leave_overlay()
        self._erase_live_region()
        self._write(b"\x1b[?25h")
        self._suspended = True

    def resume(self) -> None:
        self._suspended = False
        self._last_frame = None
        self._live_rows = 0
        self._cursor_y = 0

    def close(self) -> None:
        if self._overlay_active:
            self._leave_overlay()
        self._erase_live_region()
        self._write(b"\x1b[0m\x1b[?25h")

    def _render_normal(self, state: TuiState, view: TuiView, width: int, height: int) -> None:
        stable = tuple(entry for entry in state.transcript if not entry.provisional)
        current = {_entry_key(entry) for entry in stable}
        self._committed.intersection_update(current)
        pending = tuple(entry for entry in stable if _entry_key(entry) not in self._committed)
        frame = render_inline_frame(state, view, width, height)
        if not pending and frame == self._last_frame:
            return
        self._erase_live_region(width)
        if pending:
            self._commit_entries(pending, width)
            self._committed.update(_entry_key(entry) for entry in pending)
        self._paint_live_frame(frame, width)

    def _render_overlay(self, state: TuiState, view: TuiView, width: int, height: int) -> None:
        if not self._overlay_active:
            self._erase_live_region(width)
            self._write(_ENTER_ALT_SCREEN)
            self._overlay_active = True
            self._last_frame = None
        frame = render_frame(state, view, width, height)
        if frame == self._last_frame:
            return
        self._write(b"\x1b[?25l\x1b[H\x1b[2J")
        self._write(_frame_bytes(frame, width))
        self._position_cursor(frame)
        self._last_frame = frame

    def _leave_overlay(self) -> None:
        self._write(b"\x1b[?25h" + _LEAVE_ALT_SCREEN)
        self._overlay_active = False
        self._last_frame = None
        self._live_rows = 0
        self._cursor_y = 0

    def _commit_entries(self, entries: tuple[TranscriptEntry, ...], width: int) -> None:
        rows = transcript_lines(entries, max(width - 1, 1))
        if not rows:
            return
        payload = bytearray(b"\x1b[?25l")
        for row in rows:
            payload.extend(b"\r\x1b[2K")
            payload.extend(clip_cells(row, max(width - 1, 1)).rstrip().encode("utf-8"))
            payload.extend(b"\r\n")
        payload.extend(b"\x1b[?25h")
        self._write(bytes(payload))

    def _paint_live_frame(self, frame: TuiFrame, width: int) -> None:
        self._write(b"\x1b[?25l" + _frame_bytes(frame, width))
        self._live_rows = len(frame.lines)
        self._cursor_y = frame.cursor_y
        self._paint_width = width
        self._position_cursor(frame)
        self._last_frame = frame

    def _position_cursor(self, frame: TuiFrame) -> None:
        rows_up = max(len(frame.lines) - 1 - frame.cursor_y, 0)
        payload = "\r"
        if rows_up:
            payload += f"{_CSI}{rows_up}A"
        if frame.cursor_x:
            payload += f"{_CSI}{frame.cursor_x}C"
        payload += "\x1b[?25h"
        self._write(payload.encode("ascii"))

    def _erase_live_region(self, width: int | None = None) -> None:
        if not self._live_rows:
            return
        erase_width = max((width or self._terminal_width()) - 1, 1)
        rows_up = self._cursor_y
        if self._last_frame is not None:
            rows_up = _cursor_row_after_reflow(self._last_frame, self._paint_width, erase_width + 1)
        payload = "\r"
        if rows_up:
            payload += f"{_CSI}{rows_up}A"
        payload += "\x1b[J"
        self._write(payload.encode("ascii"))
        self._live_rows = 0
        self._cursor_y = 0
        self._last_frame = None

    def _terminal_width(self) -> int:
        try:
            return max(os.get_terminal_size(self._output.fileno()).columns, 20)
        except (AttributeError, OSError, ValueError):
            return self._paint_width

    def _write(self, payload: bytes) -> None:
        if not payload:
            return
        fd = self._output.fileno()
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("terminal output closed")
            offset += written


def _overlay_requested(view: TuiView) -> bool:
    return view.focus == "search" or bool(view.search_query)


def _entry_key(entry: TranscriptEntry) -> _EntryKey:
    return _EntryKey(entry.entry_id, entry.role, entry.text, entry.authoritative)


def _frame_bytes(frame: TuiFrame, width: int) -> bytes:
    safe_width = max(width - 1, 1)
    payload = bytearray()
    last = len(frame.lines) - 1
    for index, line in enumerate(frame.lines):
        if index:
            payload.extend(b"\r\n")
        payload.extend(b"\r\x1b[2K")
        if index == 0 or index in frame.accent_rows:
            payload.extend(b"\x1b[1;36m")
        elif index == last:
            payload.extend(b"\x1b[7m")
        payload.extend(clip_cells(line, safe_width).rstrip().encode("utf-8"))
        payload.extend(b"\x1b[0m")
    return bytes(payload)


def _cursor_row_after_reflow(frame: TuiFrame, paint_width: int, current_width: int) -> int:
    old_safe_width = max(paint_width - 1, 1)
    new_safe_width = max(current_width - 1, 1)
    rows = 0
    for line in frame.lines[:frame.cursor_y]:
        rendered = clip_cells(line, old_safe_width).rstrip()
        rows += max((cell_width(rendered) + new_safe_width - 1) // new_safe_width, 1)
    rows += frame.cursor_x // new_safe_width
    return rows

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


@dataclass(frozen=True)
class _LiveStructure:
    focus: str
    interaction: bool
    palette_rows: int
    provisional_entries: tuple[tuple[str, str, bool], ...]


class NativeScrollbackRenderer:
    """Commit settled transcript rows and repaint only the mutable terminal tail."""

    def __init__(self, output=None) -> None:
        self._output = output or sys.stdout
        self._live_rows = 0
        self._reserved_rows = 0
        self._cursor_y = 0
        self._last_frame: TuiFrame | None = None
        self._paint_width = 80
        self._paint_height = 24
        self._live_structure: _LiveStructure | None = None
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
        self._reserved_rows = 0
        self._cursor_y = 0
        self._live_structure = None

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
        structure = _live_structure(state, view)
        if not pending and frame == self._last_frame:
            return
        repaint = (
            pending
            or self._last_frame is None
            or width != self._paint_width
            or height != self._paint_height
            or len(frame.lines) != self._live_rows
            or structure != self._live_structure
        )
        if not repaint:
            self._paint_changed_rows(frame, width)
            return
        self._erase_live_region(width)
        if pending:
            self._commit_entries(pending, width)
            self._committed.update(_entry_key(entry) for entry in pending)
        self._paint_live_frame(frame, width, height, structure)

    def _render_overlay(self, state: TuiState, view: TuiView, width: int, height: int) -> None:
        if not self._overlay_active:
            self._erase_live_region(width)
            self._write(_ENTER_ALT_SCREEN)
            self._overlay_active = True
            self._last_frame = None
            self._live_structure = None
        frame = render_frame(state, view, width, height)
        if frame == self._last_frame:
            return
        self._write(b"\x1b[?25l\x1b[H\x1b[2J")
        self._write(_frame_bytes(frame, width))
        self._position_cursor(frame, len(frame.lines) - 1)
        self._last_frame = frame

    def _leave_overlay(self) -> None:
        self._write(b"\x1b[?25h" + _LEAVE_ALT_SCREEN)
        self._overlay_active = False
        self._last_frame = None
        self._live_rows = 0
        self._reserved_rows = 0
        self._cursor_y = 0
        self._live_structure = None

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
        self._reserved_rows = 0

    def _paint_live_frame(
        self,
        frame: TuiFrame,
        width: int,
        height: int,
        structure: _LiveStructure,
    ) -> None:
        self._reserve_live_region(len(frame.lines))
        self._write(b"\x1b[?25l" + _frame_bytes(frame, width))
        self._live_rows = len(frame.lines)
        self._paint_width = width
        self._paint_height = height
        self._position_cursor(frame, len(frame.lines) - 1)
        self._last_frame = frame
        self._live_structure = structure

    def _paint_changed_rows(self, frame: TuiFrame, width: int) -> None:
        previous = self._last_frame
        if previous is None:
            return
        changed = _changed_row_range(previous, frame)
        if changed is None:
            self._position_cursor(frame, self._cursor_y)
            self._last_frame = frame
            return
        first, last = changed
        payload = bytearray(b"\x1b[?25l")
        payload.extend(_move_to_row(self._cursor_y, first))
        for index in range(first, last + 1):
            if index > first:
                payload.extend(b"\r\x1b[1B")
            payload.extend(_row_bytes(frame, index, width))
        payload.extend(_cursor_bytes(last, frame))
        self._write(bytes(payload))
        self._cursor_y = frame.cursor_y
        self._last_frame = frame

    def _reserve_live_region(self, rows: int) -> None:
        """Reserve mutable rows before painting so they cannot enter scrollback."""

        if rows <= self._reserved_rows:
            return
        existing = self._reserved_rows
        payload = bytearray(b"\r")
        if existing > 1:
            payload.extend(f"{_CSI}{existing - 1}B".encode("ascii"))
        growth = rows - max(existing, 1)
        if growth:
            payload.extend(b"\r\n" * growth)
        if rows > 1:
            payload.extend(f"{_CSI}{rows - 1}A".encode("ascii"))
        self._write(bytes(payload))
        self._reserved_rows = rows

    def _position_cursor(self, frame: TuiFrame, from_row: int) -> None:
        self._write(_cursor_bytes(from_row, frame))
        self._cursor_y = frame.cursor_y

    def _erase_live_region(self, width: int | None = None) -> None:
        if not self._live_rows:
            return
        erase_width = max((width or self._terminal_width()) - 1, 1)
        rows_up = self._cursor_y
        if self._last_frame is not None:
            rows_up = _cursor_row_after_reflow(self._last_frame, self._paint_width, erase_width + 1)
        resized = width is not None and width != self._paint_width
        payload = "\r"
        if rows_up:
            payload += f"{_CSI}{rows_up}A"
        payload += "\x1b[J"
        self._write(payload.encode("ascii"))
        self._live_rows = 0
        self._cursor_y = 0
        self._last_frame = None
        self._live_structure = None
        if resized:
            self._reserved_rows = 0

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


def _live_structure(state: TuiState, view: TuiView) -> _LiveStructure:
    provisional = tuple(
        (entry.entry_id, entry.role, entry.authoritative)
        for entry in state.transcript
        if entry.provisional
    )
    return _LiveStructure(
        focus=view.focus,
        interaction=bool(view.interaction_prompt),
        palette_rows=len(view.palette),
        provisional_entries=provisional,
    )


def _frame_bytes(frame: TuiFrame, width: int) -> bytes:
    payload = bytearray()
    for index in range(len(frame.lines)):
        if index:
            payload.extend(b"\r\n")
        payload.extend(b"\r")
        payload.extend(_row_bytes(frame, index, width))
    return bytes(payload)


def _row_bytes(frame: TuiFrame, index: int, width: int) -> bytes:
    payload = bytearray(b"\x1b[2K")
    if index == 0 or index in frame.accent_rows:
        payload.extend(b"\x1b[1;36m")
    elif index == len(frame.lines) - 1:
        payload.extend(b"\x1b[7m")
    payload.extend(clip_cells(frame.lines[index], max(width - 1, 1)).rstrip().encode("utf-8"))
    payload.extend(b"\x1b[0m")
    return bytes(payload)


def _changed_row_range(previous: TuiFrame, current: TuiFrame) -> tuple[int, int] | None:
    changed = [
        index
        for index in range(len(current.lines))
        if _row_signature(previous, index) != _row_signature(current, index)
    ]
    return (changed[0], changed[-1]) if changed else None


def _row_signature(frame: TuiFrame, index: int) -> tuple[str, bool, bool]:
    return (
        frame.lines[index],
        index == 0 or index in frame.accent_rows,
        index == len(frame.lines) - 1,
    )


def _move_to_row(from_row: int, to_row: int) -> bytes:
    payload = "\r"
    if to_row < from_row:
        payload += f"{_CSI}{from_row - to_row}A"
    elif to_row > from_row:
        payload += f"{_CSI}{to_row - from_row}B"
    return payload.encode("ascii")


def _cursor_bytes(from_row: int, frame: TuiFrame) -> bytes:
    payload = bytearray(_move_to_row(from_row, frame.cursor_y))
    if frame.cursor_x:
        payload.extend(f"{_CSI}{frame.cursor_x}C".encode("ascii"))
    payload.extend(b"\x1b[?25h")
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

from __future__ import annotations

from dataclasses import dataclass

from .model import TranscriptEntry
from .model import TuiState
from .text import cell_width
from .text import clip_cells
from .text import pad_cells
from .text import tail_cells
from .text import wrap_cells


@dataclass(frozen=True)
class TuiView:
    input_text: str = ""
    cursor: int = 0
    focus: str = "chat"
    interaction_prompt: str = ""
    palette: tuple[str, ...] = ()
    palette_index: int = 0
    scroll_offset: int = 0
    notice: str = ""
    search_query: str = ""


@dataclass(frozen=True)
class TuiFrame:
    lines: tuple[str, ...]
    cursor_y: int
    cursor_x: int


def render_frame(state: TuiState, view: TuiView, width: int, height: int) -> TuiFrame:
    width = max(width, 20)
    height = max(height, 6)
    header = _header(state, width)
    footer = _footer(state, view, width)
    palette_rows = min(len(view.palette), 5) if view.palette else 0
    prompt_rows = 2 if view.interaction_prompt else 1
    body_height = max(height - 2 - prompt_rows - palette_rows, 1)
    body = _body(state, width, body_height, view.scroll_offset, view.search_query)
    palette = _palette(view, width, palette_rows)
    prompt, cursor_x = _prompt(view, width)
    lines = [header, *body, *palette]
    if view.interaction_prompt:
        lines.append(pad_cells(clip_cells(view.interaction_prompt, width, marker="..."), width))
    lines.append(prompt)
    lines.append(footer)
    lines = [pad_cells(line, width) for line in lines[:height]]
    while len(lines) < height:
        lines.insert(-1, " " * width)
    cursor_y = min(len(lines) - 2, height - 2)
    return TuiFrame(tuple(lines), cursor_y, min(cursor_x, width - 1))


def _header(state: TuiState, width: int) -> str:
    session = state.session_id[:8] if state.session_id else "new"
    activity = "RUNNING" if state.busy else "READY"
    left = f" LCA  {activity}  session {session}"
    right = f"{state.status} "
    gap = max(width - cell_width(left) - cell_width(right), 1)
    return clip_cells(left + " " * gap + right, width)


def _body(state: TuiState, width: int, height: int, scroll_offset: int, search_query: str) -> tuple[str, ...]:
    side_width = 30 if width >= 100 else 0
    transcript_width = width - side_width - (1 if side_width else 0)
    entries = state.transcript
    if search_query:
        query = search_query.casefold()
        entries = tuple(entry for entry in entries if query in entry.text.casefold())
    transcript = _transcript_lines(entries, transcript_width)
    offset = max(scroll_offset, 0)
    end = max(len(transcript) - offset, 0)
    start = max(end - height, 0)
    visible = list(transcript[start:end])
    visible = [""] * max(height - len(visible), 0) + visible
    if not side_width:
        return tuple(pad_cells(line, width) for line in visible)
    side = _side_lines(state, side_width, height)
    return tuple(
        pad_cells(left, transcript_width) + " " + pad_cells(right, side_width)
        for left, right in zip(visible, side, strict=True)
    )


def _transcript_lines(entries: tuple[TranscriptEntry, ...], width: int) -> tuple[str, ...]:
    lines: list[str] = []
    for entry in entries:
        label = {
            "assistant": "assistant",
            "error": "error",
            "system": "system",
            "user": "you",
        }.get(entry.role, entry.role)
        if entry.authoritative:
            label += " (authoritative)"
        prefix = f"{label}> "
        wrapped = wrap_cells(entry.text, max(width - cell_width(prefix), 1)) or ("",)
        lines.append(prefix + wrapped[0])
        indent = " " * cell_width(prefix)
        lines.extend(indent + line for line in wrapped[1:])
        lines.append("")
    return tuple(lines)


def _side_lines(state: TuiState, width: int, height: int) -> tuple[str, ...]:
    lines = ["CONTEXT"]
    if state.provider:
        lines.append(clip_cells(f"provider {state.provider}", width, marker="..."))
    if state.workspace:
        lines.append(clip_cells(f"workspace {state.workspace}", width, marker="..."))
    lines.append("TOOLS")
    for tool in state.tools[-max((height - len(lines)) // 2, 1):]:
        summary = f"{tool.status:<9} {tool.name} {tool.detail}".rstrip()
        lines.append(clip_cells(summary, width, marker="..."))
    if state.todos and len(lines) < height:
        lines.append("TODOS")
        lines.extend(clip_cells(f"- {todo}", width, marker="...") for todo in state.todos)
    return tuple((lines + [""] * height)[:height])


def _palette(view: TuiView, width: int, rows: int) -> tuple[str, ...]:
    result: list[str] = []
    for index, command in enumerate(view.palette[:rows]):
        marker = ">" if index == view.palette_index else " "
        result.append(pad_cells(clip_cells(f"{marker} {command}", width, marker="..."), width))
    return tuple(result)


def _prompt(view: TuiView, width: int) -> tuple[str, int]:
    label = {"approval": "approve> ", "ask": "answer> ", "search": "search> "}.get(view.focus, "> ")
    before_cursor = view.input_text[:view.cursor].replace("\n", "\\n")
    after_cursor = view.input_text[view.cursor:].replace("\n", "\\n")
    available = max(width - cell_width(label), 1)
    if cell_width(before_cursor) >= available:
        visible_before = tail_cells(before_cursor, max(available - 1, 0))
    else:
        visible_before = before_cursor
    remaining = max(available - cell_width(visible_before), 0)
    visible = visible_before + clip_cells(after_cursor, remaining)
    cursor_x = cell_width(label) + min(cell_width(visible_before), available - 1)
    return pad_cells(label + visible, width), cursor_x


def _footer(state: TuiState, view: TuiView, width: int) -> str:
    notice = view.notice or (f"dropped {state.dropped_messages} UI updates" if state.dropped_messages else "")
    keys = "Enter send  Alt-Enter newline  Ctrl-P commands  Ctrl-F search  Ctrl-Y copy  Ctrl-C cancel  Ctrl-Q quit"
    if view.search_query:
        notice = f"search: {view.search_query}" + (f" | {notice}" if notice else "")
    if notice:
        keys = f"{notice} | {keys}"
    return clip_cells(keys, width, marker="...")

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace

from .markdown import render_markdown_source
from .model import TranscriptEntry
from .model import TuiState
from .text import cell_width
from .text import clip_cells
from .text import pad_cells
from .text import tail_cells


@dataclass(frozen=True)
class TuiViewport:
    top: int = 0
    total_rows: int = 0
    visible_rows: int = 1
    follow_bottom: bool = True

    @property
    def max_top(self) -> int:
        return max(self.total_rows - self.visible_rows, 0)


@dataclass(frozen=True)
class TuiView:
    input_text: str = ""
    cursor: int = 0
    focus: str = "chat"
    interaction_prompt: str = ""
    palette: tuple[str, ...] = ()
    palette_index: int = 0
    viewport: TuiViewport = field(default_factory=TuiViewport)
    notice: str = ""
    search_query: str = ""


@dataclass(frozen=True)
class TuiFrame:
    lines: tuple[str, ...]
    cursor_y: int
    cursor_x: int
    accent_rows: tuple[int, ...] = ()


_LCA_LOGO = (
    " _        ____      _    ",
    "| |      / ___|    / \\   ",
    "| |     | |       / _ \\  ",
    "| |___  | |___   / ___ \\ ",
    "|_____|  \\____| /_/   \\_\\",
)
_LCA_WORDMARK = "LOCAL CODING AGENT"
_LCA_ACCENTS = frozenset(line.strip() for line in (*_LCA_LOGO, "LCA", _LCA_WORDMARK))


def render_frame(state: TuiState, view: TuiView, width: int, height: int) -> TuiFrame:
    width = max(width, 20)
    height = max(height, 6)
    header = _header(state, width)
    viewport = synchronize_viewport(state, view, width, height)
    footer = _footer(state, view, viewport, width)
    palette_rows = min(len(view.palette), 5) if view.palette else 0
    prompt_rows = 2 if view.interaction_prompt else 1
    body_height = max(height - 2 - prompt_rows - palette_rows, 1)
    body = _body(state, width, body_height, viewport.top, view.search_query)
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
    accent_rows = tuple(index for index, line in enumerate(lines) if line.strip() in _LCA_ACCENTS)
    return TuiFrame(tuple(lines), cursor_y, min(cursor_x, width - 1), accent_rows)


def render_inline_frame(state: TuiState, view: TuiView, width: int, height: int) -> TuiFrame:
    """Render only the mutable tail kept below native terminal scrollback."""

    width = max(width, 20)
    height = max(height, 6)
    interaction_rows = 1 if view.interaction_prompt else 0
    palette_rows = min(len(view.palette), 5, max(height - interaction_rows - 3, 0)) if view.palette else 0
    fixed_rows = 1 + palette_rows + interaction_rows + 2
    content_height = max(height - fixed_rows, 0)
    provisional = tuple(entry for entry in state.transcript if entry.provisional)
    if not state.transcript and not view.search_query:
        content = list(_welcome_lines(width, content_height))
    else:
        content = list(transcript_lines(provisional, width))
        content.extend(_inline_activity_lines(state, width))
        if not content_height:
            content = []
        elif len(content) > content_height:
            content = content[-content_height:]

    lines = [_header(state, width), *content, *_palette(view, width, palette_rows)]
    if view.interaction_prompt:
        lines.append(pad_cells(clip_cells(view.interaction_prompt, width, marker="..."), width))
    prompt, cursor_x = _prompt(view, width)
    cursor_y = len(lines)
    lines.extend((prompt, _footer(state, view, TuiViewport(), width)))
    accent_rows = tuple(index for index, line in enumerate(lines) if line.strip() in _LCA_ACCENTS)
    return TuiFrame(tuple(lines[:height]), min(cursor_y, height - 2), min(cursor_x, width - 1), accent_rows)


def synchronize_viewport(state: TuiState, view: TuiView, width: int, height: int) -> TuiViewport:
    safe_width = max(width, 20)
    safe_height = max(height, 6)
    palette_rows = min(len(view.palette), 5) if view.palette else 0
    prompt_rows = 2 if view.interaction_prompt else 1
    visible_rows = max(safe_height - 2 - prompt_rows - palette_rows, 1)
    transcript_width = _transcript_width(safe_width)
    entries = _filtered_entries(state, view.search_query)
    total_rows = len(transcript_lines(entries, transcript_width)) if state.transcript or view.search_query else 0
    max_top = max(total_rows - visible_rows, 0)
    clamped_to_bottom = not view.viewport.follow_bottom and view.viewport.top >= max_top
    follow_bottom = view.viewport.follow_bottom or clamped_to_bottom
    top = max_top if follow_bottom else min(max(view.viewport.top, 0), max_top)
    return TuiViewport(
        top=top,
        total_rows=total_rows,
        visible_rows=visible_rows,
        follow_bottom=follow_bottom,
    )


def scroll_viewport(viewport: TuiViewport, rows: int) -> TuiViewport:
    top = min(max(viewport.top + rows, 0), viewport.max_top)
    return replace(viewport, top=top, follow_bottom=top == viewport.max_top)


def page_viewport(viewport: TuiViewport, pages: int) -> TuiViewport:
    return scroll_viewport(viewport, pages * max(viewport.visible_rows - 1, 1))


def wheel_viewport(viewport: TuiViewport, direction: int) -> TuiViewport:
    """Move a useful fraction of the viewport for one wheel notch."""

    step = max(viewport.visible_rows // 3, 3)
    return scroll_viewport(viewport, step if direction > 0 else -step)


def follow_viewport(viewport: TuiViewport) -> TuiViewport:
    return replace(viewport, top=viewport.max_top, follow_bottom=True)


def _header(state: TuiState, width: int) -> str:
    session = state.session_id[:8] if state.session_id else "new"
    activity = "RUNNING" if state.busy else "READY"
    left = f" LCA  {activity}  session {session}"
    right = f"{state.status} "
    gap = max(width - cell_width(left) - cell_width(right), 1)
    return clip_cells(left + " " * gap + right, width)


def _body(state: TuiState, width: int, height: int, scroll_top: int, search_query: str) -> tuple[str, ...]:
    side_width = 30 if width >= 100 else 0
    transcript_width = width - side_width - (1 if side_width else 0)
    entries = _filtered_entries(state, search_query)
    if not state.transcript and not search_query:
        visible = list(_welcome_lines(transcript_width, height))
    else:
        transcript = transcript_lines(entries, transcript_width)
        start = min(max(scroll_top, 0), max(len(transcript) - height, 0))
        visible = list(transcript[start:start + height])
        visible.extend([""] * max(height - len(visible), 0))
    if not side_width:
        return tuple(pad_cells(line, width) for line in visible)
    side = _side_lines(state, side_width, height)
    return tuple(
        pad_cells(left, transcript_width) + " " + pad_cells(right, side_width)
        for left, right in zip(visible, side, strict=True)
    )


def _transcript_width(width: int) -> int:
    side_width = 30 if width >= 100 else 0
    return width - side_width - (1 if side_width else 0)


def _filtered_entries(state: TuiState, search_query: str) -> tuple[TranscriptEntry, ...]:
    if not search_query:
        return state.transcript
    query = search_query.casefold()
    return tuple(entry for entry in state.transcript if query in entry.text.casefold())


def _welcome_lines(width: int, height: int) -> tuple[str, ...]:
    content = (*_LCA_LOGO, "", _LCA_WORDMARK) if width >= 32 and height >= 7 else ("LCA", _LCA_WORDMARK)
    top = max((height - len(content)) // 2, 0)
    lines = [""] * top
    for line in content[:height]:
        clipped = clip_cells(line, width)
        left = max((width - cell_width(clipped)) // 2, 0)
        lines.append(" " * left + clipped)
    lines.extend([""] * max(height - len(lines), 0))
    return tuple(pad_cells(line, width) for line in lines[:height])


def transcript_lines(entries: tuple[TranscriptEntry, ...], width: int) -> tuple[str, ...]:
    lines: list[str] = []
    for entry in entries:
        marker = _transcript_marker(entry)
        rendered = render_markdown_source(entry.text, max(width - 2, 1)) or ("",)
        for index, line in enumerate(rendered):
            if not line:
                lines.append(marker.rstrip() if index == 0 else "")
                continue
            lines.append((marker if index == 0 else "  ") + line)
        lines.append("")
    return tuple(lines)


def _transcript_marker(entry: TranscriptEntry) -> str:
    if entry.authoritative:
        return "* "
    return {
        "assistant": "• ",
        "error": "! ",
        "system": "· ",
        "user": "› ",
    }.get(entry.role, "? ")


def _inline_activity_lines(state: TuiState, width: int) -> tuple[str, ...]:
    lines: list[str] = []
    context = " | ".join(
        part
        for part in (
            f"provider {state.provider}" if state.provider else "",
            f"workspace {state.workspace}" if state.workspace else "",
        )
        if part
    )
    if context:
        lines.append(clip_cells(context, width, marker="..."))
    for tool in state.tools[-4:]:
        summary = f"{tool.status:<9} {tool.name} {tool.detail}".rstrip()
        lines.append(clip_cells(summary, width, marker="..."))
    if state.todos:
        lines.extend(clip_cells(f"todo {todo}", width, marker="...") for todo in state.todos[-2:])
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


def _footer(state: TuiState, view: TuiView, viewport: TuiViewport, width: int) -> str:
    notice = view.notice or (f"dropped {state.dropped_messages} UI updates" if state.dropped_messages else "")
    if viewport.total_rows and not viewport.follow_bottom:
        start = viewport.top + 1
        end = min(viewport.top + viewport.visible_rows, viewport.total_rows)
        history = f"history {start}-{end}/{viewport.total_rows} | scroll down for latest"
    else:
        history = ""
    keys = "Enter send  Ctrl-P commands  Ctrl-F search  Ctrl-C cancel  Ctrl-Q quit"
    if history:
        keys = f"{history} | {keys}"
    if view.search_query:
        notice = f"search: {view.search_query}" + (f" | {notice}" if notice else "")
    if notice:
        keys = f"{notice} | {keys}"
    return clip_cells(keys, width, marker="...")

from __future__ import annotations

from dataclasses import dataclass

from ..text import sanitize_terminal_text
from .text import cell_width
from .text import clip_cells
from .text import display_clusters


MAX_COMPOSER_ROWS = 6
MAX_COMPOSER_INPUT_BYTES = 64 * 1024
COMPOSER_PROMPT = "> "
COMPOSER_CONTINUATION = "| "


@dataclass(frozen=True)
class ComposerCursorStop:
    source_offset: int
    column: int


@dataclass(frozen=True)
class ComposerVisualRow:
    text: str
    source_start: int
    source_end: int
    cursor_stops: tuple[ComposerCursorStop, ...]
    soft_wrap_after: bool = False


@dataclass(frozen=True)
class ComposerLayout:
    rows: tuple[str, ...]
    visual_rows: tuple[ComposerVisualRow, ...]
    total_rows: int
    window_start: int
    cursor_row: int
    cursor_col: int
    absolute_cursor_row: int
    content_width: int


@dataclass(frozen=True)
class ComposerVerticalMove:
    cursor: int
    preferred_column: int | None
    moved: bool


def layout_composer(
    text: str,
    cursor: int,
    width: int,
    row_budget: int = MAX_COMPOSER_ROWS,
) -> ComposerLayout:
    """Build one bounded, sanitized visual projection without changing source text."""

    if len(text.encode("utf-8")) > MAX_COMPOSER_INPUT_BYTES:
        raise ValueError("composer input exceeds its 64 KiB layout budget")
    safe_width = max(width, 4)
    prefix_width = cell_width(COMPOSER_PROMPT)
    # Keep one terminal cell available for a cursor after the last visible cluster.
    content_width = max(safe_width - prefix_width - 1, 1)
    visual_rows = _build_visual_rows(text, content_width)
    safe_cursor = min(max(cursor, 0), len(text))
    absolute_cursor_row = _cursor_row(visual_rows, safe_cursor)
    cursor_column = _cursor_column(visual_rows[absolute_cursor_row], safe_cursor)
    visible_count = min(max(row_budget, 1), MAX_COMPOSER_ROWS, len(visual_rows))
    window_start = min(
        max(absolute_cursor_row - visible_count + 1, 0),
        max(len(visual_rows) - visible_count, 0),
    )
    visible = visual_rows[window_start:window_start + visible_count]
    rendered = tuple(
        _row_prefix(window_start + index) + clip_cells(row.text, content_width)
        for index, row in enumerate(visible)
    )
    return ComposerLayout(
        rows=rendered,
        visual_rows=visual_rows,
        total_rows=len(visual_rows),
        window_start=window_start,
        cursor_row=absolute_cursor_row - window_start,
        cursor_col=min(prefix_width + cursor_column, safe_width - 1),
        absolute_cursor_row=absolute_cursor_row,
        content_width=content_width,
    )


def move_composer_cursor_vertical(
    text: str,
    cursor: int,
    width: int,
    direction: int,
    preferred_column: int | None,
) -> ComposerVerticalMove:
    if direction not in {-1, 1}:
        raise ValueError("composer vertical direction must be -1 or 1")
    layout = layout_composer(text, cursor, width)
    target_index = layout.absolute_cursor_row + direction
    if target_index < 0 or target_index >= layout.total_rows:
        return ComposerVerticalMove(cursor, preferred_column, False)
    current = layout.visual_rows[layout.absolute_cursor_row]
    target = layout.visual_rows[target_index]
    desired = preferred_column
    if desired is None:
        desired = _cursor_column(current, min(max(cursor, 0), len(text)))
    stops = target.cursor_stops
    if target.soft_wrap_after and len(stops) > 1:
        stops = stops[:-1]
    selected = stops[0]
    for stop in stops:
        if stop.column > desired:
            break
        selected = stop
    return ComposerVerticalMove(selected.source_offset, desired, True)


def _build_visual_rows(text: str, content_width: int) -> tuple[ComposerVisualRow, ...]:
    rows: list[ComposerVisualRow] = []
    row_text: list[str] = []
    row_start = 0
    row_column = 0
    stops: list[ComposerCursorStop] = [ComposerCursorStop(0, 0)]
    source_offset = 0

    def finish(source_end: int, *, soft_wrap_after: bool) -> None:
        rows.append(
            ComposerVisualRow(
                "".join(row_text),
                row_start,
                source_end,
                tuple(stops),
                soft_wrap_after,
            )
        )

    for cluster in display_clusters(text):
        cluster_start = source_offset
        source_offset += len(cluster)
        if cluster == "\n":
            finish(cluster_start, soft_wrap_after=False)
            row_text = []
            row_start = source_offset
            row_column = 0
            stops = [ComposerCursorStop(row_start, 0)]
            continue
        display = sanitize_terminal_text(cluster).replace("\n", "")
        cluster_width = cell_width(display)
        if row_text and row_column + cluster_width > content_width:
            finish(cluster_start, soft_wrap_after=True)
            row_text = []
            row_start = cluster_start
            row_column = 0
            stops = [ComposerCursorStop(row_start, 0)]
        row_text.append(display)
        row_column += cluster_width
        stops.append(ComposerCursorStop(source_offset, row_column))

    finish(len(text), soft_wrap_after=False)
    return tuple(rows)


def _cursor_row(rows: tuple[ComposerVisualRow, ...], cursor: int) -> int:
    selected = 0
    for index, row in enumerate(rows):
        if row.source_start <= cursor <= row.source_end:
            selected = index
    return selected


def _cursor_column(row: ComposerVisualRow, cursor: int) -> int:
    selected = row.cursor_stops[0]
    for stop in row.cursor_stops:
        if stop.source_offset > cursor:
            break
        selected = stop
    return selected.column


def _row_prefix(absolute_row: int) -> str:
    return COMPOSER_PROMPT if absolute_row == 0 else COMPOSER_CONTINUATION

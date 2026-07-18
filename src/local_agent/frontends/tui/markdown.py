from __future__ import annotations

from .text import sanitize_terminal_text
from .text import wrap_line_cells


def render_markdown_source(source: str, width: int) -> tuple[str, ...]:
    """Project raw Markdown source into width-bounded terminal rows."""

    if width <= 0:
        return ()
    logical_lines = sanitize_terminal_text(source).split("\n")
    rows: list[str] = []
    fence: tuple[str, int] | None = None
    for logical_line in logical_lines:
        continuation = _continuation_indent(logical_line, in_fence=fence is not None)
        rows.extend(
            wrap_line_cells(
                logical_line,
                width,
                continuation_indent=continuation,
            )
        )
        if fence is None:
            fence = _opening_fence(logical_line)
        elif _is_closing_fence(logical_line, fence):
            fence = None
    return tuple(rows)


def _continuation_indent(line: str, *, in_fence: bool) -> str:
    leading = line[: len(line) - len(line.lstrip(" "))]
    if in_fence:
        return leading
    body = line[len(leading):]
    if not body:
        return leading

    heading = _heading_prefix_width(body)
    if heading:
        return leading + " " * heading

    quote = _blockquote_prefix_width(body)
    if quote:
        return leading + " " * quote

    item = _list_prefix_width(body)
    if item:
        return leading + " " * item

    return leading


def _heading_prefix_width(body: str) -> int:
    count = 0
    while count < len(body) and count < 6 and body[count] == "#":
        count += 1
    if count and count < len(body) and body[count] == " ":
        return count + 1
    return 0


def _blockquote_prefix_width(body: str) -> int:
    index = 0
    while index < len(body) and body[index] == ">":
        index += 1
        if index < len(body) and body[index] == " ":
            index += 1
    return index if index else 0


def _list_prefix_width(body: str) -> int:
    if len(body) >= 2 and body[0] in "-*+" and body[1] == " ":
        width = 2
        if len(body) >= 6 and body[2] == "[" and body[4] == "]" and body[5] == " ":
            width = 6
        return width

    index = 0
    while index < len(body) and body[index].isdigit():
        index += 1
    if (
        index
        and index + 1 < len(body)
        and body[index] in ".)"
        and body[index + 1] == " "
    ):
        return index + 2
    return 0


def _opening_fence(line: str) -> tuple[str, int] | None:
    body = line.lstrip(" ")
    if not body or body[0] not in "`~":
        return None
    marker = body[0]
    count = len(body) - len(body.lstrip(marker))
    if count >= 3:
        return marker, count
    return None


def _is_closing_fence(line: str, fence: tuple[str, int]) -> bool:
    marker, opening_count = fence
    body = line.lstrip(" ")
    count = len(body) - len(body.lstrip(marker))
    return count >= opening_count and not body[count:].strip()

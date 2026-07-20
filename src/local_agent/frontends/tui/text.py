from __future__ import annotations

from collections.abc import Iterator
import unicodedata

from ..text import sanitize_terminal_text

def cell_width(text: str) -> int:
    return sum(_cluster_width(cluster) for cluster in _display_clusters(text))


def clip_cells(text: str, width: int, *, marker: str = "") -> str:
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    marker_width = min(cell_width(marker), width)
    available = width - marker_width
    result: list[str] = []
    used = 0
    for cluster in _display_clusters(text):
        cluster_width = _cluster_width(cluster)
        if used + cluster_width > available:
            break
        result.append(cluster)
        used += cluster_width
    suffix = clip_cells(marker, width) if marker else ""
    return "".join(result) + suffix


def pad_cells(text: str, width: int) -> str:
    clipped = clip_cells(text, width)
    return clipped + " " * max(width - cell_width(clipped), 0)


def tail_cells(text: str, width: int) -> str:
    if width <= 0:
        return ""
    result: list[str] = []
    used = 0
    for cluster in reversed(tuple(_display_clusters(text))):
        cluster_width = _cluster_width(cluster)
        if used + cluster_width > width:
            break
        result.append(cluster)
        used += cluster_width
    return "".join(reversed(result))


def wrap_cells(text: str, width: int) -> tuple[str, ...]:
    if width <= 0:
        return ()
    normalized = sanitize_terminal_text(text)
    logical_lines = normalized.splitlines() or [""]
    wrapped: list[str] = []
    for logical in logical_lines:
        if not logical:
            wrapped.append("")
            continue
        wrapped.extend(wrap_line_cells(logical, width))
    return tuple(wrapped)


def display_clusters(text: str) -> Iterator[str]:
    """Expose the shared terminal grapheme approximation to layout owners."""

    yield from _display_clusters(text)


def wrap_line_cells(text: str, width: int, *, continuation_indent: str = "") -> tuple[str, ...]:
    """Wrap one logical line without splitting terminal grapheme clusters."""

    if width <= 0:
        return ()
    if not text:
        return ("",)
    indent = clip_cells(continuation_indent, max(width - 1, 0))
    indent_width = cell_width(indent)
    rows: list[str] = []
    current: list[str] = []
    used = 0
    for cluster in _display_clusters(text):
        cluster_width = _cluster_width(cluster)
        if current and used + cluster_width > width:
            rows.append("".join(current))
            current = [indent] if indent else []
            used = indent_width
        if used + cluster_width > width:
            current = []
            used = 0
            if cluster_width > width:
                continue
        current.append(cluster)
        used += cluster_width
    rows.append("".join(current))
    return tuple(rows)


def _display_clusters(text: str) -> Iterator[str]:
    index = 0
    while index < len(text):
        start = index
        character = text[index]
        index += 1
        if _is_regional_indicator(character) and index < len(text):
            if _is_regional_indicator(text[index]):
                index += 1
        while index < len(text) and _is_cluster_extension(text[index]):
            index += 1
        while index < len(text) and text[index] == "\u200d":
            index += 1
            if index < len(text):
                index += 1
                while index < len(text) and _is_cluster_extension(text[index]):
                    index += 1
        yield text[start:index]


def _cluster_width(cluster: str) -> int:
    if not cluster:
        return 0
    visible = [character for character in cluster if _base_character_width(character)]
    if not visible:
        return 0
    if (
        "\u200d" in cluster
        or "\ufe0f" in cluster
        or "\u20e3" in cluster
        or any(_is_emoji_modifier(character) for character in cluster)
        or any(_is_regional_indicator(character) for character in cluster)
    ):
        return 2
    return sum(_base_character_width(character) for character in visible)


def _base_character_width(character: str) -> int:
    if character == "\n" or _is_cluster_extension(character) or character == "\u200d":
        return 0
    if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def _is_cluster_extension(character: str) -> bool:
    category = unicodedata.category(character)
    return (
        unicodedata.combining(character) != 0
        or category in {"Mc", "Me"}
        or "\ufe00" <= character <= "\ufe0f"
        or "\U000e0100" <= character <= "\U000e01ef"
        or _is_emoji_modifier(character)
        or character == "\u20e3"
    )


def _is_emoji_modifier(character: str) -> bool:
    return "\U0001f3fb" <= character <= "\U0001f3ff"


def _is_regional_indicator(character: str) -> bool:
    return "\U0001f1e6" <= character <= "\U0001f1ff"

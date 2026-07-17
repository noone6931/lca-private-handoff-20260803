from __future__ import annotations

import unicodedata


def cell_width(text: str) -> int:
    return sum(_character_width(character) for character in text)


def clip_cells(text: str, width: int, *, marker: str = "") -> str:
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    marker_width = min(cell_width(marker), width)
    available = width - marker_width
    result: list[str] = []
    used = 0
    for character in text:
        character_width = _character_width(character)
        if used + character_width > available:
            break
        result.append(character)
        used += character_width
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
    for character in reversed(text):
        character_width = _character_width(character)
        if used + character_width > width:
            break
        result.append(character)
        used += character_width
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
        current: list[str] = []
        used = 0
        for character in logical:
            character_width = _character_width(character)
            if current and used + character_width > width:
                wrapped.append("".join(current))
                current = []
                used = 0
            if character_width > width:
                continue
            current.append(character)
            used += character_width
        wrapped.append("".join(current))
    return tuple(wrapped)


def sanitize_terminal_text(text: str) -> str:
    expanded = text.replace("\t", "    ")
    return "".join(
        character
        for character in expanded
        if character in "\n\r" or unicodedata.category(character) not in {"Cc", "Cs"}
    )


def _character_width(character: str) -> int:
    if character in "\n\r":
        return 0
    if unicodedata.combining(character):
        return 0
    if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1

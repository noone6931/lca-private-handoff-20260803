"""Semantic-transparent text safety shared by terminal frontends."""

from __future__ import annotations

import unicodedata


_BIDI_CONTROL_CODEPOINTS = frozenset(
    {
        0x061C,
        0x200E,
        0x200F,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    }
)


def sanitize_terminal_text(text: str) -> str:
    """Remove terminal and bidi controls while preserving visible source text."""

    expanded = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    return "".join(
        character
        for character in expanded
        if (
            character == "\n"
            or (
                ord(character) not in _BIDI_CONTROL_CODEPOINTS
                and unicodedata.category(character) not in {"Cc", "Cs"}
            )
        )
    )


__all__ = ["sanitize_terminal_text"]

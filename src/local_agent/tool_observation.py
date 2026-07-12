"""Neutral, normalized tool observations shared by runtime policy owners."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolResultSummary:
    """A bounded runtime observation, not a raw provider or tool payload."""

    name: str
    content: str = ""
    is_error: bool = False
    useless: bool = False
    path: str | None = None
    changed: bool | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

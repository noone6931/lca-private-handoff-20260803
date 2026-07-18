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


def tool_result_was_not_executed(result: ToolResultSummary) -> bool:
    """Return true for protocol/directive rejections, not real tool attempts.

    New runtime paths should mark this with typed metadata.  The content
    fallback is kept only for older synthetic results created before every
    suppression path carried metadata.
    """

    if result.metadata.get("provider_schema_violation"):
        return True
    if result.metadata.get("active_tool_rejection"):
        return True
    if result.metadata.get("suppressed"):
        return True
    return "tool call was not executed" in result.content.lower()


def tool_result_is_executed_attempt(result: ToolResultSummary) -> bool:
    """Return true when a tool result represents an actual tool execution."""

    if result.metadata.get("evidence_origin") == "session_cached":
        return False
    return not tool_result_was_not_executed(result)

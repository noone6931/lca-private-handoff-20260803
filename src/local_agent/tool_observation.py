"""Compatibility imports for typed tool observations."""

from .tools.observation import (
    ToolResultSummary,
    tool_result_is_executed_attempt,
    tool_result_was_not_executed,
)

__all__ = [
    "ToolResultSummary",
    "tool_result_is_executed_attempt",
    "tool_result_was_not_executed",
]

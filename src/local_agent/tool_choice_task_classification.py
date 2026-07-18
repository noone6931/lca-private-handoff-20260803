"""Compatibility imports for tool-choice task classification."""

from .workflows.tool_choice.classification import is_implementation_task, is_read_only_task

__all__ = ["is_implementation_task", "is_read_only_task"]

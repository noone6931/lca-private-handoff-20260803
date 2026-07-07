from __future__ import annotations

from .base import ToolRegistry
from .files import file_tools
from .git import git_tools
from .interaction import interaction_tools
from .memory import memory_tools
from .search import search_tools
from .shell import shell_tools
from .todo import todo_tools


def create_default_registry() -> ToolRegistry:
    tools = [
        *file_tools(),
        *search_tools(),
        *shell_tools(),
        *git_tools(),
        *memory_tools(),
        *todo_tools(),
        *interaction_tools(),
    ]
    return ToolRegistry(tools)

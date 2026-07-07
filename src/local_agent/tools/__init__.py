from __future__ import annotations

from .base import ToolRegistry
from .files import file_tools
from .git import git_tools
from .memory import memory_tools
from .search import search_tools
from .shell import shell_tools


def create_default_registry() -> ToolRegistry:
    tools = [
        *file_tools(),
        *search_tools(),
        *shell_tools(),
        *git_tools(),
        *memory_tools(),
    ]
    return ToolRegistry(tools)

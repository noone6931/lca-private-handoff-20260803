from __future__ import annotations

from .base import Tool, ToolRegistry
from .files import file_tools
from .git import git_tools
from .interaction import interaction_tools
from .lsp import lsp_tools
from .lsp_code_action import lsp_code_action_tools
from .lsp_rename import lsp_rename_tools
from .memory import memory_tools
from .search import search_tools
from .shell import shell_tools
from .todo import todo_tools


def create_default_registry(extra_tools: tuple[Tool, ...] = ()) -> ToolRegistry:
    tools = [
        *file_tools(),
        *search_tools(),
        *shell_tools(),
        *git_tools(),
        *memory_tools(),
        *todo_tools(),
        *interaction_tools(),
        *lsp_tools(),
        *lsp_code_action_tools(),
        *lsp_rename_tools(),
        *extra_tools,
    ]
    return ToolRegistry(tools)


def create_runtime_registry(
    client: object,
    enable_subagents: bool,
    subagent_budget_seconds: int,
    *,
    base_registry: ToolRegistry | None = None,
) -> ToolRegistry:
    registry = base_registry or create_default_registry()
    if not enable_subagents:
        return registry
    from ..explore_subagent import delegate_explore_tool

    child_registry = create_default_registry()
    return registry.extended(
        (delegate_explore_tool(client, child_registry, budget_seconds=subagent_budget_seconds),)
    )

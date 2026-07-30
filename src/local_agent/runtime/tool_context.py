from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import AgentConfig
from ..protocol.interactions import InteractionHandler
from ..tools.base import ToolContext
from ..tools.container_runtime import ContainerExecutionRuntime
from ..tools.isolation_routing import ConfiguredIsolationProcessRunner
from ..tools.process_environment import build_container_control_environment
from ..workspace.context import WorkspaceRootIdentity


def build_runtime_tool_context(
    config: AgentConfig,
    *,
    state_dir: Path,
    allowed_dirs: tuple[Path, ...],
    session_id: str,
    workspace_identity: WorkspaceRootIdentity,
    session_tool_approval: dict[str, str],
    event_callback: Callable[[str, dict[str, Any]], None],
    interaction_handler: InteractionHandler | None,
    vision_inspector: Callable[[Path, str, bytes, str], str],
) -> ToolContext:
    return ToolContext(
        workspace=config.workspace,
        approval_mode=config.approval_mode,
        state_dir=state_dir,
        allowed_dirs=allowed_dirs,
        session_id=session_id,
        workspace_identity=workspace_identity,
        auto_approve_tools=config.auto_approve_tools,
        tool_approval=config.tool_approval,
        session_tool_approval=session_tool_approval,
        event_callback=event_callback,
        interaction_handler=interaction_handler,
        vision_inspector=vision_inspector,
        process_runner=build_isolation_process_runner(config),
    )


def build_isolation_process_runner(
    config: AgentConfig,
) -> ConfiguredIsolationProcessRunner | None:
    isolation = config.isolation
    if isolation.mode == "off":
        return None
    authority = isolation.container
    runtime = None
    if authority is not None:
        environment = build_container_control_environment(
            client_config_directory=authority.client_config_directory
        )
        runtime = ContainerExecutionRuntime(
            authority,
            control_environment=environment.values,
        )
    return ConfiguredIsolationProcessRunner(
        isolation,
        container_runtime=runtime,
    )


__all__ = [
    "build_isolation_process_runner",
    "build_runtime_tool_context",
]

"""Workspace roots, startup context, and migration boundaries."""

from .context import MAX_SESSION_ROOTS, WorkspaceContext, WorkspaceContextError

__all__ = ["MAX_SESSION_ROOTS", "WorkspaceContext", "WorkspaceContextError"]

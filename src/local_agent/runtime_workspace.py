"""Compatibility facade for Runtime workspace lifecycle."""

from .runtime.workspace import WorkspaceLifecycle, WorkspaceRuntimePort

__all__ = ["WorkspaceLifecycle", "WorkspaceRuntimePort"]

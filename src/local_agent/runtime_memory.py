"""Compatibility facade for the Runtime memory lifecycle."""

from .runtime.memory import MemoryConsolidationLifecycle, MemoryRuntimePort

__all__ = ["MemoryConsolidationLifecycle", "MemoryRuntimePort"]

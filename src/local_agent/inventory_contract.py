"""Compatibility imports for workspace inventory contracts."""

from .workflows.inventory import (
    MAX_INVENTORY_GLOB_PATHS,
    inventory_glob_arguments_for_roots,
    inventory_glob_call_hint,
)

__all__ = [
    "MAX_INVENTORY_GLOB_PATHS",
    "inventory_glob_arguments_for_roots",
    "inventory_glob_call_hint",
]

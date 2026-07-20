from __future__ import annotations

import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


PROVIDER_CREDENTIAL_ENVIRONMENT_KEYS = frozenset(
    {
        "AI_API_KEY",
        "BAILIAN_API_KEY",
        "DASHSCOPE_API_KEY",
    }
)
_PROVIDER_CREDENTIAL_ENVIRONMENT_KEY_FOLDS = frozenset(
    key.casefold() for key in PROVIDER_CREDENTIAL_ENVIRONMENT_KEYS
)
NONINTERACTIVE_ENVIRONMENT_DEFAULTS = MappingProxyType(
    {
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        "MANPAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONUNBUFFERED": "1",
        "NO_COLOR": "1",
    }
)


@dataclass(frozen=True)
class ChildProcessEnvironment:
    values: Mapping[str, str]
    explicit_keys: tuple[str, ...]


def is_provider_credential_environment_key(key: str) -> bool:
    return key.casefold() in _PROVIDER_CREDENTIAL_ENVIRONMENT_KEY_FOLDS


def build_child_process_environment(
    *,
    parent: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> ChildProcessEnvironment:
    """Project a child-only environment without LCA provider credentials."""

    values = dict(os.environ if parent is None else parent)
    for key in tuple(values):
        if is_provider_credential_environment_key(key):
            values.pop(key)
    for key, value in NONINTERACTIVE_ENVIRONMENT_DEFAULTS.items():
        values.setdefault(key, value)
    explicit = dict(overrides or {})
    for key, value in explicit.items():
        if not is_provider_credential_environment_key(key):
            values[key] = value
    for key in tuple(values):
        if is_provider_credential_environment_key(key):
            values.pop(key)
    return ChildProcessEnvironment(
        values=MappingProxyType(values),
        explicit_keys=tuple(sorted(explicit)),
    )

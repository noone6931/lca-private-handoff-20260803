from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from ..execution.environment import is_provider_credential_environment_key
from .process_environment_defaults import NONINTERACTIVE_ENVIRONMENT_DEFAULTS

@dataclass(frozen=True)
class ChildProcessEnvironment:
    values: Mapping[str, str]
    explicit_keys: tuple[str, ...]

def build_child_process_environment(
    *,
    parent: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> ChildProcessEnvironment:
    """Project a child-only environment without LCA provider credentials."""

    values = {
        key: value
        for key, value in (os.environ if parent is None else parent).items()
        if not is_provider_credential_environment_key(key)
    }
    for key, value in NONINTERACTIVE_ENVIRONMENT_DEFAULTS.items():
        values.setdefault(key, value)
    explicit = dict(overrides or {})
    values.update(
        (key, value)
        for key, value in explicit.items()
        if not is_provider_credential_environment_key(key)
    )
    return ChildProcessEnvironment(
        values=MappingProxyType(values),
        explicit_keys=tuple(sorted(explicit)),
    )
def build_container_control_environment(
    *, client_config_directory: Path
) -> ChildProcessEnvironment:
    """Build a fixed Docker control environment without inheriting parent state."""

    if not client_config_directory.is_absolute():
        raise ValueError("container client config directory must be absolute")
    directory = str(client_config_directory)
    values = {
        **NONINTERACTIVE_ENVIRONMENT_DEFAULTS,
        "DOCKER_CONFIG": directory,
        "HOME": directory,
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.defpath,
        "TMPDIR": "/tmp",
    }
    return ChildProcessEnvironment(MappingProxyType(values), ())

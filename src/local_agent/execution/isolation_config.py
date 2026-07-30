from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .container_types import parse_gate_image_authority
from .contracts import ContainerBackendAuthority
from .contracts import IsolationConfiguration


_ISOLATION_KEYS = frozenset(
    {
        "mode",
        "profile",
        "backend",
        "network_policy",
        "container",
    }
)
_CONTAINER_REQUIRED_KEYS = frozenset(
    {
        "executable",
        "executable_sha256",
        "socket_path",
        "client_config_directory",
        "gate_image",
        "workspace_transport",
    }
)
_CONTAINER_KEYS = _CONTAINER_REQUIRED_KEYS | frozenset(
    {
        "staging_root",
    }
)


@dataclass(frozen=True)
class IsolationConfigOverrides:
    mode: str | None = None
    profile: str | None = None
    backend: str | None = None
    network_policy: str | None = None
    container_executable: str | None = None
    container_executable_sha256: str | None = None
    container_socket_path: str | None = None
    container_client_config_directory: str | None = None
    container_gate_image: str | None = None
    container_workspace_transport: str | None = None
    container_staging_root: str | None = None


def resolve_isolation_configuration(
    raw_config: object,
    overrides: IsolationConfigOverrides | None = None,
) -> IsolationConfiguration:
    config = _mapping("isolation", raw_config)
    _reject_unknown_keys("isolation", config, _ISOLATION_KEYS)
    raw_container = _mapping("isolation.container", config.get("container"))
    _reject_unknown_keys("isolation.container", raw_container, _CONTAINER_KEYS)
    selected = overrides or IsolationConfigOverrides()
    container_values = {
        "executable": _pick(selected.container_executable, raw_container, "executable"),
        "executable_sha256": _pick(
            selected.container_executable_sha256,
            raw_container,
            "executable_sha256",
        ),
        "socket_path": _pick(
            selected.container_socket_path,
            raw_container,
            "socket_path",
        ),
        "client_config_directory": _pick(
            selected.container_client_config_directory,
            raw_container,
            "client_config_directory",
        ),
        "gate_image": _pick(
            selected.container_gate_image,
            raw_container,
            "gate_image",
        ),
        "workspace_transport": _pick(
            selected.container_workspace_transport,
            raw_container,
            "workspace_transport",
        ),
        "staging_root": _pick(
            selected.container_staging_root,
            raw_container,
            "staging_root",
        ),
    }
    authority = _container_authority(container_values)
    return IsolationConfiguration(
        mode=_text(_pick(selected.mode, config, "mode"), "isolation.mode", "off"),
        profile=_text(
            _pick(selected.profile, config, "profile"),
            "isolation.profile",
            "workspace-write",
        ),
        backend=_text(
            _pick(selected.backend, config, "backend"),
            "isolation.backend",
            "auto",
        ),
        network_policy=_text(
            _pick(selected.network_policy, config, "network_policy"),
            "isolation.network_policy",
            "deny",
        ),
        container=authority,
    )


def _container_authority(
    values: Mapping[str, object | None],
) -> ContainerBackendAuthority | None:
    present = {key for key, value in values.items() if value is not None}
    if not present:
        return None
    missing = sorted(_CONTAINER_REQUIRED_KEYS - present)
    if missing:
        raise ValueError(
            "isolation.container requires all authority fields; missing: "
            + ", ".join(missing)
        )
    executable = _path(values["executable"], "isolation.container.executable")
    executable_sha256 = _text(
        values["executable_sha256"],
        "isolation.container.executable_sha256",
    )
    socket_path = _path(values["socket_path"], "isolation.container.socket_path")
    config_directory = _path(
        values["client_config_directory"],
        "isolation.container.client_config_directory",
    )
    gate_image = _text(values["gate_image"], "isolation.container.gate_image")
    parse_gate_image_authority(gate_image)
    workspace_transport = _text(
        values["workspace_transport"],
        "isolation.container.workspace_transport",
    )
    staging_value = values["staging_root"]
    staging_root = (
        _path(staging_value, "isolation.container.staging_root")
        if staging_value is not None
        else None
    )
    return ContainerBackendAuthority(
        executable=executable,
        executable_sha256=executable_sha256,
        socket_path=socket_path,
        client_config_directory=config_directory,
        gate_image=gate_image,
        workspace_transport=workspace_transport,
        staging_root=staging_root,
    )


def _mapping(name: str, value: object) -> Mapping[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object")
    return value


def _reject_unknown_keys(
    name: str,
    values: Mapping[str, object],
    allowed: frozenset[str],
) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ValueError(f"{name} contains unknown fields: {', '.join(unknown)}")


def _pick(
    override: str | None,
    values: Mapping[str, object],
    key: str,
) -> object | None:
    return override if override is not None else values.get(key)


def _text(value: object, name: str, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _path(value: object, name: str) -> Path:
    path = Path(_text(value, name)).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path


__all__ = [
    "IsolationConfigOverrides",
    "resolve_isolation_configuration",
]

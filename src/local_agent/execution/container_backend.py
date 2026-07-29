from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from .contracts import IsolationBackendCapability


ContainerEngine = Literal["docker", "podman"]
CONTAINER_ENGINES = ("docker", "podman")
_CONTAINER_PROFILES = frozenset({"read-only", "workspace-write"})
_CONTAINER_NETWORK_POLICIES = frozenset({"deny", "allow"})


@dataclass(frozen=True)
class ContainerProbePlan:
    engine: ContainerEngine
    executable: Path
    argv: tuple[str, ...]
    timeout_seconds: int = 5

    def __post_init__(self) -> None:
        if self.engine not in CONTAINER_ENGINES:
            raise ValueError(f"unsupported container engine: {self.engine}")
        if not self.executable.is_absolute():
            raise ValueError("container engine executable must be absolute")
        if not self.argv or self.argv[0] != str(self.executable):
            raise ValueError("probe argv must begin with the resolved executable")
        if not 1 <= self.timeout_seconds <= 30:
            raise ValueError("probe timeout must be between 1 and 30 seconds")


@dataclass(frozen=True)
class ContainerEngineIdentity:
    engine: ContainerEngine
    executable: Path
    server_version: str
    server_os: str
    server_arch: str

    def __post_init__(self) -> None:
        if self.engine not in CONTAINER_ENGINES:
            raise ValueError(f"unsupported container engine: {self.engine}")
        if not self.executable.is_absolute():
            raise ValueError("container engine executable must be absolute")
        if not self.server_version.strip():
            raise ValueError("container server version must not be empty")
        if self.server_os.strip().lower() != "linux":
            raise ValueError("container isolation requires a Linux server")
        if not self.server_arch.strip():
            raise ValueError("container server architecture must not be empty")

    def event_payload(self) -> dict[str, object]:
        return {
            "engine": self.engine,
            "executable": str(self.executable),
            "server_version": self.server_version,
            "server_os": self.server_os,
            "server_arch": self.server_arch,
        }


@dataclass(frozen=True)
class ContainerProbeResult:
    capability: IsolationBackendCapability
    identity: ContainerEngineIdentity | None = None

    def __post_init__(self) -> None:
        if self.capability.availability == "available" and self.identity is None:
            raise ValueError("available container backend requires engine identity")
        if self.capability.availability != "available" and self.identity is not None:
            raise ValueError("unavailable container backend cannot expose engine identity")


def discover_container_probe(
    *,
    preferred: str = "auto",
    which: Callable[[str], str | None] = shutil.which,
) -> ContainerProbePlan | None:
    engines = CONTAINER_ENGINES if preferred == "auto" else (preferred,)
    for engine in engines:
        if engine not in CONTAINER_ENGINES:
            raise ValueError(f"container engine must be one of: auto, {', '.join(CONTAINER_ENGINES)}")
        resolved = which(engine)
        if not resolved:
            continue
        executable = Path(resolved)
        if not executable.is_absolute():
            raise ValueError("resolved container engine executable must be absolute")
        return _probe_plan(engine, executable)
    return None


def missing_container_probe_result() -> ContainerProbeResult:
    return ContainerProbeResult(_unavailable("engine_missing"))


def parse_container_probe_result(
    plan: ContainerProbePlan,
    *,
    exit_code: int | None,
    stdout: str,
    timed_out: bool = False,
    spawn_failed: bool = False,
) -> ContainerProbeResult:
    if spawn_failed:
        return ContainerProbeResult(_unavailable("probe_spawn_failed"))
    if timed_out:
        return ContainerProbeResult(_unavailable("probe_timed_out"))
    if exit_code is None or exit_code != 0:
        return ContainerProbeResult(_unavailable("daemon_unavailable"))
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return ContainerProbeResult(_unavailable("probe_invalid_json"))
    try:
        version, server_os, arch = _engine_identity_fields(plan.engine, payload)
        identity = ContainerEngineIdentity(
            engine=plan.engine,
            executable=plan.executable,
            server_version=version,
            server_os=server_os,
            server_arch=arch,
        )
    except (TypeError, ValueError):
        return ContainerProbeResult(_unavailable("probe_invalid_identity"))
    return ContainerProbeResult(
        capability=IsolationBackendCapability(
            backend="container",
            availability="available",
            reason_code="engine_ready",
            supported_profiles=_CONTAINER_PROFILES,
            supported_network_policies=_CONTAINER_NETWORK_POLICIES,
            enforces_isolation=True,
        ),
        identity=identity,
    )


def _probe_plan(engine: str, executable: Path) -> ContainerProbePlan:
    if engine == "docker":
        argv = (str(executable), "version", "--format", "{{json .Server}}")
    else:
        argv = (str(executable), "info", "--format", "json")
    return ContainerProbePlan(engine=engine, executable=executable, argv=argv)


def _engine_identity_fields(engine: str, payload: object) -> tuple[str, str, str]:
    if not isinstance(payload, dict):
        raise TypeError("container probe payload must be an object")
    if engine == "docker":
        return (
            _required_string(payload, "Version"),
            _required_string(payload, "Os"),
            _required_string(payload, "Arch"),
        )
    host = payload.get("host")
    version = payload.get("version")
    if not isinstance(host, dict) or not isinstance(version, dict):
        raise TypeError("podman probe payload is missing host/version")
    return (
        _required_string(version, "Version"),
        _required_string(host, "os"),
        _required_string(host, "arch"),
    )


def _required_string(payload: dict[object, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"container probe field {key} must be a non-empty string")
    return value.strip()


def _unavailable(reason_code: str) -> IsolationBackendCapability:
    return IsolationBackendCapability(
        backend="container",
        availability="unavailable",
        reason_code=reason_code,
        supported_profiles=frozenset(),
        supported_network_policies=frozenset(),
        enforces_isolation=False,
    )

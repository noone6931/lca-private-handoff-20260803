from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .container_types import ContainerCommandResult
from .container_types import ContainerEndpointIdentity
from .container_types import ContainerEngineIdentity
from .container_types import ContainerExecutableIdentity
from .container_types import ContainerGateImageAuthority
from .container_types import ContainerWorkspaceAuthority
from .container_types import capture_empty_directory
from .container_types import capture_trusted_executable
from .container_types import capture_unix_socket
from .container_types import capture_workspace_authority
from .container_types import command_output_is_complete
from .container_types import container_command_id
from .container_types import command_workspace_authority_matches
from .container_types import executable_identity_matches
from .container_types import parse_gate_image_authority
from .container_types import validate_attempt_id


_DOCKER_VERSION_TEMPLATE = "{{json .}}"
_MAX_SERVER_OUTPUT_CHARS = 65_536
_MINIMUM_DOCKER_API = (1, 45)


@dataclass(frozen=True)
class ContainerServerProbePlan:
    attempt_id: str
    executable: Path
    executable_identity: ContainerExecutableIdentity
    endpoint: ContainerEndpointIdentity
    gate_image: ContainerGateImageAuthority
    workspace_authority: ContainerWorkspaceAuthority
    argv: tuple[str, ...]
    timeout_seconds: int = 5

    def __post_init__(self) -> None:
        validate_attempt_id(self.attempt_id)
        if not self.executable.is_absolute():
            raise ValueError("container engine executable must be absolute")
        expected = (
            str(self.executable),
            *self.endpoint.command_prefix,
            "version",
            "--format",
            _DOCKER_VERSION_TEMPLATE,
        )
        if self.argv != expected:
            raise ValueError("server probe argv does not match its Docker authority")
        if not 1 <= self.timeout_seconds <= 30:
            raise ValueError("server probe timeout must be between 1 and 30 seconds")


@dataclass(frozen=True)
class ContainerProbeResult:
    reason_code: str
    identity: ContainerEngineIdentity | None = None

    def __post_init__(self) -> None:
        if not self.reason_code.strip():
            raise ValueError("container probe reason_code must not be empty")
        if (self.reason_code == "engine_ready") != (self.identity is not None):
            raise ValueError("only a ready engine probe may expose engine identity")


def build_docker_server_probe(
    *,
    attempt_id: str,
    workspace_roots: tuple[Path, ...],
    workspace_roots_revision: int,
    executable: Path,
    executable_sha256: str,
    socket_path: Path,
    client_config_directory: Path,
    gate_image: str,
) -> ContainerServerProbePlan:
    """Build a probe from explicit user-level Docker authority.

    No PATH, Docker context, or environment-based endpoint discovery participates.
    """

    validate_attempt_id(attempt_id)
    if not workspace_roots:
        raise ValueError("container probe requires at least one workspace root")
    workspace_authority = capture_workspace_authority(
        workspace_roots,
        revision=workspace_roots_revision,
    )
    gate_image_authority = parse_gate_image_authority(gate_image)
    canonical_roots = workspace_authority.roots
    canonical_executable, executable_identity = capture_trusted_executable(
        executable,
        expected_sha256=executable_sha256,
        workspace_roots=canonical_roots,
    )
    canonical_socket, socket_identity = capture_unix_socket(
        socket_path,
        workspace_roots=canonical_roots,
    )
    config_directory, config_identity = capture_empty_directory(
        client_config_directory,
        workspace_roots=canonical_roots,
    )
    uri = f"unix://{canonical_socket}"
    endpoint = ContainerEndpointIdentity(
        uri=uri,
        command_prefix=(
            "--config",
            str(config_directory),
            "--host",
            uri,
        ),
        socket_path=canonical_socket,
        socket_identity=socket_identity,
        client_config_directory=config_directory,
        client_config_identity=config_identity,
    )
    argv = (
        str(canonical_executable),
        *endpoint.command_prefix,
        "version",
        "--format",
        _DOCKER_VERSION_TEMPLATE,
    )
    return ContainerServerProbePlan(
        attempt_id=attempt_id,
        executable=canonical_executable,
        executable_identity=executable_identity,
        endpoint=endpoint,
        gate_image=gate_image_authority,
        workspace_authority=workspace_authority,
        argv=argv,
    )


def missing_container_probe_result(reason_code: str = "authority_unconfigured") -> ContainerProbeResult:
    return ContainerProbeResult(reason_code)


def unsupported_podman_probe_result() -> ContainerProbeResult:
    return ContainerProbeResult("podman_host_path_authority_unproven")


def parse_container_probe_result(
    plan: ContainerServerProbePlan,
    result: ContainerCommandResult,
) -> ContainerProbeResult:
    failure = _result_failure(plan, result)
    if failure is not None:
        return ContainerProbeResult(f"probe_{failure}")
    if result.stderr:
        return ContainerProbeResult("probe_unexpected_stderr")
    if len(result.stdout) > _MAX_SERVER_OUTPUT_CHARS:
        return ContainerProbeResult("probe_output_too_large")
    if not executable_identity_matches(plan.executable, plan.executable_identity):
        return ContainerProbeResult("probe_executable_changed")
    if not plan.endpoint.is_current():
        return ContainerProbeResult("probe_endpoint_changed")
    try:
        payload = json.loads(result.stdout)
        client = _required_mapping(payload, "Client")
        server = _required_mapping(payload, "Server")
        client_version = _required_string(client, "Version")
        client_api = _required_string(client, "ApiVersion")
        server_version = _required_string(server, "Version")
        server_api = _required_string(server, "ApiVersion")
        server_os = _required_string(server, "Os")
        server_arch = _required_string(server, "Arch")
        if not _api_at_least(client_api, _MINIMUM_DOCKER_API) or not _api_at_least(
            server_api,
            _MINIMUM_DOCKER_API,
        ):
            return ContainerProbeResult("probe_api_unsupported")
        identity = ContainerEngineIdentity(
            engine="docker",
            executable=plan.executable,
            executable_identity=plan.executable_identity,
            endpoint=plan.endpoint,
            gate_image=plan.gate_image,
            workspace_authority=plan.workspace_authority,
            client_version=client_version,
            client_api_version=client_api,
            server_version=server_version,
            server_api_version=server_api,
            server_os=server_os,
            server_arch=server_arch,
        )
    except (TypeError, ValueError):
        return ContainerProbeResult("probe_invalid_identity")
    return ContainerProbeResult("engine_ready", identity)


def _result_failure(
    plan: ContainerServerProbePlan,
    result: ContainerCommandResult,
) -> str | None:
    if (
        result.attempt_id != plan.attempt_id
        or result.command_id != container_command_id(plan.attempt_id, "server")
        or result.step != "server"
        or result.argv != plan.argv
    ):
        return "correlation_mismatch"
    if not command_workspace_authority_matches(
        plan.workspace_authority,
        result,
    ):
        return "workspace_authority_changed"
    if result.outcome == "spawn_failed":
        return "spawn_failed"
    if result.outcome == "parent_failed":
        return "parent_failed"
    if result.outcome == "timed_out":
        return "timed_out"
    if result.outcome == "cancelled":
        return "cancelled"
    if result.exit_code != 0:
        return "failed"
    if not command_output_is_complete(result):
        return "output_incomplete"
    return None


def _required_mapping(payload: object, key: str) -> dict[object, object]:
    if not isinstance(payload, dict):
        raise TypeError("Docker version payload must be an object")
    value = payload.get(key)
    if not isinstance(value, dict):
        raise TypeError(f"Docker version payload is missing {key}")
    return value


def _required_string(payload: dict[object, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"container probe field {key} must be a non-empty string")
    return value.strip()


def _api_at_least(value: str, minimum: tuple[int, int]) -> bool:
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)", value)
    if match is None:
        raise ValueError("Docker API version is invalid")
    return (int(match.group(1)), int(match.group(2))) >= minimum

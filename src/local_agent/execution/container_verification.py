from __future__ import annotations

import json
import re

from .container_plan import CONTAINER_LOG_DRIVER
from .container_plan import CONTAINER_LOG_OPTIONS
from .container_plan import CONTAINER_MEMORY_BYTES
from .container_plan import CONTAINER_PIDS_LIMIT
from .container_plan import CONTAINER_EXECUTION_RESOURCE
from .container_plan import GATE_ENTRYPOINT
from .container_plan import ContainerExecutionPlan
from .container_plan import ContainerMount
from .container_plan import ContainerVolumeMount
from .container_plan import ContainerWorkspaceMount
from .container_plan import expected_container_environment
from .container_plan import mount_source_objects_unchanged
from .container_plan import mount_sources_unchanged


_CONTAINER_ID = re.compile(r"(?:sha256:)?([0-9a-f]{64})")
_MAX_INSPECT_OUTPUT_CHARS = 262_144
_TMPFS_TARGET = "/tmp"
_TMPFS_OPTIONS = frozenset({"rw", "nosuid", "nodev"})


def parse_container_inspect_payload(
    stdout: str,
) -> tuple[dict[object, object] | None, str | None]:
    if len(stdout) > _MAX_INSPECT_OUTPUT_CHARS:
        return None, "inspect_output_too_large"
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None, "inspect_invalid_json"
    if not isinstance(payload, dict):
        return None, "inspect_invalid_shape"
    return payload, None


def normalized_container_id(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    match = _CONTAINER_ID.fullmatch(raw)
    return match.group(1) if match is not None else None


def ownership_matches(
    plan: ContainerExecutionPlan,
    container_id: str,
    payload: dict[object, object],
) -> bool:
    raw_name = payload.get("name")
    normalized_name = (
        raw_name[1:]
        if isinstance(raw_name, str) and raw_name.startswith("/")
        else raw_name
    )
    return (
        normalized_container_id(payload.get("id")) == container_id
        and normalized_name == plan.instance_name
        and payload.get("instance_label") == plan.attempt_id
        and payload.get("resource_label") == CONTAINER_EXECUTION_RESOURCE
    )


def static_container_mismatch_reason(
    plan: ContainerExecutionPlan,
    container_id: str,
    payload: dict[object, object],
    *,
    require_source_path_identity: bool = True,
) -> str | None:
    if not plan.identity.control_authority_is_current():
        return "inspect_engine_changed"
    if not plan.identity.workspace_authority.is_current():
        return "inspect_workspace_authority_changed"
    if not ownership_matches(plan, container_id, payload):
        return "inspect_ownership_mismatch"
    if payload.get("config_image") != plan.runtime_image:
        return "inspect_image_mismatch"
    if normalized_container_id(payload.get("image_id")) != plan.image_id:
        return "inspect_image_id_mismatch"
    if payload.get("config_user") != f"{plan.user_id}:{plan.group_id}":
        return "inspect_user_mismatch"
    if not _environment_matches(plan, payload.get("config_env")):
        return "inspect_environment_mismatch"
    if not _gate_command_matches(plan, payload):
        return "inspect_command_mismatch"
    if payload.get("working_dir") != str(plan.working_directory):
        return "inspect_workdir_mismatch"
    if payload.get("stop_signal") != "SIGTERM":
        return "inspect_stop_signal_mismatch"
    if payload.get("readonly_rootfs") is not True:
        return "inspect_rootfs_mismatch"
    expected_network = "none" if plan.request.network_policy == "deny" else "bridge"
    if payload.get("network_mode") != expected_network:
        return "inspect_network_mismatch"
    if not _namespaces_match(payload):
        return "inspect_namespaces_mismatch"
    if payload.get("privileged") is not False:
        return "inspect_privileged_mismatch"
    if not _host_access_lists_empty(payload):
        return "inspect_host_access_mismatch"
    if not _healthcheck_disabled(payload):
        return "inspect_healthcheck_mismatch"
    if not _restart_disabled(payload):
        return "inspect_restart_mismatch"
    if not _logging_matches(payload):
        return "inspect_logging_mismatch"
    if not _capabilities_match(payload):
        return "inspect_capabilities_mismatch"
    if not _security_options_match(payload):
        return "inspect_security_mismatch"
    if payload.get("init") is not False:
        return "inspect_init_mismatch"
    if payload.get("pids_limit") != CONTAINER_PIDS_LIMIT:
        return "inspect_pids_mismatch"
    if payload.get("memory") != CONTAINER_MEMORY_BYTES:
        return "inspect_memory_mismatch"
    if not _tmpfs_matches(payload):
        return "inspect_tmpfs_mismatch"
    if not _host_mounts_match(plan.mounts, payload.get("host_mounts")):
        return "inspect_host_mounts_mismatch"
    if not _mounts_match(plan.mounts, payload.get("mounts")):
        return "inspect_mounts_mismatch"
    if plan.workspace_transport == "direct-bind":
        source_is_current = (
            mount_sources_unchanged(plan.mounts)
            if require_source_path_identity
            else mount_source_objects_unchanged(plan.mounts)
        )
        if not source_is_current:
            return "inspect_source_identity_changed"
    return None


def running_state_mismatch(payload: dict[object, object]) -> str | None:
    if payload.get("state_status") != "running" or payload.get("state_running") is not True:
        return "inspect_state_mismatch"
    if payload.get("state_oom_killed") is not False:
        return "inspect_oom_state_mismatch"
    if payload.get("state_error") not in ("", None):
        return "inspect_state_error"
    return None


def _environment_matches(plan: ContainerExecutionPlan, raw: object) -> bool:
    try:
        observed = _environment_mapping(raw)
    except ValueError:
        return False
    return observed == expected_container_environment(plan)


def _environment_mapping(raw: object) -> dict[str, str]:
    if not isinstance(raw, list):
        raise ValueError("container environment must be a list")
    values: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, str) or "=" not in item or "\0" in item:
            raise ValueError("container environment entry is invalid")
        name, value = item.split("=", 1)
        if not name or name in values:
            raise ValueError("container environment contains duplicate keys")
        values[name] = value
    return values


def _gate_command_matches(
    plan: ContainerExecutionPlan,
    payload: dict[object, object],
) -> bool:
    expected_command = list(plan.gate_command_argv)
    return (
        payload.get("entrypoint") == [GATE_ENTRYPOINT]
        and payload.get("cmd") == expected_command
        and payload.get("path") == GATE_ENTRYPOINT
        and payload.get("args") == expected_command
    )


def _capabilities_match(payload: dict[object, object]) -> bool:
    return (
        payload.get("cap_add") in (None, [])
        and payload.get("cap_drop") == ["ALL"]
    )


def _namespaces_match(payload: dict[object, object]) -> bool:
    return (
        payload.get("pid_mode") == ""
        and payload.get("uts_mode") == ""
        and payload.get("ipc_mode") == "private"
        and payload.get("cgroupns_mode") == "private"
    )


def _host_access_lists_empty(payload: dict[object, object]) -> bool:
    return all(
        payload.get(field) in (None, [])
        for field in ("devices", "device_requests", "volumes_from")
    )


def _security_options_match(payload: dict[object, object]) -> bool:
    options = payload.get("security_opt")
    return (
        isinstance(options, list)
        and len(options) == 2
        and frozenset(options)
        in (
            frozenset({"no-new-privileges", "seccomp=builtin"}),
            frozenset({"no-new-privileges:true", "seccomp=builtin"}),
            frozenset({"no-new-privileges=true", "seccomp=builtin"}),
        )
    )


def _healthcheck_disabled(payload: dict[object, object]) -> bool:
    healthcheck = payload.get("healthcheck")
    return healthcheck is None or (
        isinstance(healthcheck, dict)
        and healthcheck.get("Test") == ["NONE"]
    )


def _restart_disabled(payload: dict[object, object]) -> bool:
    restart_policy = payload.get("restart_policy")
    return (
        isinstance(restart_policy, dict)
        and restart_policy.get("Name") == "no"
        and restart_policy.get("MaximumRetryCount", 0) == 0
    )


def _logging_matches(payload: dict[object, object]) -> bool:
    log_config = payload.get("log_config")
    return (
        isinstance(log_config, dict)
        and log_config.get("Type") == CONTAINER_LOG_DRIVER
        and log_config.get("Config") == dict(CONTAINER_LOG_OPTIONS)
        and set(log_config) == {"Type", "Config"}
    )


def _tmpfs_matches(payload: dict[object, object]) -> bool:
    tmpfs = payload.get("tmpfs")
    if not isinstance(tmpfs, dict) or set(tmpfs) != {_TMPFS_TARGET}:
        return False
    options = tmpfs.get(_TMPFS_TARGET)
    return (
        isinstance(options, str)
        and frozenset(options.split(",")) == _TMPFS_OPTIONS
    )


def _host_mounts_match(
    expected: tuple[ContainerWorkspaceMount, ...],
    raw_mounts: object,
) -> bool:
    if not isinstance(raw_mounts, list) or len(raw_mounts) != len(expected):
        return False
    expected_by_target = {str(mount.destination): mount for mount in expected}
    observed_targets: set[str] = set()
    for raw in raw_mounts:
        if not isinstance(raw, dict):
            return False
        target = raw.get("Target")
        if not isinstance(target, str) or target in observed_targets:
            return False
        observed_targets.add(target)
        mount = expected_by_target.get(target)
        read_only = raw.get("ReadOnly", False)
        if mount is None or not isinstance(read_only, bool):
            return False
        if read_only is not (not mount.writable):
            return False
        if isinstance(mount, ContainerMount):
            if (
                raw.get("Type") != "bind"
                or raw.get("Source") != str(mount.source)
            ):
                return False
            bind_options = raw.get("BindOptions")
            if not isinstance(bind_options, dict):
                return False
            if bind_options.get("Propagation") not in (
                "rprivate",
                "private",
                "",
            ):
                return False
            if bind_options.get("NonRecursive") is not True:
                return False
            continue
        if (
            not isinstance(mount, ContainerVolumeMount)
            or raw.get("Type") != "volume"
            or raw.get("Source") != mount.name
            or raw.get("BindOptions") not in (None, {})
        ):
            return False
        volume_options = raw.get("VolumeOptions")
        if (
            not isinstance(volume_options, dict)
            or volume_options.get("NoCopy") is not True
            or volume_options.get("Subpath") != mount.subpath
        ):
            return False
    return observed_targets == set(expected_by_target)


def _mounts_match(
    expected: tuple[ContainerWorkspaceMount, ...],
    raw_mounts: object,
) -> bool:
    if not isinstance(raw_mounts, list):
        return False
    expected_by_destination = {str(mount.destination): mount for mount in expected}
    observed_destinations: set[str] = set()
    tmpfs_seen = False
    for raw in raw_mounts:
        if not isinstance(raw, dict):
            return False
        mount_type = raw.get("Type")
        destination = raw.get("Destination")
        if not isinstance(mount_type, str) or not isinstance(destination, str):
            return False
        if destination in observed_destinations:
            return False
        observed_destinations.add(destination)
        if mount_type == "tmpfs" and destination == _TMPFS_TARGET:
            if raw.get("RW") is not True:
                return False
            tmpfs_seen = True
            continue
        expected_mount = expected_by_destination.get(destination)
        if (
            expected_mount is None
            or raw.get("RW") is not expected_mount.writable
        ):
            return False
        if isinstance(expected_mount, ContainerMount):
            if (
                mount_type != "bind"
                or raw.get("Source") != str(expected_mount.source)
                or raw.get("Propagation") != "rprivate"
            ):
                return False
            continue
        if (
            not isinstance(expected_mount, ContainerVolumeMount)
            or mount_type != "volume"
            or raw.get("Name") != expected_mount.name
            or raw.get("Driver") != "local"
            or raw.get("Propagation") not in ("", None)
        ):
            return False
    return (tmpfs_seen or _TMPFS_TARGET not in observed_destinations) and observed_destinations - {_TMPFS_TARGET} == set(
        expected_by_destination
    )

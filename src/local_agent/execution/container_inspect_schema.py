from __future__ import annotations

from .container_types import ContainerEngineIdentity


_DOCKER_INSPECT_TEMPLATE = (
    '{"id":{{json .Id}},'
    '"name":{{json .Name}},'
    '"instance_label":{{json (index .Config.Labels "io.local-agent.instance")}},'
    '"resource_label":{{json (index .Config.Labels "io.local-agent.resource")}},'
    '"config_image":{{json .Config.Image}},'
    '"image_id":{{json .Image}},'
    '"config_user":{{json .Config.User}},'
    '"config_env":{{json .Config.Env}},'
    '"entrypoint":{{json .Config.Entrypoint}},'
    '"cmd":{{json .Config.Cmd}},'
    '"path":{{json .Path}},'
    '"args":{{json .Args}},'
    '"healthcheck":{{json .Config.Healthcheck}},'
    '"stop_signal":{{json .Config.StopSignal}},'
    '"working_dir":{{json .Config.WorkingDir}},'
    '"readonly_rootfs":{{json .HostConfig.ReadonlyRootfs}},'
    '"network_mode":{{json .HostConfig.NetworkMode}},'
    '"pid_mode":{{json .HostConfig.PidMode}},'
    '"ipc_mode":{{json .HostConfig.IpcMode}},'
    '"uts_mode":{{json .HostConfig.UTSMode}},'
    '"cgroupns_mode":{{json .HostConfig.CgroupnsMode}},'
    '"cap_add":{{json .HostConfig.CapAdd}},'
    '"cap_drop":{{json .HostConfig.CapDrop}},'
    '"devices":{{json .HostConfig.Devices}},'
    '"device_requests":{{json .HostConfig.DeviceRequests}},'
    '"volumes_from":{{json .HostConfig.VolumesFrom}},'
    '"security_opt":{{json .HostConfig.SecurityOpt}},'
    '"privileged":{{json .HostConfig.Privileged}},'
    '"restart_policy":{{json .HostConfig.RestartPolicy}},'
    '"log_config":{{json .HostConfig.LogConfig}},'
    '"init":{{json .HostConfig.Init}},'
    '"pids_limit":{{json .HostConfig.PidsLimit}},'
    '"memory":{{json .HostConfig.Memory}},'
    '"tmpfs":{{json .HostConfig.Tmpfs}},'
    '"host_mounts":{{json .HostConfig.Mounts}},'
    '"state_status":{{json .State.Status}},'
    '"state_running":{{json .State.Running}},'
    '"state_exit_code":{{json .State.ExitCode}},'
    '"state_oom_killed":{{json .State.OOMKilled}},'
    '"state_error":{{json .State.Error}},'
    '"mounts":{{json .Mounts}}}'
)


def build_container_inspect_argv(
    identity: ContainerEngineIdentity,
    reference: str,
) -> tuple[str, ...]:
    if not reference or "\0" in reference:
        raise ValueError("container inspect reference is invalid")
    return identity.command(
        "inspect",
        "--type=container",
        "--format",
        _DOCKER_INSPECT_TEMPLATE,
        reference,
    )

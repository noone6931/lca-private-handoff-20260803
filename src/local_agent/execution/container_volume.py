from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .container_cleanup import ContainerCleanupHandle
from .container_cleanup import build_durable_container_cleanup_handle
from .container_plan import CONTAINER_INSTANCE_LABEL
from .container_plan import CONTAINER_RESOURCE_LABEL
from .container_plan import ContainerExecutionPlan
from .container_plan import ContainerVolumeMount
from .container_staging import ContainerStagingAttempt
from .container_staging import StagedWorkspaceRoot
from .container_types import ContainerCommandResult
from .container_types import ContainerEngineIdentity
from .container_types import command_output_is_complete
from .container_types import command_workspace_authority_matches
from .container_types import container_command_id
from .container_verification import normalized_container_id


CONTAINER_PREP_RESOURCE = "staging-prep"
CONTAINER_VOLUME_RESOURCE_PREFIX = "root-"
_MAX_RESOURCE_OUTPUT_CHARS = 16_384
VOLUME_INSPECT_TEMPLATE = (
    '{"name":{{json .Name}},'
    '"driver":{{json .Driver}},'
    '"labels":{{json .Labels}},'
    '"options":{{json .Options}},'
    '"scope":{{json .Scope}}}'
)
_PREP_INSPECT_TEMPLATE = (
    '{"id":{{json .Id}},'
    '"name":{{json .Name}},'
    '"instance_label":{{json (index .Config.Labels "io.local-agent.instance")}},'
    '"resource_label":{{json (index .Config.Labels "io.local-agent.resource")}},'
    '"config_image":{{json .Config.Image}},'
    '"image_id":{{json .Image}},'
    '"state_status":{{json .State.Status}},'
    '"state_running":{{json .State.Running}},'
    '"host_mounts":{{json .HostConfig.Mounts}},'
    '"mounts":{{json .Mounts}}}'
)


@dataclass(frozen=True)
class ContainerVolumePreparationPlan:
    execution: ContainerExecutionPlan
    staging: ContainerStagingAttempt
    prep_instance_name: str

    def __post_init__(self) -> None:
        if (
            self.execution.staging != self.staging
            or self.execution.workspace_transport != "staged-copy"
            or self.prep_instance_name
            != f"{self.execution.instance_name}-prep"
            or not all(
                isinstance(mount, ContainerVolumeMount)
                for mount in self.execution.mounts
            )
            or tuple(mount.name for mount in self.execution.mounts)
            != tuple(root.volume_name for root in self.staging.roots)
        ):
            raise ValueError("container volume preparation plan is invalid")

    @property
    def identity(self) -> ContainerEngineIdentity:
        return self.execution.identity

    @property
    def attempt_id(self) -> str:
        return self.execution.attempt_id

    @property
    def roots(self) -> tuple[StagedWorkspaceRoot, ...]:
        return self.staging.roots

    @property
    def prep_create_argv(self) -> tuple[str, ...]:
        argv = [
            *self.identity.command("create"),
            "--pull=never",
            "--name",
            self.prep_instance_name,
            "--label",
            f"{CONTAINER_INSTANCE_LABEL}={self.attempt_id}",
            "--label",
            f"{CONTAINER_RESOURCE_LABEL}={CONTAINER_PREP_RESOURCE}",
            "--network=none",
            "--read-only",
            "--no-healthcheck",
            "--restart=no",
        ]
        for root in self.roots:
            argv.extend(
                (
                    "--mount",
                    ",".join(
                        (
                            "type=volume",
                            f"src={root.volume_name}",
                            f"dst={_prep_destination(root)}",
                            "volume-nocopy",
                        )
                    ),
                )
            )
        argv.append(self.execution.runtime_image)
        return tuple(argv)

    def volume_create_argv(
        self,
        root: StagedWorkspaceRoot,
    ) -> tuple[str, ...]:
        self._require_root(root)
        return self.identity.command(
            "volume",
            "create",
            "--driver=local",
            "--label",
            f"{CONTAINER_INSTANCE_LABEL}={self.attempt_id}",
            "--label",
            f"{CONTAINER_RESOURCE_LABEL}={_volume_resource(root)}",
            root.volume_name,
        )

    def volume_inspect_argv(
        self,
        root: StagedWorkspaceRoot,
    ) -> tuple[str, ...]:
        self._require_root(root)
        return volume_inspect_argv(self.identity, root.volume_name)

    def stage_copy_argv(
        self,
        root: StagedWorkspaceRoot,
        prep_container_id: str,
    ) -> tuple[str, ...]:
        self._require_root(root)
        _require_container_id(prep_container_id)
        return self.identity.command(
            "cp",
            "--archive",
            str(root.staging_path),
            f"{prep_container_id}:{_prep_destination(root)}/",
        )

    def prep_inspect_argv(
        self,
        prep_container_id: str,
    ) -> tuple[str, ...]:
        _require_container_id(prep_container_id)
        return self.identity.command(
            "inspect",
            "--type=container",
            "--format",
            _PREP_INSPECT_TEMPLATE,
            prep_container_id,
        )

    def output_copy_argv(
        self,
        root: StagedWorkspaceRoot,
        execution_container_id: str,
    ) -> tuple[str, ...]:
        self._require_root(root)
        _require_container_id(execution_container_id)
        return self.identity.command(
            "cp",
            f"{execution_container_id}:{root.destination_root}/.",
            f"{root.output_path}/",
        )

    def volume_remove_argv(
        self,
        root: StagedWorkspaceRoot,
    ) -> tuple[str, ...]:
        self._require_root(root)
        return self.identity.command("volume", "rm", root.volume_name)

    def volume_absence_argv(
        self,
        root: StagedWorkspaceRoot,
    ) -> tuple[str, ...]:
        self._require_root(root)
        return volume_query_argv(self.identity, root.volume_name)

    def prep_cleanup(
        self,
        prep_container_id: str,
    ) -> ContainerCleanupHandle:
        _require_container_id(prep_container_id)
        return build_durable_container_cleanup_handle(
            self.identity,
            self.attempt_id,
            prep_container_id,
            command_ordinal=2,
        )

    def _require_root(self, root: StagedWorkspaceRoot) -> None:
        if (
            root.ordinal >= len(self.roots)
            or self.roots[root.ordinal] != root
        ):
            raise ValueError("container volume root is not in its plan")


@dataclass(frozen=True)
class ContainerResourceResult:
    reason_code: str
    verified: bool

    def __post_init__(self) -> None:
        if not self.reason_code:
            raise ValueError("container resource result is invalid")


@dataclass(frozen=True)
class ContainerPrepCreateResult:
    reason_code: str
    container_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.reason_code
            or (self.reason_code == "prep_created")
            != (self.container_id is not None)
        ):
            raise ValueError("container prep create result is invalid")


def build_volume_preparation_plan(
    plan: ContainerExecutionPlan,
) -> ContainerVolumePreparationPlan:
    staging = plan.staging
    if staging is None:
        raise ValueError("volume preparation requires staged-copy")
    return ContainerVolumePreparationPlan(
        execution=plan,
        staging=staging,
        prep_instance_name=f"{plan.instance_name}-prep",
    )


def parse_volume_create_result(
    plan: ContainerVolumePreparationPlan,
    root: StagedWorkspaceRoot,
    result: ContainerCommandResult,
) -> ContainerResourceResult:
    argv = plan.volume_create_argv(root)
    failure = _result_failure(
        plan,
        "volume_create",
        root.ordinal + 1,
        argv,
        result,
    )
    if failure is not None:
        return ContainerResourceResult(f"volume_create_{failure}", False)
    if result.stderr or result.stdout != f"{root.volume_name}\n":
        return ContainerResourceResult("volume_create_output_invalid", False)
    return ContainerResourceResult("volume_created", True)


def parse_volume_inspect_result(
    plan: ContainerVolumePreparationPlan,
    root: StagedWorkspaceRoot,
    result: ContainerCommandResult,
    *,
    command_ordinal: int,
) -> ContainerResourceResult:
    argv = plan.volume_inspect_argv(root)
    failure = _result_failure(
        plan,
        "volume_inspect",
        command_ordinal,
        argv,
        result,
    )
    if failure is not None:
        return ContainerResourceResult(f"volume_inspect_{failure}", False)
    if result.stderr or len(result.stdout) > _MAX_RESOURCE_OUTPUT_CHARS:
        return ContainerResourceResult("volume_inspect_output_invalid", False)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ContainerResourceResult("volume_inspect_output_invalid", False)
    if not volume_payload_matches(
        name=root.volume_name,
        attempt_id=plan.attempt_id,
        resource_label=_volume_resource(root),
        payload=payload,
    ):
        return ContainerResourceResult("volume_ownership_mismatch", False)
    return ContainerResourceResult("volume_inspect_verified", True)


def parse_prep_create_result(
    plan: ContainerVolumePreparationPlan,
    result: ContainerCommandResult,
) -> ContainerPrepCreateResult:
    failure = _result_failure(
        plan,
        "prep_create",
        1,
        plan.prep_create_argv,
        result,
    )
    if failure is not None:
        return ContainerPrepCreateResult(f"prep_create_{failure}")
    if result.stderr or len(result.stdout) > 256:
        return ContainerPrepCreateResult("prep_create_output_invalid")
    container_id = normalized_container_id(result.stdout.strip())
    if container_id is None:
        return ContainerPrepCreateResult("prep_create_output_invalid")
    return ContainerPrepCreateResult("prep_created", container_id)


def parse_prep_inspect_result(
    plan: ContainerVolumePreparationPlan,
    prep_container_id: str,
    result: ContainerCommandResult,
) -> ContainerResourceResult:
    argv = plan.prep_inspect_argv(prep_container_id)
    failure = _result_failure(
        plan,
        "prep_inspect",
        1,
        argv,
        result,
    )
    if failure is not None:
        return ContainerResourceResult(f"prep_inspect_{failure}", False)
    if result.stderr or len(result.stdout) > _MAX_RESOURCE_OUTPUT_CHARS:
        return ContainerResourceResult("prep_inspect_output_invalid", False)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ContainerResourceResult("prep_inspect_output_invalid", False)
    if not _prep_payload_matches(plan, prep_container_id, payload):
        return ContainerResourceResult("prep_inspect_mismatch", False)
    return ContainerResourceResult("prep_inspect_verified", True)


def parse_copy_result(
    plan: ContainerVolumePreparationPlan,
    root: StagedWorkspaceRoot,
    *,
    step: str,
    command_ordinal: int,
    argv: tuple[str, ...],
    result: ContainerCommandResult,
) -> ContainerResourceResult:
    plan._require_root(root)
    failure = _result_failure(
        plan,
        step,
        command_ordinal,
        argv,
        result,
    )
    if failure is not None:
        return ContainerResourceResult(f"{step}_{failure}", False)
    if result.stdout or result.stderr:
        return ContainerResourceResult(f"{step}_unexpected_output", False)
    return ContainerResourceResult(f"{step}_completed", True)


def parse_volume_remove_result(
    plan: ContainerVolumePreparationPlan,
    root: StagedWorkspaceRoot,
    result: ContainerCommandResult,
) -> ContainerResourceResult:
    argv = plan.volume_remove_argv(root)
    failure = _result_failure(
        plan,
        "volume_remove",
        root.ordinal + 1,
        argv,
        result,
        require_success=False,
    )
    if failure is not None:
        return ContainerResourceResult(f"volume_remove_{failure}", False)
    return ContainerResourceResult("volume_remove_attempted", True)


def parse_volume_absence_result(
    plan: ContainerVolumePreparationPlan,
    root: StagedWorkspaceRoot,
    result: ContainerCommandResult,
) -> ContainerResourceResult:
    argv = plan.volume_absence_argv(root)
    failure = _result_failure(
        plan,
        "volume_removal_check",
        root.ordinal + 1,
        argv,
        result,
    )
    if failure is not None:
        return ContainerResourceResult(
            f"volume_removal_check_{failure}",
            False,
        )
    return parse_exact_volume_absence(root.volume_name, result)


def _prep_payload_matches(
    plan: ContainerVolumePreparationPlan,
    prep_container_id: str,
    payload: object,
) -> bool:
    if not isinstance(payload, dict) or set(payload) != {
        "id",
        "name",
        "instance_label",
        "resource_label",
        "config_image",
        "image_id",
        "state_status",
        "state_running",
        "host_mounts",
        "mounts",
    }:
        return False
    raw_name = payload.get("name")
    name = (
        raw_name[1:]
        if isinstance(raw_name, str) and raw_name.startswith("/")
        else raw_name
    )
    return (
        normalized_container_id(payload.get("id")) == prep_container_id
        and name == plan.prep_instance_name
        and payload.get("instance_label") == plan.attempt_id
        and payload.get("resource_label") == CONTAINER_PREP_RESOURCE
        and payload.get("config_image") == plan.execution.runtime_image
        and normalized_container_id(payload.get("image_id"))
        == plan.execution.image_id
        and payload.get("state_status") == "created"
        and payload.get("state_running") is False
        and _prep_host_mounts_match(plan, payload.get("host_mounts"))
        and _prep_runtime_mounts_match(plan, payload.get("mounts"))
    )


def _prep_host_mounts_match(
    plan: ContainerVolumePreparationPlan,
    raw: object,
) -> bool:
    if not isinstance(raw, list) or len(raw) != len(plan.roots):
        return False
    expected = {
        str(_prep_destination(root)): root.volume_name
        for root in plan.roots
    }
    observed: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            return False
        target = item.get("Target")
        if not isinstance(target, str) or target in observed:
            return False
        observed.add(target)
        options = item.get("VolumeOptions")
        if (
            expected.get(target) != item.get("Source")
            or item.get("Type") != "volume"
            or item.get("ReadOnly", False) is not False
            or not isinstance(options, dict)
            or options.get("NoCopy") is not True
        ):
            return False
    return observed == set(expected)


def _prep_runtime_mounts_match(
    plan: ContainerVolumePreparationPlan,
    raw: object,
) -> bool:
    if not isinstance(raw, list) or len(raw) != len(plan.roots):
        return False
    expected = {
        str(_prep_destination(root)): root.volume_name
        for root in plan.roots
    }
    observed: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            return False
        destination = item.get("Destination")
        if not isinstance(destination, str) or destination in observed:
            return False
        observed.add(destination)
        if (
            expected.get(destination) != item.get("Name")
            or item.get("Type") != "volume"
            or item.get("Driver") != "local"
            or item.get("RW") is not True
            or item.get("Propagation") not in ("", None)
        ):
            return False
    return observed == set(expected)


def _result_failure(
    plan: ContainerVolumePreparationPlan,
    step: str,
    ordinal: int,
    argv: tuple[str, ...],
    result: ContainerCommandResult,
    *,
    require_success: bool = True,
) -> str | None:
    if (
        result.attempt_id != plan.attempt_id
        or result.command_id
        != container_command_id(plan.attempt_id, step, ordinal=ordinal)
        or result.step != step
        or result.argv != argv
    ):
        return "correlation_mismatch"
    if not command_workspace_authority_matches(
        plan.identity.workspace_authority,
        result,
    ):
        return "workspace_authority_changed"
    if result.outcome != "exited":
        return result.outcome
    if require_success and result.exit_code != 0:
        return "failed"
    if not command_output_is_complete(result):
        return "output_incomplete"
    if not plan.identity.control_authority_is_current():
        return "engine_changed"
    return None


def volume_query_argv(
    identity: ContainerEngineIdentity,
    volume_name: str,
) -> tuple[str, ...]:
    return identity.command(
        "volume",
        "ls",
        "--filter",
        f"name={volume_name}",
        "--format",
        "{{json .Name}}",
    )


def volume_inspect_argv(
    identity: ContainerEngineIdentity,
    volume_name: str,
) -> tuple[str, ...]:
    return identity.command(
        "volume",
        "inspect",
        "--format",
        VOLUME_INSPECT_TEMPLATE,
        volume_name,
    )


def volume_payload_matches(
    *,
    name: str,
    attempt_id: str,
    resource_label: str,
    payload: object,
) -> bool:
    return (
        isinstance(payload, dict)
        and set(payload) == {"name", "driver", "labels", "options", "scope"}
        and payload.get("name") == name
        and payload.get("driver") == "local"
        and payload.get("labels")
        == {
            CONTAINER_INSTANCE_LABEL: attempt_id,
            CONTAINER_RESOURCE_LABEL: resource_label,
        }
        and payload.get("options") in (None, {})
        and payload.get("scope") == "local"
    )


def parse_exact_volume_absence(
    volume_name: str,
    result: ContainerCommandResult,
) -> ContainerResourceResult:
    if result.stderr or len(result.stdout) > _MAX_RESOURCE_OUTPUT_CHARS:
        return ContainerResourceResult(
            "volume_removal_check_output_invalid",
            False,
        )
    if result.stdout == "":
        return ContainerResourceResult("volume_cleanup_verified_absent", True)
    try:
        names = tuple(json.loads(line) for line in result.stdout.splitlines())
    except json.JSONDecodeError:
        return ContainerResourceResult(
            "volume_removal_check_output_invalid",
            False,
        )
    if names == (volume_name,):
        return ContainerResourceResult("volume_still_present", False)
    return ContainerResourceResult(
        "volume_removal_check_unexpected_identity",
        False,
    )


def _prep_destination(root: StagedWorkspaceRoot) -> str:
    return f"/lca-stage/root-{root.ordinal:04d}"


def _volume_resource(root: StagedWorkspaceRoot) -> str:
    return _volume_resource_ordinal(root.ordinal)


def _volume_resource_ordinal(ordinal: int) -> str:
    return f"{CONTAINER_VOLUME_RESOURCE_PREFIX}{ordinal:04d}"


def _require_container_id(container_id: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise ValueError("container resource identity is invalid")


__all__ = [
    "CONTAINER_PREP_RESOURCE",
    "CONTAINER_VOLUME_RESOURCE_PREFIX",
    "VOLUME_INSPECT_TEMPLATE",
    "ContainerPrepCreateResult",
    "ContainerResourceResult",
    "ContainerVolumePreparationPlan",
    "build_volume_preparation_plan",
    "parse_copy_result",
    "parse_prep_create_result",
    "parse_prep_inspect_result",
    "parse_volume_absence_result",
    "parse_volume_create_result",
    "parse_volume_inspect_result",
    "parse_volume_remove_result",
    "parse_exact_volume_absence",
    "volume_inspect_argv",
    "volume_payload_matches",
    "volume_query_argv",
]

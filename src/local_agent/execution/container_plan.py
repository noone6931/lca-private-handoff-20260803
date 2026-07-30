from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .environment import is_provider_credential_environment_key
from .container_staging import ContainerStagingAttempt
from .container_types import CONTAINER_NETWORK_POLICIES
from .container_types import CONTAINER_PROFILES
from .container_types import ContainerCommandResult
from .container_types import ContainerDirectoryPathIdentity
from .container_types import ContainerEngineIdentity
from .container_types import command_output_is_complete
from .container_types import container_command_id
from .container_types import command_workspace_authority_matches
from .container_types import validate_attempt_id
from .contracts import IsolationRequest


_IMAGE_ID = re.compile(r"(?:sha256:)?([0-9a-f]{64})")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STAGING_VOLUME_NAME = re.compile(r"lca-[0-9a-f]{32}-(root-[0-9]{4})")
_DOCKER_IMAGE_TEMPLATE = (
    '{"id":{{json .Id}},'
    '"repo_digests":{{json .RepoDigests}},'
    '"config_env":{{json .Config.Env}},'
    '"labels":{{json .Config.Labels}},'
    '"volumes":{{json (index .Config "Volumes")}}}'
)
_MAX_IMAGE_OUTPUT_CHARS = 262_144
_TMPFS_TARGET = Path("/tmp")
CONTAINER_INSTANCE_LABEL = "io.local-agent.instance"
CONTAINER_RESOURCE_LABEL = "io.local-agent.resource"
CONTAINER_EXECUTION_RESOURCE = "execution"
GATE_PROTOCOL_LABEL = "io.local-agent.gate-protocol"
GATE_PROTOCOL = "signal-v1"
GATE_ENTRYPOINT = "/opt/local-agent/bin/isolation-gate"
GATE_READY_CHECK = "/opt/local-agent/bin/isolation-gate-ready"
GATE_MOUNT_PROOF = "/opt/local-agent/bin/isolation-gate-mount-proof"
GATE_STAGE_PROOF = "/opt/local-agent/bin/isolation-gate-stage-proof"
GATE_READY_ATTEMPTS = 100
GATE_READY_DELAY_SECONDS = "0.05"
GATE_READY_TIMEOUT_SECONDS = 8
GATE_RELEASE_SIGNAL = "SIGUSR1"
GATE_COMMAND_PREFIX = (
    "--protocol",
    GATE_PROTOCOL,
    "--release-signal",
    GATE_RELEASE_SIGNAL,
)
CONTAINER_MEMORY_BYTES = 1024 * 1024 * 1024
CONTAINER_PIDS_LIMIT = 512
CONTAINER_LOG_DRIVER = "local"
CONTAINER_LOG_OPTIONS = (
    ("compress", "false"),
    ("max-file", "1"),
    ("max-size", "4m"),
)
_GATE_UNSAFE_ENVIRONMENT_KEYS = frozenset(
    {
        "BASH_ENV",
        "ENV",
        "IFS",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "SHELLOPTS",
    }
)
_MANAGED_RUNTIME_PATHS = (
    _TMPFS_TARGET,
    Path("/bin"),
    Path("/dev"),
    Path("/etc"),
    Path("/lib"),
    Path("/lib64"),
    Path("/opt/local-agent"),
    Path("/proc"),
    Path("/run"),
    Path("/sbin"),
    Path("/sys"),
    Path("/usr"),
)


@dataclass(frozen=True)
class ContainerDirectoryIdentity:
    device: int
    inode: int

    def __post_init__(self) -> None:
        if self.device < 0 or self.inode <= 0:
            raise ValueError("container directory identity is invalid")


@dataclass(frozen=True)
class ContainerMount:
    source: Path
    destination: Path
    writable: bool
    source_path_identity: ContainerDirectoryPathIdentity

    def __post_init__(self) -> None:
        if not self.source.is_absolute() or not self.destination.is_absolute():
            raise ValueError("container bind paths must be absolute")
        _validate_mount_path(self.source)
        _validate_mount_path(self.destination)
        if self.source_path_identity != directory_path_identity(self.source):
            raise ValueError("container bind source identity changed while planning")

    @property
    def source_identity(self) -> ContainerDirectoryIdentity:
        return ContainerDirectoryIdentity(
            self.source_path_identity.device,
            self.source_path_identity.inode,
        )

    def create_argument(self) -> str:
        fields = [
            "type=bind",
            f"src={self.source}",
            f"dst={self.destination}",
            "bind-propagation=rprivate",
            "bind-recursive=disabled",
        ]
        if not self.writable:
            fields.append("readonly")
        return ",".join(fields)


@dataclass(frozen=True)
class ContainerVolumeMount:
    name: str
    destination: Path
    writable: bool

    def __post_init__(self) -> None:
        if (
            _STAGING_VOLUME_NAME.fullmatch(self.name) is None
            or not self.destination.is_absolute()
        ):
            raise ValueError("container volume mount is invalid")
        _validate_mount_path(self.destination)

    @property
    def subpath(self) -> str:
        matched = _STAGING_VOLUME_NAME.fullmatch(self.name)
        assert matched is not None
        return matched.group(1)

    def create_argument(self) -> str:
        fields = [
            "type=volume",
            f"src={self.name}",
            f"dst={self.destination}",
            f"volume-subpath={self.subpath}",
            "volume-nocopy",
        ]
        if not self.writable:
            fields.append("readonly")
        return ",".join(fields)


ContainerWorkspaceMount = ContainerMount | ContainerVolumeMount


@dataclass(frozen=True)
class ContainerExecutionDraft:
    identity: ContainerEngineIdentity
    request: IsolationRequest
    image: str
    image_digest: str
    attempt_id: str
    instance_name: str
    workspace: Path
    readable_roots: tuple[Path, ...]
    writable_roots: tuple[Path, ...]
    working_directory: Path
    command_argv: tuple[str, ...]
    user_id: int
    group_id: int
    mounts: tuple[ContainerWorkspaceMount, ...]
    staging: ContainerStagingAttempt | None
    image_inspect_argv: tuple[str, ...]
    timeout_seconds: int = 30

    def __post_init__(self) -> None:
        if not self.identity.is_current():
            raise ValueError("container engine identity changed while planning")
        if self.request.backend not in {"auto", "container"}:
            raise ValueError("container draft requires the auto or container backend")
        if self.request.profile not in CONTAINER_PROFILES:
            raise ValueError("container draft does not support danger-full-access")
        if self.image != self.identity.gate_image.reference:
            raise ValueError("container image must come from backend authority")
        if self.image_digest != self.identity.gate_image.digest:
            raise ValueError("container image digest must come from backend authority")
        validate_attempt_id(self.attempt_id)
        if self.instance_name != f"lca-{self.attempt_id}":
            raise ValueError("container instance name must derive from its attempt id")
        if self.user_id < 0 or self.group_id < 0:
            raise ValueError("container uid and gid must not be negative")
        _validate_command(self.command_argv)
        if not 1 <= self.timeout_seconds <= 120:
            raise ValueError("container command timeout must be between 1 and 120 seconds")
        if self.workspace != _canonical_directory("workspace", self.request.workspace):
            raise ValueError("container draft workspace does not match the request")
        requested_readable = tuple(
            _canonical_directory("readable root", root)
            for root in self.request.readable_roots
        )
        if self.readable_roots != requested_readable:
            raise ValueError("container draft readable roots do not match the request")
        _validate_root_layout(self.workspace, self.readable_roots)
        _validate_authorized_roots(
            self.identity,
            self.workspace,
            self.readable_roots,
        )
        requested_writable = tuple(
            _canonical_directory("writable root", root)
            for root in self.request.writable_roots
        )
        expected_writable = (
            (self.workspace,) if self.request.profile == "workspace-write" else ()
        )
        if requested_writable and requested_writable != expected_writable:
            raise ValueError("container draft writable roots do not match the request")
        if self.writable_roots != expected_writable:
            raise ValueError("container draft writable roots do not match its profile")
        if self.working_directory != _canonical_directory(
            "working directory",
            self.working_directory,
        ):
            raise ValueError("container working directory must be canonical")
        if not any(
            self.working_directory == root
            or self.working_directory.is_relative_to(root)
            for root in (self.workspace, *self.readable_roots)
        ):
            raise ValueError("container working directory must be inside an authorized mount")
        _validate_staging(
            self.staging,
            attempt_id=self.attempt_id,
            roots=(self.workspace, *self.readable_roots),
            roots_revision=self.identity.workspace_authority.revision,
            root_identities=tuple(
                (identity.device, identity.inode)
                for identity in self.identity.workspace_authority.root_identities
            ),
        )
        if self.mounts != _expected_mounts(
            self.workspace,
            self.readable_roots,
            writable=self.request.profile == "workspace-write",
            staging=self.staging,
        ):
            raise ValueError("container draft mounts do not match its authorized roots")
        if self.image_inspect_argv != _build_image_inspect_argv(
            self.identity,
            self.image,
        ):
            raise ValueError("container image inspect argv does not match its draft")


@dataclass(frozen=True)
class ContainerExecutionPlan:
    draft: ContainerExecutionDraft
    image_id: str
    base_environment: tuple[str, ...]
    create_argv: tuple[str, ...]
    recovery_query_argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.draft.identity.is_current():
            raise ValueError("container engine identity changed after image verification")
        if not re.fullmatch(r"[0-9a-f]{64}", self.image_id):
            raise ValueError("container image id must contain 64 lowercase hex characters")
        _environment_mapping(self.base_environment)
        if self.create_argv != _build_create_argv(self.draft, self.image_id):
            raise ValueError("container create argv does not match the image proof")
        expected_recovery = build_container_recovery_query_argv(
            self.draft.identity,
            self.draft.attempt_id,
        )
        if self.recovery_query_argv != expected_recovery:
            raise ValueError("container recovery query argv does not match its plan")

    @property
    def identity(self) -> ContainerEngineIdentity:
        return self.draft.identity

    @property
    def request(self) -> IsolationRequest:
        return self.draft.request

    @property
    def image(self) -> str:
        return self.draft.image

    @property
    def image_digest(self) -> str:
        return self.draft.image_digest

    @property
    def runtime_image(self) -> str:
        return f"sha256:{self.image_id}"

    @property
    def attempt_id(self) -> str:
        return self.draft.attempt_id

    @property
    def instance_name(self) -> str:
        return self.draft.instance_name

    @property
    def workspace(self) -> Path:
        return self.draft.workspace

    @property
    def readable_roots(self) -> tuple[Path, ...]:
        return self.draft.readable_roots

    @property
    def writable_roots(self) -> tuple[Path, ...]:
        return self.draft.writable_roots

    @property
    def working_directory(self) -> Path:
        return self.draft.working_directory

    @property
    def command_argv(self) -> tuple[str, ...]:
        return self.draft.command_argv

    @property
    def gate_command_argv(self) -> tuple[str, ...]:
        return (
            *GATE_COMMAND_PREFIX,
            "--attempt-id",
            self.attempt_id,
            "--",
            *self.command_argv,
        )

    @property
    def user_id(self) -> int:
        return self.draft.user_id

    @property
    def group_id(self) -> int:
        return self.draft.group_id

    @property
    def mounts(self) -> tuple[ContainerWorkspaceMount, ...]:
        return self.draft.mounts

    @property
    def workspace_transport(self) -> str:
        return "staged-copy" if self.draft.staging is not None else "direct-bind"

    @property
    def staging(self) -> ContainerStagingAttempt | None:
        return self.draft.staging

    @property
    def stage_proof_arguments(self) -> tuple[str, ...]:
        staging = self.staging
        if staging is None:
            return ()
        by_destination = {
            root.destination_root: root for root in staging.roots
        }
        arguments: list[str] = []
        for mount in self.mounts:
            staged = by_destination.get(mount.destination)
            if (
                staged is None
                or not isinstance(mount, ContainerVolumeMount)
                or staged.volume_name != mount.name
                or staged.staging_path.name != mount.subpath
            ):
                raise ValueError("container staged proof does not match its mounts")
            arguments.extend(
                (
                    "--root",
                    str(mount.destination),
                    "--manifest-sha256",
                    staged.manifest_sha256,
                )
            )
        return tuple(arguments)

    @property
    def staged_manifest_digests(self) -> tuple[str, ...]:
        arguments = self.stage_proof_arguments
        return tuple(arguments[index] for index in range(3, len(arguments), 4))


@dataclass(frozen=True)
class ContainerImageResult:
    reason_code: str
    plan: ContainerExecutionPlan | None = None

    def __post_init__(self) -> None:
        if not self.reason_code.strip():
            raise ValueError("container image reason_code must not be empty")
        if (self.reason_code == "image_verified") != (self.plan is not None):
            raise ValueError("only a verified image may expose an execution plan")


def build_container_execution_draft(
    identity: ContainerEngineIdentity,
    request: IsolationRequest,
    *,
    attempt_id: str,
    working_directory: Path,
    command_argv: tuple[str, ...],
    user_id: int,
    group_id: int,
    staging: ContainerStagingAttempt | None = None,
) -> ContainerExecutionDraft:
    if request.backend not in {"auto", "container"}:
        raise ValueError("container draft requires the auto or container backend")
    if request.profile not in CONTAINER_PROFILES:
        raise ValueError("container draft does not support danger-full-access")
    if not identity.is_current():
        raise ValueError("container engine identity changed before planning")

    workspace = _canonical_directory("workspace", request.workspace)
    readable_roots = tuple(
        _canonical_directory("readable root", root) for root in request.readable_roots
    )
    writable_roots = tuple(
        _canonical_directory("writable root", root) for root in request.writable_roots
    )
    if len(set(readable_roots)) != len(readable_roots):
        raise ValueError("container readable roots must be canonically unique")
    if len(set(writable_roots)) != len(writable_roots):
        raise ValueError("container writable roots must be canonically unique")
    _validate_root_layout(workspace, readable_roots)
    _validate_authorized_roots(identity, workspace, readable_roots)
    if request.profile == "read-only" and writable_roots:
        raise ValueError("read-only container draft cannot declare writable roots")
    if request.profile == "workspace-write" and set(writable_roots) not in (
        set(),
        {workspace},
    ):
        raise ValueError("workspace-write container draft may only write the primary workspace")
    if request.profile == "workspace-write":
        writable_roots = (workspace,)

    canonical_cwd = _canonical_directory("working directory", working_directory)
    if not any(
        canonical_cwd == root or canonical_cwd.is_relative_to(root)
        for root in (workspace, *readable_roots)
    ):
        raise ValueError("container working directory must be inside an authorized mount")
    mounts = _expected_mounts(
        workspace,
        readable_roots,
        writable=request.profile == "workspace-write",
        staging=staging,
    )
    return ContainerExecutionDraft(
        identity=identity,
        request=request,
        image=identity.gate_image.reference,
        image_digest=identity.gate_image.digest,
        attempt_id=attempt_id,
        instance_name=f"lca-{attempt_id}",
        workspace=workspace,
        readable_roots=readable_roots,
        writable_roots=writable_roots,
        working_directory=canonical_cwd,
        command_argv=command_argv,
        user_id=user_id,
        group_id=group_id,
        mounts=mounts,
        staging=staging,
        image_inspect_argv=_build_image_inspect_argv(
            identity,
            identity.gate_image.reference,
        ),
    )


def parse_container_image_result(
    draft: ContainerExecutionDraft,
    result: ContainerCommandResult,
) -> ContainerImageResult:
    failure = _result_failure(draft, "image", draft.image_inspect_argv, result)
    if failure is not None:
        return ContainerImageResult(f"image_{failure}")
    if result.stderr:
        return ContainerImageResult("image_unexpected_stderr")
    if len(result.stdout) > _MAX_IMAGE_OUTPUT_CHARS:
        return ContainerImageResult("image_output_too_large")
    if not draft.identity.is_current():
        return ContainerImageResult("image_engine_changed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ContainerImageResult("image_invalid_json")
    if not isinstance(payload, dict):
        return ContainerImageResult("image_invalid_shape")
    image_id = _normalized_image_id(payload.get("id"))
    repo_digests = payload.get("repo_digests")
    if image_id is None or not _image_authority_observed(
        draft.identity,
        image_id,
        repo_digests,
    ):
        return ContainerImageResult("image_digest_mismatch")
    labels = payload.get("labels")
    if not isinstance(labels, dict) or labels.get(GATE_PROTOCOL_LABEL) != GATE_PROTOCOL:
        return ContainerImageResult("image_gate_protocol_mismatch")
    volumes = payload.get("volumes")
    if volumes not in (None, {}):
        return ContainerImageResult("image_declares_volumes")
    try:
        base_environment = _environment_mapping(payload.get("config_env"))
    except ValueError:
        return ContainerImageResult("image_environment_invalid")
    if any(
        is_provider_credential_environment_key(name)
        for name in base_environment
    ):
        return ContainerImageResult("image_environment_contains_provider_credential")
    if _unsafe_gate_environment(base_environment):
        return ContainerImageResult("image_environment_unsafe")
    plan = ContainerExecutionPlan(
        draft=draft,
        image_id=image_id,
        base_environment=tuple(
            f"{name}={value}" for name, value in sorted(base_environment.items())
        ),
        create_argv=_build_create_argv(draft, image_id),
        recovery_query_argv=build_container_recovery_query_argv(
            draft.identity,
            draft.attempt_id,
        ),
    )
    return ContainerImageResult("image_verified", plan)


def directory_identity(path: Path) -> ContainerDirectoryIdentity:
    identity = directory_path_identity(path)
    return ContainerDirectoryIdentity(identity.device, identity.inode)


def directory_path_identity(path: Path) -> ContainerDirectoryPathIdentity:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ValueError("container bind source identity is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("container bind source must remain a directory")
    return ContainerDirectoryPathIdentity.from_stat(metadata)


def mount_sources_unchanged(
    mounts: tuple[ContainerWorkspaceMount, ...],
) -> bool:
    try:
        return all(
            mount.source_path_identity == directory_path_identity(mount.source)
            for mount in mounts
            if isinstance(mount, ContainerMount)
        )
    except ValueError:
        return False


def mount_source_objects_unchanged(
    mounts: tuple[ContainerWorkspaceMount, ...],
) -> bool:
    try:
        return all(
            mount.source_identity == directory_identity(mount.source)
            for mount in mounts
            if isinstance(mount, ContainerMount)
        )
    except ValueError:
        return False


def expected_container_environment(plan: ContainerExecutionPlan) -> dict[str, str]:
    return _environment_mapping(plan.base_environment)


def build_container_recovery_query_argv(
    identity: ContainerEngineIdentity,
    attempt_id: str,
) -> tuple[str, ...]:
    validate_attempt_id(attempt_id)
    return identity.command(
        "ps",
        "--all",
        "--no-trunc",
        "--filter",
        f"label={CONTAINER_INSTANCE_LABEL}={attempt_id}",
        "--format",
        "{{json .ID}}",
    )


def _build_image_inspect_argv(
    identity: ContainerEngineIdentity,
    image: str,
) -> tuple[str, ...]:
    return identity.command(
        "image",
        "inspect",
        "--format",
        _DOCKER_IMAGE_TEMPLATE,
        image,
    )


def _build_create_argv(
    draft: ContainerExecutionDraft,
    image_id: str,
) -> tuple[str, ...]:
    if draft.request.network_policy not in CONTAINER_NETWORK_POLICIES:
        raise ValueError("container network policy must be deny or allow")
    if not re.fullmatch(r"[0-9a-f]{64}", image_id):
        raise ValueError("container image proof is invalid")
    network_mode = "none" if draft.request.network_policy == "deny" else "bridge"
    argv = [
        *draft.identity.command("create"),
        "--pull=never",
        "--name",
        draft.instance_name,
        "--label",
        f"{CONTAINER_INSTANCE_LABEL}={draft.attempt_id}",
        "--label",
        f"{CONTAINER_RESOURCE_LABEL}={CONTAINER_EXECUTION_RESOURCE}",
        "--read-only",
        "--no-healthcheck",
        "--restart=no",
        f"--log-driver={CONTAINER_LOG_DRIVER}",
        "--init=false",
        "--stop-signal=SIGTERM",
        "--user",
        f"{draft.user_id}:{draft.group_id}",
        "--workdir",
        str(draft.working_directory),
        "--network",
        network_mode,
        "--ipc=private",
        "--cgroupns=private",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true",
        "--security-opt=seccomp=builtin",
        f"--pids-limit={CONTAINER_PIDS_LIMIT}",
        f"--memory={CONTAINER_MEMORY_BYTES}",
        "--tmpfs",
        f"{_TMPFS_TARGET}:rw,nosuid,nodev",
    ]
    for name, value in CONTAINER_LOG_OPTIONS:
        argv.append(f"--log-opt={name}={value}")
    for mount in draft.mounts:
        argv.extend(("--mount", mount.create_argument()))
    argv.extend(
        (
            "--entrypoint",
            GATE_ENTRYPOINT,
            f"sha256:{image_id}",
            *GATE_COMMAND_PREFIX,
            "--attempt-id",
            draft.attempt_id,
            "--",
            *draft.command_argv,
        )
    )
    return tuple(argv)


def _expected_mounts(
    workspace: Path,
    readable_roots: tuple[Path, ...],
    *,
    writable: bool,
    staging: ContainerStagingAttempt | None = None,
) -> tuple[ContainerWorkspaceMount, ...]:
    staged_roots = (
        {root.destination_root: root for root in staging.roots}
        if staging is not None
        else {}
    )

    def mount(root: Path, *, write: bool) -> ContainerWorkspaceMount:
        staged = staged_roots.get(root)
        if staged is not None:
            return ContainerVolumeMount(
                staged.volume_name,
                root,
                write,
            )
        return ContainerMount(
            root,
            root,
            write,
            directory_path_identity(root),
        )

    return (
        mount(workspace, write=writable),
        *(
            mount(root, write=False)
            for root in sorted(readable_roots, key=str)
        ),
    )


def _validate_staging(
    staging: ContainerStagingAttempt | None,
    *,
    attempt_id: str,
    roots: tuple[Path, ...],
    roots_revision: int,
    root_identities: tuple[tuple[int, int], ...],
) -> None:
    if staging is None:
        return
    if staging.attempt_id != attempt_id or not staging.authority_is_current():
        raise ValueError("container staging authority changed while planning")
    if tuple(root.source_root for root in staging.roots) != roots:
        raise ValueError("container staging roots do not match workspace authority")
    if tuple(root.destination_root for root in staging.roots) != roots:
        raise ValueError("container staging destinations do not match workspace roots")
    if tuple(root.source_identity for root in staging.roots) != root_identities:
        raise ValueError("container staging source identities do not match workspace roots")
    if any(root.roots_revision != roots_revision for root in staging.roots):
        raise ValueError("container staging revision does not match workspace authority")


def _canonical_directory(label: str, path: Path) -> Path:
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"container {label} must resolve to an existing directory") from exc
    if not canonical.is_dir():
        raise ValueError(f"container {label} must resolve to an existing directory")
    directory_identity(canonical)
    _validate_mount_path(canonical)
    return canonical


def _validate_mount_path(path: Path) -> None:
    rendered = str(path)
    if any(character in rendered for character in ("\0", "\n", "\r", ",")):
        raise ValueError("container bind paths must not contain NUL, newlines, or commas")


def _validate_root_layout(
    workspace: Path,
    readable_roots: tuple[Path, ...],
) -> None:
    roots = (workspace, *readable_roots)
    for root in roots:
        if any(_paths_overlap(root, managed) for managed in _MANAGED_RUNTIME_PATHS):
            raise ValueError(
                "container authorized roots must not overlap managed runtime paths"
            )
    for index, root in enumerate(roots):
        for other in roots[index + 1 :]:
            if (
                root == other
                or root.is_relative_to(other)
                or other.is_relative_to(root)
            ):
                raise ValueError("container roots must not overlap or repeat")


def _validate_authorized_roots(
    identity: ContainerEngineIdentity,
    workspace: Path,
    readable_roots: tuple[Path, ...],
) -> None:
    authorized = identity.workspace_authority.roots
    if workspace != authorized[0]:
        raise ValueError(
            "container workspace must match the primary workspace authority"
        )
    if not set(readable_roots).issubset(set(authorized[1:])):
        raise ValueError(
            "container readable roots must come from workspace authority"
        )


def _validate_command(command_argv: tuple[str, ...]) -> None:
    if (
        not command_argv
        or not command_argv[0]
        or any(not isinstance(arg, str) or "\0" in arg for arg in command_argv)
    ):
        raise ValueError("container command argv must contain non-NUL strings")


def _unsafe_gate_environment(environment: dict[str, str]) -> bool:
    return bool(_GATE_UNSAFE_ENVIRONMENT_KEYS.intersection(environment))


def _paths_overlap(left: Path, right: Path) -> bool:
    return (
        left == right
        or left.is_relative_to(right)
        or right.is_relative_to(left)
    )


def _environment_mapping(raw: object) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, (list, tuple)):
        raise ValueError("container environment must be a list")
    values: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, str) or "\0" in item or "=" not in item:
            raise ValueError("container environment entry is invalid")
        name, value = item.split("=", 1)
        if not _ENVIRONMENT_NAME.fullmatch(name) or name in values:
            raise ValueError("container environment keys must be valid and unique")
        values[name] = value
    return values


def _normalized_image_id(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    match = _IMAGE_ID.fullmatch(raw)
    return match.group(1) if match is not None else None


def _image_authority_observed(
    identity: ContainerEngineIdentity,
    image_id: str,
    raw_repo_digests: object,
) -> bool:
    authority = identity.gate_image
    if authority.kind == "local-image-id":
        return (
            authority.digest == f"sha256:{image_id}"
            and (
                raw_repo_digests is None
                or (
                    isinstance(raw_repo_digests, list)
                    and all(isinstance(item, str) for item in raw_repo_digests)
                )
            )
        )
    return (
        isinstance(raw_repo_digests, list)
        and bool(raw_repo_digests)
        and all(isinstance(item, str) for item in raw_repo_digests)
        and authority.reference in raw_repo_digests
    )


def _result_failure(
    draft: ContainerExecutionDraft,
    step: str,
    argv: tuple[str, ...],
    result: ContainerCommandResult,
) -> str | None:
    if (
        result.attempt_id != draft.attempt_id
        or result.command_id != container_command_id(draft.attempt_id, "image")
        or result.step != step
        or result.argv != argv
    ):
        return "correlation_mismatch"
    if not command_workspace_authority_matches(
        draft.identity.workspace_authority,
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

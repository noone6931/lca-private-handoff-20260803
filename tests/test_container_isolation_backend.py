from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path

from local_agent.execution.container_backend import (
    build_container_recovery_obligation,
    build_container_execution_draft,
    parse_container_create_result,
    parse_container_final_inspect_result,
    parse_container_gate_ready_result,
    parse_container_image_result,
    parse_container_inspect_result,
    parse_container_logs_result,
    parse_container_mount_proof_result,
    parse_container_recovery_inspect_result,
    parse_container_recovery_query_result,
    parse_container_release_result,
    parse_container_removal_check_result,
    parse_container_remove_result,
    parse_container_start_result,
    parse_container_wait_result,
)
from local_agent.execution.container_plan import CONTAINER_LOG_DRIVER
from local_agent.execution.container_plan import CONTAINER_LOG_OPTIONS
from local_agent.execution.container_plan import CONTAINER_MEMORY_BYTES
from local_agent.execution.container_plan import CONTAINER_PIDS_LIMIT
from local_agent.execution.container_plan import CONTAINER_EXECUTION_RESOURCE
from local_agent.execution.container_plan import GATE_COMMAND_PREFIX
from local_agent.execution.container_plan import GATE_ENTRYPOINT
from local_agent.execution.container_plan import GATE_PROTOCOL
from local_agent.execution.container_plan import GATE_PROTOCOL_LABEL
from local_agent.execution.container_plan import GATE_READY_ATTEMPTS
from local_agent.execution.container_plan import GATE_READY_CHECK
from local_agent.execution.container_plan import GATE_READY_DELAY_SECONDS
from local_agent.execution.container_plan import GATE_READY_TIMEOUT_SECONDS
from local_agent.execution.container_probe import build_docker_server_probe
from local_agent.execution.container_probe import parse_container_probe_result
from local_agent.execution.container_durable_recovery import (
    build_durable_container_recovery_plan,
)
from local_agent.execution.container_durable_recovery import (
    parse_durable_recovery_inspect_result,
)
from local_agent.execution.container_durable_recovery import (
    parse_durable_recovery_query_result,
)
from local_agent.execution.container_probe import unsupported_podman_probe_result
from local_agent.execution.container_recovery import RECOVERY_QUERY_ATTEMPT_LIMIT
from local_agent.execution.container_staging_contracts import (
    ContainerStagingContainerBinding,
)
from local_agent.execution.container_types import ContainerCommandResult
from local_agent.execution.container_types import ContainerOutputCapture
from local_agent.execution.container_types import ContainerStreamCapture
from local_agent.execution.container_types import container_command_id
from local_agent.execution.container_volume_recovery import (
    build_durable_volume_recovery_plan,
)
from local_agent.execution.container_volume_recovery import (
    parse_durable_volume_recovery_inspect_result,
)
from local_agent.execution.container_volume_recovery import (
    parse_durable_volume_recovery_query_result,
)
from local_agent.execution.contracts import IsolationRequest


ATTEMPT_ID = "1" * 32
IMAGE_DIGEST = "a" * 64
IMAGE_ID = "b" * 64
CONTAINER_ID = "c" * 64
IMAGE = f"registry.example/lca-gate@sha256:{IMAGE_DIGEST}"
ROOTS_REVISION = 7


class ContainerIsolationBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.readable = self.root / "readable"
        self.readable.mkdir()
        self.config_dir = self.root / "docker-config"
        self.config_dir.mkdir(mode=0o700)
        self.executable = self.root / "trusted" / "docker"
        self.executable.parent.mkdir()
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o755)
        self.executable_sha256 = hashlib.sha256(
            self.executable.read_bytes()
        ).hexdigest()
        self.socket_path = self.root / "trusted" / "docker.sock"
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(self.socket_path))

    def tearDown(self) -> None:
        self.socket.close()
        self.temp.cleanup()

    def _probe_plan(self, *, gate_image: str = IMAGE):
        return build_docker_server_probe(
            attempt_id=ATTEMPT_ID,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=ROOTS_REVISION,
            executable=self.executable,
            executable_sha256=self.executable_sha256,
            socket_path=self.socket_path,
            client_config_directory=self.config_dir,
            gate_image=gate_image,
        )

    def _result(
        self,
        owner,
        step: str,
        *,
        argv: tuple[str, ...] | None = None,
        stdout: str = "",
        stderr: str = "",
        outcome: str = "exited",
        exit_code: int | None = 0,
        attempt_id: str = ATTEMPT_ID,
        workspace_roots: tuple[Path, ...] | None = None,
        workspace_roots_revision: int = ROOTS_REVISION,
        command_id: str | None = None,
        event_sequence: int | None = None,
        started_monotonic_ns: int | None = None,
        finished_monotonic_ns: int | None = None,
        output_capture: ContainerOutputCapture | None = None,
    ) -> ContainerCommandResult:
        if argv is None:
            argv = getattr(owner, f"{step}_argv", None)
        if argv is None and step == "server":
            argv = owner.argv
        if argv is None and step == "image":
            argv = owner.image_inspect_argv
        if argv is None and step == "create":
            argv = owner.create_argv
        if argv is None and step == "recovery_query":
            argv = owner.query_argv
        if argv is None and step == "recovery_inspect":
            argv = owner.inspect_argv
        if argv is None and step == "resource_recovery_query":
            argv = owner.query_argv
        if argv is None and step == "resource_recovery_inspect":
            argv = owner.inspect_argv
        if argv is None and step == "gate_ready":
            argv = owner.ready_argv
        if argv is None and step == "mount_proof":
            argv = owner.mount_proof_argv
        assert argv is not None
        if command_id is None:
            command_id = getattr(owner, f"{step}_command_id", None)
        if command_id is None and step == "recovery_query":
            command_id = owner.query_command_id
        if command_id is None and step == "recovery_inspect":
            command_id = owner.inspect_command_id
        if command_id is None and step == "resource_recovery_query":
            command_id = owner.query_command_id
        if command_id is None and step == "resource_recovery_inspect":
            command_id = owner.inspect_command_id
        if command_id is None:
            command_id = container_command_id(attempt_id, step)
        if event_sequence is None:
            event_sequence = getattr(owner, "minimum_event_sequence", 1)
        if started_monotonic_ns is None:
            started_monotonic_ns = getattr(
                owner,
                "not_before_monotonic_ns",
                1_000_000_000,
            )
        if finished_monotonic_ns is None:
            finished_monotonic_ns = started_monotonic_ns + 1
        if output_capture is None:
            output_capture = ContainerOutputCapture(
                stdout=self._stream_capture(stdout),
                stderr=self._stream_capture(stderr),
            )
        return ContainerCommandResult(
            attempt_id=attempt_id,
            command_id=command_id,
            event_sequence=event_sequence,
            step=step,
            argv=argv,
            outcome=outcome,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            output_capture=output_capture,
            started_monotonic_ns=started_monotonic_ns,
            finished_monotonic_ns=finished_monotonic_ns,
            workspace_roots=workspace_roots
            or (self.workspace.resolve(), self.readable.resolve()),
            workspace_roots_revision=workspace_roots_revision,
        )

    @staticmethod
    def _stream_capture(
        text: str,
        *,
        observed_bytes: int | None = None,
        captured_bytes: int | None = None,
        dropped_bytes: int = 0,
        truncated: bool = False,
        text_sha256: str | None = None,
    ) -> ContainerStreamCapture:
        encoded = text.encode("utf-8")
        captured = len(encoded) if captured_bytes is None else captured_bytes
        observed = (
            captured + dropped_bytes
            if observed_bytes is None
            else observed_bytes
        )
        return ContainerStreamCapture(
            observed,
            captured,
            dropped_bytes,
            truncated,
            text_sha256 or hashlib.sha256(encoded).hexdigest(),
        )

    def _engine(self, *, gate_image: str = IMAGE):
        plan = self._probe_plan(gate_image=gate_image)
        payload = {
            "Client": {
                "Version": "27.1.0",
                "ApiVersion": "1.46",
                "Os": "darwin",
                "Arch": "arm64",
            },
            "Server": {
                "Version": "27.1.0",
                "ApiVersion": "1.46",
                "Os": "linux",
                "Arch": "arm64",
            },
        }
        parsed = parse_container_probe_result(
            plan,
            self._result(plan, "server", stdout=json.dumps(payload)),
        )
        self.assertEqual(parsed.reason_code, "engine_ready")
        assert parsed.identity is not None
        return parsed.identity

    def _draft(
        self,
        *,
        profile: str = "workspace-write",
        network_policy: str = "deny",
        readable_roots: tuple[Path, ...] | None = None,
        writable_roots: tuple[Path, ...] | None = None,
        command_argv: tuple[str, ...] = ("/usr/bin/python3", "-V"),
    ):
        readable = (self.readable,) if readable_roots is None else readable_roots
        if writable_roots is None:
            writable_roots = (self.workspace,) if profile == "workspace-write" else ()
        request = IsolationRequest(
            mode="required",
            profile=profile,
            backend="container",
            network_policy=network_policy,
            workspace=self.workspace,
            readable_roots=readable,
            writable_roots=writable_roots,
        )
        return build_container_execution_draft(
            self._engine(),
            request,
            attempt_id=ATTEMPT_ID,
            working_directory=self.workspace,
            command_argv=command_argv,
            user_id=os.getuid(),
            group_id=os.getgid(),
        )

    def _plan(self, **kwargs):
        draft = self._draft(**kwargs)
        payload = {
            "id": f"sha256:{IMAGE_ID}",
            "repo_digests": [IMAGE],
            "config_env": ["PATH=/usr/bin:/bin"],
            "labels": {GATE_PROTOCOL_LABEL: GATE_PROTOCOL},
            "volumes": None,
        }
        parsed = parse_container_image_result(
            draft,
            self._result(draft, "image", stdout=json.dumps(payload)),
        )
        self.assertEqual(parsed.reason_code, "image_verified")
        assert parsed.plan is not None
        return parsed.plan

    def _inspect_payload(
        self,
        plan,
        *,
        state: str = "running",
        running: bool = True,
        exit_code: int = 0,
    ) -> dict[str, object]:
        environment = dict(item.split("=", 1) for item in plan.base_environment)
        host_mounts = []
        mounts = []
        for mount in plan.mounts:
            host_mount = {
                "Type": "bind",
                "Source": str(mount.source),
                "Target": str(mount.destination),
                "BindOptions": {
                    "Propagation": "rprivate",
                    "NonRecursive": True,
                },
            }
            if not mount.writable:
                host_mount["ReadOnly"] = True
            host_mounts.append(host_mount)
            mounts.append(
                {
                    "Type": "bind",
                    "Source": str(mount.source),
                    "Destination": str(mount.destination),
                    "RW": mount.writable,
                    "Propagation": "rprivate",
                }
            )
        return {
            "id": CONTAINER_ID,
            "name": f"/{plan.instance_name}",
            "instance_label": plan.attempt_id,
            "resource_label": CONTAINER_EXECUTION_RESOURCE,
            "config_image": plan.runtime_image,
            "image_id": f"sha256:{plan.image_id}",
            "config_user": f"{plan.user_id}:{plan.group_id}",
            "config_env": [
                f"{name}={value}" for name, value in sorted(environment.items())
            ],
            "entrypoint": [GATE_ENTRYPOINT],
            "cmd": list(plan.gate_command_argv),
            "path": GATE_ENTRYPOINT,
            "args": list(plan.gate_command_argv),
            "healthcheck": None,
            "stop_signal": "SIGTERM",
            "working_dir": str(plan.working_directory),
            "readonly_rootfs": True,
            "network_mode": (
                "none" if plan.request.network_policy == "deny" else "bridge"
            ),
            "pid_mode": "",
            "ipc_mode": "private",
            "uts_mode": "",
            "cgroupns_mode": "private",
            "cap_add": None,
            "cap_drop": ["ALL"],
            "devices": None,
            "device_requests": None,
            "volumes_from": None,
            "security_opt": [
                "no-new-privileges=true",
                "seccomp=builtin",
            ],
            "privileged": False,
            "restart_policy": {"Name": "no", "MaximumRetryCount": 0},
            "log_config": {
                "Type": CONTAINER_LOG_DRIVER,
                "Config": dict(CONTAINER_LOG_OPTIONS),
            },
            "init": False,
            "pids_limit": CONTAINER_PIDS_LIMIT,
            "memory": CONTAINER_MEMORY_BYTES,
            "tmpfs": {"/tmp": "rw,nosuid,nodev"},
            "host_mounts": host_mounts,
            "state_status": state,
            "state_running": running,
            "state_exit_code": exit_code,
            "state_oom_killed": False,
            "state_error": "",
            "mounts": mounts,
        }

    def _staging_binding(self, plan=None) -> ContainerStagingContainerBinding:
        plan = plan or self._plan()
        identity = plan.identity
        return ContainerStagingContainerBinding(
            instance_name=plan.instance_name,
            prep_instance_name=f"{plan.instance_name}-prep",
            volume_names=tuple(
                f"{plan.instance_name}-root-{ordinal:04d}"
                for ordinal in range(len(plan.mounts))
            ),
            runtime_image=plan.runtime_image,
            executable=identity.executable,
            executable_sha256=identity.executable_identity.sha256,
            socket_path=identity.endpoint.socket_path,
            socket_identity=identity.endpoint.socket_identity,
            client_config_directory=identity.endpoint.client_config_directory,
            client_config_identity=identity.endpoint.client_config_identity,
            gate_image_reference=identity.gate_image.reference,
            gate_image_digest=identity.gate_image.digest,
        )

    def _created(self, plan=None):
        plan = plan or self._plan()
        parsed = parse_container_create_result(
            plan,
            self._result(plan, "create", stdout=CONTAINER_ID),
        )
        self.assertEqual(parsed.reason_code, "container_created")
        assert parsed.created is not None
        return parsed.created

    def _started(self, plan=None):
        created = self._created(plan)
        parsed = parse_container_start_result(
            created,
            self._result(created, "start", stdout=CONTAINER_ID),
        )
        self.assertEqual(parsed.reason_code, "gate_started")
        assert parsed.started is not None
        return parsed.started

    def _verified(self, plan=None):
        mounted = self._mounted(plan)
        parsed = parse_container_inspect_result(
            mounted,
            self._result(
                mounted,
                "inspect",
                stdout=json.dumps(self._inspect_payload(mounted.plan)),
            ),
        )
        self.assertEqual(parsed.reason_code, "inspect_applied")
        assert parsed.verified is not None
        return parsed.verified

    def _ready(self, plan=None):
        started = self._started(plan)
        parsed = parse_container_gate_ready_result(
            started,
            self._result(started, "gate_ready"),
        )
        self.assertEqual(parsed.reason_code, "gate_ready")
        assert parsed.ready is not None
        return parsed.ready

    def _mounted(self, plan=None):
        ready = self._ready(plan)
        output = "".join(
            f"{mount.source_identity.device}:{mount.source_identity.inode}\n"
            for mount in ready.plan.mounts
        )
        parsed = parse_container_mount_proof_result(
            ready,
            self._result(
                ready,
                "mount_proof",
                stdout=output,
            ),
        )
        self.assertEqual(
            parsed.reason_code,
            "mount_object_identity_verified",
        )
        assert parsed.mounted is not None
        return parsed.mounted

    def _completed_execution(self, plan=None, *, exit_code: int = 0):
        verified = self._verified(plan)
        released = parse_container_release_result(
            verified,
            self._result(verified, "release"),
        ).released
        assert released is not None
        waited = parse_container_wait_result(
            released,
            self._result(released, "wait", stdout=str(exit_code)),
        ).waited
        assert waited is not None
        final = parse_container_final_inspect_result(
            waited,
            self._result(
                waited,
                "final_inspect",
                stdout=json.dumps(
                    self._inspect_payload(
                        verified.plan,
                        state="exited",
                        running=False,
                        exit_code=exit_code,
                    )
                ),
            ),
        ).exited
        assert final is not None
        return final

    def test_probe_requires_explicit_digest_socket_and_empty_config(self) -> None:
        plan = self._probe_plan()
        self.assertEqual(plan.argv[0], str(self.executable.resolve()))
        self.assertIn("--config", plan.argv)
        self.assertIn("--host", plan.argv)
        self.assertIn(f"unix://{self.socket_path.resolve()}", plan.argv)
        self.assertNotIn("context", plan.argv)
        self.assertNotIn("DOCKER_HOST", " ".join(plan.argv))

        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            build_docker_server_probe(
                attempt_id=ATTEMPT_ID,
                workspace_roots=(self.workspace, self.readable),
                workspace_roots_revision=ROOTS_REVISION,
                executable=self.executable,
                executable_sha256="0" * 64,
                socket_path=self.socket_path,
                client_config_directory=self.config_dir,
                gate_image=IMAGE,
            )

    def test_probe_binds_workspace_root_revision_without_tracking_contents(self) -> None:
        plan = self._probe_plan()
        payload = {
            "Client": {
                "Version": "27.1.0",
                "ApiVersion": "1.46",
                "Os": "darwin",
                "Arch": "arm64",
            },
            "Server": {
                "Version": "27.1.0",
                "ApiVersion": "1.46",
                "Os": "linux",
                "Arch": "arm64",
            },
        }
        changed = parse_container_probe_result(
            plan,
            self._result(
                plan,
                "server",
                stdout=json.dumps(payload),
                workspace_roots_revision=ROOTS_REVISION + 1,
            ),
        )
        self.assertEqual(changed.reason_code, "probe_workspace_authority_changed")

        (self.workspace / "ordinary-change.txt").write_text(
            "allowed",
            encoding="utf-8",
        )
        current = parse_container_probe_result(
            plan,
            self._result(plan, "server", stdout=json.dumps(payload)),
        )
        self.assertEqual(current.reason_code, "engine_ready")

        (self.config_dir / "config.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must be empty"):
            self._probe_plan()

    def test_probe_rejects_workspace_cli_and_non_socket_endpoint(self) -> None:
        workspace_cli = self.workspace / "docker"
        workspace_cli.write_bytes(self.executable.read_bytes())
        workspace_cli.chmod(0o755)
        digest = hashlib.sha256(workspace_cli.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "must not be inside"):
            build_docker_server_probe(
                attempt_id=ATTEMPT_ID,
                workspace_roots=(self.workspace, self.readable),
                workspace_roots_revision=ROOTS_REVISION,
                executable=workspace_cli,
                executable_sha256=digest,
                socket_path=self.socket_path,
                client_config_directory=self.config_dir,
                gate_image=IMAGE,
            )

        regular = self.root / "trusted" / "not-a-socket"
        regular.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unix socket"):
            build_docker_server_probe(
                attempt_id=ATTEMPT_ID,
                workspace_roots=(self.workspace, self.readable),
                workspace_roots_revision=ROOTS_REVISION,
                executable=self.executable,
                executable_sha256=self.executable_sha256,
                socket_path=regular,
                client_config_directory=self.config_dir,
                gate_image=IMAGE,
            )

    def test_probe_never_claims_backend_capability_and_rejects_old_api(self) -> None:
        plan = self._probe_plan()
        old = {
            "Client": {"Version": "23", "ApiVersion": "1.42"},
            "Server": {
                "Version": "23",
                "ApiVersion": "1.42",
                "Os": "linux",
                "Arch": "amd64",
            },
        }
        parsed = parse_container_probe_result(
            plan,
            self._result(plan, "server", stdout=json.dumps(old)),
        )
        self.assertEqual(parsed.reason_code, "probe_api_unsupported")
        self.assertIsNone(parsed.identity)
        self.assertNotIn("capability", vars(parsed))

    def test_probe_correlation_timeout_and_identity_change_fail_closed(self) -> None:
        plan = self._probe_plan()
        mismatch = parse_container_probe_result(
            plan,
            self._result(
                plan,
                "server",
                attempt_id="2" * 32,
                stdout="{}",
            ),
        )
        self.assertEqual(mismatch.reason_code, "probe_correlation_mismatch")

        timed_out = parse_container_probe_result(
            plan,
            self._result(
                plan,
                "server",
                outcome="timed_out",
                exit_code=None,
            ),
        )
        self.assertEqual(timed_out.reason_code, "probe_timed_out")

        self.executable.write_bytes(b"#!/bin/sh\nexit 1\n")
        changed = parse_container_probe_result(
            plan,
            self._result(plan, "server", stdout="{}"),
        )
        self.assertEqual(changed.reason_code, "probe_executable_changed")

    def test_control_plane_stderr_fails_closed_and_create_keeps_recovery(
        self,
    ) -> None:
        probe = self._probe_plan()
        probe_payload = {
            "Client": {
                "Version": "27.1.0",
                "ApiVersion": "1.46",
                "Os": "darwin",
                "Arch": "arm64",
            },
            "Server": {
                "Version": "27.1.0",
                "ApiVersion": "1.46",
                "Os": "linux",
                "Arch": "arm64",
            },
        }
        probed = parse_container_probe_result(
            probe,
            self._result(
                probe,
                "server",
                stdout=json.dumps(probe_payload),
                stderr="warning",
            ),
        )
        self.assertEqual(probed.reason_code, "probe_unexpected_stderr")

        draft = self._draft()
        image_payload = {
            "id": f"sha256:{IMAGE_ID}",
            "repo_digests": [IMAGE],
            "config_env": ["PATH=/usr/bin:/bin"],
            "labels": {GATE_PROTOCOL_LABEL: GATE_PROTOCOL},
            "volumes": None,
        }
        image = parse_container_image_result(
            draft,
            self._result(
                draft,
                "image",
                stdout=json.dumps(image_payload),
                stderr="warning",
            ),
        )
        self.assertEqual(image.reason_code, "image_unexpected_stderr")

        plan = self._plan()
        created_result = parse_container_create_result(
            plan,
            self._result(
                plan,
                "create",
                stdout=CONTAINER_ID,
                stderr="warning",
            ),
        )
        self.assertEqual(created_result.reason_code, "create_unexpected_stderr")
        self.assertIsNotNone(created_result.recovery)

        created = self._created(plan)
        started_result = parse_container_start_result(
            created,
            self._result(created, "start", stderr="warning"),
        )
        self.assertEqual(started_result.reason_code, "start_unexpected_stderr")

        mounted = self._mounted(plan)
        inspected = parse_container_inspect_result(
            mounted,
            self._result(
                mounted,
                "inspect",
                stdout=json.dumps(self._inspect_payload(plan)),
                stderr="warning",
            ),
        )
        self.assertEqual(inspected.reason_code, "inspect_unexpected_stderr")

        verified = self._verified(plan)
        released_result = parse_container_release_result(
            verified,
            self._result(verified, "release", stderr="warning"),
        )
        self.assertEqual(released_result.reason_code, "release_unexpected_stderr")

        released = parse_container_release_result(
            verified,
            self._result(verified, "release"),
        ).released
        assert released is not None
        waited_result = parse_container_wait_result(
            released,
            self._result(
                released,
                "wait",
                stdout="0",
                stderr="warning",
            ),
        )
        self.assertEqual(waited_result.reason_code, "wait_unexpected_stderr")

        waited = parse_container_wait_result(
            released,
            self._result(released, "wait", stdout="0"),
        ).waited
        assert waited is not None
        final = parse_container_final_inspect_result(
            waited,
            self._result(
                waited,
                "final_inspect",
                stdout=json.dumps(
                    self._inspect_payload(
                        plan,
                        state="exited",
                        running=False,
                    )
                ),
                stderr="warning",
            ),
        )
        self.assertEqual(final.reason_code, "final_inspect_unexpected_stderr")

    def test_podman_is_explicitly_unsupported(self) -> None:
        parsed = unsupported_podman_probe_result()
        self.assertEqual(
            parsed.reason_code,
            "podman_host_path_authority_unproven",
        )
        self.assertIsNone(parsed.identity)

    def test_image_requires_exact_digest_gate_and_no_declared_volumes(self) -> None:
        draft = self._draft()
        template = draft.image_inspect_argv[-2]
        self.assertIn('{{json (index .Config "Volumes")}}', template)
        self.assertNotIn("{{json .Config.Volumes}}", template)
        baseline = {
            "id": f"sha256:{IMAGE_ID}",
            "repo_digests": [IMAGE],
            "config_env": ["PATH=/usr/bin:/bin"],
            "labels": {GATE_PROTOCOL_LABEL: GATE_PROTOCOL},
            "volumes": None,
        }
        cases = (
            ({"repo_digests": [f"other@sha256:{IMAGE_DIGEST}"]}, "image_digest_mismatch"),
            ({"labels": {}}, "image_gate_protocol_mismatch"),
            ({"volumes": {"/data": {}}}, "image_declares_volumes"),
            ({"config_env": ["A=1", "A=2"]}, "image_environment_invalid"),
            (
                {"config_env": ["DashScope_Api_Key=image-secret"]},
                "image_environment_contains_provider_credential",
            ),
            (
                {"config_env": ["LD_PRELOAD=/workspace/inject.so"]},
                "image_environment_unsafe",
            ),
        )
        for replacement, expected in cases:
            with self.subTest(expected=expected):
                payload = dict(baseline)
                payload.update(replacement)
                parsed = parse_container_image_result(
                    draft,
                    self._result(draft, "image", stdout=json.dumps(payload)),
                )
                self.assertEqual(parsed.reason_code, expected)
                self.assertIsNone(parsed.plan)

    def test_gate_image_is_backend_authority_and_supports_exact_local_id(
        self,
    ) -> None:
        local_reference = f"sha256:{IMAGE_ID}"
        identity = self._engine(gate_image=local_reference)
        request = IsolationRequest(
            mode="required",
            profile="read-only",
            backend="container",
            network_policy="deny",
            workspace=self.workspace,
            readable_roots=(self.readable,),
        )
        draft = build_container_execution_draft(
            identity,
            request,
            attempt_id=ATTEMPT_ID,
            working_directory=self.workspace,
            command_argv=("/bin/true",),
            user_id=os.getuid(),
            group_id=os.getgid(),
        )
        self.assertEqual(draft.image, local_reference)
        self.assertEqual(draft.image_digest, local_reference)
        payload = {
            "id": local_reference,
            "repo_digests": [],
            "config_env": ["PATH=/usr/bin:/bin"],
            "labels": {GATE_PROTOCOL_LABEL: GATE_PROTOCOL},
            "volumes": None,
        }
        parsed = parse_container_image_result(
            draft,
            self._result(draft, "image", stdout=json.dumps(payload)),
        )
        self.assertEqual(parsed.reason_code, "image_verified")
        assert parsed.plan is not None
        self.assertIn(local_reference, parsed.plan.create_argv)
        self.assertNotIn(IMAGE, parsed.plan.create_argv)

        with self.assertRaises(TypeError):
            build_container_execution_draft(
                identity,
                request,
                image=IMAGE,  # type: ignore[call-arg]
                attempt_id=ATTEMPT_ID,
                working_directory=self.workspace,
                command_argv=("/bin/true",),
                user_id=os.getuid(),
                group_id=os.getgid(),
            )

    def test_create_uses_gate_nonrecursive_mounts_and_explicit_limits(self) -> None:
        plan = self._plan(command_argv=("/bin/sh", "-lc", "printf ok"))
        argv = plan.create_argv
        self.assertIn(f"--memory={CONTAINER_MEMORY_BYTES}", argv)
        self.assertIn(f"--pids-limit={CONTAINER_PIDS_LIMIT}", argv)
        self.assertIn("--init=false", argv)
        self.assertIn("--pull=never", argv)
        self.assertIn("--log-driver=local", argv)
        self.assertIn("--log-opt=max-size=4m", argv)
        self.assertIn("--security-opt=seccomp=builtin", argv)
        self.assertIn("--network", argv)
        self.assertIn("none", argv)
        self.assertIn(GATE_ENTRYPOINT, argv)
        self.assertEqual(
            plan.gate_command_argv,
            (
                *GATE_COMMAND_PREFIX,
                "--attempt-id",
                ATTEMPT_ID,
                "--",
                "/bin/sh",
                "-lc",
                "printf ok",
            ),
        )
        mount_args = [argv[index + 1] for index, item in enumerate(argv) if item == "--mount"]
        self.assertTrue(mount_args)
        self.assertTrue(all("bind-recursive=disabled" in item for item in mount_args))
        self.assertTrue(all("bind-propagation=rprivate" in item for item in mount_args))
        self.assertIn("--config", argv)
        self.assertIn("--host", argv)

    def test_start_gate_does_not_expose_proof_until_running_inspect(self) -> None:
        created = self._created()
        self.assertNotIn("proof", vars(created))
        started_result = parse_container_start_result(
            created,
            self._result(created, "start", stdout=CONTAINER_ID),
        )
        assert started_result.started is not None
        self.assertNotIn("proof", vars(started_result.started))
        self.assertEqual(
            started_result.started.ready_argv[-7:],
            (
                GATE_READY_CHECK,
                "--attempt-id",
                ATTEMPT_ID,
                "--attempts",
                str(GATE_READY_ATTEMPTS),
                "--delay-seconds",
                GATE_READY_DELAY_SECONDS,
            ),
        )
        self.assertEqual(
            started_result.started.ready_timeout_seconds,
            GATE_READY_TIMEOUT_SECONDS,
        )
        ready_result = parse_container_gate_ready_result(
            started_result.started,
            self._result(started_result.started, "gate_ready"),
        )
        assert ready_result.ready is not None
        self.assertNotIn("proof", vars(ready_result.ready))
        mount_output = "".join(
            f"{mount.source_identity.device}:{mount.source_identity.inode}\n"
            for mount in created.plan.mounts
        )
        mounted_result = parse_container_mount_proof_result(
            ready_result.ready,
            self._result(
                ready_result.ready,
                "mount_proof",
                stdout=mount_output,
            ),
        )
        assert mounted_result.mounted is not None
        self.assertNotIn("proof", vars(mounted_result.mounted))

        created_payload = self._inspect_payload(
            created.plan,
            state="created",
            running=False,
        )
        rejected = parse_container_inspect_result(
            mounted_result.mounted,
            self._result(
                mounted_result.mounted,
                "inspect",
                stdout=json.dumps(created_payload),
            ),
        )
        self.assertEqual(rejected.reason_code, "inspect_state_mismatch")
        self.assertIsNone(rejected.verified)

        applied = parse_container_inspect_result(
            mounted_result.mounted,
            self._result(
                mounted_result.mounted,
                "inspect",
                stdout=json.dumps(self._inspect_payload(created.plan)),
            ),
        )
        self.assertEqual(applied.reason_code, "inspect_applied")
        assert applied.verified is not None
        self.assertTrue(applied.verified.proof.event_payload()["applied"])
        self.assertIn("--signal=SIGUSR1", applied.verified.release_argv)

    def test_mount_object_identity_rejects_source_path_aba(self) -> None:
        ready = self._ready()
        original = self.root / "workspace-original"
        replacement = self.root / "workspace-replacement"
        self.workspace.rename(original)
        self.workspace.mkdir()
        replacement_identity = self.workspace.stat()
        replacement_pair = (
            replacement_identity.st_dev,
            replacement_identity.st_ino,
        )
        self.workspace.rename(replacement)
        original.rename(self.workspace)

        observed = []
        for mount in ready.plan.mounts:
            if mount.source == self.workspace.resolve():
                observed.append(replacement_pair)
            else:
                observed.append(
                    (mount.source_identity.device, mount.source_identity.inode)
                )
        output = "".join(f"{device}:{inode}\n" for device, inode in observed)
        parsed = parse_container_mount_proof_result(
            ready,
            self._result(ready, "mount_proof", stdout=output),
        )
        self.assertEqual(parsed.reason_code, "mount_object_identity_mismatch")
        self.assertIsNone(parsed.mounted)

    def test_source_path_aba_rejects_matching_mounted_identity(self) -> None:
        ready = self._ready()
        moved = self.root / "workspace-moved"
        self.workspace.rename(moved)
        moved.rename(self.workspace)

        output = "".join(
            f"{mount.source_identity.device}:{mount.source_identity.inode}\n"
            for mount in ready.plan.mounts
        )
        parsed = parse_container_mount_proof_result(
            ready,
            self._result(ready, "mount_proof", stdout=output),
        )

        self.assertEqual(parsed.reason_code, "mount_source_identity_changed")
        self.assertIsNone(parsed.mounted)

    def test_running_inspect_rejects_environment_command_mount_and_limits(self) -> None:
        ready = self._mounted()
        cases = (
            ("environment", "config_env", ["HTTPS_PROXY=http://secret"], "inspect_environment_mismatch"),
            ("entrypoint", "entrypoint", ["/bin/evil"], "inspect_command_mismatch"),
            ("network", "network_mode", "host", "inspect_network_mismatch"),
            ("pids", "pids_limit", 999, "inspect_pids_mismatch"),
            ("memory", "memory", 1, "inspect_memory_mismatch"),
            ("init", "init", True, "inspect_init_mismatch"),
            (
                "seccomp",
                "security_opt",
                ["no-new-privileges:true", "seccomp=unconfined"],
                "inspect_security_mismatch",
            ),
            (
                "logging",
                "log_config",
                {"Type": "fluentd", "Config": {}},
                "inspect_logging_mismatch",
            ),
        )
        for name, key, value, expected in cases:
            with self.subTest(name=name):
                payload = self._inspect_payload(ready.plan)
                payload[key] = value
                parsed = parse_container_inspect_result(
                    ready,
                    self._result(
                        ready,
                        "inspect",
                        stdout=json.dumps(payload),
                    ),
                )
                self.assertEqual(parsed.reason_code, expected)

        payload = self._inspect_payload(ready.plan)
        host_mounts = payload["host_mounts"]
        assert isinstance(host_mounts, list)
        host_mounts[0]["BindOptions"]["NonRecursive"] = False
        parsed = parse_container_inspect_result(
            ready,
            self._result(ready, "inspect", stdout=json.dumps(payload)),
        )
        self.assertEqual(parsed.reason_code, "inspect_host_mounts_mismatch")

    def test_running_inspect_rejects_source_swap_before_release(self) -> None:
        ready = self._ready()
        moved = self.root / "workspace-moved"
        self.workspace.rename(moved)
        self.workspace.mkdir()
        output = "".join(
            f"{mount.source_identity.device}:{mount.source_identity.inode}\n"
            for mount in ready.plan.mounts
        )
        parsed = parse_container_mount_proof_result(
            ready,
            self._result(ready, "mount_proof", stdout=output),
        )
        self.assertEqual(
            parsed.reason_code,
            "mount_proof_workspace_authority_changed",
        )

    def test_release_wait_final_inspect_and_logs_are_correlated(self) -> None:
        verified = self._verified()
        released_result = parse_container_release_result(
            verified,
            self._result(verified, "release", stdout=CONTAINER_ID),
        )
        self.assertEqual(released_result.reason_code, "gate_released")
        assert released_result.released is not None

        waited_result = parse_container_wait_result(
            released_result.released,
            self._result(released_result.released, "wait", stdout="7\n"),
        )
        self.assertEqual(waited_result.reason_code, "container_waited")
        assert waited_result.waited is not None
        self.assertEqual(waited_result.waited.command_exit_code, 7)

        final_payload = self._inspect_payload(
            verified.plan,
            state="exited",
            running=False,
            exit_code=7,
        )
        final_result = parse_container_final_inspect_result(
            waited_result.waited,
            self._result(
                waited_result.waited,
                "final_inspect",
                stdout=json.dumps(final_payload),
            ),
        )
        self.assertEqual(final_result.reason_code, "final_state_verified")
        assert final_result.exited is not None

        logs = parse_container_logs_result(
            final_result.exited,
            self._result(
                final_result.exited,
                "logs",
                stdout="hello\n",
                stderr="warning\n",
            ),
        )
        self.assertEqual(logs.reason_code, "logs_captured")
        assert logs.captured is not None
        self.assertEqual(logs.captured.stdout, "hello\n")
        self.assertEqual(logs.captured.stderr, "warning\n")
        self.assertEqual(logs.captured.command_exit_code, 7)

    def test_control_plane_requires_complete_capture_but_logs_retain_truncation(
        self,
    ) -> None:
        draft = self._draft()
        payload = json.dumps(
            {
                "id": f"sha256:{IMAGE_ID}",
                "repo_digests": [IMAGE],
                "config_env": ["PATH=/usr/bin:/bin"],
                "labels": {GATE_PROTOCOL_LABEL: GATE_PROTOCOL},
                "volumes": None,
            }
        )
        payload_bytes = len(payload.encode("utf-8"))
        incomplete_payload = ContainerOutputCapture(
            stdout=self._stream_capture(
                payload,
                observed_bytes=payload_bytes + 10,
                captured_bytes=payload_bytes,
                dropped_bytes=10,
                truncated=True,
            ),
            stderr=self._stream_capture(""),
        )
        image = parse_container_image_result(
            draft,
            self._result(
                draft,
                "image",
                stdout=payload,
                output_capture=incomplete_payload,
            ),
        )
        self.assertEqual(image.reason_code, "image_output_incomplete")
        self.assertIsNone(image.plan)

        exited = self._completed_execution()
        truncated_logs = ContainerOutputCapture(
            stdout=self._stream_capture(
                "head",
                observed_bytes=8,
                captured_bytes=4,
                dropped_bytes=4,
                truncated=True,
            ),
            stderr=self._stream_capture(""),
        )
        logs = parse_container_logs_result(
            exited,
            self._result(
                exited,
                "logs",
                stdout="head",
                output_capture=truncated_logs,
            ),
        )
        self.assertEqual(logs.reason_code, "logs_captured")
        assert logs.captured is not None
        self.assertTrue(logs.captured.output_capture.stdout.truncated)
        self.assertEqual(logs.captured.output_capture, truncated_logs)

    def test_command_result_binds_capture_provenance_to_exact_text(self) -> None:
        plan = self._plan()
        empty_with_captured_bytes = ContainerOutputCapture(
            stdout=self._stream_capture(
                "",
                observed_bytes=1,
                captured_bytes=1,
            ),
            stderr=self._stream_capture(""),
        )
        with self.assertRaisesRegex(ValueError, "captured-byte presence"):
            self._result(
                plan,
                "create",
                output_capture=empty_with_captured_bytes,
            )

        wrong_digest = ContainerOutputCapture(
            stdout=self._stream_capture("x", text_sha256="0" * 64),
            stderr=self._stream_capture(""),
        )
        with self.assertRaisesRegex(ValueError, "text digest"):
            self._result(
                plan,
                "create",
                stdout="x",
                output_capture=wrong_digest,
            )

        wrong_complete_size = ContainerOutputCapture(
            stdout=self._stream_capture(
                "xx",
                observed_bytes=1,
                captured_bytes=1,
            ),
            stderr=self._stream_capture(""),
        )
        with self.assertRaisesRegex(ValueError, "captured-byte count"):
            self._result(
                plan,
                "create",
                stdout="xx",
                output_capture=wrong_complete_size,
            )

    def test_wait_and_final_state_mismatch_fail_closed(self) -> None:
        verified = self._verified()
        released = parse_container_release_result(
            verified,
            self._result(verified, "release"),
        ).released
        assert released is not None
        invalid_wait = parse_container_wait_result(
            released,
            self._result(released, "wait", stdout="not-an-exit"),
        )
        self.assertEqual(invalid_wait.reason_code, "wait_invalid_exit")

        waited = parse_container_wait_result(
            released,
            self._result(released, "wait", stdout="0"),
        ).waited
        assert waited is not None
        payload = self._inspect_payload(
            verified.plan,
            state="exited",
            running=False,
            exit_code=1,
        )
        mismatch = parse_container_final_inspect_result(
            waited,
            self._result(waited, "final_inspect", stdout=json.dumps(payload)),
        )
        self.assertEqual(mismatch.reason_code, "final_state_mismatch")

    def test_durable_recovery_never_treats_empty_query_as_absence(self) -> None:
        execution = self._plan()
        plan = build_durable_container_recovery_plan(
            self._staging_binding(execution),
            execution.identity,
        )

        for ordinal in range(1, RECOVERY_QUERY_ATTEMPT_LIMIT + 1):
            self.assertEqual(plan.query_ordinal, ordinal)
            parsed = parse_durable_recovery_query_result(
                plan,
                self._result(plan, "resource_recovery_query"),
            )
            if ordinal < RECOVERY_QUERY_ATTEMPT_LIMIT:
                self.assertIsNotNone(parsed.retry)
                self.assertFalse(parsed.unresolved)
                assert parsed.retry is not None
                self.assertNotEqual(
                    plan.query_command_id,
                    parsed.retry.query_command_id,
                )
                self.assertGreater(
                    parsed.retry.not_before_monotonic_ns,
                    plan.not_before_monotonic_ns,
                )
                plan = parsed.retry
            else:
                self.assertTrue(parsed.unresolved)
                self.assertIsNone(parsed.retry)
                self.assertEqual(
                    parsed.reason_code,
                    "staging_recovery_absence_unverified_exhausted",
                )

    def test_durable_recovery_requires_one_exact_owned_candidate(self) -> None:
        execution = self._plan()
        plan = build_durable_container_recovery_plan(
            self._staging_binding(execution),
            execution.identity,
        )
        ambiguous = parse_durable_recovery_query_result(
            plan,
            self._result(
                plan,
                "resource_recovery_query",
                stdout=(
                    f"{json.dumps(CONTAINER_ID)}\n"
                    f"{json.dumps('d' * 64)}\n"
                ),
            ),
        )
        self.assertTrue(ambiguous.unresolved)
        self.assertEqual(
            ambiguous.reason_code,
            "staging_recovery_query_ambiguous",
        )

        observed = parse_durable_recovery_query_result(
            plan,
            self._result(
                plan,
                "resource_recovery_query",
                stdout=f"{json.dumps(CONTAINER_ID)}\n",
            ),
        )
        assert observed.candidate is not None
        candidate = observed.candidate
        wrong_owner = parse_durable_recovery_inspect_result(
            candidate,
            self._result(
                candidate,
                "resource_recovery_inspect",
                stdout=json.dumps(
                    {
                        "id": CONTAINER_ID,
                        "name": "/lca-not-this-attempt",
                        "instance_label": ATTEMPT_ID,
                        "resource_label": CONTAINER_EXECUTION_RESOURCE,
                        "config_image": execution.runtime_image,
                        "image_id": execution.runtime_image,
                    }
                ),
            ),
        )
        self.assertTrue(wrong_owner.unresolved)
        self.assertEqual(
            wrong_owner.reason_code,
            "staging_recovery_not_owned",
        )

        owned = parse_durable_recovery_inspect_result(
            candidate,
            self._result(
                candidate,
                "resource_recovery_inspect",
                stdout=json.dumps(
                    {
                        "id": CONTAINER_ID,
                        "name": f"/lca-{ATTEMPT_ID}",
                        "instance_label": ATTEMPT_ID,
                        "resource_label": CONTAINER_EXECUTION_RESOURCE,
                        "config_image": execution.runtime_image,
                        "image_id": execution.runtime_image,
                    }
                ),
            ),
        )
        assert owned.cleanup is not None
        self.assertFalse(owned.unresolved)
        self.assertEqual(owned.cleanup.container_id, CONTAINER_ID)
        self.assertEqual(owned.cleanup.plan.attempt_id, ATTEMPT_ID)

    def test_durable_recovery_rejects_changed_backend_authority(self) -> None:
        execution = self._plan()
        binding = self._staging_binding(execution)
        self.executable.write_bytes(b"#!/bin/sh\nexit 1\n")

        with self.assertRaisesRegex(
            ValueError,
            "authority changed",
        ):
            build_durable_container_recovery_plan(
                binding,
                execution.identity,
            )

    def test_durable_volume_recovery_requires_exact_owned_name(self) -> None:
        execution = self._plan()
        binding = self._staging_binding(execution)
        plan = build_durable_volume_recovery_plan(
            binding,
            execution.identity,
            root_ordinal=0,
        )
        empty = parse_durable_volume_recovery_query_result(
            plan,
            self._result(plan, "resource_recovery_query"),
        )
        self.assertIsNotNone(empty.retry)
        self.assertFalse(empty.absent_verified)

        wrong_name = parse_durable_volume_recovery_query_result(
            plan,
            self._result(
                plan,
                "resource_recovery_query",
                stdout=f"{json.dumps(f'{plan.volume_name}-other')}\n",
            ),
        )
        self.assertTrue(wrong_name.unresolved)
        self.assertEqual(
            wrong_name.reason_code,
            "volume_recovery_query_ambiguous",
        )

        observed = parse_durable_volume_recovery_query_result(
            plan,
            self._result(
                plan,
                "resource_recovery_query",
                stdout=f"{json.dumps(plan.volume_name)}\n",
            ),
        )
        assert observed.candidate is not None
        candidate = observed.candidate
        wrong_owner = parse_durable_volume_recovery_inspect_result(
            candidate,
            self._result(
                candidate,
                "resource_recovery_inspect",
                stdout=json.dumps(
                    {
                        "name": plan.volume_name,
                        "driver": "local",
                        "labels": {
                            "io.local-agent.instance": ATTEMPT_ID,
                            "io.local-agent.resource": "root-9999",
                        },
                        "options": None,
                        "scope": "local",
                    }
                ),
            ),
        )
        self.assertTrue(wrong_owner.unresolved)
        self.assertEqual(
            wrong_owner.reason_code,
            "volume_recovery_not_owned",
        )

        owned = parse_durable_volume_recovery_inspect_result(
            candidate,
            self._result(
                candidate,
                "resource_recovery_inspect",
                stdout=json.dumps(
                    {
                        "name": plan.volume_name,
                        "driver": "local",
                        "labels": {
                            "io.local-agent.instance": ATTEMPT_ID,
                            "io.local-agent.resource": "root-0000",
                        },
                        "options": None,
                        "scope": "local",
                    }
                ),
            ),
        )
        self.assertIsNotNone(owned.cleanup)
        self.assertFalse(owned.unresolved)

    def test_durable_volume_absence_requires_recorded_cleanup_phase(
        self,
    ) -> None:
        execution = self._plan()
        binding = self._staging_binding(execution)
        required_present = build_durable_volume_recovery_plan(
            binding,
            execution.identity,
            root_ordinal=0,
        )
        verified_absent = build_durable_volume_recovery_plan(
            binding,
            execution.identity,
            root_ordinal=0,
            absence_allowed=True,
        )

        unresolved = parse_durable_volume_recovery_query_result(
            required_present,
            self._result(
                required_present,
                "resource_recovery_query",
            ),
        )
        absent = parse_durable_volume_recovery_query_result(
            verified_absent,
            self._result(
                verified_absent,
                "resource_recovery_query",
            ),
        )

        self.assertIsNotNone(unresolved.retry)
        self.assertFalse(unresolved.absent_verified)
        self.assertTrue(absent.absent_verified)
        self.assertFalse(absent.unresolved)

    def test_recovery_is_cleanup_only_and_requires_exact_ownership(self) -> None:
        plan = self._plan()
        create = parse_container_create_result(
            plan,
            self._result(
                plan,
                "create",
                outcome="timed_out",
                exit_code=None,
            ),
        )
        self.assertEqual(create.reason_code, "create_timed_out")
        assert create.recovery is not None
        observed = parse_container_recovery_query_result(
            create.recovery,
            self._result(
                create.recovery,
                "recovery_query",
                stdout=json.dumps(CONTAINER_ID),
            ),
        )
        self.assertEqual(observed.reason_code, "recovery_candidate_observed")
        assert observed.candidate is not None
        disappeared = parse_container_recovery_inspect_result(
            observed.candidate,
            self._result(
                observed.candidate,
                "recovery_inspect",
                exit_code=1,
            ),
        )
        self.assertEqual(
            disappeared.reason_code,
            "recovery_inspect_failed_retry",
        )
        self.assertIsNotNone(disappeared.retry)

        payload = self._inspect_payload(
            plan,
            state="created",
            running=False,
        )
        recovered = parse_container_recovery_inspect_result(
            observed.candidate,
            self._result(
                observed.candidate,
                "recovery_inspect",
                stdout=json.dumps(payload),
            ),
        )
        self.assertEqual(recovered.reason_code, "container_recovered_for_cleanup")
        assert recovered.cleanup is not None
        self.assertNotIn("start_argv", vars(recovered.cleanup))

        payload["readonly_rootfs"] = False
        owned_mismatch = parse_container_recovery_inspect_result(
            observed.candidate,
            self._result(
                observed.candidate,
                "recovery_inspect",
                stdout=json.dumps(payload),
            ),
        )
        self.assertEqual(
            owned_mismatch.reason_code,
            "recovery_owned_rootfs_mismatch",
        )
        self.assertIsNotNone(owned_mismatch.cleanup)

        payload["instance_label"] = "2" * 32
        not_owned = parse_container_recovery_inspect_result(
            observed.candidate,
            self._result(
                observed.candidate,
                "recovery_inspect",
                stdout=json.dumps(payload),
            ),
        )
        self.assertEqual(not_owned.reason_code, "recovery_not_owned")
        self.assertIsNone(not_owned.cleanup)

    def test_ambiguous_create_never_turns_finite_absence_into_cleanup_proof(
        self,
    ) -> None:
        plan = self._plan()
        create = parse_container_create_result(
            plan,
            self._result(
                plan,
                "create",
                outcome="cancelled",
                exit_code=None,
            ),
        )
        assert create.recovery is not None
        obligation = create.recovery
        parsed = None
        for attempt in range(1, RECOVERY_QUERY_ATTEMPT_LIMIT + 1):
            parsed = parse_container_recovery_query_result(
                obligation,
                self._result(obligation, "recovery_query"),
            )
            if attempt < RECOVERY_QUERY_ATTEMPT_LIMIT:
                self.assertTrue(parsed.reason_code.endswith("_retry"))
                assert parsed.retry is not None
                self.assertEqual(parsed.retry.query_ordinal, attempt + 1)
                obligation = parsed.retry
        assert parsed is not None
        self.assertEqual(
            parsed.reason_code,
            "recovery_absence_unverified_exhausted",
        )
        self.assertEqual(parsed.unresolved, obligation)
        self.assertIsNone(parsed.retry)

    def test_recovery_terminal_failures_retain_unresolved_obligation(self) -> None:
        plan = self._plan()
        create = parse_container_create_result(
            plan,
            self._result(
                plan,
                "create",
                outcome="spawn_failed",
                exit_code=None,
            ),
        )
        assert create.recovery is not None
        obligation = create.recovery

        authority_changed = parse_container_recovery_query_result(
            obligation,
            self._result(
                obligation,
                "recovery_query",
                workspace_roots_revision=ROOTS_REVISION + 1,
            ),
        )
        self.assertEqual(
            authority_changed.reason_code,
            "recovery_query_workspace_authority_changed",
        )
        self.assertEqual(authority_changed.unresolved, obligation)
        self.assertIsNone(authority_changed.retry)

        unexpected_stderr = parse_container_recovery_query_result(
            obligation,
            self._result(
                obligation,
                "recovery_query",
                stderr="warning",
            ),
        )
        self.assertEqual(
            unexpected_stderr.reason_code,
            "recovery_query_unexpected_stderr",
        )
        self.assertEqual(unexpected_stderr.unresolved, obligation)
        self.assertIsNone(unexpected_stderr.retry)

        incomplete_empty = ContainerOutputCapture(
            stdout=self._stream_capture(
                "",
                observed_bytes=12,
                captured_bytes=0,
                dropped_bytes=12,
                truncated=True,
            ),
            stderr=self._stream_capture(""),
        )
        incomplete = parse_container_recovery_query_result(
            obligation,
            self._result(
                obligation,
                "recovery_query",
                output_capture=incomplete_empty,
            ),
        )
        self.assertEqual(
            incomplete.reason_code,
            "recovery_query_output_incomplete",
        )
        self.assertEqual(incomplete.unresolved, obligation)

    def test_uncorrelated_create_result_cannot_seed_recovery(self) -> None:
        plan = self._plan()
        wrong_origin = self._result(
            plan,
            "create",
            command_id=container_command_id(ATTEMPT_ID, "server"),
            outcome="timed_out",
            exit_code=None,
        )
        parsed = parse_container_create_result(plan, wrong_origin)
        self.assertEqual(parsed.reason_code, "create_correlation_mismatch")
        self.assertTrue(parsed.unresolved_without_correlation)
        self.assertIsNone(parsed.created)
        self.assertIsNone(parsed.recovery)

        with self.assertRaisesRegex(ValueError, "exact correlated create"):
            build_container_recovery_obligation(plan, wrong_origin)

    def test_recovery_retry_has_unique_command_time_and_event_correlation(
        self,
    ) -> None:
        plan = self._plan()
        create = parse_container_create_result(
            plan,
            self._result(
                plan,
                "create",
                outcome="timed_out",
                exit_code=None,
            ),
        )
        assert create.recovery is not None
        first_result = self._result(create.recovery, "recovery_query")
        first = parse_container_recovery_query_result(
            create.recovery,
            first_result,
        )
        assert first.retry is not None
        self.assertNotEqual(
            create.recovery.query_command_id,
            first.retry.query_command_id,
        )

        replayed = parse_container_recovery_query_result(
            first.retry,
            first_result,
        )
        self.assertEqual(
            replayed.reason_code,
            "recovery_query_correlation_mismatch",
        )
        self.assertEqual(replayed.unresolved, first.retry)

        too_early = parse_container_recovery_query_result(
            first.retry,
            self._result(
                first.retry,
                "recovery_query",
                started_monotonic_ns=first.retry.not_before_monotonic_ns - 1,
                finished_monotonic_ns=first.retry.not_before_monotonic_ns,
            ),
        )
        self.assertEqual(
            too_early.reason_code,
            "recovery_query_retry_too_early",
        )
        self.assertEqual(too_early.unresolved, first.retry)

    def test_recovery_retry_exhaustion_keeps_cleanup_unverified(self) -> None:
        plan = self._plan()
        create = parse_container_create_result(
            plan,
            self._result(
                plan,
                "create",
                outcome="timed_out",
                exit_code=None,
            ),
        )
        assert create.recovery is not None
        obligation = create.recovery
        exhausted = None
        for attempt in range(1, RECOVERY_QUERY_ATTEMPT_LIMIT + 1):
            exhausted = parse_container_recovery_query_result(
                obligation,
                self._result(
                    obligation,
                    "recovery_query",
                    outcome="timed_out",
                    exit_code=None,
                ),
            )
            if attempt < RECOVERY_QUERY_ATTEMPT_LIMIT:
                assert exhausted.retry is not None
                obligation = exhausted.retry
        assert exhausted is not None
        self.assertEqual(
            exhausted.reason_code,
            "recovery_query_timed_out_exhausted",
        )
        self.assertEqual(exhausted.unresolved, obligation)

    def test_recovery_inspect_exhaustion_and_authority_change_are_unresolved(
        self,
    ) -> None:
        plan = self._plan()
        create = parse_container_create_result(
            plan,
            self._result(
                plan,
                "create",
                outcome="cancelled",
                exit_code=None,
            ),
        )
        assert create.recovery is not None
        obligation = create.recovery
        for _ in range(RECOVERY_QUERY_ATTEMPT_LIMIT - 1):
            retry = parse_container_recovery_query_result(
                obligation,
                self._result(
                    obligation,
                    "recovery_query",
                    exit_code=1,
                ),
            )
            assert retry.retry is not None
            obligation = retry.retry
        observed = parse_container_recovery_query_result(
            obligation,
            self._result(
                obligation,
                "recovery_query",
                stdout=json.dumps(CONTAINER_ID),
            ),
        )
        assert observed.candidate is not None

        exhausted = parse_container_recovery_inspect_result(
            observed.candidate,
            self._result(
                observed.candidate,
                "recovery_inspect",
                exit_code=1,
            ),
        )
        self.assertEqual(
            exhausted.reason_code,
            "recovery_inspect_failed_exhausted",
        )
        self.assertEqual(exhausted.unresolved, obligation)
        self.assertIsNone(exhausted.retry)

        authority_changed = parse_container_recovery_inspect_result(
            observed.candidate,
            self._result(
                observed.candidate,
                "recovery_inspect",
                workspace_roots_revision=ROOTS_REVISION + 1,
            ),
        )
        self.assertEqual(
            authority_changed.reason_code,
            "recovery_inspect_workspace_authority_changed",
        )
        self.assertEqual(authority_changed.unresolved, obligation)
        self.assertIsNone(authority_changed.retry)

    def test_cleanup_requires_exact_absence_even_when_remove_fails(self) -> None:
        cleanup = self._created().cleanup
        remove = parse_container_remove_result(
            cleanup,
            self._result(
                cleanup,
                "remove",
                outcome="timed_out",
                exit_code=None,
            ),
        )
        self.assertEqual(remove.reason_code, "remove_timed_out")
        self.assertEqual(remove.removal_check_argv, cleanup.removal_check_argv)

        absent = parse_container_removal_check_result(
            remove,
            self._result(remove, "removal_check", stdout=""),
        )
        self.assertEqual(absent.reason_code, "cleanup_verified_absent")
        self.assertTrue(absent.cleanup_verified)
        self.assertIsNone(absent.unresolved)

        whitespace = parse_container_removal_check_result(
            remove,
            self._result(remove, "removal_check", stdout=" \n"),
        )
        self.assertEqual(whitespace.reason_code, "cleanup_check_invalid_output")
        self.assertFalse(whitespace.cleanup_verified)
        self.assertEqual(whitespace.unresolved, cleanup)

        present = parse_container_removal_check_result(
            remove,
            self._result(
                remove,
                "removal_check",
                stdout=json.dumps(CONTAINER_ID),
            ),
        )
        self.assertEqual(present.reason_code, "cleanup_container_still_present")
        self.assertFalse(present.cleanup_verified)
        self.assertEqual(present.unresolved, cleanup)

        invalid = parse_container_removal_check_result(
            remove,
            self._result(remove, "removal_check", stdout="not-json"),
        )
        self.assertEqual(invalid.reason_code, "cleanup_check_invalid_output")
        self.assertFalse(invalid.cleanup_verified)
        self.assertEqual(invalid.unresolved, cleanup)

        incomplete_empty = ContainerOutputCapture(
            stdout=self._stream_capture(
                "",
                observed_bytes=5,
                captured_bytes=0,
                dropped_bytes=5,
                truncated=True,
            ),
            stderr=self._stream_capture(""),
        )
        incomplete = parse_container_removal_check_result(
            remove,
            self._result(
                remove,
                "removal_check",
                output_capture=incomplete_empty,
            ),
        )
        self.assertEqual(
            incomplete.reason_code,
            "cleanup_check_output_incomplete",
        )
        self.assertEqual(incomplete.unresolved, cleanup)

    def test_cleanup_requires_remove_chain_and_current_engine_authority(self) -> None:
        cleanup = self._created().cleanup
        with self.assertRaisesRegex(TypeError, "remove attempt"):
            parse_container_removal_check_result(
                cleanup,  # type: ignore[arg-type]
                self._result(cleanup, "removal_check"),
            )

        removed = parse_container_remove_result(
            cleanup,
            self._result(cleanup, "remove"),
        )
        self.executable.write_bytes(b"#!/bin/sh\nexit 1\n")
        changed = parse_container_removal_check_result(
            removed,
            self._result(removed, "removal_check"),
        )
        self.assertEqual(changed.reason_code, "cleanup_check_engine_changed")
        self.assertFalse(changed.cleanup_verified)
        self.assertEqual(changed.unresolved, cleanup)

    def test_read_only_and_workspace_write_mount_contracts(self) -> None:
        readonly = self._plan(profile="read-only")
        self.assertEqual(readonly.writable_roots, ())
        readonly_mounts = [
            readonly.create_argv[index + 1]
            for index, item in enumerate(readonly.create_argv)
            if item == "--mount"
        ]
        self.assertTrue(all("readonly" in value for value in readonly_mounts))

        writable = self._plan(profile="workspace-write")
        self.assertEqual(writable.writable_roots, (self.workspace.resolve(),))
        mounts = {
            str(mount.destination): mount.writable for mount in writable.mounts
        }
        self.assertTrue(mounts[str(self.workspace.resolve())])
        self.assertFalse(mounts[str(self.readable.resolve())])

    def test_execution_roots_must_come_from_probed_workspace_authority(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        identity = self._engine()
        outside_request = IsolationRequest(
            mode="required",
            profile="read-only",
            backend="container",
            network_policy="deny",
            workspace=self.workspace,
            readable_roots=(outside,),
        )
        with self.assertRaisesRegex(ValueError, "workspace authority"):
            build_container_execution_draft(
                identity,
                outside_request,
                attempt_id=ATTEMPT_ID,
                working_directory=self.workspace,
                command_argv=("/bin/true",),
                user_id=os.getuid(),
                group_id=os.getgid(),
            )

        wrong_primary = IsolationRequest(
            mode="required",
            profile="read-only",
            backend="container",
            network_policy="deny",
            workspace=self.readable,
        )
        with self.assertRaisesRegex(ValueError, "primary workspace authority"):
            build_container_execution_draft(
                identity,
                wrong_primary,
                attempt_id=ATTEMPT_ID,
                working_directory=self.readable,
                command_argv=("/bin/true",),
                user_id=os.getuid(),
                group_id=os.getgid(),
            )

    def test_root_overlap_environment_order_and_unpinned_image_fail_closed(self) -> None:
        nested = self.workspace / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(ValueError, "overlap"):
            self._draft(readable_roots=(nested,))

        with self.assertRaisesRegex(ValueError, "managed runtime paths"):
            self._draft(readable_roots=(Path("/usr"),))

        identity = self._engine()
        request = IsolationRequest(
            mode="required",
            profile="read-only",
            backend="container",
            network_policy="deny",
            workspace=self.workspace,
        )
        with self.assertRaisesRegex(ValueError, "exact local image id or repository"):
            build_docker_server_probe(
                attempt_id=ATTEMPT_ID,
                workspace_roots=(self.workspace, self.readable),
                workspace_roots_revision=ROOTS_REVISION,
                executable=self.executable,
                executable_sha256=self.executable_sha256,
                socket_path=self.socket_path,
                client_config_directory=self.config_dir,
                gate_image="ubuntu:latest",
            )

    def test_special_paths_and_invalid_attempt_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "32 lowercase hex"):
            build_docker_server_probe(
                attempt_id="not-random",
                workspace_roots=(self.workspace, self.readable),
                workspace_roots_revision=ROOTS_REVISION,
                executable=self.executable,
                executable_sha256=self.executable_sha256,
                socket_path=self.socket_path,
                client_config_directory=self.config_dir,
                gate_image=IMAGE,
            )

        comma_root = self.root / "bad,root"
        comma_root.mkdir()
        with self.assertRaisesRegex(ValueError, "commas"):
            self._draft(readable_roots=(comma_root,))

    def test_private_config_directory_mode_is_required(self) -> None:
        self.config_dir.chmod(0o775)
        with self.assertRaisesRegex(ValueError, "group-writable"):
            self._probe_plan()

    def test_effective_group_writable_authority_is_rejected_even_when_user_owned(
        self,
    ) -> None:
        trusted = self.executable.parent
        os.chown(trusted, -1, os.getegid())
        self.assertIn(
            trusted.stat().st_gid,
            set(os.getgroups()) | {os.getegid()},
        )
        trusted.chmod(0o770)

        with self.assertRaisesRegex(ValueError, "group-writable"):
            self._probe_plan()

    def test_engine_file_identity_includes_ctime(self) -> None:
        plan = self._probe_plan()
        before = plan.executable_identity.file
        old_mtime = before.modified_ns
        original = self.executable.read_bytes()
        replacement = original.replace(b"0", b"1")
        self.assertEqual(len(original), len(replacement))
        self.executable.write_bytes(replacement)
        os.utime(self.executable, ns=(old_mtime, old_mtime))
        after = self.executable.stat()
        self.assertNotEqual(after.st_ctime_ns, before.changed_ns)
        changed = parse_container_probe_result(
            plan,
            self._result(plan, "server", stdout="{}"),
        )
        self.assertEqual(changed.reason_code, "probe_executable_changed")


if __name__ == "__main__":
    unittest.main()

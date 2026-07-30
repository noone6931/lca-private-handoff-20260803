from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from local_agent.execution.container_plan import CONTAINER_LOG_DRIVER
from local_agent.execution.container_plan import CONTAINER_LOG_OPTIONS
from local_agent.execution.container_plan import CONTAINER_MEMORY_BYTES
from local_agent.execution.container_plan import CONTAINER_PIDS_LIMIT
from local_agent.execution.container_plan import CONTAINER_EXECUTION_RESOURCE
from local_agent.execution.container_plan import GATE_COMMAND_PREFIX
from local_agent.execution.container_plan import GATE_ENTRYPOINT
from local_agent.execution.container_plan import GATE_MOUNT_PROOF
from local_agent.execution.container_plan import GATE_PROTOCOL
from local_agent.execution.container_plan import GATE_PROTOCOL_LABEL
from local_agent.execution.container_plan import GATE_READY_CHECK
from local_agent.execution.container_plan import GATE_STAGE_PROOF
from local_agent.execution.container_staging_contracts import (
    ContainerStagingRecoveryResult,
)
from local_agent.execution.contracts import AppliedIsolationProof
from local_agent.execution.contracts import ContainerBackendAuthority
from local_agent.execution.contracts import IsolationConfiguration
from local_agent.execution.contracts import IsolationRequest
from local_agent.tools.base import ToolContext
from local_agent.tools.container_runtime import ContainerCleanupSummary
from local_agent.tools.container_runtime import ContainerExecutionOutcome
from local_agent.tools.container_runtime import ContainerExecutionRuntime
from local_agent.tools.isolation_routing import ConfiguredIsolationProcessRunner
from local_agent.tools.isolation_routing import IsolationExecutionError
from local_agent.tools.isolation_routing import isolation_metadata
from local_agent.tools.process_cancellation import CapturedRunCancelled
from local_agent.tools.process_output import BoundedByteCapture
from local_agent.tools.process_output import CapturedCompletedProcess
from local_agent.tools.process_output import CapturedTimeoutExpired
from local_agent.tools.process_output import ProcessOutputCapture
from local_agent.tools.shell import run_shell
from local_agent.tools.shell import run_tests
from local_agent.workspace.context import WorkspaceRootIdentity
from local_agent.workspace.snapshot import capture_workspace_snapshot


ATTEMPT_ID = "a" * 32
CONTAINER_ID = "b" * 64
PREP_CONTAINER_ID = "d" * 64
IMAGE_ID = "c" * 64
IMAGE = f"local-agent/gate@sha256:{IMAGE_ID}"


def _execution_remove_index(calls: list[tuple[str, ...]]) -> int:
    return next(
        index
        for index, argv in enumerate(calls)
        if argv[5] == "rm" and argv[-1] == CONTAINER_ID
    )


def _execution_absence_index(calls: list[tuple[str, ...]]) -> int:
    return next(
        index
        for index, argv in enumerate(calls)
        if (
            argv[5] == "ps"
            and any(item == f"id={CONTAINER_ID}" for item in argv)
        )
    )


def _capture(stdout: str = "", stderr: str = "") -> ProcessOutputCapture:
    stdout_capture = BoundedByteCapture()
    stdout_capture.push(stdout.encode("utf-8"))
    stderr_capture = BoundedByteCapture()
    stderr_capture.push(stderr.encode("utf-8"))
    return ProcessOutputCapture(stdout_capture.finish(), stderr_capture.finish())


def _copy_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)


def _volume_subpath(volume_name: str) -> str:
    return "-".join(volume_name.rsplit("-", 2)[-2:])


class FakeDockerProcess:
    def __init__(
        self,
        test: "ContainerRuntimeTests",
        *,
        user_command: tuple[str, ...] = ("/bin/sh", "-c", "printf ok"),
        cancel_at_wait: bool = False,
        timeout_at_wait: bool = False,
        timeout_at_termination_wait: bool = False,
        timeout_at_logs: bool = False,
        timeout_at_release: bool = False,
        cancel_at_release: bool = False,
        fail_final_inspect: bool = False,
        raise_at_final_inspect: bool = False,
        parent_failure_message: str = "parent final-inspect failure",
        raise_after_create: bool = False,
        raise_at_remove: bool = False,
        raise_before_remove: bool = False,
        raise_before_prep_remove: bool = False,
        raise_before_volume_remove: bool = False,
        raise_at_removal_check: bool = False,
        recover_created: bool = False,
        staged_mutation: Callable[[list[tuple[Path, Path, bool]]], None]
        | None = None,
        before_stage_proof: Callable[[list[tuple[Path, Path, bool]]], None]
        | None = None,
        wait_exit_code: int = 0,
    ) -> None:
        self.test = test
        self.user_command = user_command
        self.cancel_at_wait = cancel_at_wait
        self.timeout_at_wait = timeout_at_wait
        self.timeout_at_termination_wait = timeout_at_termination_wait
        self.timeout_at_logs = timeout_at_logs
        self.timeout_at_release = timeout_at_release
        self.cancel_at_release = cancel_at_release
        self.fail_final_inspect = fail_final_inspect
        self.raise_at_final_inspect = raise_at_final_inspect
        self.parent_failure_message = parent_failure_message
        self.raise_after_create = raise_after_create
        self.raise_at_remove = raise_at_remove
        self.raise_before_remove = raise_before_remove
        self.raise_before_prep_remove = raise_before_prep_remove
        self.raise_before_volume_remove = raise_before_volume_remove
        self.raise_at_removal_check = raise_at_removal_check
        self.recover_created = recover_created
        self.staged_mutation = staged_mutation
        self.before_stage_proof = before_stage_proof
        self.wait_exit_code = wait_exit_code
        self.calls: list[tuple[str, ...]] = []
        self.waited = False
        self.wait_calls = 0
        self.bind_mounts: list[tuple[Path, Path, bool]] = []
        self.volume_mounts: list[tuple[str, Path, bool]] = []
        self.prep_mounts: list[tuple[str, Path, bool]] = []
        self.volume_root = self.test.root / "fake-docker-volumes"
        self.volume_root.mkdir(exist_ok=True)
        self.volumes: dict[str, Path] = {}
        self.volume_labels: dict[str, dict[str, str]] = {}
        self.present_containers: set[str] = set()
        self.volume_remove_failed = False

    def inherit_daemon_state(self, source: "FakeDockerProcess") -> None:
        self.volumes = dict(source.volumes)
        self.volume_labels = {
            name: dict(labels)
            for name, labels in source.volume_labels.items()
        }
        self.present_containers = set(source.present_containers)
        self.volume_mounts = list(source.volume_mounts)
        self.prep_mounts = list(source.prep_mounts)

    def __call__(self, command, **kwargs):
        argv = tuple(command)
        self.calls.append(argv)
        verb = argv[5]
        if verb == "version":
            return self._completed(
                argv,
                json.dumps(
                    {
                        "Client": {
                            "Version": "29.4.0",
                            "ApiVersion": "1.53",
                            "Os": "darwin",
                            "Arch": "arm64",
                        },
                        "Server": {
                            "Version": "29.4.0",
                            "ApiVersion": "1.53",
                            "Os": "linux",
                            "Arch": "arm64",
                        },
                    }
                ),
            )
        if verb == "image":
            return self._completed(
                argv,
                json.dumps(
                    {
                        "id": f"sha256:{IMAGE_ID}",
                        "repo_digests": [IMAGE],
                        "config_env": ["PATH=/usr/bin:/bin"],
                        "labels": {GATE_PROTOCOL_LABEL: GATE_PROTOCOL},
                        "volumes": None,
                    }
                ),
            )
        if verb == "volume":
            return self._volume_command(argv)
        if verb == "create":
            name = argv[argv.index("--name") + 1]
            if name.endswith("-prep"):
                self.prep_mounts = self._volume_mounts(argv)
                self.present_containers.add(PREP_CONTAINER_ID)
                return self._completed(argv, f"{PREP_CONTAINER_ID}\n")
            self.volume_mounts = self._volume_mounts(argv)
            self.bind_mounts = [
                (
                    self.volumes[source] / _volume_subpath(source),
                    destination,
                    writable,
                )
                for source, destination, writable in self.volume_mounts
            ]
            self.present_containers.add(CONTAINER_ID)
            if self.raise_after_create:
                self.recover_created = True
                raise RuntimeError("parent create failure after daemon acceptance")
            return self._completed(argv, f"{CONTAINER_ID}\n")
        if verb == "cp":
            return self._copy_command(argv)
        if verb == "start":
            return self._completed(argv, f"{CONTAINER_ID}\n")
        if verb == "exec" and GATE_READY_CHECK in argv:
            return self._completed(argv)
        if verb == "exec" and GATE_MOUNT_PROOF in argv:
            output = "".join(
                f"{path.stat().st_dev}:{path.stat().st_ino}\n"
                for path in (self.test.workspace, self.test.readable)
            )
            return self._completed(argv, output)
        if verb == "exec" and GATE_STAGE_PROOF in argv:
            if self.before_stage_proof is not None:
                self.before_stage_proof(self.bind_mounts)
            return self._completed(
                argv,
                returncode=(
                    0 if self._stage_manifests_match(argv) else 1
                ),
            )
        if verb == "inspect":
            if argv[-1] == PREP_CONTAINER_ID:
                return self._completed(
                    argv,
                    json.dumps(self._prep_inspect_payload()),
                )
            if self.waited and self.raise_at_final_inspect:
                raise RuntimeError(self.parent_failure_message)
            if self.waited and self.fail_final_inspect:
                return self._completed(argv, returncode=1)
            return self._completed(
                argv,
                json.dumps(
                    self.test.inspect_payload(
                        command_argv=self.user_command,
                        exited=self.waited,
                        volume_mounts=self.volume_mounts,
                        exit_code=self.wait_exit_code,
                    )
                ),
            )
        if verb == "kill":
            if "--signal=SIGUSR1" in argv:
                if self.cancel_at_release:
                    raise CapturedRunCancelled(
                        "cancelled",
                        _capture("release-started\n"),
                    )
                if self.timeout_at_release:
                    raise CapturedTimeoutExpired(
                        list(argv),
                        1,
                        _capture("release-started\n"),
                    )
            return self._completed(argv, f"{CONTAINER_ID}\n")
        if verb == "wait":
            self.wait_calls += 1
            if self.cancel_at_wait and self.wait_calls == 1:
                raise CapturedRunCancelled("cancelled", _capture("wait-started\n"))
            if self.timeout_at_wait and self.wait_calls == 1:
                raise CapturedTimeoutExpired(
                    list(argv),
                    1,
                    _capture("wait-started\n"),
                )
            if self.timeout_at_termination_wait and self.wait_calls == 2:
                raise CapturedTimeoutExpired(
                    list(argv),
                    1,
                    _capture(),
                )
            self.waited = True
            if self.staged_mutation is not None:
                mutation = self.staged_mutation
                self.staged_mutation = None
                mutation(self.bind_mounts)
            return self._completed(argv, f"{self.wait_exit_code}\n")
        if verb == "logs":
            if self.timeout_at_logs:
                raise CapturedTimeoutExpired(
                    list(argv),
                    1,
                    _capture("partial stdout\n", "partial stderr\n"),
                )
            return self._completed(argv, "hello from container\n")
        if verb == "rm":
            container_id = argv[-1]
            if (
                self.raise_before_prep_remove
                and container_id == PREP_CONTAINER_ID
            ):
                raise RuntimeError(
                    "parent prep remove failure before daemon acceptance"
                )
            if self.raise_before_remove and container_id == CONTAINER_ID:
                raise RuntimeError("parent remove failure before daemon acceptance")
            if self.raise_at_remove and container_id == CONTAINER_ID:
                self.present_containers.discard(container_id)
                raise RuntimeError("parent remove failure")
            self.present_containers.discard(container_id)
            return self._completed(argv, f"{container_id}\n")
        if verb == "ps":
            identifier = next(
                (
                    argument.removeprefix("id=")
                    for argument in argv
                    if argument.startswith("id=")
                ),
                None,
            )
            if (
                self.raise_at_removal_check
                and identifier == CONTAINER_ID
            ):
                raise RuntimeError("parent removal-check failure")
            name_filter = next(
                (
                    argument.removeprefix("name=")
                    for argument in argv
                    if argument.startswith("name=")
                ),
                None,
            )
            if self.recover_created and name_filter is not None:
                expected_id = (
                    PREP_CONTAINER_ID
                    if name_filter.endswith("-prep")
                    else CONTAINER_ID
                )
                if expected_id in self.present_containers:
                    return self._completed(
                        argv,
                        f"{json.dumps(expected_id)}\n",
                    )
            if (
                self.recover_created
                and any(argument.startswith("label=") for argument in argv)
                and CONTAINER_ID in self.present_containers
            ):
                return self._completed(
                    argv,
                    f"{json.dumps(CONTAINER_ID)}\n",
                )
            if identifier in self.present_containers:
                return self._completed(
                    argv,
                    f"{json.dumps(identifier)}\n",
                )
            return self._completed(argv)
        raise AssertionError(f"unexpected Docker command: {argv}")

    @staticmethod
    def _bind_mounts(
        argv: tuple[str, ...],
    ) -> list[tuple[Path, Path, bool]]:
        mounts: list[tuple[Path, Path, bool]] = []
        for index, item in enumerate(argv):
            if item != "--mount":
                continue
            fields: dict[str, str] = {}
            flags: set[str] = set()
            for field in argv[index + 1].split(","):
                if "=" in field:
                    name, value = field.split("=", 1)
                    fields[name] = value
                else:
                    flags.add(field)
            mounts.append(
                (
                    Path(fields["src"]),
                    Path(fields["dst"]),
                    "readonly" not in flags,
                )
            )
        return mounts

    @staticmethod
    def _volume_mounts(
        argv: tuple[str, ...],
    ) -> list[tuple[str, Path, bool]]:
        mounts = []
        for index, item in enumerate(argv):
            if item != "--mount":
                continue
            fields: dict[str, str] = {}
            flags: set[str] = set()
            for field in argv[index + 1].split(","):
                if "=" in field:
                    name, value = field.split("=", 1)
                    fields[name] = value
                else:
                    flags.add(field)
            if fields.get("type") != "volume":
                continue
            mounts.append(
                (
                    fields["src"],
                    Path(fields["dst"]),
                    "readonly" not in flags,
                )
            )
        return mounts

    def _volume_command(
        self,
        argv: tuple[str, ...],
    ) -> CapturedCompletedProcess:
        action = argv[6]
        if action == "create":
            name = argv[-1]
            path = self.volume_root / name
            path.mkdir(mode=0o700, exist_ok=True)
            self.volumes[name] = path
            labels = {}
            for index, value in enumerate(argv):
                if value != "--label":
                    continue
                key, label_value = argv[index + 1].split("=", 1)
                labels[key] = label_value
            self.volume_labels[name] = labels
            return self._completed(argv, f"{name}\n")
        if action == "inspect":
            name = argv[-1]
            if name not in self.volumes:
                return self._completed(
                    argv,
                    stderr=f"no such volume: {name}\n",
                    returncode=1,
                )
            return self._completed(
                argv,
                json.dumps(
                    {
                        "name": name,
                        "driver": "local",
                        "labels": self.volume_labels[name],
                        "options": None,
                        "scope": "local",
                    }
                ),
            )
        if action == "rm":
            name = argv[-1]
            if (
                self.raise_before_volume_remove
                and not self.volume_remove_failed
            ):
                self.volume_remove_failed = True
                raise RuntimeError(
                    "parent volume remove failure before daemon acceptance"
                )
            path = self.volumes.pop(name, None)
            self.volume_labels.pop(name, None)
            if path is None:
                return self._completed(argv, returncode=1)
            shutil.rmtree(path)
            return self._completed(argv, f"{name}\n")
        if action == "ls":
            label_filters = [
                argv[index + 1].removeprefix("label=")
                for index, value in enumerate(argv)
                if (
                    value == "--filter"
                    and argv[index + 1].startswith("label=")
                )
            ]
            name_filters = [
                argv[index + 1].removeprefix("name=")
                for index, value in enumerate(argv)
                if (
                    value == "--filter"
                    and argv[index + 1].startswith("name=")
                )
            ]
            names = [
                name
                for name, labels in self.volume_labels.items()
                if all(
                    labels.get(key) == value
                    for key, value in (
                        item.split("=", 1) for item in label_filters
                    )
                )
                and all(name_filter in name for name_filter in name_filters)
            ]
            return self._completed(
                argv,
                "".join(f"{json.dumps(name)}\n" for name in sorted(names)),
            )
        raise AssertionError(f"unexpected Docker volume command: {argv}")

    def _copy_command(
        self,
        argv: tuple[str, ...],
    ) -> CapturedCompletedProcess:
        source, destination = argv[-2:]
        if ":" not in source:
            prep_id, remote = destination.split(":", 1)
            self.test.assertEqual(prep_id, PREP_CONTAINER_ID)
            volume_name = next(
                name
                for name, mount, _writable in self.prep_mounts
                if str(mount) == remote.rstrip("/")
            )
            source_path = Path(source)
            _copy_tree(
                source_path,
                self.volumes[volume_name] / source_path.name,
            )
            return self._completed(argv)
        container_id, remote = source.split(":", 1)
        self.test.assertEqual(container_id, CONTAINER_ID)
        remote_root = Path(remote.removesuffix("/."))
        volume_name = next(
            name
            for name, mount, _writable in self.volume_mounts
            if mount == remote_root
        )
        _copy_tree(
            self.volumes[volume_name] / _volume_subpath(volume_name),
            Path(destination),
        )
        return self._completed(argv)

    def _stage_manifests_match(self, argv: tuple[str, ...]) -> bool:
        index = argv.index(GATE_STAGE_PROOF) + 1
        expected = {}
        while index < len(argv):
            if argv[index] != "--root" or argv[index + 2] != "--manifest-sha256":
                return False
            expected[Path(argv[index + 1])] = argv[index + 3]
            index += 4
        for name, destination, _writable in self.volume_mounts:
            snapshot = capture_workspace_snapshot(
                self.volumes[name] / _volume_subpath(name),
                roots_revision=3,
            )
            if expected.get(destination) != snapshot.manifest_sha256:
                return False
        return set(expected) == {
            destination for _name, destination, _writable in self.volume_mounts
        }

    def _prep_inspect_payload(self) -> dict[str, object]:
        host_mounts = []
        mounts = []
        for name, destination, _writable in self.prep_mounts:
            host_mounts.append(
                {
                    "Type": "volume",
                    "Source": name,
                    "Target": str(destination),
                    "VolumeOptions": {"NoCopy": True},
                }
            )
            mounts.append(
                {
                    "Type": "volume",
                    "Name": name,
                    "Destination": str(destination),
                    "Driver": "local",
                    "RW": True,
                    "Propagation": "",
                }
            )
        return {
            "id": PREP_CONTAINER_ID,
            "name": f"/lca-{ATTEMPT_ID}-prep",
            "instance_label": ATTEMPT_ID,
            "resource_label": "staging-prep",
            "config_image": f"sha256:{IMAGE_ID}",
            "image_id": f"sha256:{IMAGE_ID}",
            "state_status": "created",
            "state_running": False,
            "host_mounts": host_mounts,
            "mounts": mounts,
        }

    @staticmethod
    def _completed(
        argv: tuple[str, ...],
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> CapturedCompletedProcess:
        return CapturedCompletedProcess(
            list(argv),
            returncode,
            _capture(stdout, stderr),
        )


class ContainerRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.readable = self.root / "readable"
        self.readable.mkdir()
        trusted = self.root / "trusted"
        trusted.mkdir()
        self.executable = trusted / "docker"
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o755)
        self.config_dir = trusted / "config"
        self.config_dir.mkdir(mode=0o700)
        self.socket_path = trusted / "docker.sock"
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(self.socket_path))
        self.staging_root = self.root / "default-staging"
        self.staging_root.mkdir(mode=0o700)
        self.authority = ContainerBackendAuthority(
            executable=self.executable,
            executable_sha256=hashlib.sha256(
                self.executable.read_bytes()
            ).hexdigest(),
            socket_path=self.socket_path,
            client_config_directory=self.config_dir,
            gate_image=IMAGE,
            workspace_transport="staged-copy",
            staging_root=self.staging_root,
        )
        self.request = IsolationRequest(
            mode="required",
            profile="workspace-write",
            backend="container",
            network_policy="deny",
            workspace=self.workspace,
            readable_roots=(self.readable,),
            writable_roots=(self.workspace,),
        )

    def tearDown(self) -> None:
        self.socket.close()
        self.temporary.cleanup()

    def runtime(self, process_runner) -> ContainerExecutionRuntime:
        return ContainerExecutionRuntime(
            self.authority,
            control_environment={
                "HOME": str(self.config_dir),
                "PATH": "/usr/bin:/bin",
            },
            process_runner=process_runner,
            attempt_id_factory=lambda: ATTEMPT_ID,
        )

    def test_direct_bind_is_typed_unsupported_before_any_control_process(self) -> None:
        direct = ContainerBackendAuthority(
            executable=self.authority.executable,
            executable_sha256=self.authority.executable_sha256,
            socket_path=self.authority.socket_path,
            client_config_directory=self.authority.client_config_directory,
            gate_image=self.authority.gate_image,
            workspace_transport="direct-bind",
        )
        calls = []
        outcome = ContainerExecutionRuntime(
            direct,
            control_environment={"HOME": str(self.config_dir)},
            process_runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            attempt_id_factory=lambda: ATTEMPT_ID,
        ).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=("/bin/true",),
            timeout=60,
            cancel_event=None,
        )

        self.assertEqual(outcome.reason_code, "direct_bind_unsupported")
        self.assertEqual(outcome.workspace_transport, "direct-bind")
        self.assertEqual(calls, [])

    def test_success_runs_proof_before_release_and_closes_cleanup(self) -> None:
        process = FakeDockerProcess(self)
        outcome = self.runtime(process).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=("/bin/sh", "-c", "printf ok"),
            timeout=60,
            cancel_event=None,
        )

        self.assertEqual(outcome.reason_code, "container_execution_completed")
        self.assertEqual(outcome.completed.returncode, 0)
        self.assertEqual(outcome.completed.stdout, "hello from container\n")
        self.assertTrue(outcome.cleanup.verified)
        verbs = [argv[5] for argv in process.calls]
        prep_create = next(
            index
            for index, argv in enumerate(process.calls)
            if (
                argv[5] == "create"
                and argv[argv.index("--name") + 1].endswith("-prep")
            )
        )
        execution_create = next(
            index
            for index, argv in enumerate(process.calls)
            if (
                argv[5] == "create"
                and not argv[argv.index("--name") + 1].endswith("-prep")
            )
        )
        stage_copies = [
            index
            for index, argv in enumerate(process.calls)
            if argv[5] == "cp" and ":" not in argv[-2]
        ]
        output_copies = [
            index
            for index, argv in enumerate(process.calls)
            if argv[5] == "cp" and argv[-2].startswith(f"{CONTAINER_ID}:")
        ]
        proof_index = next(
            index
            for index, argv in enumerate(process.calls)
            if argv[5] == "exec" and GATE_STAGE_PROOF in argv
        )
        release_index = next(
            index
            for index, argv in enumerate(process.calls)
            if argv[5] == "kill" and "--signal=SIGUSR1" in argv
        )
        logs_index = verbs.index("logs")
        execution_remove = _execution_remove_index(process.calls)
        execution_absence = _execution_absence_index(process.calls)
        volume_removes = [
            index
            for index, argv in enumerate(process.calls)
            if argv[5:7] == ("volume", "rm")
        ]
        self.assertEqual(len(stage_copies), 2)
        self.assertEqual(len(output_copies), 2)
        self.assertTrue(
            all("--archive" in process.calls[index] for index in stage_copies)
        )
        self.assertTrue(
            all("--archive" not in process.calls[index] for index in output_copies)
        )
        self.assertTrue(
            all(
                not process.calls[index][-2].endswith("/.")
                for index in stage_copies
            )
        )
        execution_mount_arguments = [
            process.calls[execution_create][index + 1]
            for index, item in enumerate(process.calls[execution_create])
            if item == "--mount"
        ]
        self.assertEqual(
            [
                field.split("=", 1)[1]
                for argument in execution_mount_arguments
                for field in argument.split(",")
                if field.startswith("volume-subpath=")
            ],
            ["root-0000", "root-0001"],
        )
        self.assertLess(prep_create, min(stage_copies))
        self.assertLess(max(stage_copies), execution_create)
        self.assertLess(execution_create, proof_index)
        self.assertLess(proof_index, release_index)
        self.assertLess(release_index, logs_index)
        self.assertLess(logs_index, min(output_copies))
        self.assertLess(max(output_copies), execution_remove)
        self.assertLess(execution_remove, execution_absence)
        self.assertTrue(volume_removes)
        self.assertLess(execution_absence, min(volume_removes))
        self.assertTrue(outcome.metadata()["sandboxed"])

    def test_staged_transport_uses_manifest_proof_and_cleans_exact_attempt(
        self,
    ) -> None:
        staging_root = self.root / "staging"
        staging_root.mkdir(mode=0o700)
        authority = ContainerBackendAuthority(
            executable=self.authority.executable,
            executable_sha256=self.authority.executable_sha256,
            socket_path=self.authority.socket_path,
            client_config_directory=self.authority.client_config_directory,
            gate_image=self.authority.gate_image,
            workspace_transport="staged-copy",
            staging_root=staging_root,
        )
        process = FakeDockerProcess(self, user_command=("/bin/true",))
        outcome = ContainerExecutionRuntime(
            authority,
            control_environment={
                "HOME": str(self.config_dir),
                "PATH": "/usr/bin:/bin",
            },
            process_runner=process,
            attempt_id_factory=lambda: ATTEMPT_ID,
        ).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=("/bin/true",),
            timeout=60,
            cancel_event=None,
        )

        self.assertEqual(outcome.reason_code, "container_execution_completed")
        self.assertEqual(outcome.execution_outcome, "exited")
        self.assertEqual(outcome.workspace_transport, "staged-copy")
        self.assertTrue(outcome.staging_cleanup.verified)
        self.assertEqual(
            {path.name for path in staging_root.iterdir()},
            {".lca-staging-journal", ".lca-staging.lock"},
        )
        proof_call = next(
            argv for argv in process.calls
            if GATE_STAGE_PROOF in argv
        )
        self.assertNotIn(GATE_MOUNT_PROOF, proof_call)
        self.assertEqual(proof_call.count("--root"), 2)
        self.assertEqual(proof_call.count("--manifest-sha256"), 2)
        self.assertEqual(
            tuple(
                destination
                for _name, destination, _writable in process.volume_mounts
            ),
            (self.workspace, self.readable),
        )
        self.assertEqual(
            tuple(name for name, _destination, _writable in process.volume_mounts),
            (
                f"lca-{ATTEMPT_ID}-root-0000",
                f"lca-{ATTEMPT_ID}-root-0001",
            ),
        )
        self.assertEqual(process.volumes, {})
        execution_create = next(
            argv
            for argv in process.calls
            if argv[5] == "create"
            and not argv[argv.index("--name") + 1].endswith("-prep")
        )
        self.assertNotIn(str(staging_root), " ".join(execution_create))

    def test_staged_restart_recovers_owned_container_before_new_execution(
        self,
    ) -> None:
        staging_root = self.root / "staging"
        staging_root.mkdir(mode=0o700)
        authority = ContainerBackendAuthority(
            executable=self.authority.executable,
            executable_sha256=self.authority.executable_sha256,
            socket_path=self.authority.socket_path,
            client_config_directory=self.authority.client_config_directory,
            gate_image=self.authority.gate_image,
            workspace_transport="staged-copy",
            staging_root=staging_root,
        )
        first_process = FakeDockerProcess(
            self,
            user_command=("/bin/true",),
            raise_after_create=True,
            raise_before_remove=True,
        )
        first = ContainerExecutionRuntime(
            authority,
            control_environment={
                "HOME": str(self.config_dir),
                "PATH": "/usr/bin:/bin",
            },
            process_runner=first_process,
            attempt_id_factory=lambda: ATTEMPT_ID,
        ).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=("/bin/true",),
            timeout=60,
            cancel_event=None,
        )

        self.assertFalse(first.cleanup.verified)
        self.assertFalse(first.staging_cleanup.verified)
        self.assertTrue((staging_root / ATTEMPT_ID).exists())
        self.assertEqual(
            len(tuple((staging_root / ".lca-staging-journal").iterdir())),
            1,
        )

        second_process = FakeDockerProcess(
            self,
            user_command=("/bin/true",),
            recover_created=True,
        )
        second_process.inherit_daemon_state(first_process)
        second = ContainerExecutionRuntime(
            authority,
            control_environment={
                "HOME": str(self.config_dir),
                "PATH": "/usr/bin:/bin",
            },
            process_runner=second_process,
            attempt_id_factory=lambda: ATTEMPT_ID,
        ).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=("/bin/true",),
            timeout=60,
            cancel_event=None,
        )

        self.assertEqual(second.reason_code, "container_execution_completed")
        self.assertTrue(second.staging_cleanup.verified)
        verbs = [argv[5] for argv in second_process.calls]
        self.assertEqual(
            verbs[:5],
            ["version", "ps", "inspect", "rm", "ps"],
        )
        self.assertEqual(
            {path.name for path in staging_root.iterdir()},
            {".lca-staging-journal", ".lca-staging.lock"},
        )

    def test_staged_restart_recovers_prep_and_volumes_before_execution(
        self,
    ) -> None:
        staging_root = self.root / "staging-prep-recovery"
        staging_root.mkdir(mode=0o700)
        authority = ContainerBackendAuthority(
            executable=self.authority.executable,
            executable_sha256=self.authority.executable_sha256,
            socket_path=self.authority.socket_path,
            client_config_directory=self.authority.client_config_directory,
            gate_image=self.authority.gate_image,
            workspace_transport="staged-copy",
            staging_root=staging_root,
        )
        first_process = FakeDockerProcess(
            self,
            user_command=("/bin/true",),
            raise_before_prep_remove=True,
        )
        first = ContainerExecutionRuntime(
            authority,
            control_environment={
                "HOME": str(self.config_dir),
                "PATH": "/usr/bin:/bin",
            },
            process_runner=first_process,
            attempt_id_factory=lambda: ATTEMPT_ID,
        ).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=("/bin/true",),
            timeout=60,
            cancel_event=None,
        )

        self.assertEqual(first.reason_code, "staging_cleanup_unverified")
        self.assertEqual(first.cleanup.reason_code, "cleanup_container_still_present")
        self.assertTrue(first.recovery_unresolved)
        self.assertIn(PREP_CONTAINER_ID, first_process.present_containers)
        self.assertEqual(len(first_process.volumes), 2)
        record_path = next(
            (staging_root / ".lca-staging-journal").iterdir()
        )
        self.assertEqual(
            json.loads(record_path.read_text(encoding="utf-8"))["state"],
            "create_possible",
        )

        second_process = FakeDockerProcess(
            self,
            user_command=("/bin/true",),
            recover_created=True,
        )
        second_process.inherit_daemon_state(first_process)
        second = ContainerExecutionRuntime(
            authority,
            control_environment={
                "HOME": str(self.config_dir),
                "PATH": "/usr/bin:/bin",
            },
            process_runner=second_process,
            attempt_id_factory=lambda: ATTEMPT_ID,
        ).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=("/bin/true",),
            timeout=60,
            cancel_event=None,
        )

        self.assertEqual(second.reason_code, "container_execution_completed")
        self.assertTrue(second.staging_cleanup.verified)
        self.assertEqual(second_process.present_containers, set())
        self.assertEqual(second_process.volumes, {})

    def test_staged_restart_resumes_volume_cleanup_after_execution_absent(
        self,
    ) -> None:
        staging_root = self.root / "staging-volume-recovery"
        staging_root.mkdir(mode=0o700)
        authority = ContainerBackendAuthority(
            executable=self.authority.executable,
            executable_sha256=self.authority.executable_sha256,
            socket_path=self.authority.socket_path,
            client_config_directory=self.authority.client_config_directory,
            gate_image=self.authority.gate_image,
            workspace_transport="staged-copy",
            staging_root=staging_root,
        )
        first_process = FakeDockerProcess(
            self,
            user_command=("/bin/true",),
            raise_before_volume_remove=True,
        )
        first = ContainerExecutionRuntime(
            authority,
            control_environment={
                "HOME": str(self.config_dir),
                "PATH": "/usr/bin:/bin",
            },
            process_runner=first_process,
            attempt_id_factory=lambda: ATTEMPT_ID,
        ).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=("/bin/true",),
            timeout=60,
            cancel_event=None,
        )

        self.assertEqual(first.reason_code, "staging_cleanup_unverified")
        self.assertEqual(first.cleanup.reason_code, "volume_remove_parent_failed")
        self.assertTrue(first.recovery_unresolved)
        self.assertNotIn(CONTAINER_ID, first_process.present_containers)
        self.assertEqual(len(first_process.volumes), 2)
        record_path = next(
            (staging_root / ".lca-staging-journal").iterdir()
        )
        self.assertEqual(
            json.loads(record_path.read_text(encoding="utf-8"))["state"],
            "execution_absent",
        )

        second_process = FakeDockerProcess(
            self,
            user_command=("/bin/true",),
            recover_created=True,
        )
        second_process.inherit_daemon_state(first_process)
        second = ContainerExecutionRuntime(
            authority,
            control_environment={
                "HOME": str(self.config_dir),
                "PATH": "/usr/bin:/bin",
            },
            process_runner=second_process,
            attempt_id_factory=lambda: ATTEMPT_ID,
        ).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=("/bin/true",),
            timeout=60,
            cancel_event=None,
        )

        self.assertEqual(second.reason_code, "container_execution_completed")
        self.assertTrue(second.staging_cleanup.verified)
        self.assertEqual(second_process.volumes, {})

    def test_named_volume_stage_is_immune_to_host_path_replace_restore(
        self,
    ) -> None:
        staging_root = self.root / "staging"
        staging_root.mkdir(mode=0o700)
        authority = ContainerBackendAuthority(
            executable=self.authority.executable,
            executable_sha256=self.authority.executable_sha256,
            socket_path=self.authority.socket_path,
            client_config_directory=self.authority.client_config_directory,
            gate_image=self.authority.gate_image,
            workspace_transport="staged-copy",
            staging_root=staging_root,
        )

        def replace_restore(mounts: list[tuple[Path, Path, bool]]) -> None:
            primary = next(
                source
                for source, destination, _writable in mounts
                if destination == self.workspace
            )
            moved = primary.with_name(f"{primary.name}-moved")
            primary.rename(moved)
            moved.rename(primary)

        process = FakeDockerProcess(
            self,
            user_command=("/bin/true",),
            before_stage_proof=replace_restore,
        )
        outcome = ContainerExecutionRuntime(
            authority,
            control_environment={
                "HOME": str(self.config_dir),
                "PATH": "/usr/bin:/bin",
            },
            process_runner=process,
            attempt_id_factory=lambda: ATTEMPT_ID,
        ).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=("/bin/true",),
            timeout=60,
            cancel_event=None,
        )

        self.assertEqual(outcome.reason_code, "container_execution_completed")
        self.assertIsNotNone(outcome.staging_cleanup)
        self.assertTrue(outcome.staging_cleanup.verified)
        self.assertFalse(outcome.staging_cleanup.unresolved)
        self.assertTrue(outcome.command_released)
        self.assertTrue(outcome.cleanup.verified)
        self.assertIn("--signal=SIGUSR1", " ".join(map(str, process.calls)))

    def test_staged_source_content_change_is_rejected_before_release(
        self,
    ) -> None:
        (self.workspace / "keep.txt").write_text("before\n", encoding="utf-8")
        staging_root = self.root / "staging"
        staging_root.mkdir(mode=0o700)
        authority = ContainerBackendAuthority(
            executable=self.authority.executable,
            executable_sha256=self.authority.executable_sha256,
            socket_path=self.authority.socket_path,
            client_config_directory=self.authority.client_config_directory,
            gate_image=self.authority.gate_image,
            workspace_transport="staged-copy",
            staging_root=staging_root,
        )

        def change_content(mounts: list[tuple[Path, Path, bool]]) -> None:
            primary = next(
                source
                for source, destination, _writable in mounts
                if destination == self.workspace
            )
            (primary / "keep.txt").write_text("changed\n", encoding="utf-8")

        process = FakeDockerProcess(
            self,
            user_command=("/bin/true",),
            before_stage_proof=change_content,
        )
        outcome = ContainerExecutionRuntime(
            authority,
            control_environment={
                "HOME": str(self.config_dir),
                "PATH": "/usr/bin:/bin",
            },
            process_runner=process,
            attempt_id_factory=lambda: ATTEMPT_ID,
        ).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=("/bin/true",),
            timeout=60,
            cancel_event=None,
        )

        self.assertEqual(outcome.reason_code, "stage_proof_failed")
        self.assertFalse(outcome.command_released)
        self.assertTrue(outcome.cleanup.verified)
        self.assertTrue(outcome.staging_cleanup.verified)
        self.assertEqual(
            {path.name for path in staging_root.iterdir()},
            {".lca-staging-journal", ".lca-staging.lock"},
        )
        self.assertNotIn("--signal=SIGUSR1", " ".join(map(str, process.calls)))

    def test_staged_router_commits_nonzero_command_output_through_patch_owner(
        self,
    ) -> None:
        (self.workspace / "replace.txt").write_text(
            "before\n",
            encoding="utf-8",
        )
        (self.workspace / "delete.txt").write_text(
            "delete\n",
            encoding="utf-8",
        )
        staging_root = self.root / "staging"
        staging_root.mkdir(mode=0o700)
        authority = ContainerBackendAuthority(
            executable=self.authority.executable,
            executable_sha256=self.authority.executable_sha256,
            socket_path=self.authority.socket_path,
            client_config_directory=self.authority.client_config_directory,
            gate_image=self.authority.gate_image,
            workspace_transport="staged-copy",
            staging_root=staging_root,
        )

        def mutate(mounts: list[tuple[Path, Path, bool]]) -> None:
            primary = next(
                source
                for source, destination, _writable in mounts
                if destination == self.workspace
            )
            (primary / "replace.txt").write_text("after\n", encoding="utf-8")
            (primary / "delete.txt").unlink()
            (primary / "create.txt").write_text("created\n", encoding="utf-8")

        process = FakeDockerProcess(
            self,
            user_command=("/bin/sh", "-c", "exit 7"),
            staged_mutation=mutate,
            wait_exit_code=7,
        )
        runtime = ContainerExecutionRuntime(
            authority,
            control_environment={
                "HOME": str(self.config_dir),
                "PATH": "/usr/bin:/bin",
            },
            process_runner=process,
            attempt_id_factory=lambda: ATTEMPT_ID,
        )
        runner = ConfiguredIsolationProcessRunner(
            IsolationConfiguration(
                mode="required",
                profile="workspace-write",
                backend="container",
                container=authority,
            ),
            container_runtime=runtime,
            local_runner=lambda *args, **kwargs: self.fail("local fallback"),
        )
        identity = self.workspace.lstat()
        (self.root / "state").mkdir()
        completed = runner(
            "exit 7",
            cwd=self.workspace,
            shell=True,
            timeout=60,
            cancel_event=None,
            context=ToolContext(
                self.workspace,
                "yolo",
                state_dir=self.root / "state",
                allowed_dirs=(self.readable,),
                session_id="session",
                run_id="run",
                tool_call_id="call",
                workspace_revision=3,
                workspace_identity=WorkspaceRootIdentity(
                    identity.st_dev,
                    identity.st_ino,
                ),
            ),
        )

        self.assertEqual(completed.returncode, 7)
        self.assertEqual((self.workspace / "replace.txt").read_text(), "after\n")
        self.assertFalse((self.workspace / "delete.txt").exists())
        self.assertEqual((self.workspace / "create.txt").read_text(), "created\n")
        self.assertEqual(
            {path.name for path in staging_root.iterdir()},
            {".lca-staging-journal", ".lca-staging.lock"},
        )
        metadata = isolation_metadata(completed)
        self.assertTrue(metadata["workspace_changed"])
        self.assertIsInstance(metadata["workspace_transaction_id"], str)
        self.assertEqual(
            metadata["isolation"]["workspace_output_commit"]["state"],
            "committed",
        )
        journal = self.root / "state/patches/session.jsonl"
        record = json.loads(journal.read_text(encoding="utf-8"))
        self.assertEqual(record["source"], "container_staged_copy")
        self.assertEqual(
            [item["operation"] for item in record["files"]],
            ["create", "delete", "replace"],
        )

    def test_cancelled_wait_still_uses_bounded_cleanup_path(self) -> None:
        command = ("/bin/sh", "-c", "sleep 60")
        process = FakeDockerProcess(
            self,
            user_command=command,
            cancel_at_wait=True,
        )
        outcome = self.runtime(process).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=command,
            timeout=60,
            cancel_event=None,
        )

        self.assertEqual(outcome.reason_code, "wait_cancelled")
        self.assertIsNotNone(outcome.cancellation)
        self.assertTrue(outcome.command_released)
        self.assertTrue(outcome.cleanup.verified)
        remove_index = _execution_remove_index(process.calls)
        absence_index = _execution_absence_index(process.calls)
        self.assertLess(remove_index, absence_index)
        self.assertIn("logs", [argv[5] for argv in process.calls])
        self.assertEqual(outcome.execution_outcome, "cancelled")
        self.assertEqual(outcome.user_output.stdout.text, "hello from container\n")
        self.assertLess(
            [argv[5] for argv in process.calls].index("logs"),
            remove_index,
        )

    def test_timed_out_wait_captures_user_output_before_cleanup(self) -> None:
        command = ("/bin/sh", "-c", "sleep 60")
        process = FakeDockerProcess(
            self,
            user_command=command,
            timeout_at_wait=True,
        )
        outcome = self.runtime(process).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=command,
            timeout=60,
            cancel_event=None,
        )

        verbs = [argv[5] for argv in process.calls]
        self.assertEqual(outcome.reason_code, "wait_timed_out")
        self.assertEqual(outcome.execution_outcome, "timed_out")
        self.assertEqual(outcome.user_output.stdout.text, "hello from container\n")
        self.assertEqual(outcome.user_output_reason_code, "termination_logs_captured")
        self.assertLess(verbs.index("logs"), _execution_remove_index(process.calls))
        self.assertTrue(outcome.cleanup.verified)

    def test_unwind_escalates_from_term_to_kill_before_logs_and_remove(self) -> None:
        command = ("/bin/sh", "-c", "sleep 60")
        process = FakeDockerProcess(
            self,
            user_command=command,
            cancel_at_wait=True,
            timeout_at_termination_wait=True,
        )
        outcome = self.runtime(process).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=command,
            timeout=60,
            cancel_event=None,
        )

        signals = [
            argument
            for argv in process.calls
            if argv[5] == "kill"
            for argument in argv
            if argument.startswith("--signal=")
        ]
        verbs = [argv[5] for argv in process.calls]
        self.assertEqual(signals, ["--signal=SIGUSR1", "--signal=TERM", "--signal=KILL"])
        self.assertEqual(outcome.termination_reason_code, "kill_wait_stopped")
        self.assertLess(verbs.index("logs"), _execution_remove_index(process.calls))
        self.assertTrue(outcome.cleanup.verified)

    def test_unwind_preserves_bounded_partial_logs_when_logs_time_out(self) -> None:
        command = ("/bin/sh", "-c", "sleep 60")
        process = FakeDockerProcess(
            self,
            user_command=command,
            cancel_at_wait=True,
            timeout_at_logs=True,
        )
        outcome = self.runtime(process).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=command,
            timeout=60,
            cancel_event=None,
        )

        self.assertEqual(
            outcome.user_output_reason_code,
            "termination_logs_partial_timed_out",
        )
        self.assertEqual(outcome.user_output.stdout.text, "partial stdout\n")
        self.assertEqual(outcome.user_output.stderr.text, "partial stderr\n")
        self.assertFalse(outcome.user_output.truncated)
        self.assertTrue(outcome.cleanup.verified)

    def test_release_timeout_is_ambiguous_and_unwinds_before_cleanup(self) -> None:
        command = ("/bin/sh", "-c", "sleep 60")
        process = FakeDockerProcess(
            self,
            user_command=command,
            timeout_at_release=True,
        )
        outcome = self.runtime(process).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=command,
            timeout=60,
            cancel_event=None,
        )

        verbs = [argv[5] for argv in process.calls]
        self.assertEqual(outcome.reason_code, "release_timed_out")
        self.assertEqual(outcome.command_release_state, "ambiguous")
        self.assertEqual(outcome.execution_outcome, "indeterminate")
        self.assertIsNone(outcome.user_output)
        self.assertIsNone(outcome.user_output_reason_code)
        self.assertLess(verbs.index("logs"), _execution_remove_index(process.calls))
        self.assertTrue(outcome.cleanup.verified)

    def test_release_cancellation_is_ambiguous_and_preserves_cancellation(self) -> None:
        command = ("/bin/sh", "-c", "sleep 60")
        process = FakeDockerProcess(
            self,
            user_command=command,
            cancel_at_release=True,
        )
        outcome = self.runtime(process).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=command,
            timeout=60,
            cancel_event=None,
        )

        self.assertEqual(outcome.reason_code, "release_cancelled")
        self.assertEqual(outcome.command_release_state, "ambiguous")
        self.assertEqual(outcome.execution_outcome, "cancelled")
        self.assertIsNotNone(outcome.cancellation)
        self.assertTrue(outcome.cleanup.verified)

    def test_ambiguous_release_cancellation_hides_control_plane_output(self) -> None:
        command = ("/bin/sh", "-c", "sleep 60")
        process = FakeDockerProcess(
            self,
            user_command=command,
            cancel_at_release=True,
        )
        runner = ConfiguredIsolationProcessRunner(
            IsolationConfiguration(
                mode="required",
                profile="workspace-write",
                backend="container",
                container=self.authority,
            ),
            container_runtime=self.runtime(process),
            local_runner=lambda *args, **kwargs: self.fail("local fallback"),
        )

        with self.assertRaises(CapturedRunCancelled) as raised:
            runner(
                "sleep 60",
                cwd=self.workspace,
                shell=True,
                timeout=60,
                cancel_event=None,
                context=ToolContext(
                    self.workspace,
                    "yolo",
                    allowed_dirs=(self.readable,),
                ),
            )

        self.assertEqual(raised.exception.stdout, "")
        self.assertEqual(raised.exception.stderr, "")
        isolation = raised.exception.isolation_metadata["isolation"]
        self.assertEqual(isolation["command_release_state"], "ambiguous")
        self.assertIsNone(isolation["user_output_reason_code"])

    def test_final_inspect_failure_captures_logs_before_cleanup(self) -> None:
        command = ("/bin/sh", "-c", "printf ok")
        process = FakeDockerProcess(
            self,
            user_command=command,
            fail_final_inspect=True,
        )
        outcome = self.runtime(process).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=command,
            timeout=60,
            cancel_event=None,
        )

        verbs = [argv[5] for argv in process.calls]
        self.assertEqual(outcome.reason_code, "final_inspect_failed")
        self.assertEqual(outcome.command_release_state, "verified")
        self.assertEqual(outcome.execution_outcome, "indeterminate")
        self.assertEqual(outcome.user_output.stdout.text, "hello from container\n")
        self.assertLess(verbs.index("logs"), _execution_remove_index(process.calls))
        self.assertTrue(outcome.cleanup.verified)

    def test_create_parse_exception_recovers_owned_instance_for_cleanup(self) -> None:
        process = FakeDockerProcess(self, recover_created=True)
        with patch(
            "local_agent.tools.container_runtime.parse_container_create_result",
            side_effect=ValueError("invalid create result"),
        ):
            outcome = self.runtime(process).execute(
                request=self.request,
                workspace_roots=(self.workspace, self.readable),
                workspace_roots_revision=3,
                working_directory=self.workspace,
                command_argv=("/bin/sh", "-c", "printf ok"),
                timeout=60,
                cancel_event=None,
            )

        verbs = [argv[5] for argv in process.calls]
        self.assertEqual(outcome.reason_code, "create_result_invalid")
        self.assertEqual(outcome.execution_outcome, "not_run")
        self.assertFalse(outcome.recovery_unresolved)
        self.assertTrue(outcome.cleanup.verified)
        self.assertNotIn("start", verbs)
        self.assertLess(
            _execution_remove_index(process.calls),
            _execution_absence_index(process.calls),
        )

    def test_create_parent_exception_recovers_and_closes_owned_instance(
        self,
    ) -> None:
        process = FakeDockerProcess(self, raise_after_create=True)

        outcome = self.runtime(process).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=("/bin/sh", "-c", "printf ok"),
            timeout=60,
            cancel_event=None,
        )

        verbs = [argv[5] for argv in process.calls]
        self.assertEqual(outcome.reason_code, "create_parent_exception")
        self.assertEqual(outcome.execution_outcome, "not_run")
        self.assertFalse(outcome.recovery_unresolved)
        self.assertTrue(outcome.cleanup.verified)
        self.assertNotIn("start", verbs)
        recovered_inspect = next(
            index
            for index, argv in enumerate(process.calls)
            if argv[5] == "inspect" and argv[-1] == CONTAINER_ID
        )
        execution_remove = _execution_remove_index(process.calls)
        self.assertLess(recovered_inspect, execution_remove)
        self.assertLess(
            execution_remove,
            _execution_absence_index(process.calls),
        )

    def test_recovery_parse_exception_retains_unresolved_obligation(self) -> None:
        process = FakeDockerProcess(self, recover_created=True)
        with (
            patch(
                "local_agent.tools.container_runtime.parse_container_create_result",
                side_effect=ValueError("invalid create result"),
            ),
            patch(
                "local_agent.tools.container_recovery_runtime."
                "parse_container_recovery_query_result",
                side_effect=ValueError("invalid recovery result"),
            ),
        ):
            outcome = self.runtime(process).execute(
                request=self.request,
                workspace_roots=(self.workspace, self.readable),
                workspace_roots_revision=3,
                working_directory=self.workspace,
                command_argv=("/bin/sh", "-c", "printf ok"),
                timeout=60,
                cancel_event=None,
            )

        self.assertEqual(outcome.reason_code, "staging_cleanup_unverified")
        self.assertEqual(
            outcome.cleanup.reason_code,
            "recovery_query_result_invalid",
        )
        self.assertFalse(outcome.cleanup.verified)
        self.assertTrue(outcome.recovery_unresolved)

    def test_cleanup_parse_exception_is_never_reported_as_clean(self) -> None:
        process = FakeDockerProcess(self)
        with patch(
            "local_agent.tools.container_runtime.parse_container_remove_result",
            side_effect=ValueError("invalid remove result"),
        ):
            outcome = self.runtime(process).execute(
                request=self.request,
                workspace_roots=(self.workspace, self.readable),
                workspace_roots_revision=3,
                working_directory=self.workspace,
                command_argv=("/bin/sh", "-c", "printf ok"),
                timeout=60,
                cancel_event=None,
            )

        self.assertEqual(outcome.reason_code, "staging_cleanup_unverified")
        self.assertEqual(outcome.cleanup.reason_code, "remove_result_invalid")
        self.assertFalse(outcome.cleanup.verified)
        self.assertTrue(outcome.cleanup.unresolved)

    def test_remove_parent_failure_is_closed_by_exact_absence(self) -> None:
        outcome = self.runtime(
            FakeDockerProcess(self, raise_at_remove=True)
        ).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=("/bin/sh", "-c", "printf ok"),
            timeout=60,
            cancel_event=None,
        )

        self.assertEqual(outcome.reason_code, "container_execution_completed")
        self.assertEqual(
            outcome.cleanup.reason_code,
            "container_resources_cleanup_verified",
        )
        self.assertTrue(outcome.cleanup.verified)
        self.assertFalse(outcome.cleanup.unresolved)

    def test_removal_check_parent_failure_is_typed_unresolved(self) -> None:
        outcome = self.runtime(
            FakeDockerProcess(self, raise_at_removal_check=True)
        ).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=("/bin/sh", "-c", "printf ok"),
            timeout=60,
            cancel_event=None,
        )

        self.assertEqual(outcome.reason_code, "staging_cleanup_unverified")
        self.assertEqual(
            outcome.cleanup.reason_code,
            "cleanup_check_parent_failed",
        )
        self.assertFalse(outcome.cleanup.verified)
        self.assertTrue(outcome.cleanup.unresolved)

    def test_parent_exception_becomes_typed_outcome_without_raw_text(self) -> None:
        process = FakeDockerProcess(self, raise_at_final_inspect=True)
        outcome = self.runtime(process).execute(
            request=self.request,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            working_directory=self.workspace,
            command_argv=("/bin/sh", "-c", "printf ok"),
            timeout=60,
            cancel_event=None,
        )

        metadata = outcome.metadata()["isolation"]
        self.assertEqual(metadata["reason_code"], "container_runtime_exception")
        self.assertEqual(metadata["command_release_state"], "verified")
        self.assertEqual(metadata["execution_outcome"], "indeterminate")
        self.assertTrue(metadata["cleanup_verified"])
        self.assertEqual(outcome.user_output.stdout.text, "hello from container\n")
        self.assertNotIn("parent final-inspect failure", repr(outcome))
        self.assertNotIn("parent final-inspect failure", json.dumps(metadata))
        verbs = [argv[5] for argv in process.calls]
        self.assertLess(verbs.index("logs"), _execution_remove_index(process.calls))

    def test_parent_exception_secret_is_absent_at_tool_process_boundary(self) -> None:
        secret = "docker-auth-token-must-not-project"
        runtime = self.runtime(
            FakeDockerProcess(
                self,
                raise_at_final_inspect=True,
                parent_failure_message=secret,
            )
        )
        routed = ConfiguredIsolationProcessRunner(
            IsolationConfiguration(
                mode="required",
                profile="workspace-write",
                backend="container",
                network_policy="deny",
                container=self.authority,
            ),
            container_runtime=runtime,
        )
        context = ToolContext(
            self.workspace,
            "yolo",
            allowed_dirs=(self.readable,),
            workspace_revision=3,
        )

        with self.assertRaises(IsolationExecutionError) as raised:
            routed(
                "printf ok",
                cwd=self.workspace,
                shell=True,
                timeout=60,
                cancel_event=None,
                context=context,
            )

        rendered = "\n".join(
            (
                str(raised.exception),
                raised.exception.stdout,
                raised.exception.stderr,
                json.dumps(raised.exception.isolation_metadata, sort_keys=True),
            )
        )
        self.assertNotIn(secret, rendered)
        self.assertEqual(
            raised.exception.reason_code,
            "container_runtime_exception",
        )

    def test_routing_off_preserves_local_and_required_never_falls_back(self) -> None:
        local_calls = []

        def local_runner(*args, **kwargs):
            local_calls.append((args, kwargs))
            return FakeDockerProcess._completed(("local",), "local\n")

        context = ToolContext(self.workspace, "yolo", workspace_revision=4)
        off = ConfiguredIsolationProcessRunner(
            IsolationConfiguration(),
            local_runner=local_runner,
        )
        completed = off(
            ["python3", "-V"],
            cwd=self.workspace,
            shell=False,
            timeout=5,
            cancel_event=None,
            context=context,
        )
        self.assertEqual(completed.stdout, "local\n")
        self.assertEqual(len(local_calls), 1)

        required = ConfiguredIsolationProcessRunner(
            IsolationConfiguration(
                mode="required",
                profile="read-only",
                backend="container",
                container=None,
            ),
            local_runner=local_runner,
        )
        with self.assertRaises(IsolationExecutionError) as raised:
            required(
                ["python3", "-V"],
                cwd=self.workspace,
                shell=False,
                timeout=5,
                cancel_event=None,
                context=context,
            )
        self.assertEqual(
            raised.exception.reason_code,
            "container_authority_unconfigured",
        )
        self.assertEqual(len(local_calls), 1)

    def test_routing_projects_applied_metadata_without_raw_authority(self) -> None:
        completed = FakeDockerProcess._completed(("container",), "isolated\n")
        proof = AppliedIsolationProof(
            backend="container",
            backend_instance_id=CONTAINER_ID,
            profile="workspace-write",
            network_policy="deny",
            workspace=self.workspace,
            readable_roots=(self.readable,),
            writable_roots=(self.workspace,),
            image_digest=f"sha256:{IMAGE_ID}",
        )
        outcome = ContainerExecutionOutcome(
            "container_execution_completed",
            ATTEMPT_ID,
            completed=completed,
            proof=proof,
            cleanup=ContainerCleanupSummary(
                "cleanup_verified_absent",
                True,
                False,
            ),
            command_release_state="verified",
            execution_outcome="exited",
        )

        class Runtime:
            def execute(self, **kwargs):
                self.kwargs = kwargs
                return outcome

        runtime = Runtime()
        runner = ConfiguredIsolationProcessRunner(
            IsolationConfiguration(
                mode="required",
                profile="workspace-write",
                backend="container",
                container=self.authority,
            ),
            container_runtime=runtime,
            local_runner=lambda *args, **kwargs: self.fail("local fallback"),
        )
        observed = runner(
            "printf ok",
            cwd=self.workspace,
            shell=True,
            timeout=5,
            cancel_event=None,
            context=ToolContext(
                self.workspace,
                "yolo",
                allowed_dirs=(self.readable,),
                workspace_revision=9,
            ),
        )

        metadata = isolation_metadata(observed)
        self.assertTrue(metadata["sandboxed"])
        self.assertEqual(
            runtime.kwargs["command_argv"],
            ("/bin/sh", "-c", "printf ok"),
        )
        self.assertEqual(runtime.kwargs["workspace_roots_revision"], 9)
        self.assertNotIn(str(self.socket_path), json.dumps(metadata))
        self.assertNotIn(str(self.executable), json.dumps(metadata))

    def test_routing_reraises_cancellation_with_container_user_output(self) -> None:
        proof = AppliedIsolationProof(
            backend="container",
            backend_instance_id=CONTAINER_ID,
            profile="workspace-write",
            network_policy="deny",
            workspace=self.workspace,
            readable_roots=(self.readable,),
            writable_roots=(self.workspace,),
            image_digest=f"sha256:{IMAGE_ID}",
        )
        cancellation = CapturedRunCancelled("cancelled", _capture("control output\n"))
        outcome = ContainerExecutionOutcome(
            "wait_cancelled",
            ATTEMPT_ID,
            proof=proof,
            cleanup=ContainerCleanupSummary(
                "cleanup_verified_absent",
                True,
                False,
            ),
            cancellation=cancellation,
            command_release_state="verified",
            execution_outcome="cancelled",
            user_output=_capture("user stdout\n", "user stderr\n"),
            termination_reason_code="termination_wait_stopped",
            user_output_reason_code="termination_logs_captured",
        )

        class Runtime:
            def execute(self, **kwargs):
                return outcome

        runner = ConfiguredIsolationProcessRunner(
            IsolationConfiguration(
                mode="required",
                profile="workspace-write",
                backend="container",
                container=self.authority,
            ),
            container_runtime=Runtime(),
            local_runner=lambda *args, **kwargs: self.fail("local fallback"),
        )

        with self.assertRaises(CapturedRunCancelled) as raised:
            runner(
                "sleep 60",
                cwd=self.workspace,
                shell=True,
                timeout=5,
                cancel_event=None,
                context=ToolContext(self.workspace, "yolo"),
            )

        self.assertIs(raised.exception, cancellation)
        self.assertEqual(raised.exception.stdout, "user stdout\n")
        self.assertEqual(raised.exception.stderr, "user stderr\n")
        self.assertEqual(
            raised.exception.isolation_metadata["isolation"]["execution_outcome"],
            "cancelled",
        )

    def test_timed_out_container_execution_remains_timed_out_at_tool_boundary(self) -> None:
        proof = AppliedIsolationProof(
            backend="container",
            backend_instance_id=CONTAINER_ID,
            profile="workspace-write",
            network_policy="deny",
            workspace=self.workspace,
            readable_roots=(self.readable,),
            writable_roots=(self.workspace,),
            image_digest=f"sha256:{IMAGE_ID}",
        )
        outcome = ContainerExecutionOutcome(
            "wait_timed_out",
            ATTEMPT_ID,
            proof=proof,
            cleanup=ContainerCleanupSummary(
                "cleanup_verified_absent",
                True,
                False,
            ),
            command_release_state="verified",
            execution_outcome="timed_out",
            user_output=_capture("user stdout\n", "user stderr\n"),
            termination_reason_code="termination_wait_stopped",
            user_output_reason_code="termination_logs_captured",
        )

        class Runtime:
            def execute(self, **kwargs):
                return outcome

        runner = ConfiguredIsolationProcessRunner(
            IsolationConfiguration(
                mode="required",
                profile="workspace-write",
                backend="container",
                container=self.authority,
            ),
            container_runtime=Runtime(),
            local_runner=lambda *args, **kwargs: self.fail("local fallback"),
        )
        result = run_shell(
            {"command": "sleep 60"},
            ToolContext(
                self.workspace,
                "yolo",
                process_runner=runner,
            ),
        )

        self.assertTrue(result.is_error)
        self.assertEqual(
            result.metadata["execution_v1"]["outcome"],
            {"kind": "timed_out", "exit_code": None},
        )
        self.assertEqual(
            result.metadata["isolation"]["execution_outcome"],
            "timed_out",
        )
        self.assertIn("user stdout", result.content)
        self.assertIn("user stderr", result.content)

    def test_shell_and_run_tests_project_router_metadata(self) -> None:
        calls = []

        def process_runner(command, **kwargs):
            calls.append((command, kwargs.get("isolated_command")))
            completed = FakeDockerProcess._completed(
                kwargs.get("isolated_command") or ("container",),
                "ok\n",
            )
            completed.isolation_metadata = {
                "sandboxed": True,
                "isolation": {
                    "backend": "container",
                    "reason_code": "container_execution_completed",
                    "applied": True,
                },
            }
            return completed

        context = ToolContext(
            self.workspace,
            "yolo",
            process_runner=process_runner,
        )
        shell_result = run_shell({"command": "printf ok"}, context)
        test_result = run_tests(
            {"command": "python3 -m unittest"},
            context,
        )

        self.assertFalse(shell_result.is_error)
        self.assertFalse(test_result.is_error)
        self.assertTrue(shell_result.metadata["sandboxed"])
        self.assertTrue(test_result.metadata["sandboxed"])
        self.assertEqual(calls[0], ("printf ok", None))
        self.assertEqual(
            calls[1][1],
            ("python3", "-m", "unittest"),
        )
        self.assertEqual(
            test_result.metadata["execution_v1"]["command"]["argv"],
            ["python3", "-m", "unittest"],
        )

    def test_run_tests_projects_allowed_explicit_environment_into_isolated_argv(self) -> None:
        calls = []

        def process_runner(command, **kwargs):
            calls.append(
                (
                    command,
                    kwargs.get("isolated_command"),
                    kwargs.get("isolated_environment"),
                )
            )
            completed = FakeDockerProcess._completed(("container",), "ok\n")
            completed.isolation_metadata = {
                "sandboxed": True,
                "isolation": {
                    "backend": "container",
                    "reason_code": "container_execution_completed",
                    "applied": True,
                },
            }
            return completed

        result = run_tests(
            {
                "command": (
                    "PYTHONPATH=src CUSTOM_TOOLCHAIN='with spaces' "
                    "AI_API_KEY=must-not-enter-container python3 -m unittest"
                )
            },
            ToolContext(
                self.workspace,
                "yolo",
                process_runner=process_runner,
            ),
        )

        self.assertFalse(result.is_error)
        self.assertNotIn(
            "must-not-enter-container",
            json.dumps(result.metadata, sort_keys=True),
        )
        self.assertEqual(
            calls[0][1],
            ("python3", "-m", "unittest"),
        )
        self.assertEqual(
            calls[0][2],
            {
                "AI_API_KEY": "must-not-enter-container",
                "CUSTOM_TOOLCHAIN": "with spaces",
                "PYTHONPATH": "src",
            },
        )

    def test_isolation_runner_projects_environment_without_provider_credentials(self) -> None:
        completed = FakeDockerProcess._completed(("container",), "ok\n")
        proof = AppliedIsolationProof(
            backend="container",
            backend_instance_id=CONTAINER_ID,
            profile="read-only",
            network_policy="deny",
            workspace=self.workspace,
            readable_roots=(self.readable,),
            writable_roots=(),
            image_digest=f"sha256:{IMAGE_ID}",
        )
        outcome = ContainerExecutionOutcome(
            "container_execution_completed",
            ATTEMPT_ID,
            completed=completed,
            proof=proof,
            cleanup=ContainerCleanupSummary(
                "cleanup_verified_absent",
                True,
                False,
            ),
            command_release_state="verified",
            execution_outcome="exited",
        )

        class Runtime:
            def execute(self, **kwargs):
                self.kwargs = kwargs
                return outcome

        runtime = Runtime()
        runner = ConfiguredIsolationProcessRunner(
            IsolationConfiguration(
                mode="required",
                profile="read-only",
                backend="container",
                container=self.authority,
            ),
            container_runtime=runtime,
            local_runner=lambda *args, **kwargs: self.fail("local fallback"),
        )
        runner(
            ["python3", "-m", "unittest"],
            cwd=self.workspace,
            shell=False,
            timeout=5,
            cancel_event=None,
            context=ToolContext(
                self.workspace,
                "yolo",
                allowed_dirs=(self.readable,),
                workspace_revision=9,
            ),
            isolated_command=("python3", "-m", "unittest"),
            isolated_environment={
                "PYTHONPATH": "src",
                "CUSTOM_TOOLCHAIN": "with spaces",
                "AI_API_KEY": "must-not-enter-container",
            },
        )

        self.assertEqual(
            runtime.kwargs["command_argv"],
            (
                "/usr/bin/env",
                "CUSTOM_TOOLCHAIN=with spaces",
                "PYTHONPATH=src",
                "python3",
                "-m",
                "unittest",
            ),
        )
        self.assertNotIn(
            "must-not-enter-container",
            repr(runtime.kwargs["command_argv"]),
        )

    def test_isolation_rejects_state_authority_overlapping_workspace(self) -> None:
        state = self.workspace / ".state"
        state.mkdir()
        calls = []

        class Runtime:
            def execute(self, **kwargs):
                calls.append(kwargs)
                self.fail("container runtime must not be reached")

        runner = ConfiguredIsolationProcessRunner(
            IsolationConfiguration(
                mode="required",
                profile="read-only",
                backend="container",
                container=self.authority,
            ),
            container_runtime=Runtime(),
            local_runner=lambda *args, **kwargs: self.fail("local fallback"),
        )

        with self.assertRaises(IsolationExecutionError) as raised:
            runner(
                ["/bin/true"],
                cwd=self.workspace,
                shell=False,
                timeout=5,
                cancel_event=None,
                context=ToolContext(
                    self.workspace,
                    "yolo",
                    state_dir=state,
                    allowed_dirs=(self.readable,),
                ),
            )

        self.assertEqual(
            raised.exception.reason_code,
            "isolation_state_authority_overlap",
        )
        self.assertEqual(calls, [])

    def test_state_authority_rename_into_workspace_is_rejected_before_staging(
        self,
    ) -> None:
        state = self.root / "state"
        state.mkdir()
        (state / "secret").write_text("must-not-stage", encoding="utf-8")
        moved = self.workspace / "moved-state"
        process_calls = []

        def move_state_during_recovery(**kwargs):
            state.rename(moved)
            return ContainerStagingRecoveryResult(
                "staging_recovery_ready",
                True,
                False,
            )

        runtime = ContainerExecutionRuntime(
            self.authority,
            control_environment={
                "HOME": str(self.config_dir),
                "PATH": "/usr/bin:/bin",
            },
            process_runner=lambda *args, **kwargs: process_calls.append(
                (args, kwargs)
            ),
            attempt_id_factory=lambda: ATTEMPT_ID,
        )
        runner = ConfiguredIsolationProcessRunner(
            IsolationConfiguration(
                mode="required",
                profile="read-only",
                backend="container",
                container=self.authority,
            ),
            container_runtime=runtime,
            local_runner=lambda *args, **kwargs: self.fail("local fallback"),
        )

        with (
            patch(
                "local_agent.tools.container_runtime.recover_staging_authority",
                side_effect=move_state_during_recovery,
            ),
            self.assertRaises(IsolationExecutionError) as raised,
        ):
            runner(
                ["/bin/true"],
                cwd=self.workspace,
                shell=False,
                timeout=5,
                cancel_event=None,
                context=ToolContext(
                    self.workspace,
                    "yolo",
                    state_dir=state,
                    allowed_dirs=(self.readable,),
                ),
            )

        self.assertEqual(
            raised.exception.reason_code,
            "workspace_snapshot_forbidden_directory_identity",
        )
        self.assertEqual(process_calls, [])
        self.assertEqual(
            (moved / "secret").read_text(encoding="utf-8"),
            "must-not-stage",
        )

    def test_preferred_unavailable_is_typed_and_never_falls_back_locally(self) -> None:
        local_calls = []
        runner = ConfiguredIsolationProcessRunner(
            IsolationConfiguration(
                mode="preferred",
                profile="read-only",
                backend="container",
                container=None,
            ),
            local_runner=lambda *args, **kwargs: local_calls.append((args, kwargs)),
        )

        with self.assertRaises(IsolationExecutionError) as raised:
            runner(
                ["python3", "-V"],
                cwd=self.workspace,
                shell=False,
                timeout=5,
                cancel_event=None,
                context=ToolContext(self.workspace, "yolo"),
            )

        self.assertEqual(
            raised.exception.reason_code,
            "container_authority_unconfigured",
        )
        self.assertEqual(local_calls, [])

    def test_tool_router_rejection_is_not_reported_as_a_process_exit(self) -> None:
        metadata = {
            "sandboxed": False,
            "isolation": {
                "backend": "container",
                "reason_code": "container_authority_unconfigured",
                "applied": False,
            },
        }

        def process_runner(command, **kwargs):
            raise IsolationExecutionError(
                "container_authority_unconfigured",
                metadata,
            )

        result = run_shell(
            {"command": "printf must-not-run"},
            ToolContext(
                self.workspace,
                "yolo",
                process_runner=process_runner,
            ),
        )

        self.assertTrue(result.is_error)
        self.assertFalse(result.metadata["sandboxed"])
        self.assertEqual(
            result.metadata["execution_v1"]["outcome"],
            {"kind": "not_run", "exit_code": None},
        )
        self.assertIn(
            "container_authority_unconfigured",
            result.content,
        )

    def inspect_payload(
        self,
        *,
        command_argv: tuple[str, ...],
        exited: bool,
        bind_mounts: list[tuple[Path, Path, bool]] | None = None,
        volume_mounts: list[tuple[str, Path, bool]] | None = None,
        exit_code: int = 0,
    ) -> dict[str, object]:
        command = (
            *GATE_COMMAND_PREFIX,
            "--attempt-id",
            ATTEMPT_ID,
            "--",
            *command_argv,
        )
        mounts = []
        host_mounts = []
        selected_mounts = bind_mounts
        if selected_mounts is None and volume_mounts is None:
            selected_mounts = [
                (self.workspace, self.workspace, True),
                (self.readable, self.readable, False),
            ]
        for source, destination, writable in selected_mounts or []:
            host_mount = {
                "Type": "bind",
                "Source": str(source),
                "Target": str(destination),
                "BindOptions": {
                    "Propagation": "rprivate",
                    "NonRecursive": True,
                },
            }
            if not writable:
                host_mount["ReadOnly"] = True
            host_mounts.append(host_mount)
            mounts.append(
                {
                    "Type": "bind",
                    "Source": str(source),
                    "Destination": str(destination),
                    "RW": writable,
                    "Propagation": "rprivate",
                }
            )
        for name, destination, writable in volume_mounts or []:
            host_mount = {
                "Type": "volume",
                "Source": name,
                "Target": str(destination),
                "VolumeOptions": {
                    "NoCopy": True,
                    "Subpath": _volume_subpath(name),
                },
            }
            if not writable:
                host_mount["ReadOnly"] = True
            host_mounts.append(host_mount)
            mounts.append(
                {
                    "Type": "volume",
                    "Name": name,
                    "Source": str(
                        self.root / "daemon-volume-source" / name
                    ),
                    "Destination": str(destination),
                    "Driver": "local",
                    "RW": writable,
                    "Propagation": "",
                }
            )
        return {
            "id": CONTAINER_ID,
            "name": f"/lca-{ATTEMPT_ID}",
            "instance_label": ATTEMPT_ID,
            "resource_label": CONTAINER_EXECUTION_RESOURCE,
            "config_image": f"sha256:{IMAGE_ID}",
            "image_id": f"sha256:{IMAGE_ID}",
            "config_user": f"{os.getuid()}:{os.getgid()}",
            "config_env": ["PATH=/usr/bin:/bin"],
            "entrypoint": [GATE_ENTRYPOINT],
            "cmd": list(command),
            "path": GATE_ENTRYPOINT,
            "args": list(command),
            "healthcheck": None,
            "stop_signal": "SIGTERM",
            "working_dir": str(self.workspace),
            "readonly_rootfs": True,
            "network_mode": "none",
            "pid_mode": "",
            "ipc_mode": "private",
            "uts_mode": "",
            "cgroupns_mode": "private",
            "cap_add": None,
            "cap_drop": ["ALL"],
            "devices": None,
            "device_requests": None,
            "volumes_from": None,
            "security_opt": ["no-new-privileges=true", "seccomp=builtin"],
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
            "state_status": "exited" if exited else "running",
            "state_running": not exited,
            "state_exit_code": exit_code,
            "state_oom_killed": False,
            "state_error": "",
            "mounts": mounts,
        }


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Run the explicit T-273 live Docker isolation acceptance matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_agent.execution.container_plan import CONTAINER_EXECUTION_RESOURCE
from local_agent.execution.container_plan import CONTAINER_INSTANCE_LABEL
from local_agent.execution.container_plan import CONTAINER_RESOURCE_LABEL
from local_agent.execution.contracts import ContainerBackendAuthority
from local_agent.execution.contracts import IsolationConfiguration
from local_agent.execution.contracts import IsolationRequest
from local_agent.tools.base import ToolContext
from local_agent.tools.container_runtime import ContainerExecutionRuntime
from local_agent.tools.isolation_routing import ConfiguredIsolationProcessRunner
from local_agent.tools.isolation_routing import IsolationExecutionError
from local_agent.tools.isolation_routing import isolation_metadata
from local_agent.tools.process_environment import build_container_control_environment
from local_agent.tools.process_runtime import run_process
from local_agent.tools.shell import run_tests
from local_agent.workspace.context import WorkspaceRootIdentity


REVISION = 23
SECRET_SENTINEL = "lca-live-matrix-secret-must-not-project"


class MatrixFailure(AssertionError):
    pass


@dataclass(frozen=True)
class DockerAuthority:
    executable: Path
    executable_sha256: str
    socket_path: Path
    client_config_directory: Path
    gate_image: str

    @property
    def control_environment(self) -> dict[str, str]:
        environment = build_container_control_environment(
            client_config_directory=self.client_config_directory
        )
        return dict(environment.values)

    @property
    def command_prefix(self) -> tuple[str, ...]:
        return (
            str(self.executable),
            "--config",
            str(self.client_config_directory),
            "--host",
            f"unix://{self.socket_path}",
        )


class MatrixCase:
    def __init__(self, parent: Path, name: str, authority: DockerAuthority) -> None:
        self.name = name
        self.root = parent / name
        self.root.mkdir(mode=0o700)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.readable = self.root / "additional"
        self.readable.mkdir()
        self.staging = self.root / "staging"
        self.staging.mkdir(mode=0o700)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.sibling = self.root / "sibling"
        self.sibling.mkdir()
        self.fake_home = self.root / "home"
        self.fake_home.mkdir()
        self.authority = authority

        (self.workspace / "replace.txt").write_text("before\n", encoding="utf-8")
        (self.workspace / "delete.txt").write_text("delete\n", encoding="utf-8")
        (self.workspace / "keep.txt").write_text("keep\n", encoding="utf-8")
        (self.readable / "additional.txt").write_text(
            "additional\n", encoding="utf-8"
        )
        (self.sibling / "secret.txt").write_text(
            SECRET_SENTINEL, encoding="utf-8"
        )
        (self.fake_home / ".credential").write_text(
            SECRET_SENTINEL, encoding="utf-8"
        )

    def backend_authority(
        self,
        *,
        transport: str = "staged-copy",
        gate_image: str | None = None,
    ) -> ContainerBackendAuthority:
        return ContainerBackendAuthority(
            executable=self.authority.executable,
            executable_sha256=self.authority.executable_sha256,
            socket_path=self.authority.socket_path,
            client_config_directory=self.authority.client_config_directory,
            gate_image=gate_image or self.authority.gate_image,
            workspace_transport=transport,
            staging_root=self.staging if transport == "staged-copy" else None,
        )

    def request(
        self,
        *,
        profile: str,
        network_policy: str = "deny",
    ) -> IsolationRequest:
        return IsolationRequest(
            mode="required",
            profile=profile,
            backend="container",
            network_policy=network_policy,
            workspace=self.workspace,
            readable_roots=(self.readable,),
            writable_roots=(
                (self.workspace,) if profile == "workspace-write" else ()
            ),
        )

    def context(
        self,
        *,
        attempt_id: str,
        process_runner: Callable[..., Any],
    ) -> ToolContext:
        identity = self.workspace.lstat()
        return ToolContext(
            workspace=self.workspace,
            approval_mode="yolo",
            state_dir=self.state,
            allowed_dirs=(self.readable,),
            session_id=f"live-{self.name}",
            run_id=f"run-{attempt_id}",
            tool_call_id=f"call-{attempt_id}",
            workspace_revision=REVISION,
            workspace_identity=WorkspaceRootIdentity(
                identity.st_dev,
                identity.st_ino,
            ),
            process_runner=process_runner,
        )

    def runtime(
        self,
        *,
        attempt_id: str,
        transport: str = "staged-copy",
        gate_image: str | None = None,
        process_runner: Callable[..., Any] = run_process,
    ) -> ContainerExecutionRuntime:
        return ContainerExecutionRuntime(
            self.backend_authority(
                transport=transport,
                gate_image=gate_image,
            ),
            control_environment=self.authority.control_environment,
            process_runner=process_runner,
            attempt_id_factory=lambda: attempt_id,
        )

    def configured_runner(
        self,
        *,
        attempt_id: str,
        profile: str,
        network_policy: str = "deny",
        runtime: ContainerExecutionRuntime | Any | None = None,
    ) -> ConfiguredIsolationProcessRunner:
        authority = self.backend_authority()
        return ConfiguredIsolationProcessRunner(
            IsolationConfiguration(
                mode="required",
                profile=profile,
                backend="container",
                network_policy=network_policy,
                container=authority,
            ),
            container_runtime=runtime
            or self.runtime(attempt_id=attempt_id),
            local_runner=lambda *args, **kwargs: _fail("local fallback executed"),
        )

    def run_shell(
        self,
        command: str,
        *,
        profile: str,
        network_policy: str = "deny",
        attempt_id: str | None = None,
        runtime: ContainerExecutionRuntime | Any | None = None,
        timeout: float = 30,
        cancel_event=None,
    ):
        selected_attempt = attempt_id or uuid.uuid4().hex
        runner = self.configured_runner(
            attempt_id=selected_attempt,
            profile=profile,
            network_policy=network_policy,
            runtime=runtime,
        )
        context = self.context(
            attempt_id=selected_attempt,
            process_runner=runner,
        )
        return runner(
            command,
            cwd=self.workspace,
            shell=True,
            timeout=timeout,
            cancel_event=cancel_event,
            context=context,
        )

    def assert_closed(self, attempt_id: str) -> None:
        entries = {path.name for path in self.staging.iterdir()}
        if not entries:
            self.assert_container_closed(attempt_id)
            return
        _require(
            entries == {".lca-staging-journal", ".lca-staging.lock"},
            f"staging authority contains unexpected entries: {sorted(entries)}",
        )
        journal = self.staging / ".lca-staging-journal"
        _require(
            tuple(journal.iterdir()) == (),
            "staging journal retains an unresolved record",
        )
        self.assert_container_closed(attempt_id)

    def assert_recovery_retained(
        self,
        attempt_id: str,
        *,
        expected_state: str,
    ) -> None:
        entries = {path.name for path in self.staging.iterdir()}
        _require(
            entries
            == {
                ".lca-staging-journal",
                ".lca-staging.lock",
                attempt_id,
            },
            f"staging recovery entries are incomplete: {sorted(entries)}",
        )
        records = tuple(
            (self.staging / ".lca-staging-journal").iterdir()
        )
        _require(
            tuple(path.name for path in records)
            == (f"{attempt_id}.json",),
            "staging recovery journal correlation is invalid",
        )
        record = json.loads(records[0].read_text(encoding="utf-8"))
        _require(
            record.get("attempt_id") == attempt_id
            and record.get("state") == expected_state,
            "staging recovery record lost its create obligation: "
            f"expected={expected_state!r}, state={record.get('state')!r}",
        )

    def assert_container_closed(self, attempt_id: str) -> None:
        completed = subprocess.run(
            [
                *self.authority.command_prefix,
                "ps",
                "--all",
                "--no-trunc",
                "--filter",
                f"label=io.local-agent.instance={attempt_id}",
                "--format",
                "{{.ID}}",
            ],
            cwd=self.authority.client_config_directory,
            env=self.authority.control_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
        _require(completed.returncode == 0, "cleanup absence query failed")
        _require(not completed.stderr, "cleanup absence query emitted stderr")
        _require(not completed.stdout.strip(), "container instance remains")


class DockerControlHook:
    def __init__(
        self,
        *,
        attempt_id: str,
        before_stage_copy: Callable[[Path], None] | None = None,
        release_event: threading.Event | None = None,
        raise_on_final_inspect: bool = False,
    ) -> None:
        self.attempt_id = attempt_id
        self.before_stage_copy = before_stage_copy
        self.release_event = release_event
        self.raise_on_final_inspect = raise_on_final_inspect
        self.execution_container_id: str | None = None
        self.inspect_count = 0
        self.inspect_payloads: list[dict[str, object]] = []
        self.final_inspect_fault_used = False

    def __call__(self, command, **kwargs):
        argv = tuple(str(item) for item in command)
        verb = argv[5] if len(argv) > 5 else ""
        execution_create = _is_execution_create(argv, self.attempt_id)
        if (
            verb == "cp"
            and len(argv) >= 8
            and ":" not in argv[-2]
            and ":" in argv[-1]
            and self.before_stage_copy is not None
        ):
            self.before_stage_copy(Path(argv[-2].removesuffix("/.")))
        execution_inspect = _targets_execution_container(
            argv,
            verb="inspect",
            container_id=self.execution_container_id,
        )
        if execution_inspect:
            self.inspect_count += 1
            if self.raise_on_final_inspect and self.inspect_count == 2:
                self.final_inspect_fault_used = True
                raise RuntimeError("injected final-inspect parent exception")
        completed = run_process(command, **kwargs)
        if execution_create and completed.returncode == 0:
            self.execution_container_id = _created_container_id(completed)
        if execution_inspect and completed.returncode == 0:
            payload = json.loads(completed.stdout)
            if isinstance(payload, dict):
                self.inspect_payloads.append(payload)
        if (
            verb == "kill"
            and "--signal=SIGUSR1" in argv
            and completed.returncode == 0
            and self.release_event is not None
        ):
            self.release_event.set()
        return completed


def _option_values(argv: tuple[str, ...], option: str) -> tuple[str, ...]:
    return tuple(
        argv[index + 1]
        for index, value in enumerate(argv[:-1])
        if value == option
    )


def _is_execution_create(argv: tuple[str, ...], attempt_id: str) -> bool:
    labels = _option_values(argv, "--label")
    return (
        len(argv) > 5
        and argv[5] == "create"
        and _option_values(argv, "--name") == (f"lca-{attempt_id}",)
        and f"{CONTAINER_INSTANCE_LABEL}={attempt_id}" in labels
        and f"{CONTAINER_RESOURCE_LABEL}={CONTAINER_EXECUTION_RESOURCE}" in labels
    )


def _created_container_id(completed) -> str:
    container_id = completed.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
        raise MatrixFailure("execution create returned an invalid container id")
    return container_id


def _targets_execution_container(
    argv: tuple[str, ...],
    *,
    verb: str,
    container_id: str | None,
) -> bool:
    return (
        container_id is not None
        and len(argv) > 6
        and argv[5] == verb
        and argv[-1] == container_id
    )


class MutatingRuntime:
    def __init__(self, runtime: ContainerExecutionRuntime, mutation: Callable[[], None]):
        self._runtime = runtime
        self._mutation = mutation

    def execute(self, **kwargs):
        outcome = self._runtime.execute(**kwargs)
        self._mutation()
        return outcome


class LiveMatrix:
    def __init__(self, authority: DockerAuthority, *, temp_root: Path) -> None:
        self.authority = authority
        self._temporary = tempfile.TemporaryDirectory(
            prefix="lca-t273-live-",
            dir=temp_root,
        )
        self.root = Path(self._temporary.name).resolve()
        self.results: list[dict[str, str]] = []

    def close(self) -> None:
        self._temporary.cleanup()

    def run(self) -> bool:
        cases = (
            ("daemon-and-image", self.daemon_and_image),
            ("direct-bind-unsupported", self.direct_bind_unsupported),
            ("staged-text-commit", self.staged_text_commit),
            ("read-only-and-additional", self.read_only_and_additional),
            ("run-tests-environment", self.run_tests_environment),
            ("visibility-and-metadata", self.visibility_and_metadata),
            ("network-deny", self.network_deny),
            ("manifest-mismatch", self.manifest_mismatch),
            ("staging-source-aba", self.staging_source_aba),
            ("unsupported-output", self.unsupported_output),
            ("host-snapshot-stale", self.host_snapshot_stale),
            ("timeout-cleanup", self.timeout_cleanup),
            ("cancel-cleanup", self.cancel_cleanup),
            ("parent-exception-cleanup", self.parent_exception_cleanup),
            ("invalid-authority-and-image", self.invalid_authority_and_image),
        )
        for name, operation in cases:
            started = time.monotonic()
            try:
                operation()
            except BaseException as exc:
                self.results.append(
                    {
                        "case": name,
                        "status": "FAIL",
                        "detail": f"{type(exc).__name__}: {exc}",
                        "seconds": f"{time.monotonic() - started:.3f}",
                    }
                )
            else:
                self.results.append(
                    {
                        "case": name,
                        "status": "PASS",
                        "detail": "verified",
                        "seconds": f"{time.monotonic() - started:.3f}",
                    }
                )
        return all(result["status"] == "PASS" for result in self.results)

    def case(self, name: str) -> MatrixCase:
        return MatrixCase(self.root, name, self.authority)

    def daemon_and_image(self) -> None:
        completed = subprocess.run(
            [*self.authority.command_prefix, "version", "--format", "{{json .Server}}"],
            cwd=self.authority.client_config_directory,
            env=self.authority.control_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
        _require(completed.returncode == 0, "Docker daemon unavailable")
        _require(not completed.stderr, "Docker version emitted stderr")
        payload = json.loads(completed.stdout)
        _require(payload.get("Os") == "linux", "Docker server is not Linux")

    def direct_bind_unsupported(self) -> None:
        case = self.case("direct-bind-unsupported")
        attempt = uuid.uuid4().hex
        control_calls: list[tuple[str, ...]] = []

        def reject_control_process(command, **kwargs):
            del kwargs
            control_calls.append(tuple(str(item) for item in command))
            raise MatrixFailure("direct-bind started a Docker control process")

        outcome = case.runtime(
            attempt_id=attempt,
            transport="direct-bind",
            process_runner=reject_control_process,
        ).execute(
            request=case.request(profile="workspace-write"),
            workspace_roots=(case.workspace, case.readable),
            workspace_roots_revision=REVISION,
            working_directory=case.workspace,
            command_argv=(
                "/bin/sh",
                "-c",
                f"printf released > {shlex.quote(str(case.workspace / 'direct-ran.txt'))}",
            ),
            timeout=30,
            cancel_event=None,
        )
        _require(
            outcome.reason_code == "direct_bind_unsupported",
            f"unexpected direct-bind result: {outcome.reason_code}",
        )
        _require(not outcome.command_released, "direct-bind command was released")
        _require(outcome.cleanup is None, "unsupported direct-bind acquired cleanup state")
        _require(not control_calls, "unsupported direct-bind reached Docker control plane")
        _require(
            not (case.workspace / "direct-ran.txt").exists(),
            "unsupported direct-bind executed user code",
        )
        _require(tuple(case.staging.iterdir()) == (), "direct-bind created staging state")

    def staged_text_commit(self) -> None:
        case = self.case("staged-text-commit")
        attempt = uuid.uuid4().hex
        hook = DockerControlHook(attempt_id=attempt)
        command = " && ".join(
            (
                f"printf 'after\\n' > {shlex.quote(str(case.workspace / 'replace.txt'))}",
                f"rm {shlex.quote(str(case.workspace / 'delete.txt'))}",
                f"printf 'created\\n' > {shlex.quote(str(case.workspace / 'create.txt'))}",
                "printf 'matrix-output\\n'",
                "exit 7",
            )
        )
        try:
            completed = case.run_shell(
                command,
                profile="workspace-write",
                attempt_id=attempt,
                runtime=case.runtime(
                    attempt_id=attempt,
                    process_runner=hook,
                ),
            )
        except IsolationExecutionError as exc:
            payload = hook.inspect_payloads[-1] if hook.inspect_payloads else {}
            summary = {
                "reason_code": exc.reason_code,
                "host_mounts": payload.get("host_mounts"),
                "mounts": payload.get("mounts"),
            }
            raise MatrixFailure(json.dumps(summary, sort_keys=True)) from exc
        _require(completed.returncode == 7, "nonzero command status was lost")
        _require(completed.stdout == "matrix-output\n", "user stdout mismatch")
        _require(
            (case.workspace / "replace.txt").read_text(encoding="utf-8")
            == "after\n",
            "replace was not committed",
        )
        _require(not (case.workspace / "delete.txt").exists(), "delete was not committed")
        _require(
            (case.workspace / "create.txt").read_text(encoding="utf-8")
            == "created\n",
            "create was not committed",
        )
        metadata = isolation_metadata(completed)
        _require(metadata.get("workspace_changed") is True, "write attribution missing")
        _require(
            metadata["isolation"]["workspace_output_commit"]["state"] == "committed",
            "transaction commit proof missing",
        )
        journal = case.state / "patches" / f"live-{case.name}.jsonl"
        record = json.loads(journal.read_text(encoding="utf-8"))
        _require(record["source"] == "container_staged_copy", "journal source mismatch")
        _require(
            [item["operation"] for item in record["files"]]
            == ["create", "delete", "replace"],
            "journal file operations mismatch",
        )
        case.assert_closed(attempt)

    def read_only_and_additional(self) -> None:
        readonly = self.case("read-only")
        readonly_attempt = uuid.uuid4().hex
        target = readonly.workspace / "replace.txt"
        command = (
            f"if printf denied > {shlex.quote(str(target))} 2>/dev/null; "
            "then exit 41; fi; printf 'read-only-ok\\n'"
        )
        completed = readonly.run_shell(
            command,
            profile="read-only",
            attempt_id=readonly_attempt,
        )
        _require(completed.returncode == 0, "read-only write unexpectedly succeeded")
        _require(target.read_text(encoding="utf-8") == "before\n", "host workspace changed")
        readonly.assert_closed(readonly_attempt)

        additional = self.case("additional-read-only")
        additional_attempt = uuid.uuid4().hex
        target = additional.readable / "additional.txt"
        command = (
            f"if printf denied > {shlex.quote(str(target))} 2>/dev/null; "
            "then exit 42; fi; printf 'additional-ok\\n'"
        )
        completed = additional.run_shell(
            command,
            profile="workspace-write",
            attempt_id=additional_attempt,
        )
        _require(completed.returncode == 0, "additional-root write unexpectedly succeeded")
        _require(
            target.read_text(encoding="utf-8") == "additional\n",
            "additional host root changed",
        )
        additional.assert_closed(additional_attempt)

    def run_tests_environment(self) -> None:
        case = self.case("run-tests-environment")
        attempt = uuid.uuid4().hex
        wrapper = case.workspace / "mvnw"
        wrapper.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            '[ "$LCA_TEST_ENV" = "container-value with spaces" ]\n'
            '[ "${AI_API_KEY+x}" != x ]\n'
            "printf 'run-tests-environment-ok\\n'\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        runner = case.configured_runner(
            attempt_id=attempt,
            profile="read-only",
        )
        context = case.context(
            attempt_id=attempt,
            process_runner=runner,
        )

        result = run_tests(
            {
                "command": (
                    "LCA_TEST_ENV='container-value with spaces' "
                    "AI_API_KEY=must-not-enter-container ./mvnw test"
                ),
                "timeout": 30,
            },
            context,
        )

        _require(not result.is_error, f"run_tests failed: {result.content}")
        _require(
            "run-tests-environment-ok" in result.content,
            "run_tests environment was not observed in the container",
        )
        _require(result.metadata["sandboxed"] is True, "run_tests isolation proof missing")
        _require(
            "must-not-enter-container" not in json.dumps(
                result.metadata.get("isolation", {}),
                sort_keys=True,
            ),
            "provider credential entered isolation metadata",
        )
        case.assert_closed(attempt)

    def visibility_and_metadata(self) -> None:
        case = self.case("visibility-and-metadata")
        attempt = uuid.uuid4().hex
        hidden = (
            case.sibling / "secret.txt",
            case.fake_home / ".credential",
            case.state / "session.jsonl",
            self.authority.socket_path,
        )
        checks = " && ".join(
            f"test ! -e {shlex.quote(str(path))}" for path in hidden
        )
        completed = case.run_shell(
            f"{checks} && printf 'visibility-ok\\n'",
            profile="read-only",
            attempt_id=attempt,
        )
        _require(completed.returncode == 0, "an unauthorized host path was visible")
        rendered = json.dumps(isolation_metadata(completed), sort_keys=True)
        _require(SECRET_SENTINEL not in rendered, "secret entered metadata")
        _require(str(self.authority.socket_path) not in rendered, "socket path entered metadata")
        _require(
            isolation_metadata(completed)["isolation"]["workspace_transport"]
            == "staged-copy",
            "transport proof missing",
        )
        case.assert_closed(attempt)

    def network_deny(self) -> None:
        case = self.case("network-deny")
        attempt = uuid.uuid4().hex
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host_port = server.getsockname()[1]
        command = " ; ".join(
            (
                "set -eu",
                "if /bin/busybox nslookup example.com >/dev/null 2>&1; then exit 51; fi",
                "if /bin/busybox nc -z -w 1 1.1.1.1 53 >/dev/null 2>&1; then exit 52; fi",
                f"if /bin/busybox nc -z -w 1 127.0.0.1 {host_port} >/dev/null 2>&1; then exit 53; fi",
                "rm -f /tmp/lca-loopback",
                "/bin/busybox nc -l -p 18081 > /tmp/lca-loopback & server=$!",
                "sleep 0.2",
                "printf loopback | /bin/busybox nc -w 2 127.0.0.1 18081",
                "wait $server",
                "test \"$(cat /tmp/lca-loopback)\" = loopback",
                "printf 'network-deny-ok\\n'",
            )
        )
        try:
            completed = case.run_shell(
                command,
                profile="read-only",
                attempt_id=attempt,
                timeout=30,
            )
        finally:
            server.close()
        _require(completed.returncode == 0, f"network deny matrix failed: {completed.stderr}")
        _require(completed.stdout == "network-deny-ok\n", "network result mismatch")
        case.assert_closed(attempt)

    def manifest_mismatch(self) -> None:
        case = self.case("manifest-mismatch")
        attempt = uuid.uuid4().hex

        def tamper(source: Path) -> None:
            if source.name == "root-0000":
                (source / "keep.txt").write_text("tamper\n", encoding="utf-8")

        hook = DockerControlHook(
            attempt_id=attempt,
            before_stage_copy=tamper,
        )
        outcome = case.runtime(
            attempt_id=attempt,
            process_runner=hook,
        ).execute(
            request=case.request(profile="workspace-write"),
            workspace_roots=(case.workspace, case.readable),
            workspace_roots_revision=REVISION,
            working_directory=case.workspace,
            command_argv=("/bin/sh", "-c", "printf should-not-run"),
            timeout=30,
            cancel_event=None,
        )
        _require(
            outcome.reason_code == "stage_source_snapshot_changed",
            f"manifest mismatch result: {outcome.reason_code}",
        )
        _require(not outcome.command_released, "manifest mismatch released command")
        _require(outcome.cleanup is not None and outcome.cleanup.verified, "cleanup failed")
        _require(
            outcome.staging_cleanup is not None and outcome.staging_cleanup.verified,
            "staging cleanup failed",
        )
        case.assert_closed(attempt)

    def staging_source_aba(self) -> None:
        case = self.case("staging-source-aba")
        attempt = uuid.uuid4().hex

        def replace_restore(source: Path) -> None:
            if source.name == "root-0000":
                moved = source.with_name(f"{source.name}-moved")
                source.rename(moved)
                moved.rename(source)

        hook = DockerControlHook(
            attempt_id=attempt,
            before_stage_copy=replace_restore,
        )
        outcome = case.runtime(
            attempt_id=attempt,
            process_runner=hook,
        ).execute(
            request=case.request(profile="workspace-write"),
            workspace_roots=(case.workspace, case.readable),
            workspace_roots_revision=REVISION,
            working_directory=case.workspace,
            command_argv=("/bin/sh", "-c", "printf should-not-run"),
            timeout=30,
            cancel_event=None,
        )
        _require(
            outcome.reason_code == "staging_cleanup_unverified",
            f"staging ABA result: {outcome.reason_code}",
        )
        _require(not outcome.command_released, "staging ABA released command")
        _require(
            outcome.staging_cleanup is not None
            and not outcome.staging_cleanup.verified
            and outcome.staging_cleanup.unresolved,
            "staging ABA cleanup was not retained as unresolved",
        )
        case.assert_container_closed(attempt)

    def unsupported_output(self) -> None:
        binary = self.case("unsupported-binary")
        binary_attempt = uuid.uuid4().hex
        try:
            binary.run_shell(
                f"printf '\\377' > {shlex.quote(str(binary.workspace / 'binary.dat'))}",
                profile="workspace-write",
                attempt_id=binary_attempt,
            )
        except IsolationExecutionError as exc:
            _require(
                exc.reason_code
                == "staging_output_changed_file_not_utf8",
                f"binary output result: {exc.reason_code}",
            )
        else:
            _fail("binary staged output was accepted")
        _require(not (binary.workspace / "binary.dat").exists(), "binary reached host")
        binary.assert_closed(binary_attempt)

        symlink = self.case("unsupported-symlink")
        symlink_attempt = uuid.uuid4().hex
        try:
            symlink.run_shell(
                "ln -s keep.txt unsupported-link",
                profile="workspace-write",
                attempt_id=symlink_attempt,
            )
        except IsolationExecutionError as exc:
            _require(
                "unsupported_entry_type" in exc.reason_code,
                f"symlink output result: {exc.reason_code}",
            )
        else:
            _fail("symlink staged output was accepted")
        _require(not (symlink.workspace / "unsupported-link").exists(), "symlink reached host")
        symlink.assert_closed(symlink_attempt)

    def host_snapshot_stale(self) -> None:
        case = self.case("host-snapshot-stale")
        attempt = uuid.uuid4().hex
        runtime = MutatingRuntime(
            case.runtime(attempt_id=attempt),
            lambda: (case.workspace / "replace.txt").write_text(
                "host-concurrent\n", encoding="utf-8"
            ),
        )
        try:
            case.run_shell(
                f"printf 'container\\n' > {shlex.quote(str(case.workspace / 'replace.txt'))}",
                profile="workspace-write",
                attempt_id=attempt,
                runtime=runtime,
            )
        except IsolationExecutionError as exc:
            _require(
                exc.reason_code == "container_workspace_commit_stale",
                f"stale result: {exc.reason_code}",
            )
        else:
            _fail("stale host snapshot was committed")
        _require(
            (case.workspace / "replace.txt").read_text(encoding="utf-8")
            == "host-concurrent\n",
            "stale rejection overwrote concurrent host state",
        )
        case.assert_closed(attempt)

    def timeout_cleanup(self) -> None:
        case = self.case("timeout-cleanup")
        attempt = uuid.uuid4().hex
        target = case.workspace / "timeout.txt"
        outcome = case.runtime(attempt_id=attempt).execute(
            request=case.request(profile="workspace-write"),
            workspace_roots=(case.workspace, case.readable),
            workspace_roots_revision=REVISION,
            working_directory=case.workspace,
            command_argv=(
                "/bin/sh",
                "-c",
                f"printf partial > {shlex.quote(str(target))}; sleep 30",
            ),
            timeout=3,
            cancel_event=None,
        )
        _require(outcome.execution_outcome == "timed_out", "timeout was not typed")
        _require(outcome.cleanup is not None and outcome.cleanup.verified, "cleanup failed")
        _require(
            outcome.staging_cleanup is not None and outcome.staging_cleanup.verified,
            "staging cleanup failed",
        )
        _require(not target.exists(), "timed-out staged output reached host")
        case.assert_closed(attempt)

    def cancel_cleanup(self) -> None:
        case = self.case("cancel-cleanup")
        attempt = uuid.uuid4().hex
        target = case.workspace / "cancel.txt"
        cancellation = threading.Event()
        released = threading.Event()
        hook = DockerControlHook(
            attempt_id=attempt,
            release_event=released,
        )

        def request_cancel() -> None:
            if released.wait(timeout=20):
                time.sleep(0.2)
                cancellation.set()

        waiter = threading.Thread(target=request_cancel, daemon=True)
        waiter.start()
        outcome = case.runtime(
            attempt_id=attempt,
            process_runner=hook,
        ).execute(
            request=case.request(profile="workspace-write"),
            workspace_roots=(case.workspace, case.readable),
            workspace_roots_revision=REVISION,
            working_directory=case.workspace,
            command_argv=(
                "/bin/sh",
                "-c",
                f"printf partial > {shlex.quote(str(target))}; sleep 30",
            ),
            timeout=25,
            cancel_event=cancellation,
        )
        waiter.join(timeout=2)
        _require(released.is_set(), "cancel did not observe released command")
        _require(outcome.execution_outcome == "cancelled", "cancel was not typed")
        _require(outcome.cleanup is not None and outcome.cleanup.verified, "cleanup failed")
        _require(
            outcome.staging_cleanup is not None and outcome.staging_cleanup.verified,
            "staging cleanup failed",
        )
        _require(not target.exists(), "cancelled staged output reached host")
        case.assert_closed(attempt)

    def parent_exception_cleanup(self) -> None:
        case = self.case("parent-exception-cleanup")
        attempt = uuid.uuid4().hex
        hook = DockerControlHook(
            attempt_id=attempt,
            raise_on_final_inspect=True,
        )
        outcome = case.runtime(
            attempt_id=attempt,
            process_runner=hook,
        ).execute(
            request=case.request(profile="workspace-write"),
            workspace_roots=(case.workspace, case.readable),
            workspace_roots_revision=REVISION,
            working_directory=case.workspace,
            command_argv=("/bin/sh", "-c", "printf completed"),
            timeout=30,
            cancel_event=None,
        )
        _require(
            outcome.reason_code == "container_runtime_exception",
            f"unexpected parent failure result: {outcome.reason_code}",
        )
        _require(
            hook.final_inspect_fault_used,
            "parent failure did not target the final execution inspect",
        )
        _require(
            outcome.cleanup is not None and outcome.cleanup.verified,
            "parent failure cleanup was not verified",
        )
        _require(
            "final-inspect" not in repr(outcome),
            "raw parent exception leaked into the typed outcome",
        )
        case.assert_closed(attempt)

    def invalid_authority_and_image(self) -> None:
        invalid_socket = self.case("invalid-socket")
        invalid_attempt = uuid.uuid4().hex
        missing_socket = invalid_socket.root / "missing.sock"
        authority = ContainerBackendAuthority(
            executable=self.authority.executable,
            executable_sha256=self.authority.executable_sha256,
            socket_path=missing_socket,
            client_config_directory=self.authority.client_config_directory,
            gate_image=self.authority.gate_image,
            workspace_transport="staged-copy",
            staging_root=invalid_socket.staging,
        )
        outcome = ContainerExecutionRuntime(
            authority,
            control_environment=self.authority.control_environment,
            attempt_id_factory=lambda: invalid_attempt,
        ).execute(
            request=invalid_socket.request(profile="read-only"),
            workspace_roots=(invalid_socket.workspace, invalid_socket.readable),
            workspace_roots_revision=REVISION,
            working_directory=invalid_socket.workspace,
            command_argv=("/bin/sh", "-c", "exit 99"),
            timeout=10,
            cancel_event=None,
        )
        _require(
            outcome.reason_code == "container_authority_invalid",
            f"invalid socket result: {outcome.reason_code}",
        )
        _require(not outcome.command_released, "invalid authority released command")
        invalid_socket.assert_closed(invalid_attempt)

        invalid_image = self.case("invalid-image")
        image_attempt = uuid.uuid4().hex
        outcome = invalid_image.runtime(
            attempt_id=image_attempt,
            gate_image=f"sha256:{'0' * 64}",
        ).execute(
            request=invalid_image.request(profile="read-only"),
            workspace_roots=(invalid_image.workspace, invalid_image.readable),
            workspace_roots_revision=REVISION,
            working_directory=invalid_image.workspace,
            command_argv=("/bin/sh", "-c", "exit 99"),
            timeout=15,
            cancel_event=None,
        )
        _require(
            outcome.reason_code == "image_failed",
            f"invalid image result: {outcome.reason_code}",
        )
        _require(not outcome.command_released, "invalid image released command")
        invalid_image.assert_closed(image_attempt)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MatrixFailure(message)


def _fail(message: str):
    raise MatrixFailure(message)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-executable", required=True)
    parser.add_argument("--docker-sha256", required=True)
    parser.add_argument("--socket-path", required=True)
    parser.add_argument("--client-config-directory", required=True)
    parser.add_argument(
        "--gate-image",
        required=True,
        help="Exact local image ID or repository digest; tags are rejected.",
    )
    parser.add_argument(
        "--temp-root",
        default="/private/tmp",
        help="Existing directory used only for per-run private fixtures.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    executable = Path(args.docker_executable).expanduser().resolve(strict=True)
    observed_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
    _require(
        observed_sha256 == args.docker_sha256,
        "Docker executable digest does not match --docker-sha256",
    )
    authority = DockerAuthority(
        executable=executable,
        executable_sha256=args.docker_sha256,
        socket_path=Path(args.socket_path).expanduser().resolve(strict=False),
        client_config_directory=Path(
            args.client_config_directory
        ).expanduser().resolve(strict=True),
        gate_image=args.gate_image,
    )
    matrix = LiveMatrix(
        authority,
        temp_root=Path(args.temp_root).expanduser().resolve(strict=True),
    )
    try:
        passed = matrix.run()
        print(json.dumps(matrix.results, indent=2, sort_keys=True))
        print(
            f"T-273 live container matrix: "
            f"{sum(item['status'] == 'PASS' for item in matrix.results)}/"
            f"{len(matrix.results)} passed"
        )
        return 0 if passed else 1
    finally:
        matrix.close()


if __name__ == "__main__":
    raise SystemExit(main())

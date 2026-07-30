#!/usr/bin/env python3
"""Run T-273 real-Docker fault-injection isolation acceptance cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import run_container_isolation_matrix as live

from local_agent.execution.contracts import ContainerBackendAuthority
from local_agent.tools.process_output import BoundedByteCapture
from local_agent.tools.process_output import CapturedCompletedProcess
from local_agent.tools.process_output import ProcessOutputCapture
from local_agent.tools.process_runtime import run_process


class DockerFaultHook:
    """Inject a typed control-plane fault while retaining the real daemon."""

    def __init__(
        self,
        *,
        attempt_id: str,
        attempt_path: Path | None = None,
        permission_denied: bool = False,
        inspect_mismatch: bool = False,
        attempt_aba: bool = False,
        create_parent_failure: bool = False,
        remove_nonzero: bool = False,
        removal_check_parent_failure: bool = False,
    ) -> None:
        self.attempt_id = attempt_id
        self.attempt_path = attempt_path
        self.permission_denied = permission_denied
        self.inspect_mismatch = inspect_mismatch
        self.attempt_aba = attempt_aba
        self.create_parent_failure = create_parent_failure
        self.remove_nonzero = remove_nonzero
        self.removal_check_parent_failure = removal_check_parent_failure
        self.release_seen = False
        self._execution_container_id: str | None = None
        self._inspect_fault_used = False
        self._remove_fault_used = False
        self._removal_check_fault_used = False
        if self.attempt_aba and self.attempt_path is None:
            raise ValueError("attempt ABA injection requires its exact staging path")

    @property
    def remove_fault_used(self) -> bool:
        return self._remove_fault_used

    def __call__(self, command, **kwargs):
        argv = tuple(str(item) for item in command)
        verb = argv[5] if len(argv) > 5 else ""
        execution_create = live._is_execution_create(argv, self.attempt_id)
        if verb == "version" and self.permission_denied:
            raise PermissionError("fault-injected Docker authority denial")
        if execution_create and self.attempt_aba:
            assert self.attempt_path is not None
            completed = self._create_through_attempt_aba(
                argv,
                kwargs,
                self.attempt_path,
            )
        else:
            completed = run_process(command, **kwargs)
        if execution_create and completed.returncode == 0:
            self._execution_container_id = live._created_container_id(completed)
            if self.create_parent_failure:
                raise RuntimeError("fault-injected parent failure after create")
        if (
            self.inspect_mismatch
            and not self._inspect_fault_used
            and live._targets_execution_container(
                argv,
                verb="inspect",
                container_id=self._execution_container_id,
            )
            and completed.returncode == 0
        ):
            self._inspect_fault_used = True
            completed = _rewrite_inspect_network(completed)
        if (
            verb == "kill"
            and "--signal=SIGUSR1" in argv
            and completed.returncode == 0
        ):
            self.release_seen = True
        if (
            self.remove_nonzero
            and not self._remove_fault_used
            and _is_execution_remove(argv, self._execution_container_id)
            and completed.returncode == 0
        ):
            self._remove_fault_used = True
            completed = CapturedCompletedProcess(
                list(argv),
                73,
                completed.output_capture,
            )
        if (
            self.removal_check_parent_failure
            and not self._removal_check_fault_used
            and _is_execution_removal_check(
                argv,
                self._execution_container_id,
            )
        ):
            self._removal_check_fault_used = True
            raise RuntimeError("fault-injected removal-check parent failure")
        return completed

    @staticmethod
    def _create_through_attempt_aba(argv, kwargs, attempt_path: Path):
        moved = attempt_path.with_name(f"{attempt_path.name}-matrix-moved")
        attempt_path.rename(moved)
        shutil.copytree(moved, attempt_path, symlinks=True)
        try:
            return run_process(list(argv), **kwargs)
        finally:
            shutil.rmtree(attempt_path)
            moved.rename(attempt_path)


def _is_execution_remove(
    argv: tuple[str, ...],
    container_id: str | None,
) -> bool:
    return (
        container_id is not None
        and argv[5:] == ("rm", "--force", "--volumes", container_id)
    )


def _is_execution_removal_check(
    argv: tuple[str, ...],
    container_id: str | None,
) -> bool:
    return (
        container_id is not None
        and len(argv) > 6
        and argv[5] == "ps"
        and live._option_values(argv, "--filter") == (f"id={container_id}",)
    )


class FaultMatrix:
    def __init__(self, authority: live.DockerAuthority, *, temp_root: Path) -> None:
        self.authority = authority
        self._temporary = tempfile.TemporaryDirectory(
            prefix="lca-t273-live-fault-",
            dir=temp_root,
        )
        self.root = Path(self._temporary.name).resolve()
        self.results: list[dict[str, str]] = []

    def close(self) -> None:
        self._temporary.cleanup()

    def run(self) -> bool:
        cases = (
            ("daemon-unavailable", "real-cli-dead-socket", self.daemon_unavailable),
            (
                "permission-denied",
                "deterministic-fault-real-authority",
                self.permission_denied,
            ),
            (
                "inspect-mismatch",
                "deterministic-fault-real-daemon",
                self.inspect_mismatch,
            ),
            (
                "attempt-directory-aba",
                "real-filesystem-real-daemon",
                self.attempt_directory_aba,
            ),
            (
                "create-ambiguity-recovery",
                "deterministic-fault-real-daemon",
                self.create_ambiguity_recovery,
            ),
            (
                "remove-nonzero-exact-absence",
                "deterministic-fault-real-daemon",
                self.remove_nonzero_exact_absence,
            ),
            (
                "cleanup-check-parent-failure",
                "deterministic-fault-real-daemon",
                self.cleanup_check_parent_failure,
            ),
            (
                "staging-output-parent-failure",
                "deterministic-fault-real-daemon",
                self.staging_output_parent_failure,
            ),
        )
        for name, evidence_kind, operation in cases:
            started = time.monotonic()
            try:
                operation()
            except BaseException as exc:
                self.results.append(
                    {
                        "case": name,
                        "evidence_kind": evidence_kind,
                        "status": "FAIL",
                        "detail": f"{type(exc).__name__}: {exc}",
                        "seconds": f"{time.monotonic() - started:.3f}",
                    }
                )
            else:
                self.results.append(
                    {
                        "case": name,
                        "evidence_kind": evidence_kind,
                        "status": "PASS",
                        "detail": "verified",
                        "seconds": f"{time.monotonic() - started:.3f}",
                    }
                )
        return all(result["status"] == "PASS" for result in self.results)

    def case(self, name: str) -> live.MatrixCase:
        return live.MatrixCase(self.root, name, self.authority)

    def daemon_unavailable(self) -> None:
        case = self.case("daemon-unavailable")
        attempt = uuid.uuid4().hex
        dead_socket = case.root / "dead.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(dead_socket))
        listener.close()
        os.chmod(dead_socket, 0o600)
        authority = ContainerBackendAuthority(
            executable=self.authority.executable,
            executable_sha256=self.authority.executable_sha256,
            socket_path=dead_socket,
            client_config_directory=self.authority.client_config_directory,
            gate_image=self.authority.gate_image,
            workspace_transport="staged-copy",
            staging_root=case.staging,
        )
        outcome = live.ContainerExecutionRuntime(
            authority,
            control_environment=self.authority.control_environment,
            process_runner=run_process,
            attempt_id_factory=lambda: attempt,
        ).execute(
            request=case.request(profile="read-only"),
            workspace_roots=(case.workspace, case.readable),
            workspace_roots_revision=live.REVISION,
            working_directory=case.workspace,
            command_argv=("/bin/sh", "-c", "exit 91"),
            timeout=10,
            cancel_event=None,
        )
        live._require(outcome.reason_code == "probe_failed", outcome.reason_code)
        live._require(not outcome.command_released, "dead daemon released command")
        live._require(
            outcome.staging_cleanup is not None
            and outcome.staging_cleanup.verified,
            "dead-daemon staging cleanup failed",
        )
        case.assert_closed(attempt)

    def permission_denied(self) -> None:
        case = self.case("permission-denied")
        attempt = uuid.uuid4().hex
        hook = DockerFaultHook(
            attempt_id=attempt,
            permission_denied=True,
        )
        outcome = case.runtime(
            attempt_id=attempt,
            process_runner=hook,
        ).execute(
            request=case.request(profile="read-only"),
            workspace_roots=(case.workspace, case.readable),
            workspace_roots_revision=live.REVISION,
            working_directory=case.workspace,
            command_argv=("/bin/sh", "-c", "exit 92"),
            timeout=10,
            cancel_event=None,
        )
        live._require(
            outcome.reason_code == "probe_spawn_failed",
            outcome.reason_code,
        )
        live._require(not outcome.command_released, "permission denial released command")
        case.assert_closed(attempt)

    def inspect_mismatch(self) -> None:
        case = self.case("inspect-mismatch")
        attempt = uuid.uuid4().hex
        hook = DockerFaultHook(
            attempt_id=attempt,
            inspect_mismatch=True,
        )
        outcome = case.runtime(
            attempt_id=attempt,
            process_runner=hook,
        ).execute(
            request=case.request(profile="read-only"),
            workspace_roots=(case.workspace, case.readable),
            workspace_roots_revision=live.REVISION,
            working_directory=case.workspace,
            command_argv=("/bin/sh", "-c", "exit 93"),
            timeout=20,
            cancel_event=None,
        )
        live._require(
            outcome.reason_code == "inspect_network_mismatch",
            outcome.reason_code,
        )
        live._require(not hook.release_seen, "inspect mismatch released command")
        live._require(outcome.cleanup is not None and outcome.cleanup.verified, "cleanup failed")
        case.assert_closed(attempt)

    def attempt_directory_aba(self) -> None:
        case = self.case("attempt-directory-aba")
        attempt = uuid.uuid4().hex
        hook = DockerFaultHook(
            attempt_id=attempt,
            attempt_path=case.staging / attempt,
            attempt_aba=True,
        )
        outcome = case.runtime(
            attempt_id=attempt,
            process_runner=hook,
        ).execute(
            request=case.request(profile="workspace-write"),
            workspace_roots=(case.workspace, case.readable),
            workspace_roots_revision=live.REVISION,
            working_directory=case.workspace,
            command_argv=("/bin/sh", "-c", "exit 94"),
            timeout=20,
            cancel_event=None,
        )
        live._require(
            outcome.reason_code == "staging_cleanup_unverified",
            outcome.reason_code,
        )
        live._require(not hook.release_seen, "attempt ABA released command")
        live._require(
            outcome.cleanup is not None
            and not outcome.cleanup.verified
            and outcome.cleanup.unresolved
            and outcome.cleanup.reason_code
            == "staging_execution_absence_journal_failed",
            f"attempt ABA cleanup state was not retained: {outcome.cleanup!r}",
        )
        live._require(
            outcome.staging_cleanup is not None
            and not outcome.staging_cleanup.verified
            and outcome.staging_cleanup.unresolved,
            "attempt ABA did not retain the staging obligation",
        )
        case.assert_recovery_retained(
            attempt,
            expected_state="execution_create_possible",
        )
        case.assert_container_closed(attempt)

    def create_ambiguity_recovery(self) -> None:
        case = self.case("create-ambiguity-recovery")
        attempt = uuid.uuid4().hex
        hook = DockerFaultHook(
            attempt_id=attempt,
            create_parent_failure=True,
        )
        outcome = case.runtime(
            attempt_id=attempt,
            process_runner=hook,
        ).execute(
            request=case.request(profile="read-only"),
            workspace_roots=(case.workspace, case.readable),
            workspace_roots_revision=live.REVISION,
            working_directory=case.workspace,
            command_argv=("/bin/sh", "-c", "exit 95"),
            timeout=20,
            cancel_event=None,
        )
        live._require(outcome.reason_code == "create_parent_exception", outcome.reason_code)
        live._require(not hook.release_seen, "ambiguous create released command")
        live._require(outcome.cleanup is not None and outcome.cleanup.verified, "recovery failed")
        case.assert_closed(attempt)

    def remove_nonzero_exact_absence(self) -> None:
        case = self.case("remove-nonzero-exact-absence")
        attempt = uuid.uuid4().hex
        hook = DockerFaultHook(
            attempt_id=attempt,
            remove_nonzero=True,
        )
        outcome = case.runtime(
            attempt_id=attempt,
            process_runner=hook,
        ).execute(
            request=case.request(profile="read-only"),
            workspace_roots=(case.workspace, case.readable),
            workspace_roots_revision=live.REVISION,
            working_directory=case.workspace,
            command_argv=("/bin/sh", "-c", "printf ok"),
            timeout=20,
            cancel_event=None,
        )
        live._require(
            outcome.reason_code == "container_execution_completed",
            outcome.reason_code,
        )
        live._require(hook.remove_fault_used, "remove fault was not exercised")
        live._require(
            outcome.cleanup is not None
            and outcome.cleanup.verified
            and outcome.cleanup.reason_code
            == "container_resources_cleanup_verified",
            f"exact absence did not close nonzero remove: {outcome.cleanup!r}",
        )
        case.assert_closed(attempt)

    def cleanup_check_parent_failure(self) -> None:
        case = self.case("cleanup-check-parent-failure")
        attempt = uuid.uuid4().hex
        hook = DockerFaultHook(
            attempt_id=attempt,
            removal_check_parent_failure=True,
        )
        outcome = case.runtime(
            attempt_id=attempt,
            process_runner=hook,
        ).execute(
            request=case.request(profile="read-only"),
            workspace_roots=(case.workspace, case.readable),
            workspace_roots_revision=live.REVISION,
            working_directory=case.workspace,
            command_argv=("/bin/sh", "-c", "printf ok"),
            timeout=20,
            cancel_event=None,
        )
        live._require(
            outcome.reason_code == "staging_cleanup_unverified",
            outcome.reason_code,
        )
        live._require(
            outcome.cleanup is not None
            and not outcome.cleanup.verified
            and outcome.cleanup.unresolved
            and outcome.cleanup.reason_code == "cleanup_check_parent_failed",
            "cleanup uncertainty was not retained",
        )
        live._require(
            outcome.staging_cleanup is not None
            and not outcome.staging_cleanup.verified
            and outcome.staging_cleanup.unresolved,
            "staging obligation was falsely closed",
        )
        case.assert_recovery_retained(
            attempt,
            expected_state="execution_create_possible",
        )
        case.assert_container_closed(attempt)

    def staging_output_parent_failure(self) -> None:
        case = self.case("staging-output-parent-failure")
        attempt = uuid.uuid4().hex
        with patch(
            "local_agent.execution.container_staging.observe_staged_workspace_output",
            side_effect=RuntimeError("fault-injected staging output observation"),
        ):
            outcome = case.runtime(attempt_id=attempt).execute(
                request=case.request(profile="workspace-write"),
                workspace_roots=(case.workspace, case.readable),
                workspace_roots_revision=live.REVISION,
                working_directory=case.workspace,
                command_argv=("/bin/sh", "-c", "printf ok"),
                timeout=20,
                cancel_event=None,
            )
        live._require(
            outcome.reason_code == "staging_output_parent_exception",
            outcome.reason_code,
        )
        live._require(
            outcome.staging_cleanup is not None
            and outcome.staging_cleanup.verified,
            "staging parent failure cleanup was not verified",
        )
        live._require(
            "staging output observation" not in repr(outcome),
            "raw staging parent failure leaked into the typed outcome",
        )
        case.assert_closed(attempt)


def _rewrite_inspect_network(completed) -> CapturedCompletedProcess:
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise live.MatrixFailure("Docker inspect payload was not an object")
    payload["network_mode"] = "host"
    stdout = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    return CapturedCompletedProcess(
        completed.args,
        completed.returncode,
        _capture(stdout, completed.stderr),
    )


def _capture(stdout: str, stderr: str) -> ProcessOutputCapture:
    stdout_capture = BoundedByteCapture()
    stdout_capture.push(stdout.encode("utf-8"))
    stderr_capture = BoundedByteCapture()
    stderr_capture.push(stderr.encode("utf-8"))
    return ProcessOutputCapture(stdout_capture.finish(), stderr_capture.finish())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-executable", required=True)
    parser.add_argument("--docker-sha256", required=True)
    parser.add_argument("--socket-path", required=True)
    parser.add_argument("--client-config-directory", required=True)
    parser.add_argument("--gate-image", required=True)
    parser.add_argument("--temp-root", default="/private/tmp")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    executable = Path(args.docker_executable).expanduser().resolve(strict=True)
    live._require(
        hashlib.sha256(executable.read_bytes()).hexdigest() == args.docker_sha256,
        "Docker executable digest does not match --docker-sha256",
    )
    authority = live.DockerAuthority(
        executable=executable,
        executable_sha256=args.docker_sha256,
        socket_path=Path(args.socket_path).expanduser().resolve(strict=False),
        client_config_directory=Path(
            args.client_config_directory
        ).expanduser().resolve(strict=True),
        gate_image=args.gate_image,
    )
    matrix = FaultMatrix(
        authority,
        temp_root=Path(args.temp_root).expanduser().resolve(strict=True),
    )
    try:
        passed = matrix.run()
        print(json.dumps(matrix.results, indent=2, sort_keys=True))
        print(
            "T-273 real-Docker fault matrix: "
            f"{sum(item['status'] == 'PASS' for item in matrix.results)}/"
            f"{len(matrix.results)} passed"
        )
        return 0 if passed else 1
    finally:
        matrix.close()


if __name__ == "__main__":
    raise SystemExit(main())

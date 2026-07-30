from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..execution.state_authority import IsolationStateAuthority
from ..execution.state_authority import IsolationStateAuthorityError
from ..execution.state_authority import acquire_isolation_state_authority
from ..execution.contracts import IsolationConfiguration
from ..execution.contracts import IsolationRequest
from ..protocol.cancellation import RunCancelled
from .base import ToolContext
from .container_runtime import ContainerExecutionRuntime
from .execution_interrupt import attach_execution_control_facts
from .process_environment import is_provider_credential_environment_key
from .process_output import ProcessOutputCapture
from .process_output import process_output_capture
from .process_runtime import run_process
from .workspace_mutation import ContainerMutationProvenance
from .workspace_mutation import WorkspaceMutationCommitResult
from .workspace_mutation import commit_container_workspace_output


class IsolationExecutionError(OSError):
    """An isolation request failed without a truthful local-process fallback."""

    def __init__(
        self,
        reason_code: str,
        metadata: dict[str, object],
        *,
        completed: object | None = None,
        execution_outcome: str = "not_run",
        output_capture: ProcessOutputCapture | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(f"Isolation prevented execution: {reason_code}.")
        self.reason_code = reason_code
        self.isolation_metadata = metadata
        self.completed = completed
        self.execution_outcome = execution_outcome
        self.output_capture = output_capture or process_output_capture(completed)
        self.stdout = self.output_capture.stdout.text
        self.stderr = self.output_capture.stderr.text
        self.returncode = exit_code


class ConfiguredIsolationProcessRunner:
    """Route exec tools without changing their single approval or process owners."""

    def __init__(
        self,
        configuration: IsolationConfiguration,
        *,
        container_runtime: ContainerExecutionRuntime | None = None,
        local_runner: Callable[..., Any] = run_process,
    ) -> None:
        self._configuration = configuration
        self._container_runtime = container_runtime
        self._local_runner = local_runner

    def __call__(
        self,
        command: str | list[str],
        *,
        cwd: Path,
        shell: bool,
        timeout: float,
        cancel_event,
        env=None,
        context: ToolContext,
        isolated_command: tuple[str, ...] | None = None,
        isolated_environment: Mapping[str, str] | None = None,
    ):
        configuration = self._configuration
        if configuration.mode == "off":
            return self._local_runner(
                command,
                cwd=cwd,
                shell=shell,
                timeout=timeout,
                cancel_event=cancel_event,
                env=env,
            )
        if (
            configuration.backend not in {"auto", "container"}
            or configuration.container is None
            or self._container_runtime is None
        ):
            raise _unavailable_error(
                "container_authority_unconfigured",
                configuration,
            )
        try:
            state_authority = acquire_isolation_state_authority(
                context.state_dir,
                workspace_roots=(context.workspace, *context.allowed_dirs),
            )
        except IsolationStateAuthorityError as exc:
            raise _unavailable_error(exc.kind, configuration) from exc
        try:
            return self._execute_isolated(
                command,
                cwd=cwd,
                shell=shell,
                timeout=timeout,
                cancel_event=cancel_event,
                context=context,
                isolated_command=isolated_command,
                isolated_environment=isolated_environment,
                configuration=configuration,
                state_authority=state_authority,
            )
        finally:
            state_authority.close()

    def _execute_isolated(
        self,
        command: str | list[str],
        *,
        cwd: Path,
        shell: bool,
        timeout: float,
        cancel_event,
        context: ToolContext,
        isolated_command: tuple[str, ...] | None,
        isolated_environment: Mapping[str, str] | None,
        configuration: IsolationConfiguration,
        state_authority: IsolationStateAuthority,
    ):
        try:
            request = IsolationRequest(
                mode=configuration.mode,
                profile=configuration.profile,
                backend="container",
                network_policy=configuration.network_policy,
                workspace=context.workspace.resolve(),
                readable_roots=tuple(path.resolve() for path in context.allowed_dirs),
                writable_roots=(
                    (context.workspace.resolve(),)
                    if configuration.profile == "workspace-write"
                    else ()
                ),
            )
            command_argv = _with_isolated_environment(
                isolated_command or _isolated_argv(command, shell=shell),
                isolated_environment,
            )
        except (OSError, ValueError) as exc:
            raise _unavailable_error(
                "isolation_request_invalid",
                configuration,
            ) from exc
        outcome = self._container_runtime.execute(
            request=request,
            workspace_roots=(
                request.workspace,
                *request.readable_roots,
            ),
            workspace_roots_revision=context.workspace_revision,
            working_directory=cwd,
            command_argv=command_argv,
            timeout=timeout,
            cancel_event=cancel_event,
            forbidden_snapshot_directory_identities=(
                state_authority.forbidden_directory_identities
            ),
        )
        metadata = outcome.metadata()
        if outcome.cancellation is not None:
            projected_output = (
                outcome.user_output
                if outcome.user_output is not None
                else process_output_capture(None)
            )
            outcome.cancellation.output_capture = projected_output
            outcome.cancellation.stdout = projected_output.stdout.text
            outcome.cancellation.stderr = projected_output.stderr.text
            outcome.cancellation.isolation_metadata = metadata
            raise outcome.cancellation
        if outcome.completed is None or outcome.reason_code != "container_execution_completed":
            raise IsolationExecutionError(
                outcome.reason_code,
                metadata,
                completed=outcome.completed,
                execution_outcome=outcome.execution_outcome,
                output_capture=outcome.user_output,
                exit_code=(
                    outcome.completed.returncode
                    if outcome.completed is not None
                    else None
                ),
            )
        workspace_output = outcome.workspace_output_plan
        if workspace_output is not None:
            proof = outcome.proof
            assert proof is not None
            try:
                mutation = commit_container_workspace_output(
                    context=context,
                    plan=workspace_output,
                    provenance=ContainerMutationProvenance(
                        attempt_id=outcome.attempt_id,
                        image_digest=proof.image_digest,
                        profile=proof.profile,
                        workspace_transport=outcome.workspace_transport,
                    ),
                )
            except BaseException as error:
                interrupted_metadata = dict(metadata)
                interrupted_mutation = getattr(error, "workspace_mutation_result", None)
                if isinstance(interrupted_mutation, WorkspaceMutationCommitResult):
                    interrupted_metadata.update(interrupted_mutation.metadata())
                attach_execution_control_facts(
                    error,
                    execution_started=True,
                    execution_outcome=outcome.execution_outcome,
                    exit_code=outcome.completed.returncode,
                    isolation_metadata=interrupted_metadata,
                    output_capture=outcome.user_output
                    or process_output_capture(outcome.completed),
                )
                raise
            metadata = {
                **metadata,
                **mutation.metadata(),
            }
            isolation = metadata.get("isolation")
            if isinstance(isolation, dict):
                isolation["workspace_output_commit"] = {
                    "state": mutation.state,
                    "transaction_id": mutation.transaction_id,
                    "error_kind": mutation.error_kind,
                }
            if not mutation.committed:
                raise IsolationExecutionError(
                    f"container_workspace_commit_{mutation.state}",
                    metadata,
                    completed=outcome.completed,
                    execution_outcome=outcome.execution_outcome,
                    output_capture=outcome.user_output,
                    exit_code=outcome.completed.returncode,
                )
        outcome.completed.isolation_metadata = metadata
        return outcome.completed


def isolation_metadata(value: object) -> dict[str, object]:
    metadata = getattr(value, "isolation_metadata", None)
    return dict(metadata) if isinstance(metadata, dict) else {}


def run_context_process(
    context: ToolContext,
    command: str | list[str],
    *,
    local_runner: Callable[..., Any] = run_process,
    isolated_command: tuple[str, ...] | None = None,
    isolated_environment: Mapping[str, str] | None = None,
    **kwargs,
):
    if context.process_runner is None:
        return local_runner(command, **kwargs)
    return context.process_runner(
        command,
        context=context,
        isolated_command=isolated_command,
        isolated_environment=isolated_environment,
        **kwargs,
    )


def _isolated_argv(
    command: str | list[str],
    *,
    shell: bool,
) -> tuple[str, ...]:
    if shell:
        if not isinstance(command, str):
            raise ValueError("isolated shell command must be a string")
        return ("/bin/sh", "-c", command)
    if not isinstance(command, list):
        raise ValueError("isolated structured command must be an argv list")
    return tuple(command)


def _with_isolated_environment(
    argv: tuple[str, ...],
    environment: Mapping[str, str] | None,
) -> tuple[str, ...]:
    assignments = tuple(
        f"{name}={value}"
        for name, value in sorted((environment or {}).items())
        if not is_provider_credential_environment_key(name)
    )
    return ("/usr/bin/env", *assignments, *argv) if assignments else argv


def _unavailable_error(
    reason_code: str,
    configuration: IsolationConfiguration,
) -> IsolationExecutionError:
    return IsolationExecutionError(
        reason_code,
        {
            "sandboxed": False,
            "isolation": {
                "backend": configuration.backend,
                "reason_code": reason_code,
                "applied": False,
                "profile": configuration.profile,
                "network_policy": configuration.network_policy,
                "cleanup": "not_applicable",
                "cleanup_verified": False,
                "recovery_unresolved": False,
            },
        },
    )


__all__ = [
    "ConfiguredIsolationProcessRunner",
    "IsolationExecutionError",
    "isolation_metadata",
    "run_context_process",
]

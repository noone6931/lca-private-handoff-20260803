from __future__ import annotations

import hashlib
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..execution.container_types import ContainerCommandResult
from ..execution.container_types import ContainerOutputCapture
from ..execution.container_types import ContainerProcessStep
from ..execution.container_types import ContainerStreamCapture
from ..protocol.cancellation import CancellationSignal
from ..protocol.cancellation import RunCancelled
from .process_output import CapturedText
from .process_output import ProcessOutputCapture
from .process_output import process_output_capture
from .process_runtime import run_process


@dataclass(frozen=True)
class ContainerCommandObservation:
    result: ContainerCommandResult
    cancellation: RunCancelled | None = None
    parent_error: BaseException | None = None

    def __post_init__(self) -> None:
        if (self.result.outcome == "cancelled") != (self.cancellation is not None):
            raise ValueError("container cancellation observation is inconsistent")
        if (self.result.outcome == "parent_failed") != (
            self.parent_error is not None
        ):
            raise ValueError("container parent failure observation is inconsistent")


class ContainerCommandRunner:
    """Adapt typed Docker control commands to the single process lifecycle."""

    def __init__(
        self,
        *,
        attempt_id: str,
        workspace_roots: tuple[Path, ...],
        workspace_roots_revision: int,
        control_working_directory: Path,
        control_environment: Mapping[str, str],
        process_runner: Callable[..., Any] = run_process,
    ) -> None:
        self._attempt_id = attempt_id
        self._workspace_roots = workspace_roots
        self._workspace_roots_revision = workspace_roots_revision
        self._control_working_directory = control_working_directory
        self._control_environment = control_environment
        self._process_runner = process_runner
        self._event_sequence = 0

    @property
    def attempt_id(self) -> str:
        return self._attempt_id

    def run(
        self,
        *,
        command_id: str,
        step: ContainerProcessStep,
        argv: tuple[str, ...],
        timeout: float,
        cancel_event: CancellationSignal | None,
    ) -> ContainerCommandObservation:
        self._event_sequence += 1
        started_ns = time.monotonic_ns()
        cancellation: RunCancelled | None = None
        parent_error: BaseException | None = None
        exit_code: int | None = None
        outcome = "exited"
        try:
            observed = self._process_runner(
                list(argv),
                cwd=self._control_working_directory,
                env=self._control_environment,
                shell=False,
                timeout=timeout,
                cancel_event=cancel_event,
            )
            exit_code = observed.returncode
        except subprocess.TimeoutExpired as exc:
            observed = exc
            outcome = "timed_out"
        except RunCancelled as exc:
            observed = exc
            outcome = "cancelled"
            cancellation = exc
        except OSError:
            observed = None
            outcome = "spawn_failed"
        except BaseException as exc:
            observed = None
            outcome = "parent_failed"
            parent_error = exc
        finished_ns = time.monotonic_ns()
        capture = process_output_capture(observed)
        result = ContainerCommandResult(
            attempt_id=self._attempt_id,
            command_id=command_id,
            event_sequence=self._event_sequence,
            step=step,
            argv=argv,
            outcome=outcome,
            exit_code=exit_code,
            stdout=capture.stdout.text,
            stderr=capture.stderr.text,
            output_capture=_container_capture(capture),
            started_monotonic_ns=started_ns,
            finished_monotonic_ns=finished_ns,
            workspace_roots=self._workspace_roots,
            workspace_roots_revision=self._workspace_roots_revision,
        )
        return ContainerCommandObservation(result, cancellation, parent_error)


def _container_capture(capture: ProcessOutputCapture) -> ContainerOutputCapture:
    return ContainerOutputCapture(
        stdout=_container_stream(capture.stdout),
        stderr=_container_stream(capture.stderr),
    )


def _container_stream(capture: CapturedText) -> ContainerStreamCapture:
    summary = capture.summary
    return ContainerStreamCapture(
        observed_bytes=summary.observed_bytes,
        captured_bytes=summary.captured_bytes,
        dropped_bytes=summary.dropped_bytes,
        truncated=summary.truncated,
        text_sha256=hashlib.sha256(capture.text.encode("utf-8")).hexdigest(),
    )


__all__ = [
    "ContainerCommandObservation",
    "ContainerCommandRunner",
]

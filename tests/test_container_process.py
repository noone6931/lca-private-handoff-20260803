from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from local_agent.cancellation import RunCancelled
from local_agent.execution.container_types import container_command_id
from local_agent.tools.container_process import ContainerCommandRunner
from local_agent.tools.process_cancellation import CapturedRunCancelled
from local_agent.tools.process_output import BoundedByteCapture
from local_agent.tools.process_output import CapturedCompletedProcess
from local_agent.tools.process_output import ProcessOutputCapture


ATTEMPT_ID = "a" * 32


def _capture(stdout: bytes = b"", stderr: bytes = b"") -> ProcessOutputCapture:
    stdout_capture = BoundedByteCapture()
    stdout_capture.push(stdout)
    stderr_capture = BoundedByteCapture()
    stderr_capture.push(stderr)
    return ProcessOutputCapture(stdout_capture.finish(), stderr_capture.finish())


class ContainerProcessTests(unittest.TestCase):
    def _runner(self, process_runner):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        workspace = Path(temporary.name).resolve()
        return ContainerCommandRunner(
            attempt_id=ATTEMPT_ID,
            workspace_roots=(workspace,),
            workspace_roots_revision=7,
            control_working_directory=workspace,
            control_environment={"HOME": str(workspace)},
            process_runner=process_runner,
        )

    def test_completed_and_timeout_results_preserve_typed_capture(self) -> None:
        responses = [
            CapturedCompletedProcess(["docker"], 7, _capture(b"out\n", b"err\n")),
            subprocess.TimeoutExpired(["docker"], 2, output="partial\n", stderr=""),
        ]

        def process_runner(*args, **kwargs):
            response = responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response

        runner = self._runner(process_runner)

        completed = runner.run(
            command_id=container_command_id(ATTEMPT_ID, "server"),
            step="server",
            argv=("docker", "version"),
            timeout=2,
            cancel_event=None,
        ).result
        timed_out = runner.run(
            command_id=container_command_id(ATTEMPT_ID, "image"),
            step="image",
            argv=("docker", "image", "inspect"),
            timeout=2,
            cancel_event=None,
        ).result

        self.assertEqual((completed.outcome, completed.exit_code), ("exited", 7))
        self.assertEqual((completed.stdout, completed.stderr), ("out\n", "err\n"))
        self.assertEqual(completed.event_sequence, 1)
        self.assertEqual((timed_out.outcome, timed_out.exit_code), ("timed_out", None))
        self.assertEqual(timed_out.stdout, "partial\n")
        self.assertEqual(timed_out.event_sequence, 2)

    def test_cancellation_retains_capture_and_spawn_failure_is_empty(self) -> None:
        cancellation = CapturedRunCancelled("cancelled", _capture(b"ready\n"))
        responses = [cancellation, OSError("spawn failed")]

        def process_runner(*args, **kwargs):
            response = responses.pop(0)
            raise response

        runner = self._runner(process_runner)
        cancelled = runner.run(
            command_id=container_command_id(ATTEMPT_ID, "create"),
            step="create",
            argv=("docker", "create"),
            timeout=2,
            cancel_event=None,
        )
        spawn_failed = runner.run(
            command_id=container_command_id(ATTEMPT_ID, "start"),
            step="start",
            argv=("docker", "start"),
            timeout=2,
            cancel_event=None,
        ).result

        self.assertIs(cancelled.cancellation, cancellation)
        self.assertEqual(cancelled.result.outcome, "cancelled")
        self.assertEqual(cancelled.result.stdout, "ready\n")
        self.assertEqual(spawn_failed.outcome, "spawn_failed")
        self.assertEqual(spawn_failed.stdout, "")
        self.assertEqual(spawn_failed.stderr, "")

    def test_plain_cancellation_remains_a_typed_empty_capture(self) -> None:
        def process_runner(*args, **kwargs):
            raise RunCancelled("cancelled before output")

        observed = self._runner(process_runner).run(
            command_id=container_command_id(ATTEMPT_ID, "server"),
            step="server",
            argv=("docker", "version"),
            timeout=2,
            cancel_event=None,
        )

        self.assertEqual(observed.result.outcome, "cancelled")
        self.assertEqual(observed.result.output_capture.stdout.observed_bytes, 0)

    def test_parent_failure_is_typed_without_projecting_raw_error(self) -> None:
        secret = "docker-auth-secret"
        parent_error = RuntimeError(secret)

        def process_runner(*args, **kwargs):
            raise parent_error

        observed = self._runner(process_runner).run(
            command_id=container_command_id(ATTEMPT_ID, "server"),
            step="server",
            argv=("docker", "version"),
            timeout=2,
            cancel_event=None,
        )

        self.assertIs(observed.parent_error, parent_error)
        self.assertEqual(observed.result.outcome, "parent_failed")
        self.assertIsNone(observed.result.exit_code)
        self.assertEqual((observed.result.stdout, observed.result.stderr), ("", ""))
        self.assertNotIn(secret, repr(observed.result))


if __name__ == "__main__":
    unittest.main()

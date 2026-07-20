from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.cancellation import RunCancelled
from local_agent.tools.process_output import PROCESS_STREAM_CAPTURE_LIMIT_BYTES
from local_agent.tools.process_runtime import run_process


class ProcessRuntimeTests(unittest.TestCase):
    def test_completed_process_preserves_output_and_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            command = [
                sys.executable,
                "-c",
                "import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(7)",
            ]
            with patch("local_agent.tools.process_runtime._signal_process") as signal_process:
                result = run_process(
                    command,
                    cwd=Path(tmp),
                    shell=False,
                    timeout=5,
                    cancel_event=None,
                )

        self.assertEqual(result.args, command)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "out\n")
        self.assertEqual(result.stderr, "err\n")
        signal_process.assert_not_called()

    def test_large_dual_stream_capture_is_bounded_and_does_not_deadlock(self) -> None:
        stdout_bytes = PROCESS_STREAM_CAPTURE_LIMIT_BYTES + 8192
        stderr_bytes = PROCESS_STREAM_CAPTURE_LIMIT_BYTES + 16384
        code = (
            "import sys; "
            f"sys.stdout.buffer.write(b'A' * {stdout_bytes}); sys.stdout.buffer.flush(); "
            f"sys.stderr.buffer.write(b'B' * {stderr_bytes}); sys.stderr.buffer.flush()"
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = run_process(
                [sys.executable, "-c", code],
                cwd=Path(tmp),
                shell=False,
                timeout=5,
                cancel_event=None,
            )

        capture = result.output_capture
        self.assertEqual(capture.stdout.summary.observed_bytes, stdout_bytes)
        self.assertEqual(capture.stderr.summary.observed_bytes, stderr_bytes)
        self.assertEqual(capture.stdout.summary.captured_bytes, PROCESS_STREAM_CAPTURE_LIMIT_BYTES)
        self.assertEqual(capture.stderr.summary.captured_bytes, PROCESS_STREAM_CAPTURE_LIMIT_BYTES)
        self.assertEqual(capture.stdout.summary.dropped_bytes, 8192)
        self.assertEqual(capture.stderr.summary.dropped_bytes, 16384)
        self.assertTrue(capture.truncated)
        self.assertTrue(result.stdout.startswith("A" * 32))
        self.assertTrue(result.stdout.endswith("A" * 32))
        self.assertTrue(result.stderr.startswith("B" * 32))
        self.assertTrue(result.stderr.endswith("B" * 32))

    def test_timeout_exposes_bounded_capture_on_timeout_expired_protocol(self) -> None:
        emitted = PROCESS_STREAM_CAPTURE_LIMIT_BYTES + 4096
        code = (
            "import sys,time; "
            f"sys.stdout.buffer.write(b'X' * {emitted}); sys.stdout.buffer.flush(); time.sleep(5)"
        )

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                run_process(
                    [sys.executable, "-c", code],
                    cwd=Path(tmp),
                    shell=False,
                    timeout=0.2,
                    cancel_event=None,
                )

        capture = raised.exception.output_capture
        self.assertEqual(capture.stdout.summary.observed_bytes, emitted)
        self.assertEqual(capture.stdout.summary.captured_bytes, PROCESS_STREAM_CAPTURE_LIMIT_BYTES)
        self.assertEqual(capture.stdout.summary.dropped_bytes, 4096)
        self.assertEqual(raised.exception.stdout, capture.stdout.text)
        self.assertLess(len(raised.exception.stdout), emitted)

    @unittest.skipUnless(os.name == "posix", "startup cleanup characterization requires POSIX")
    def test_pipe_setup_failure_terminates_started_process_without_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            marker = workspace / "late-startup.txt"
            code = "import pathlib,sys,time; time.sleep(0.4); pathlib.Path(sys.argv[1]).write_text('late')"

            with patch("local_agent.tools.process_runtime.os.set_blocking", side_effect=RuntimeError("setup failed")):
                with self.assertRaisesRegex(RuntimeError, "setup failed"):
                    run_process(
                        [sys.executable, "-c", code, str(marker)],
                        cwd=workspace,
                        shell=False,
                        timeout=5,
                        cancel_event=None,
                    )
            time.sleep(0.5)
            marker_exists = marker.exists()

        self.assertFalse(marker_exists)

    @unittest.skipUnless(os.name == "posix", "process-group characterization requires POSIX")
    def test_timeout_without_cancel_signal_terminates_grandchild_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            marker = workspace / "late-timeout.txt"
            child_code = "import pathlib,sys,time; time.sleep(1.2); pathlib.Path(sys.argv[1]).write_text('late')"
            parent_code = (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
                "print('ready', flush=True); time.sleep(10)"
            )
            command = [sys.executable, "-c", parent_code, child_code, str(marker)]

            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                run_process(command, cwd=workspace, shell=False, timeout=1, cancel_event=None)
            time.sleep(0.4)
            marker_exists = marker.exists()

        self.assertIn("ready", raised.exception.stdout or "")
        self.assertFalse(marker_exists)

    @unittest.skipUnless(os.name == "posix", "process-group characterization requires POSIX")
    def test_timeout_closes_group_when_leader_exits_but_descendant_holds_pipes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            marker = workspace / "late-after-leader.txt"
            child_code = "import pathlib,sys,time; time.sleep(0.45); pathlib.Path(sys.argv[1]).write_text('late')"
            parent_code = (
                "import subprocess,sys; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
                "print('leader-exit', flush=True)"
            )
            command = [sys.executable, "-c", parent_code, child_code, str(marker)]
            started = time.monotonic()

            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                run_process(command, cwd=workspace, shell=False, timeout=0.1, cancel_event=None)
            elapsed = time.monotonic() - started
            time.sleep(0.5)
            marker_exists = marker.exists()

        self.assertLess(elapsed, 0.4)
        self.assertIn("leader-exit", raised.exception.stdout or "")
        self.assertFalse(marker_exists)

    @unittest.skipUnless(os.name == "posix", "process-group characterization requires POSIX")
    def test_cancel_signal_terminates_grandchild_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            marker = workspace / "late-cancel.txt"
            child_code = "import pathlib,sys,time; time.sleep(0.8); pathlib.Path(sys.argv[1]).write_text('late')"
            parent_code = (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); time.sleep(10)"
            )
            cancel = threading.Event()
            timer = threading.Timer(0.15, cancel.set)
            timer.start()
            try:
                with self.assertRaises(RunCancelled):
                    run_process(
                        [sys.executable, "-c", parent_code, child_code, str(marker)],
                        cwd=workspace,
                        shell=False,
                        timeout=5,
                        cancel_event=cancel,
                    )
            finally:
                timer.cancel()
            time.sleep(0.9)
            marker_exists = marker.exists()

        self.assertFalse(marker_exists)


if __name__ == "__main__":
    unittest.main()

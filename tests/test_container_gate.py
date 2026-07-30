from __future__ import annotations

import os
import secrets
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "packaging/container-gate/isolation-gate"
READY = ROOT / "packaging/container-gate/isolation-gate-ready"
MOUNT_PROOF = ROOT / "packaging/container-gate/isolation-gate-mount-proof"
STAGE_PROOF = ROOT / "packaging/container-gate/isolation-gate-stage-proof"
GATE_STATE = Path("/tmp/local-agent-gate")
READY_ATTEMPTS = "100"
READY_DELAY_SECONDS = "0.05"
RELEASE_TIMEOUT_SECONDS = 5


@unittest.skipUnless(os.name == "posix", "container gate requires POSIX signals")
class ContainerGateTests(unittest.TestCase):
    def _start(
        self,
        marker: Path,
        attempt_id: str,
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [
                "/bin/sh",
                str(GATE),
                "--protocol",
                "signal-v1",
                "--release-signal",
                "SIGUSR1",
                "--attempt-id",
                attempt_id,
                "--",
                "/bin/sh",
                "-c",
                'printf released > "$1"',
                "gate-test",
                str(marker),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={**os.environ, "PATH": "/path-controlled-by-user"},
        )

    def _new_attempt(self) -> tuple[str, Path]:
        attempt_id = secrets.token_hex(16)
        ready_marker = GATE_STATE / f"{attempt_id}.ready"
        ready_marker.unlink(missing_ok=True)
        return attempt_id, ready_marker

    def _wait_ready(self, attempt_id: str) -> None:
        completed = subprocess.run(
            [
                "/bin/sh",
                str(READY),
                "--attempt-id",
                attempt_id,
                "--attempts",
                READY_ATTEMPTS,
                "--delay-seconds",
                READY_DELAY_SECONDS,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, b"")

    def test_ready_checker_has_bounded_not_ready_result(self) -> None:
        attempt_id, ready_marker = self._new_attempt()
        completed = subprocess.run(
            [
                "/bin/sh",
                str(READY),
                "--attempt-id",
                attempt_id,
                "--attempts",
                "2",
                "--delay-seconds",
                "0.01",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
        )
        self.assertEqual(completed.returncode, 75)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, b"")
        self.assertFalse(ready_marker.exists())

    def test_gate_scripts_are_shell_syntax_valid_and_mount_proof_requires_roots(
        self,
    ) -> None:
        for script in (GATE, READY, MOUNT_PROOF, STAGE_PROOF):
            with self.subTest(script=script.name):
                parsed = subprocess.run(
                    ["/bin/sh", "-n", str(script)],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=2,
                )
                self.assertEqual(parsed.returncode, 0)
                self.assertEqual(parsed.stdout, b"")
                self.assertEqual(parsed.stderr, b"")
        empty = subprocess.run(
            ["/bin/sh", str(MOUNT_PROOF)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
        )
        self.assertEqual(empty.returncode, 64)
        self.assertEqual(empty.stdout, b"")
        self.assertEqual(empty.stderr, b"")

    def test_gate_executes_command_only_after_release_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker"
            attempt_id, ready_marker = self._new_attempt()
            process = self._start(marker, attempt_id)
            try:
                self._wait_ready(attempt_id)
                self.assertIsNone(process.poll())
                self.assertFalse(marker.exists())
                process.send_signal(signal.SIGUSR1)
                stdout, stderr = process.communicate(
                    timeout=RELEASE_TIMEOUT_SECONDS
                )
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
                ready_marker.unlink(missing_ok=True)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(stdout, b"")
            self.assertEqual(stderr, b"")
            self.assertEqual(marker.read_text(encoding="utf-8"), "released")
            self.assertFalse(ready_marker.exists())

    def test_gate_termination_never_executes_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker"
            attempt_id, ready_marker = self._new_attempt()
            process = self._start(marker, attempt_id)
            try:
                self._wait_ready(attempt_id)
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=RELEASE_TIMEOUT_SECONDS)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
                ready_marker.unlink(missing_ok=True)
            self.assertEqual(process.returncode, 143)
            self.assertFalse(marker.exists())

    def test_gate_rejects_unknown_protocol_without_running_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker"
            attempt_id, ready_marker = self._new_attempt()
            completed = subprocess.run(
                [
                    "/bin/sh",
                    str(GATE),
                    "--protocol",
                    "unknown",
                    "--release-signal",
                    "SIGUSR1",
                    "--attempt-id",
                    attempt_id,
                    "--",
                    "/bin/sh",
                    "-c",
                    'touch "$1"',
                    "gate-test",
                    str(marker),
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=2,
            )
            self.assertEqual(completed.returncode, 64)
            self.assertFalse(marker.exists())
            self.assertFalse(ready_marker.exists())


if __name__ == "__main__":
    unittest.main()

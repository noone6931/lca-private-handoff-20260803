from __future__ import annotations

import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/tui_pty_app.py"


@unittest.skipUnless(os.name == "posix", "PTY smoke requires a POSIX terminal")
class TuiPtyTests(unittest.TestCase):
    def test_initial_prompt_and_fragmented_multiline_paste_are_submitted_once(self) -> None:
        master, slave = os.openpty()
        self._set_size(slave, 24, 100)
        process = subprocess.Popen(
            [sys.executable, str(FIXTURE), "initial task"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "TERM": "xterm-256color"},
            close_fds=True,
        )
        output = bytearray()
        try:
            output.extend(self._read_until(master, b"SUBMITTED:'initial task'", timeout=3))
            os.write(master, b"\x1b[20")
            time.sleep(0.02)
            os.write(master, b"0~first\n")
            time.sleep(0.05)
            output.extend(self._read_available(master))
            self.assertNotIn(b"SUBMITTED:'first", output)
            os.write(master, b"second\x1b[201~")
            time.sleep(0.05)
            output.extend(self._read_available(master))
            self.assertNotIn(b"SUBMITTED:'first", output)
            os.write(master, b"\n")
            output.extend(self._read_until(master, b"SUBMITTED:'first\\nsecond'", timeout=3))
            os.write(master, b"/exit\n")
            output.extend(self._drain_until_exit(master, process, timeout=3))
            process.wait(timeout=1)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            os.close(master)
            os.close(slave)

        rendered = output.decode("utf-8", errors="replace")
        self.assertEqual(process.returncode, 0, rendered)
        self.assertEqual(rendered.count("SUBMITTED:'initial task'"), 1)
        self.assertIn("SUBMITTED:'first\\nsecond'", rendered)
        self.assertIn("\x1b[?2004h", rendered)
        self.assertIn("\x1b[?2004l", rendered)

    def test_workspace_list_dispatches_from_completed_subcommand(self) -> None:
        master, slave = os.openpty()
        before = termios.tcgetattr(slave)
        self._set_size(slave, 24, 80)
        process = subprocess.Popen(
            [sys.executable, str(FIXTURE)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "TERM": "xterm-256color"},
            close_fds=True,
        )
        output = bytearray()
        try:
            output.extend(self._read_until(master, b"LOCAL CODING AGENT", timeout=3))
            os.write(master, b"/workspace list\n")
            output.extend(self._read_until(master, b"/tmp/lca-tui-fixture", timeout=3))
            os.write(master, b"/exit\n")
            output.extend(self._drain_until_exit(master, process, timeout=3))
            process.wait(timeout=1)
            after = termios.tcgetattr(slave)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            os.close(master)
            os.close(slave)

        mask = termios.ECHO | termios.ICANON
        rendered = output.decode("utf-8", errors="replace")
        self.assertEqual(process.returncode, 0, rendered)
        self.assertIn("/tmp/lca-tui-fixture", rendered)
        self.assertNotIn("you> list", rendered)
        self.assertEqual(after[3] & mask, before[3] & mask)

    def test_normal_exit_restores_canonical_input_and_echo(self) -> None:
        master, slave = os.openpty()
        before = termios.tcgetattr(slave)
        self._set_size(slave, 24, 80)
        environment = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "TERM": "xterm-256color",
        }
        process = subprocess.Popen(
            [sys.executable, str(FIXTURE)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=environment,
            close_fds=True,
        )
        output = bytearray()
        try:
            output.extend(self._read_until(master, b"LCA", timeout=3))
            self._set_size(slave, 10, 40)
            process.send_signal(signal.SIGWINCH)
            time.sleep(0.05)
            output.extend(self._read_available(master))
            self._set_size(slave, 40, 120)
            process.send_signal(signal.SIGWINCH)
            os.write(master, b"/exit\n")
            output.extend(self._drain_until_exit(master, process, timeout=3))
            process.wait(timeout=1)
            after = termios.tcgetattr(slave)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            os.close(master)
            os.close(slave)

        mask = termios.ECHO | termios.ICANON
        self.assertEqual(process.returncode, 0, output.decode("utf-8", errors="replace"))
        self.assertEqual(after[3] & mask, before[3] & mask)
        self.assertIn(b"LCA", output)
        self.assertIn(b"\x1b[?2004h", output)
        self.assertIn(b"\x1b[?2004l", output)

    def test_sigterm_restores_canonical_input_and_echo(self) -> None:
        master, slave = os.openpty()
        before = termios.tcgetattr(slave)
        self._set_size(slave, 24, 80)
        process = subprocess.Popen(
            [sys.executable, str(FIXTURE)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "TERM": "xterm-256color"},
            close_fds=True,
        )
        output = bytearray()
        try:
            output.extend(self._read_until(master, b"LCA", timeout=3))
            process.send_signal(signal.SIGTERM)
            output.extend(self._drain_until_exit(master, process, timeout=3))
            process.wait(timeout=1)
            after = termios.tcgetattr(slave)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            os.close(master)
            os.close(slave)

        mask = termios.ECHO | termios.ICANON
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(after[3] & mask, before[3] & mask, output.decode("utf-8", errors="replace"))
        self.assertIn(b"\x1b[?2004l", output)

    def test_ctrl_c_cancels_active_run_then_normal_exit_restores_terminal(self) -> None:
        master, slave = os.openpty()
        before = termios.tcgetattr(slave)
        self._set_size(slave, 24, 80)
        process = subprocess.Popen(
            [sys.executable, str(FIXTURE)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "TERM": "xterm-256color"},
            close_fds=True,
        )
        output = bytearray()
        try:
            output.extend(self._read_until(master, b"LCA", timeout=3))
            os.write(master, b"wait\n")
            time.sleep(0.1)
            process.send_signal(signal.SIGINT)
            output.extend(self._read_until(master, b"Runtime worker failed: interrupted", timeout=2))
            os.write(master, b"/exit\n")
            output.extend(self._drain_until_exit(master, process, timeout=3))
            process.wait(timeout=1)
            after = termios.tcgetattr(slave)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            os.close(master)
            os.close(slave)

        mask = termios.ECHO | termios.ICANON
        self.assertEqual(process.returncode, 0, output.decode("utf-8", errors="replace"))
        self.assertEqual(after[3] & mask, before[3] & mask)

    @staticmethod
    def _set_size(fd: int, rows: int, columns: int) -> None:
        import fcntl

        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    @staticmethod
    def _read_until(fd: int, marker: bytes, *, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        output = bytearray()
        while marker not in output and time.monotonic() < deadline:
            readable, _, _ = select.select([fd], [], [], 0.05)
            if not readable:
                continue
            try:
                output.extend(os.read(fd, 65536))
            except OSError:
                break
        return bytes(output)

    @staticmethod
    def _read_available(fd: int) -> bytes:
        output = bytearray()
        while select.select([fd], [], [], 0)[0]:
            try:
                output.extend(os.read(fd, 65536))
            except OSError:
                break
        return bytes(output)

    @classmethod
    def _drain_until_exit(cls, fd: int, process: subprocess.Popen, *, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        output = bytearray()
        while process.poll() is None and time.monotonic() < deadline:
            readable, _, _ = select.select([fd], [], [], 0.05)
            if readable:
                try:
                    output.extend(os.read(fd, 65536))
                except OSError:
                    break
        output.extend(cls._read_available(fd))
        return bytes(output)


if __name__ == "__main__":
    unittest.main()

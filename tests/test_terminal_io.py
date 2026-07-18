from __future__ import annotations

import unittest
from unittest.mock import patch

from local_agent.frontends.terminal import io as terminal_io


class _FakeStdin:
    def __init__(self, *, tty: bool = True, fd: int = 9) -> None:
        self._tty = tty
        self._fd = fd

    def isatty(self) -> bool:
        return self._tty

    def fileno(self) -> int:
        return self._fd


class _FakeTermios:
    ECHO = 0x0008
    TCSADRAIN = 1
    TCIFLUSH = 2

    def __init__(self) -> None:
        self.attrs = [0, 0, 0, self.ECHO, 0, 0, []]
        self.set_calls: list[tuple[int, int, list]] = []
        self.flush_calls: list[tuple[int, int]] = []

    def tcgetattr(self, fd: int) -> list:
        return list(self.attrs)

    def tcsetattr(self, fd: int, when: int, attrs: list) -> None:
        copied = list(attrs)
        self.attrs = copied
        self.set_calls.append((fd, when, copied))

    def tcflush(self, fd: int, queue: int) -> None:
        self.flush_calls.append((fd, queue))


class TerminalIoTests(unittest.TestCase):
    def tearDown(self) -> None:
        terminal_io._ACTIVE_GUARDS.clear()

    def test_silenced_input_disables_echo_and_prompt_context_restores_it(self) -> None:
        fake_termios = _FakeTermios()
        stdin = _FakeStdin()

        with patch.object(terminal_io, "_termios", fake_termios):
            with terminal_io.silenced_terminal_input(stdin) as guard:
                self.assertTrue(guard.enabled)
                self.assertEqual(fake_termios.attrs[3] & fake_termios.ECHO, 0)

                with terminal_io.terminal_input_prompt(stdin):
                    self.assertNotEqual(fake_termios.attrs[3] & fake_termios.ECHO, 0)

                self.assertEqual(fake_termios.attrs[3] & fake_termios.ECHO, 0)

            self.assertNotEqual(fake_termios.attrs[3] & fake_termios.ECHO, 0)

        self.assertEqual(fake_termios.flush_calls, [(9, fake_termios.TCIFLUSH), (9, fake_termios.TCIFLUSH)])
        self.assertEqual(len(fake_termios.set_calls), 4)

    def test_silenced_input_is_noop_for_non_tty(self) -> None:
        fake_termios = _FakeTermios()
        stdin = _FakeStdin(tty=False)

        with patch.object(terminal_io, "_termios", fake_termios):
            with terminal_io.silenced_terminal_input(stdin) as guard:
                self.assertFalse(guard.enabled)

        self.assertEqual(fake_termios.set_calls, [])
        self.assertEqual(fake_termios.flush_calls, [])


if __name__ == "__main__":
    unittest.main()

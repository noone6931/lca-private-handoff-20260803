from __future__ import annotations

import signal
import termios
import unittest
from unittest.mock import patch

from local_agent.frontends.tui.native_renderer import NativeScrollbackRenderer
from local_agent.frontends.tui.screen import _copy_with_osc52
from local_agent.frontends.tui.screen import _TerminalInputReader
from local_agent.frontends.tui.screen import _TerminalSession
from local_agent.frontends.tui.screen import _TerminalSignalHandlers
from local_agent.frontends.tui.screen import streams_are_tty


class _Stream:
    def fileno(self) -> int:
        return 9

    def isatty(self) -> bool:
        return True


class _Renderer:
    def __init__(self) -> None:
        self.calls = []

    def suspend(self) -> None:
        self.calls.append("suspend")

    def resume(self) -> None:
        self.calls.append("resume")


class _Session:
    def __init__(self) -> None:
        self.calls = []

    def suspend(self) -> None:
        self.calls.append("suspend")

    def resume(self) -> None:
        self.calls.append("resume")


class TuiScreenTests(unittest.TestCase):
    def test_osc52_copy_encodes_plain_text_without_interpreting_it(self) -> None:
        with patch("local_agent.frontends.tui.screen.os.write", return_value=128) as write:
            _copy_with_osc52("answer\x1b[31m", _Stream())

        payload = write.call_args.args[1]
        self.assertTrue(payload.startswith(b"\x1b]52;c;"))
        self.assertTrue(payload.endswith(b"\x07"))
        self.assertNotIn(b"[31m", payload)

    def test_sigint_is_a_consumable_cancel_intent(self) -> None:
        handlers = _TerminalSignalHandlers(_Session(), _Renderer())  # type: ignore[arg-type]

        handlers._request_interrupt(None, None)

        self.assertTrue(handlers.consume_interrupt())
        self.assertFalse(handlers.consume_interrupt())

    def test_terminal_session_uses_normal_screen_and_restores_termios(self) -> None:
        original = [
            termios.ICRNL | getattr(termios, "IXON", 0),
            0,
            0,
            termios.ECHO | termios.ICANON | termios.ISIG,
            0,
            0,
            [0] * 32,
        ]
        payloads = []

        def write(_fd, payload):
            payloads.append(payload)
            return len(payload)

        with (
            patch("local_agent.frontends.tui.screen.termios.tcgetattr", return_value=original),
            patch("local_agent.frontends.tui.screen.termios.tcsetattr") as set_attr,
            patch("local_agent.frontends.tui.screen.os.write", side_effect=write),
        ):
            with _TerminalSession(_Stream(), _Stream()):
                pass

        configured = set_attr.call_args_list[0].args[2]
        self.assertFalse(configured[3] & termios.ECHO)
        self.assertFalse(configured[3] & termios.ICANON)
        self.assertTrue(configured[3] & termios.ISIG)
        self.assertEqual(payloads[0], b"\x1b[?2004h")
        self.assertIn(b"\x1b[?2004l", payloads[-1])
        self.assertNotIn(b"\x1b[?1007h", b"".join(payloads))
        self.assertNotIn(b"\x1b[?1049h", b"".join(payloads))
        self.assertEqual(set_attr.call_args_list[-1].args[2], original)

    def test_suspend_restores_terminal_before_process_stop(self) -> None:
        session = _Session()
        renderer = _Renderer()
        handlers = _TerminalSignalHandlers(session, renderer)  # type: ignore[arg-type]

        with (
            patch("local_agent.frontends.tui.screen.signal.signal"),
            patch("local_agent.frontends.tui.screen.os.kill"),
        ):
            handlers._suspend(getattr(signal, "SIGTSTP", 20), None)

        self.assertEqual(renderer.calls, ["suspend", "resume"])
        self.assertEqual(session.calls, ["suspend", "resume"])

    def test_input_reader_preserves_fragmented_utf8_and_csi_sequences(self) -> None:
        reader = _TerminalInputReader(_Stream())
        encoded = "中".encode("utf-8")

        with (
            patch("local_agent.frontends.tui.screen.select.select", return_value=([9], [], [])),
            patch(
                "local_agent.frontends.tui.screen.os.read",
                side_effect=(encoded[:1], encoded[1:] + b"\x1b[", b"A"),
            ),
        ):
            self.assertEqual(reader.read(timeout=0), ())
            text_events = reader.read(timeout=0)
            key_events = reader.read(timeout=0)

        self.assertEqual([(event.kind, event.value) for event in text_events], [("key", "中")])
        self.assertEqual([(event.kind, event.value) for event in key_events], [("key", "UP")])

    def test_hup_and_term_raise_for_outer_restoration(self) -> None:
        for signal_name in ("SIGHUP", "SIGTERM"):
            with self.subTest(signal_name=signal_name), self.assertRaises(KeyboardInterrupt):
                _TerminalSignalHandlers._interrupt(getattr(signal, signal_name), None)

    def test_missing_termios_fails_capability_check_instead_of_cli_import(self) -> None:
        with patch("local_agent.frontends.tui.screen.termios", None):
            self.assertFalse(streams_are_tty(_Stream(), _Stream()))


if __name__ == "__main__":
    unittest.main()

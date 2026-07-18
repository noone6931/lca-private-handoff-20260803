from __future__ import annotations

from contextlib import nullcontext
import signal
import unittest
from unittest.mock import patch

from local_agent.frontends.tui.input import BracketedPasteDecoder
from local_agent.frontends.tui.screen import _copy_with_osc52
from local_agent.frontends.tui.screen import _read_inputs
from local_agent.frontends.tui.screen import _TerminalModes
from local_agent.frontends.tui.screen import _TerminalSignalHandlers
from local_agent.frontends.tui.screen import _mouse_inputs


class _Output:
    def fileno(self) -> int:
        return 9


class TuiScreenTests(unittest.TestCase):
    def test_osc52_copy_encodes_plain_text_without_interpreting_it(self) -> None:
        with (
            patch("local_agent.frontends.tui.screen.sys.stdout", _Output()),
            patch("local_agent.frontends.tui.screen.os.write") as write,
        ):
            _copy_with_osc52("answer\x1b[31m")

        payload = write.call_args.args[1]
        self.assertTrue(payload.startswith(b"\x1b]52;c;"))
        self.assertTrue(payload.endswith(b"\x07"))
        self.assertNotIn(b"[31m", payload)

    def test_sigint_is_a_consumable_cancel_intent(self) -> None:
        handlers = _TerminalSignalHandlers(object(), object())

        handlers._request_interrupt(None, None)

        self.assertTrue(handlers.consume_interrupt())
        self.assertFalse(handlers.consume_interrupt())

    def test_terminal_modes_restore_bracketed_paste_on_normal_and_exception_exit(self) -> None:
        for failure in (False, True):
            with self.subTest(failure=failure):
                with (
                    patch("local_agent.frontends.tui.screen.os.write") as write,
                    self.assertRaises(RuntimeError) if failure else nullcontext(),
                ):
                    with _TerminalModes(_Output()):
                        if failure:
                            raise RuntimeError("boom")
                payloads = [call.args[1] for call in write.call_args_list]
                self.assertEqual(
                    payloads,
                    [b"\x1b[?1007h\x1b[?2004h", b"\x1b[?2004l\x1b[?1007l"],
                )

    def test_suspend_temporarily_restores_bracketed_paste_mode(self) -> None:
        modes = _TerminalModes(_Output())
        curses_module = type(
            "Curses",
            (),
            {
                "error": RuntimeError,
                "endwin": staticmethod(lambda: None),
                "reset_prog_mode": staticmethod(lambda: None),
            },
        )()
        stdscr = type("Screen", (), {"refresh": lambda self: None})()
        handlers = _TerminalSignalHandlers(stdscr, curses_module, terminal_modes=modes)

        with (
            patch("local_agent.frontends.tui.screen.os.write") as write,
            patch("local_agent.frontends.tui.screen.os.kill"),
            patch("local_agent.frontends.tui.screen.signal.signal"),
        ):
            modes.resume()
            handlers._suspend(20, None)

        self.assertEqual(
            [call.args[1] for call in write.call_args_list],
            [
                b"\x1b[?1007h\x1b[?2004h",
                b"\x1b[?2004l\x1b[?1007l",
                b"\x1b[?1007h\x1b[?2004h",
            ],
        )

    def test_hup_and_term_exception_paths_restore_bracketed_paste_mode(self) -> None:
        for signal_name in ("SIGHUP", "SIGTERM"):
            with self.subTest(signal_name=signal_name), patch(
                "local_agent.frontends.tui.screen.os.write"
            ) as write, self.assertRaises(KeyboardInterrupt):
                with _TerminalModes(_Output()):
                    _TerminalSignalHandlers._interrupt(getattr(signal, signal_name), None)
            self.assertEqual(
                [call.args[1] for call in write.call_args_list],
                [b"\x1b[?1007h\x1b[?2004h", b"\x1b[?2004l\x1b[?1007l"],
            )

    def test_mouse_wheel_maps_both_directions_without_platform_button5_constant(self) -> None:
        state = [1 << 25]
        curses_module = type(
            "Curses",
            (),
            {
                "error": RuntimeError,
                "BUTTON4_PRESSED": 1 << 19,
                "getmouse": staticmethod(lambda: (0, 0, 0, 0, state[0])),
            },
        )()
        self.assertEqual(_mouse_inputs(curses_module)[0].value, "WHEEL_DOWN")
        state[0] = 1 << 24
        self.assertEqual(_mouse_inputs(curses_module), ())
        state[0] = 1 << 19
        self.assertEqual(_mouse_inputs(curses_module)[0].value, "WHEEL_UP")

    def test_mouse_wheel_prefers_platform_button5_pressed_constant(self) -> None:
        curses_module = type(
            "Curses",
            (),
            {
                "error": RuntimeError,
                "BUTTON4_PRESSED": 1 << 19,
                "BUTTON5_PRESSED": 1 << 29,
                "getmouse": staticmethod(lambda: (0, 0, 0, 0, 1 << 29)),
            },
        )()

        self.assertEqual(_mouse_inputs(curses_module)[0].value, "WHEEL_DOWN")

    def test_integer_enter_preserves_pending_escape_as_alt_enter(self) -> None:
        curses_module = type(
            "Curses",
            (),
            {
                "error": RuntimeError,
                "KEY_ENTER": 343,
                "KEY_MOUSE": 409,
            },
        )()
        stdscr = type("Screen", (), {"get_wch": lambda self: 343})()
        decoder = BracketedPasteDecoder()
        self.assertEqual(decoder.feed("\x1b"), ())

        events = _read_inputs(stdscr, curses_module, decoder)

        self.assertEqual([(event.kind, event.value) for event in events], [("key", "ALT_ENTER")])
        self.assertNotIn("ENTER", [event.value for event in events])
        self.assertNotIn("ESC", [event.value for event in events])

        plain_events = _read_inputs(stdscr, curses_module, BracketedPasteDecoder())
        self.assertEqual(
            [(event.kind, event.value) for event in plain_events],
            [("key", "ENTER")],
        )
if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from unittest.mock import patch

from local_agent.frontends.tui.screen import _copy_with_osc52
from local_agent.frontends.tui.screen import _TerminalSignalHandlers


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


if __name__ == "__main__":
    unittest.main()

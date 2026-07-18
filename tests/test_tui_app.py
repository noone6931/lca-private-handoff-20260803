from __future__ import annotations

import io
import time
import unittest

from local_agent.cancellation import RunCancellation
from local_agent.frontends.tui.app import run_tui
from local_agent.frontends.tui.app import tui_is_supported
from local_agent.frontends.tui.mailbox import TuiMailbox


class _FakeRuntime:
    def __init__(self) -> None:
        self.commands = self
        self.cancellation = RunCancellation()
        self.handlers = []
        self.dispatched = []

    def set_interaction_handler(self, handler) -> None:
        self.handlers.append(handler)

    def dispatch(self, command):
        self.dispatched.append(command)
        from local_agent.protocol.commands import CommandResult

        return CommandResult(command.command_id, "s1", "r1", "ok", {"content": "done"})


class TuiAppTests(unittest.TestCase):
    def test_non_tty_is_not_supported(self) -> None:
        self.assertFalse(tui_is_supported(input_stream=io.StringIO(), output_stream=io.StringIO()))

    def test_screen_runner_owns_lifecycle_and_restores_interaction_handler(self) -> None:
        runtime = _FakeRuntime()
        mailbox = TuiMailbox(capacity=8)
        observed = []

        code = run_tui(
            runtime,  # type: ignore[arg-type]
            mailbox,
            screen_runner=lambda controller: observed.append(controller.view.focus) or 0,
        )

        self.assertEqual(code, 0)
        self.assertEqual(observed, ["chat"])
        self.assertEqual(len(runtime.handlers), 2)
        self.assertIsNotNone(runtime.handlers[0])
        self.assertIsNone(runtime.handlers[-1])

    def test_non_tty_falls_back_to_existing_chat(self) -> None:
        runtime = _FakeRuntime()
        output = io.StringIO()

        code = run_tui(
            runtime,  # type: ignore[arg-type]
            TuiMailbox(capacity=8),
            input_stream=io.StringIO("/exit\n"),
            output_stream=output,
        )

        self.assertEqual(code, 0)
        self.assertIn("Interactive TUI unavailable", output.getvalue())
        self.assertIn("local-agent chat", output.getvalue())

    def test_initial_prompt_is_dispatched_by_worker_before_screen_exit(self) -> None:
        runtime = _FakeRuntime()
        mailbox = TuiMailbox(capacity=8)

        def screen(controller) -> int:
            deadline = time.monotonic() + 1
            while not runtime.dispatched and time.monotonic() < deadline:
                controller.poll()
                time.sleep(0.005)
            return 0

        code = run_tui(
            runtime,  # type: ignore[arg-type]
            mailbox,
            screen_runner=screen,
            initial_prompt="inspect project",
        )

        self.assertEqual(code, 0)
        self.assertEqual([command.type for command in runtime.dispatched], ["SubmitPrompt"])
        self.assertEqual(runtime.dispatched[0].payload, {"prompt": "inspect project"})

    def test_non_tty_fallback_does_not_swallow_initial_prompt(self) -> None:
        runtime = _FakeRuntime()
        output = io.StringIO()

        code = run_tui(
            runtime,  # type: ignore[arg-type]
            TuiMailbox(capacity=8),
            input_stream=io.StringIO("/exit\n"),
            output_stream=output,
            initial_prompt="inspect fallback",
        )

        self.assertEqual(code, 0)
        self.assertEqual(runtime.dispatched[0].payload, {"prompt": "inspect fallback"})


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from local_agent.cli import _handle_repl_command
from local_agent.cli import _is_chat_prompt


class _FakeRuntime:
    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    def approval_summary(self) -> str:
        return "summary"

    def status_summary(self) -> str:
        return "status"

    def tool_summary(self) -> str:
        return "tools"

    def set_session_approval_mode(self, mode: str) -> None:
        self.calls.append(("mode", mode))

    def set_session_tool_policy(self, tool: str, policy: str) -> None:
        self.calls.append((policy, tool))

    def reset_session_tool_policy(self, tool: str) -> None:
        self.calls.append(("reset", tool))


class CliTests(unittest.TestCase):
    def test_chat_prompt_alias_is_detected(self) -> None:
        self.assertTrue(_is_chat_prompt(["chat"]))
        self.assertFalse(_is_chat_prompt(["chat", "about", "this", "repo"]))
        self.assertFalse(_is_chat_prompt(["describe", "chat"]))

    def test_approval_repl_commands_update_runtime(self) -> None:
        runtime = _FakeRuntime()
        output = io.StringIO()

        with redirect_stdout(output):
            _handle_repl_command(runtime, "/approval")
            _handle_repl_command(runtime, "/approval mode write")
            _handle_repl_command(runtime, "/approval allow run_tests")
            _handle_repl_command(runtime, "/approval prompt shell")
            _handle_repl_command(runtime, "/approval deny write_file")
            _handle_repl_command(runtime, "/approval reset shell")

        self.assertIn("summary", output.getvalue())
        self.assertEqual(
            runtime.calls,
            [
                ("mode", "write"),
                ("allow", "run_tests"),
                ("prompt", "shell"),
                ("deny", "write_file"),
                ("reset", "shell"),
            ],
        )

    def test_terminal_help_status_and_tools_commands(self) -> None:
        runtime = _FakeRuntime()
        output = io.StringIO()

        with redirect_stdout(output):
            _handle_repl_command(runtime, "/help")
            _handle_repl_command(runtime, "/status")
            _handle_repl_command(runtime, "/tools")

        rendered = output.getvalue()
        self.assertIn("/approval", rendered)
        self.assertIn("/exit", rendered)
        self.assertIn("status", rendered)
        self.assertIn("tools", rendered)

    def test_unknown_terminal_command_points_to_help(self) -> None:
        runtime = _FakeRuntime()
        output = io.StringIO()

        with redirect_stdout(output):
            _handle_repl_command(runtime, "/wat")

        self.assertIn("Unknown command: /wat", output.getvalue())
        self.assertIn("Type /help", output.getvalue())


if __name__ == "__main__":
    unittest.main()

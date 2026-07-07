from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from local_agent.cli import _handle_repl_command


class _FakeRuntime:
    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    def approval_summary(self) -> str:
        return "summary"

    def set_session_approval_mode(self, mode: str) -> None:
        self.calls.append(("mode", mode))

    def set_session_tool_policy(self, tool: str, policy: str) -> None:
        self.calls.append((policy, tool))

    def reset_session_tool_policy(self, tool: str) -> None:
        self.calls.append(("reset", tool))


class CliTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import contextmanager
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent.cli import _handle_repl_command
from local_agent.cli import _is_chat_prompt
from local_agent.cli import main


class _FakeRuntime:
    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    def approval_summary(self) -> str:
        return "summary"

    def status_summary(self) -> str:
        return "status"

    def tool_summary(self) -> str:
        return "tools"

    def workspace_summary(self) -> str:
        return "workspace roots"

    def add_workspace_root(self, path: str) -> None:
        self.calls.append(("workspace-add", path))

    def remove_workspace_root(self, path: str) -> None:
        self.calls.append(("workspace-remove", path))

    def reset_workspace_roots(self) -> None:
        self.calls.append(("workspace-reset", None))

    def move_workspace(self, path: str) -> None:
        self.calls.append(("workspace-move", path))

    def set_session_approval_mode(self, mode: str) -> None:
        self.calls.append(("mode", mode))

    def set_session_tool_policy(self, tool: str, policy: str) -> None:
        self.calls.append((policy, tool))

    def reset_session_tool_policy(self, tool: str) -> None:
        self.calls.append(("reset", tool))


class _FakeAgentRuntime:
    prompts: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        return None

    def run(self, prompt: str) -> str:
        type(self).prompts.append(prompt)
        return "done"


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

    def test_workspace_repl_commands_delegate_to_runtime(self) -> None:
        runtime = _FakeRuntime()
        output = io.StringIO()

        _handle_repl_command(runtime, "/workspace list", output)
        _handle_repl_command(runtime, '/workspace add "/tmp/project with spaces"', output)
        _handle_repl_command(runtime, "/add-dir /tmp/docs", output)
        _handle_repl_command(runtime, "/workspace remove /tmp/docs", output)
        _handle_repl_command(runtime, "/workspace reset", output)
        _handle_repl_command(runtime, '/move "/tmp/new primary"', output)

        self.assertIn("workspace roots", output.getvalue())
        self.assertEqual(
            runtime.calls,
            [
                ("workspace-add", "/tmp/project with spaces"),
                ("workspace-add", "/tmp/docs"),
                ("workspace-remove", "/tmp/docs"),
                ("workspace-reset", None),
                ("workspace-move", "/tmp/new primary"),
            ],
        )

    def test_unknown_terminal_command_points_to_help(self) -> None:
        runtime = _FakeRuntime()
        output = io.StringIO()

        with redirect_stdout(output):
            _handle_repl_command(runtime, "/wat")

        self.assertIn("Unknown command: /wat", output.getvalue())
        self.assertIn("Type /help", output.getvalue())

    def test_one_shot_prompt_silences_terminal_input_while_runtime_runs(self) -> None:
        _FakeAgentRuntime.prompts = []
        calls: list[str] = []

        @contextmanager
        def fake_silencer():
            calls.append("enter")
            try:
                yield
            finally:
                calls.append("exit")

        with tempfile.TemporaryDirectory() as tmp:
            config = SimpleNamespace(workspace=Path(tmp), state_dir=None)
            output = io.StringIO()
            with (
                patch("local_agent.cli.load_config", return_value=config),
                patch("local_agent.cli.AgentRuntime", _FakeAgentRuntime),
                patch("local_agent.cli.silenced_terminal_input", fake_silencer),
                redirect_stdout(output),
            ):
                code = main(["hello", "world"])

        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue().strip(), "done")
        self.assertEqual(_FakeAgentRuntime.prompts, ["hello world"])
        self.assertEqual(calls, ["enter", "exit"])


if __name__ == "__main__":
    unittest.main()

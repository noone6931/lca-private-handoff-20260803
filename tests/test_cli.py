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
from local_agent.protocol.commands import AgentCommand
from local_agent.protocol.commands import CommandResult


class _FakeRuntime:
    def __init__(self):
        self.commands = self
        self.calls: list[tuple[str, str | None]] = []

    def dispatch(self, command: AgentCommand) -> CommandResult:
        payload = command.payload
        if command.type == "SetApprovalMode":
            self.calls.append(("mode", payload["mode"]))
        elif command.type == "SetToolApproval":
            self.calls.append((payload["policy"], payload["tool"]))
        elif command.type == "ResetToolApproval":
            self.calls.append(("reset", payload["tool"]))
        elif command.type == "AddWorkspaceRoot":
            self.calls.append(("workspace-add", payload["path"]))
        elif command.type == "RemoveWorkspaceRoot":
            self.calls.append(("workspace-remove", payload["path"]))
        elif command.type == "ResetWorkspaceRoots":
            self.calls.append(("workspace-reset", None))
        elif command.type == "MoveWorkspace":
            self.calls.append(("workspace-move", payload["path"]))
        text = {
            "GetStatus": "status",
            "ListTools": "tools",
            "ListWorkspaceRoots": "workspace roots",
        }.get(command.type, "summary" if "Approval" in command.type else "workspace roots")
        return CommandResult(command.command_id, "s1", None, "ok", {"text": text})


class _FakeAgentRuntime:
    prompts: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        self.commands = self

    def dispatch(self, command: AgentCommand) -> CommandResult:
        type(self).prompts.append(str(command.payload["prompt"]))
        return CommandResult(command.command_id, "s1", "r1", "ok", {"content": "done"})


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

    def test_explicit_tui_uses_independent_frontend_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = SimpleNamespace(workspace=Path(tmp), state_dir=Path(tmp) / "state")
            with (
                patch("local_agent.cli.load_config", return_value=config),
                patch("local_agent.cli.AgentRuntime", _FakeAgentRuntime),
                patch("local_agent.cli.tui_is_supported", return_value=True),
                patch("local_agent.cli.run_tui", return_value=0) as run_tui,
            ):
                code = main(["--tui"])

        self.assertEqual(code, 0)
        self.assertEqual(run_tui.call_count, 1)
        self.assertEqual(type(run_tui.call_args.args[1]).__name__, "TuiMailbox")
        self.assertEqual(
            run_tui.call_args.kwargs["composer_history"].path,
            (config.state_dir / "composer_history.jsonl").resolve(),
        )

    def test_explicit_tui_prompt_is_forwarded_to_typed_frontend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = SimpleNamespace(workspace=Path(tmp), state_dir=Path(tmp) / "state")
            with (
                patch("local_agent.cli.load_config", return_value=config),
                patch("local_agent.cli.AgentRuntime", _FakeAgentRuntime),
                patch("local_agent.cli.tui_is_supported", return_value=True),
                patch("local_agent.cli.run_tui", return_value=0) as run_tui,
            ):
                code = main(["--tui", "inspect", "project"])

        self.assertEqual(code, 0)
        self.assertEqual(run_tui.call_args.kwargs["initial_prompt"], "inspect project")

    def test_chat_and_non_tty_tui_fallback_preserve_explicit_initial_prompt(self) -> None:
        for frontend_flag in ("--chat", "--tui"):
            with self.subTest(frontend_flag=frontend_flag), tempfile.TemporaryDirectory() as tmp:
                config = SimpleNamespace(workspace=Path(tmp), state_dir=Path(tmp) / "state")

                def consume_prompt(runtime, **kwargs) -> int:
                    del runtime
                    self.assertEqual(kwargs["input_stream"].readline(), "inspect fallback\n")
                    self.assertEqual(
                        kwargs["composer_history"].path,
                        (config.state_dir / "composer_history.jsonl").resolve(),
                    )
                    return 0

                with (
                    patch("local_agent.cli.load_config", return_value=config),
                    patch("local_agent.cli.AgentRuntime", _FakeAgentRuntime),
                    patch("local_agent.cli.tui_is_supported", return_value=False),
                    patch("local_agent.cli.run_terminal_chat", side_effect=consume_prompt) as run_chat,
                ):
                    code = main([frontend_flag, "inspect", "fallback"])

                self.assertEqual(code, 0)
                self.assertEqual(run_chat.call_count, 1)

    def test_no_args_prefers_tui_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = SimpleNamespace(workspace=Path(tmp), state_dir=Path(tmp) / "state")
            with (
                patch("local_agent.cli.load_config", return_value=config),
                patch("local_agent.cli.AgentRuntime", _FakeAgentRuntime),
                patch("local_agent.cli.tui_is_supported", return_value=True),
                patch("local_agent.cli.run_tui", return_value=0) as run_tui,
                patch("local_agent.cli.run_terminal_chat") as run_chat,
            ):
                code = main([])

        self.assertEqual(code, 0)
        self.assertEqual(run_tui.call_count, 1)
        self.assertEqual(run_chat.call_count, 0)

    def test_no_args_falls_back_to_chat_when_tui_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = SimpleNamespace(workspace=Path(tmp), state_dir=Path(tmp) / "state")
            with (
                patch("local_agent.cli.load_config", return_value=config),
                patch("local_agent.cli.AgentRuntime", _FakeAgentRuntime),
                patch("local_agent.cli.tui_is_supported", return_value=False),
                patch("local_agent.cli.run_terminal_chat", return_value=0) as run_chat,
            ):
                code = main([])

        self.assertEqual(code, 0)
        self.assertEqual(run_chat.call_count, 1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from local_agent.frontends.terminal.app import run_terminal_chat, slash_command_completions
from local_agent.frontends.terminal.renderer import TerminalEventSink
from local_agent.protocol.commands import AgentCommand
from local_agent.protocol.commands import CommandResult
from local_agent.protocol.events import AgentEvent


class _FakeRuntime:
    def __init__(self) -> None:
        self.commands = self
        self.submitted: list[AgentCommand] = []

    def dispatch(self, command: AgentCommand) -> CommandResult:
        self.submitted.append(command)
        payload = {"text": "approval summary"} if command.type == "GetApproval" else {"content": "done"}
        return CommandResult(command.command_id, "s1", "r1", "ok", payload)


class TerminalFrontendTests(unittest.TestCase):
    def test_slash_command_completions_offer_root_commands_with_descriptions(self) -> None:
        root_completions = slash_command_completions("/")
        completions = slash_command_completions("/wor")

        self.assertIn("/help", [completion.text for completion in root_completions])
        self.assertIn("/approval", [completion.text for completion in root_completions])
        self.assertTrue(all(completion.description for completion in root_completions))
        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0].text, "/workspace")
        self.assertEqual(completions[0].start_position, -4)
        self.assertIn("workspace", completions[0].description)

    def test_slash_command_completions_offer_workspace_subcommands(self) -> None:
        completions = slash_command_completions("/workspace r")

        self.assertEqual([completion.text for completion in completions], ["remove", "reset"])
        self.assertTrue(all(completion.description for completion in completions))
        self.assertTrue(all(completion.start_position == -1 for completion in completions))

    def test_slash_command_completions_offer_approval_subcommands_and_modes(self) -> None:
        subcommands = slash_command_completions("/approval ")
        modes = slash_command_completions("/approval mode w")

        self.assertEqual(
            [completion.text for completion in subcommands],
            ["mode", "allow", "prompt", "deny", "reset"],
        )
        self.assertEqual([completion.text for completion in modes], ["write"])
        self.assertIn("Allow read", modes[0].description)

    def test_slash_command_completions_do_not_touch_natural_language_or_multiline_input(self) -> None:
        self.assertEqual(slash_command_completions("please /workspace"), ())
        self.assertEqual(slash_command_completions("/workspace\nlist"), ())
        self.assertEqual(slash_command_completions("/workspace add /tmp"), ())

    def test_terminal_chat_runs_prompts_and_routes_commands(self) -> None:
        runtime = _FakeRuntime()
        input_stream = io.StringIO("/approval\nhello\n/exit\n")
        output = io.StringIO()

        code = run_terminal_chat(
            runtime,  # type: ignore[arg-type]
            input_stream=input_stream,
            output_stream=output,
        )

        self.assertEqual(code, 0)
        self.assertEqual([command.type for command in runtime.submitted], ["GetApproval", "SubmitPrompt"])
        self.assertEqual(runtime.submitted[-1].payload, {"prompt": "hello"})
        self.assertIn("local-agent chat", output.getvalue())
        self.assertIn("Type /help", output.getvalue())
        self.assertIn("approval summary", output.getvalue())
        self.assertEqual(output.getvalue().count("approval summary"), 1)

    def test_terminal_chat_keeps_help_and_exit_frontend_local(self) -> None:
        runtime = _FakeRuntime()
        input_stream = io.StringIO("/help\n/exit\n")
        output = io.StringIO()

        code = run_terminal_chat(
            runtime,  # type: ignore[arg-type]
            input_stream=input_stream,
            output_stream=output,
        )

        self.assertEqual(code, 0)
        self.assertIn("Commands:", output.getvalue())
        self.assertEqual(runtime.submitted, [])

    def test_terminal_chat_silences_input_echo_while_runtime_runs(self) -> None:
        runtime = _FakeRuntime()
        input_stream = io.StringIO("hello\n/exit\n")
        output = io.StringIO()
        calls: list[str] = []

        @contextmanager
        def fake_silencer():
            calls.append("enter")
            try:
                yield
            finally:
                calls.append("exit")

        with patch("local_agent.frontends.terminal.app.silenced_terminal_input", fake_silencer):
            code = run_terminal_chat(
                runtime,  # type: ignore[arg-type]
                input_stream=input_stream,
                output_stream=output,
            )

        self.assertEqual(code, 0)
        self.assertEqual([command.type for command in runtime.submitted], ["SubmitPrompt"])
        self.assertEqual(calls, ["enter", "exit"])

    def test_terminal_chat_relies_on_turn_event_for_one_final_render(self) -> None:
        output = io.StringIO()
        sink = TerminalEventSink(stream=output, show_tools=False, use_rich=False)

        class _RenderingRuntime(_FakeRuntime):
            def dispatch(self, command: AgentCommand) -> CommandResult:
                result = super().dispatch(command)
                if command.type == "SubmitPrompt":
                    sink.emit(_event("TurnFinished", {"content": "done"}))
                return result

        runtime = _RenderingRuntime()
        code = run_terminal_chat(
            runtime,  # type: ignore[arg-type]
            input_stream=io.StringIO("hello\n/exit\n"),
            output_stream=output,
        )

        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue().count("done"), 1)

    def test_terminal_event_sink_renders_append_only_lines(self) -> None:
        output = io.StringIO()
        sink = TerminalEventSink(stream=output, show_tools=True, use_rich=False)

        sink.emit(_event("SessionStarted", {}))
        sink.emit(_event("ToolStarted", {"name": "read_file", "arguments": '{"path":"README.md"}'}))
        sink.emit(_event("ToolOutput", {"name": "read_file", "is_error": False, "content_preview": "ok"}))
        sink.emit(_event("ToolFinished", {"name": "read_file", "content_length": 42}))
        sink.emit(_event("TurnFinished", {"content": "final answer"}))

        rendered = output.getvalue()
        self.assertIn("[session] s1", rendered)
        self.assertIn("[tool:start] read_file", rendered)
        self.assertIn("[tool:end] read_file ok (42 chars)", rendered)
        self.assertIn("final answer", rendered)
        self.assertNotIn("[tool:error]", rendered)

    def test_terminal_event_sink_can_hide_tool_timeline(self) -> None:
        output = io.StringIO()
        sink = TerminalEventSink(stream=output, show_tools=False, use_rich=False)

        sink.emit(_event("ToolStarted", {"name": "read_file", "arguments": "{}"}))
        sink.emit(_event("TurnFinished", {"content": "done"}))

        rendered = output.getvalue()
        self.assertNotIn("read_file", rendered)
        self.assertIn("done", rendered)

    def test_terminal_event_sink_renders_approval_result(self) -> None:
        output = io.StringIO()
        sink = TerminalEventSink(stream=output, show_tools=True, use_rich=False)

        sink.emit(_event("ApprovalResult", {"tool": "apply_patch", "decision": "allow_session", "allowed": True}))
        sink.emit(_event("ApprovalResult", {"tool": "shell", "decision": "reject_once", "allowed": False}))

        rendered = output.getvalue()
        self.assertIn("[approval] apply_patch allow_session", rendered)
        self.assertIn("[approval] shell reject_once", rendered)

    def test_terminal_event_sink_keeps_square_bracket_labels_with_rich(self) -> None:
        output = io.StringIO()
        sink = TerminalEventSink(stream=output, show_tools=True, use_rich=True)

        sink.emit(_event("SessionStarted", {}))

        self.assertIn("[session] s1", output.getvalue())


def _event(event_type: str, payload: dict) -> AgentEvent:
    return AgentEvent(
        event_id="e1",
        session_id="s1",
        run_id="r1",
        seq=1,
        timestamp=0.0,
        type=event_type,
        payload=payload,
    )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import unittest

from local_agent.frontends.terminal.app import run_terminal_chat
from local_agent.frontends.terminal.renderer import TerminalEventSink
from local_agent.protocol.events import AgentEvent


class _FakeRuntime:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "done"


class TerminalFrontendTests(unittest.TestCase):
    def test_terminal_chat_runs_prompts_and_routes_commands(self) -> None:
        runtime = _FakeRuntime()
        commands = []
        input_stream = io.StringIO("/approval\nhello\n/exit\n")
        output = io.StringIO()

        code = run_terminal_chat(
            runtime,  # type: ignore[arg-type]
            command_handler=lambda rt, command, _stream: commands.append((rt, command)),
            input_stream=input_stream,
            output_stream=output,
        )

        self.assertEqual(code, 0)
        self.assertEqual(runtime.prompts, ["hello"])
        self.assertEqual(commands, [(runtime, "/approval")])
        self.assertIn("local-agent chat", output.getvalue())
        self.assertIn("Type /help", output.getvalue())

    def test_terminal_chat_sends_command_output_to_frontend_stream(self) -> None:
        runtime = _FakeRuntime()
        input_stream = io.StringIO("/help\n/exit\n")
        output = io.StringIO()

        code = run_terminal_chat(
            runtime,  # type: ignore[arg-type]
            command_handler=lambda _rt, _command, stream: print("command help", file=stream),
            input_stream=input_stream,
            output_stream=output,
        )

        self.assertEqual(code, 0)
        self.assertIn("command help", output.getvalue())

    def test_terminal_event_sink_renders_append_only_lines(self) -> None:
        output = io.StringIO()
        sink = TerminalEventSink(stream=output, show_tools=True, use_rich=False)

        sink.emit(_event("SessionStarted", {}))
        sink.emit(_event("ToolStarted", {"name": "read_file", "arguments": '{"path":"README.md"}'}))
        sink.emit(_event("ToolOutput", {"name": "read_file", "is_error": False, "content_preview": "ok"}))
        sink.emit(_event("ToolFinished", {"name": "read_file", "content_length": 42}))
        sink.emit(_event("SessionFinished", {"content": "final answer"}))

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
        sink.emit(_event("SessionFinished", {"content": "done"}))

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

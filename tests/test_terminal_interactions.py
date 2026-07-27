from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from local_agent.frontends.terminal.app import run_terminal_chat
from local_agent.frontends.terminal.app import is_slash_command_input
from local_agent.frontends.terminal.interactions import InputState
from local_agent.frontends.terminal.interactions import TerminalInteractionController
from local_agent.protocol.commands import AgentCommand
from local_agent.protocol.commands import CommandResult
from local_agent.protocol.interactions import InteractionRequest
from local_agent.tools.base import Tool
from local_agent.tools.base import ToolContext
from local_agent.tools.base import ToolRegistry
from local_agent.tools.base import ToolResult
from local_agent.tools.interaction import ask_user


class _NestedInteractionRuntime:
    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self.commands = self
        self.handler = None
        self.prompts: list[str] = []
        self.results: list[ToolResult] = []

    def set_interaction_handler(self, handler) -> None:
        self.handler = handler

    def dispatch(self, command: AgentCommand) -> CommandResult:
        prompt = str(command.payload["prompt"])
        self.prompts.append(prompt)
        result = ask_user(
            {"question": "Which scope should I inspect?"},
            ToolContext(
                workspace=self._workspace,
                approval_mode="always-ask",
                interaction_handler=self.handler,
            ),
        )
        self.results.append(result)
        return CommandResult(command.command_id, "s1", "r1", "ok", {"content": result.content})


class TerminalInteractionTests(unittest.TestCase):
    def test_slash_command_input_is_detected_for_completion_only(self) -> None:
        self.assertTrue(is_slash_command_input("/help"))
        self.assertTrue(is_slash_command_input("  /workspace list"))
        self.assertFalse(is_slash_command_input("请分析这个项目"))
        self.assertFalse(is_slash_command_input("\n继续分析"))

    def test_ask_keeps_slash_commands_inside_focused_interaction_until_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _NestedInteractionRuntime(Path(tmp))
            output = io.StringIO()
            input_stream = io.StringIO("inspect this\n/help\n/workspace list\n/cancel\n/exit\n")

            code = run_terminal_chat(runtime, input_stream=input_stream, output_stream=output)  # type: ignore[arg-type]

        self.assertEqual(code, 0)
        self.assertEqual(runtime.prompts, ["inspect this"])
        self.assertTrue(runtime.results[0].is_error)
        self.assertIn("User cancelled the clarification question", runtime.results[0].content)
        self.assertEqual(runtime.handler, None)
        self.assertEqual(output.getvalue().count("This input is answering an Agent question"), 2)
        self.assertNotIn("Unknown command: /help", output.getvalue())

    def test_exec_approval_uses_once_only_choices_and_never_caches_session_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            decisions: dict[str, str] = {}
            events: list[tuple[str, dict]] = []
            output = io.StringIO()
            controller = TerminalInteractionController(
                input_stream=io.StringIO("s\ny\n"),
                output_stream=output,
            )
            calls: list[str] = []
            registry = ToolRegistry(
                [
                    Tool(
                        name="sample_exec",
                        description="sample execution",
                        tier="exec",
                        input_schema={
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                            "additionalProperties": False,
                        },
                        handler=lambda args, _context: calls.append(args["command"]) or ToolResult("ok"),
                    )
                ]
            )
            first = registry.execute(
                "sample_exec",
                {"command": "first"},
                ToolContext(
                    workspace=Path(tmp),
                    approval_mode="always-ask",
                    session_tool_approval=decisions,
                    interaction_handler=controller,
                    event_callback=lambda event_type, payload: events.append((event_type, payload)),
                ),
            )
            second = registry.execute(
                "sample_exec",
                {"command": "second"},
                ToolContext(
                    workspace=Path(tmp),
                    approval_mode="always-ask",
                    session_tool_approval=decisions,
                    interaction_handler=controller,
                    event_callback=lambda event_type, payload: events.append((event_type, payload)),
                ),
            )

        self.assertTrue(first.is_error)
        self.assertFalse(second.is_error)
        self.assertEqual(calls, ["second"])
        self.assertEqual(decisions, {})
        self.assertEqual(controller.state, InputState.CHAT)
        self.assertNotIn("always this session", output.getvalue())
        self.assertNotIn("reject this session", output.getvalue())
        self.assertNotIn("y/s/n/d", output.getvalue())
        event_types = [event_type for event_type, _payload in events]
        self.assertEqual(event_types.count("ApprovalRequested"), 2)
        self.assertEqual(event_types.count("InteractionRequested"), 2)
        self.assertEqual(event_types.count("InteractionResolved"), 2)

    def test_cancelled_approval_returns_a_cancelled_tool_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = TerminalInteractionController(
                input_stream=io.StringIO("/cancel\n"),
                output_stream=io.StringIO(),
            )
            registry = ToolRegistry(
                [
                    Tool(
                        name="sample_exec",
                        description="sample execution",
                        tier="exec",
                        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                        handler=lambda _args, _context: ToolResult("ok"),
                    )
                ]
            )
            events: list[str] = []
            result = registry.execute(
                "sample_exec",
                {},
                ToolContext(
                    workspace=Path(tmp),
                    approval_mode="always-ask",
                    interaction_handler=controller,
                    event_callback=lambda event_type, _payload: events.append(event_type),
                ),
            )

        self.assertTrue(result.is_error)
        self.assertIn("approval cancelled by user", result.content)
        self.assertEqual(controller.state, InputState.CHAT)
        self.assertIn("InteractionCancelled", events)

    def test_approval_eof_is_not_misreported_as_budget_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = TerminalInteractionController(input_stream=io.StringIO(), output_stream=io.StringIO())
            registry = ToolRegistry(
                [
                    Tool(
                        name="sample_exec",
                        description="sample execution",
                        tier="exec",
                        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                        handler=lambda _args, _context: ToolResult("ok"),
                    )
                ]
            )
            result = registry.execute(
                "sample_exec",
                {},
                ToolContext(
                    workspace=Path(tmp),
                    approval_mode="always-ask",
                    interaction_handler=controller,
                ),
            )

        self.assertTrue(result.is_error)
        self.assertIn("stdin closed", result.content)
        self.assertNotIn("budget_seconds", result.content)

    def test_timeout_without_reading_input_is_reported_to_runtime(self) -> None:
        controller = TerminalInteractionController(input_stream=io.StringIO(), output_stream=io.StringIO())

        result = controller.request_interaction(InteractionRequest("ask", "Continue?", timeout_seconds=0))

        self.assertEqual(result.status, "timed_out")
        self.assertEqual(controller.state, InputState.CHAT)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from local_agent.frontends.composer_history import ComposerHistory
from local_agent.frontends.terminal.app import run_terminal_chat, slash_command_completions
from local_agent.frontends.terminal.prompt import TerminalHistoryRebindError
from local_agent.frontends.terminal.renderer import TerminalEventSink
from local_agent.frontends.tui.controller import TuiController
from local_agent.frontends.tui.mailbox import TuiMailbox
from local_agent.frontends.tui.model import TuiProjector
from local_agent.protocol.commands import AgentCommand
from local_agent.protocol.commands import CommandResult
from local_agent.protocol.events import AgentEvent


class _FakeRuntime:
    def __init__(self) -> None:
        self.commands = self
        self.submitted: list[AgentCommand] = []

    def dispatch(self, command: AgentCommand) -> CommandResult:
        self.submitted.append(command)
        if command.type == "GetApproval":
            payload = {"text": "approval summary"}
        elif command.type == "MoveWorkspace":
            payload = {"text": "workspace roots", "state_dir": "/state/new-workspace"}
        else:
            payload = {"content": "done"}
        return CommandResult(command.command_id, "s1", "r1", "ok", payload)


class _RecordingPrompt:
    def __init__(self, *, rebind_error: bool = False) -> None:
        self.history_paths: list[Path | None] = []
        self.rebind_error = rebind_error

    def __call__(self, *, input_stream=None) -> str:
        line = input_stream.readline()
        if line == "":
            raise EOFError
        return line

    def rebind_history(self, history_path: Path | None) -> None:
        self.history_paths.append(history_path)
        if self.rebind_error:
            raise TerminalHistoryRebindError(
                "Workspace moved, but persistent terminal history is disabled for this chat."
            )


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

    def test_terminal_chat_records_only_submitted_prompts_in_shared_history(self) -> None:
        runtime = _FakeRuntime()
        with tempfile.TemporaryDirectory() as tmp:
            history = ComposerHistory(Path(tmp) / "history.jsonl")
            code = run_terminal_chat(
                runtime,  # type: ignore[arg-type]
                composer_history=history,
                input_stream=io.StringIO("/status\nhello\nhello\n/exit\n"),
                output_stream=io.StringIO(),
            )

            self.assertEqual(code, 0)
            self.assertEqual(history.snapshot.local_entries, ("hello",))

    def test_terminal_submission_is_recalled_by_tui_from_the_same_jsonl_format(self) -> None:
        runtime = _FakeRuntime()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "composer_history.jsonl"
            terminal_history = ComposerHistory(path)
            run_terminal_chat(
                runtime,  # type: ignore[arg-type]
                composer_history=terminal_history,
                input_stream=io.StringIO("shared prompt\n/exit\n"),
                output_stream=io.StringIO(),
            )
            tui_history = ComposerHistory(path)
            controller = TuiController(
                TuiMailbox(capacity=8),
                TuiProjector(),
                type(
                    "Worker",
                    (),
                    {
                        "submit": lambda self, command: True,
                        "request_cancel": lambda self: True,
                        "interaction_bridge": type("Bridge", (), {"resolve": lambda *args: True})(),
                    },
                )(),  # type: ignore[arg-type]
                composer_history=tui_history,
            )

            controller.handle_key("UP")

            self.assertEqual(controller.view.input_text, "shared prompt")

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

    def test_terminal_chat_rebinds_history_after_successful_workspace_move(self) -> None:
        runtime = _FakeRuntime()
        prompt = _RecordingPrompt()
        history = ComposerHistory(None)

        with patch("local_agent.frontends.terminal.app.build_terminal_prompt", return_value=prompt) as build_prompt:
            code = run_terminal_chat(
                runtime,  # type: ignore[arg-type]
                composer_history=history,
                input_stream=io.StringIO("/move /workspace/new\n/exit\n"),
                output_stream=io.StringIO(),
            )

        self.assertEqual(code, 0)
        build_prompt.assert_called_once_with(history)
        self.assertEqual([command.type for command in runtime.submitted], ["MoveWorkspace"])
        self.assertEqual(prompt.history_paths, [Path("/state/new-workspace/composer_history.jsonl")])

    def test_terminal_chat_keeps_running_when_moved_history_is_unavailable(self) -> None:
        runtime = _FakeRuntime()
        prompt = _RecordingPrompt(rebind_error=True)
        output = io.StringIO()

        with patch("local_agent.frontends.terminal.app.build_terminal_prompt", return_value=prompt):
            code = run_terminal_chat(
                runtime,  # type: ignore[arg-type]
                input_stream=io.StringIO("/move /workspace/new\n/exit\n"),
                output_stream=output,
            )

        self.assertEqual(code, 0)
        self.assertIn("persistent terminal history is disabled", output.getvalue())

    def test_terminal_chat_does_not_rebind_history_after_failed_workspace_move(self) -> None:
        class _FailedMoveRuntime(_FakeRuntime):
            def dispatch(self, command: AgentCommand) -> CommandResult:
                self.submitted.append(command)
                return CommandResult(command.command_id, "s1", None, "error", {}, "move_failed", "failed")

        runtime = _FailedMoveRuntime()
        prompt = _RecordingPrompt()

        with patch("local_agent.frontends.terminal.app.build_terminal_prompt", return_value=prompt):
            code = run_terminal_chat(
                runtime,  # type: ignore[arg-type]
                input_stream=io.StringIO("/move /workspace/new\n/exit\n"),
                output_stream=io.StringIO(),
            )

        self.assertEqual(code, 0)
        self.assertEqual(prompt.history_paths, [])

    def test_terminal_chat_disables_old_history_when_move_result_has_no_partition(self) -> None:
        class _MissingPartitionRuntime(_FakeRuntime):
            def dispatch(self, command: AgentCommand) -> CommandResult:
                self.submitted.append(command)
                return CommandResult(command.command_id, "s1", "r1", "ok", {"text": "workspace roots"})

        runtime = _MissingPartitionRuntime()
        prompt = _RecordingPrompt()
        output = io.StringIO()

        with patch("local_agent.frontends.terminal.app.build_terminal_prompt", return_value=prompt):
            code = run_terminal_chat(
                runtime,  # type: ignore[arg-type]
                input_stream=io.StringIO("/move /workspace/new\n/exit\n"),
                output_stream=output,
            )

        self.assertEqual(code, 0)
        self.assertEqual(prompt.history_paths, [None])
        self.assertIn("without a terminal history partition", output.getvalue())

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

    def test_terminal_streams_deltas_and_does_not_repeat_identical_final(self) -> None:
        output = io.StringIO()
        sink = TerminalEventSink(stream=output, show_tools=True, use_rich=False)

        sink.emit(_event("TurnStarted", {}))
        sink.emit(
            _event(
                "AssistantDelta",
                {"message_id": "m1", "delta": "streamed ", "delta_index": 0, "provisional": True},
            )
        )
        sink.emit(
            _event(
                "AssistantDelta",
                {"message_id": "m1", "delta": "answer", "delta_index": 1, "provisional": True},
            )
        )
        sink.emit(_event("AssistantMessage", {"message_id": "m1", "content": "streamed answer"}))
        sink.emit(_event("TurnFinished", {"content": "streamed answer"}))

        self.assertEqual(output.getvalue(), "streamed answer\n")

    def test_terminal_marks_a_different_authoritative_final_after_provisional_text(self) -> None:
        output = io.StringIO()
        sink = TerminalEventSink(stream=output, show_tools=True, use_rich=False)

        sink.emit(_event("TurnStarted", {}))
        sink.emit(
            _event(
                "AssistantDelta",
                {"message_id": "m1", "delta": "unsafe draft", "delta_index": 0, "provisional": True},
            )
        )
        sink.emit(
            _event(
                "AssistantMessage",
                {"message_id": "m1", "content": "safe authoritative final", "authoritative": True},
            )
        )
        sink.emit(_event("TurnFinished", {"content": "safe authoritative final"}))

        rendered = output.getvalue()
        self.assertEqual(rendered.count("unsafe draft"), 1)
        self.assertEqual(rendered.count("safe authoritative final"), 1)
        self.assertIn("[authoritative final]", rendered)

    def test_terminal_closes_aborted_delta_and_renders_runtime_delivery(self) -> None:
        output = io.StringIO()
        sink = TerminalEventSink(stream=output, show_tools=True, use_rich=False)

        sink.emit(_event("TurnStarted", {}))
        sink.emit(
            _event(
                "AssistantDelta",
                {"message_id": "m1", "delta": "draft", "delta_index": 0, "provisional": True},
            )
        )
        sink.emit(_event("AssistantMessageAborted", {"message_id": "m1", "reason": "provider_error"}))
        sink.emit(_event("TurnFinished", {"content": "Provider request failed.", "reason": "provider_error"}))

        rendered = output.getvalue()
        self.assertEqual(rendered.count("draft"), 1)
        self.assertEqual(rendered.count("Provider request failed."), 1)
        self.assertIn("[authoritative final]", rendered)

    def test_terminal_preserves_a_distinct_runtime_delivery_after_provider_message(self) -> None:
        output = io.StringIO()
        sink = TerminalEventSink(stream=output, show_tools=True, use_rich=False)

        sink.emit(_event("TurnStarted", {}))
        sink.emit(_event("AssistantMessage", {"message_id": "m1", "content": "provider final"}))
        sink.emit(_event("TurnFinished", {"content": "runtime wrapper"}))

        rendered = output.getvalue()
        self.assertEqual(rendered.count("provider final"), 1)
        self.assertEqual(rendered.count("runtime wrapper"), 1)
        self.assertIn("[authoritative final]", rendered)

    def test_terminal_prints_only_the_runtime_appendix_for_augmented_delivery(self) -> None:
        output = io.StringIO()
        sink = TerminalEventSink(stream=output, show_tools=True, use_rich=False)

        sink.emit(_event("TurnStarted", {}))
        sink.emit(_event("AssistantMessage", {"message_id": "m1", "content": "provider final"}))
        sink.emit(
            _event(
                "TurnFinished",
                {
                    "content": "provider final\n\nVerification: tests passed.",
                    "final_message_id": "m1",
                    "origin": "runtime",
                    "output_kind": "runtime_augmented",
                },
            )
        )

        rendered = output.getvalue()
        self.assertEqual(rendered.count("provider final"), 1)
        self.assertEqual(rendered.count("Verification: tests passed."), 1)
        self.assertIn("[runtime delivery]", rendered)

    def test_terminal_removes_control_and_bidi_sequences_from_model_text(self) -> None:
        output = io.StringIO()
        sink = TerminalEventSink(stream=output, show_tools=True, use_rich=False)

        sink.emit(_event("TurnStarted", {}))
        sink.emit(
            _event(
                "AssistantDelta",
                {"message_id": "m1", "delta": "safe\x1b[31m\u202etext", "delta_index": 0},
            )
        )
        sink.emit(_event("AssistantMessage", {"message_id": "m1", "content": "safe\x1b[31m\u202etext"}))

        rendered = output.getvalue()
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertIn("safe[31mtext", rendered)

    def test_terminal_keeps_tool_logs_on_their_own_line_after_a_delta(self) -> None:
        output = io.StringIO()
        sink = TerminalEventSink(stream=output, show_tools=True, use_rich=False)

        sink.emit(
            _event(
                "AssistantDelta",
                {"message_id": "m1", "delta": "working", "delta_index": 0, "provisional": True},
            )
        )
        sink.emit(_event("ToolStarted", {"name": "read_file", "arguments": {"path": "README.md"}}))

        self.assertIn("working\n[tool:start] read_file", output.getvalue())

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

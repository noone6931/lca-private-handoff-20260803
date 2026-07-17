from __future__ import annotations

import unittest
from pathlib import Path

from local_agent.command_dispatcher import CommandDispatcher
from local_agent.protocol.commands import AgentCommand
from local_agent.protocol.commands import new_command
from local_agent.protocol.events import EventEmitter
from local_agent.protocol.events import ListEventSink


class _Session:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def append(self, event: str, payload: dict) -> None:
        self.records.append((event, payload))

    def load_event_payloads(self, event: str, *, max_events: int = 0) -> list[dict]:
        payloads = [payload for name, payload in self.records if name == event]
        return payloads[-max_events:] if max_events > 0 else payloads


class _FailFirstRunSummarySink(ListEventSink):
    def emit(self, event) -> None:
        self.events.append(event)
        if event.type == "RunSummary" and sum(item.type == "RunSummary" for item in self.events) == 1:
            raise OSError("simulated RunSummary sink failure after observe")


class _Runtime:
    def __init__(self, events: EventEmitter, mode: str = "final") -> None:
        self._events = events
        self.mode = mode
        self._is_running = False
        self._last_run_summary = None
        self._session = _Session()
        self.calls: list[tuple[str, str | None]] = []

    def _run_prompt(self, prompt: str) -> str:
        self._events.emit("UserMessage", {"content": prompt})
        self._events.emit("LlmRequest", {"step": 1})
        if self.mode == "interrupt":
            raise KeyboardInterrupt
        if self.mode == "exception":
            raise RuntimeError("runtime exploded")
        if self.mode == "unterminated":
            return "unreviewed draft"
        content = {
            "provider_error": "provider failed",
            "budget": "budget stopped",
            "length": "length stopped",
        }.get(self.mode, "done")
        reason = self.mode if self.mode != "final" else "final"
        if reason == "provider_error":
            self._events.emit("ErrorEvent", {"kind": "provider_error", "message": content})
        summary = {
            "run_id": self._events.run_id,
            "command_id": self._events.command_id,
            "termination_reason": reason,
        }
        self._last_run_summary = summary
        self._session.append("final", {"content": content})
        self._session.append("run_summary", summary)
        self._events.emit("RunSummary", summary)
        self._events.finish_turn(content=content, reason=reason, run_summary=summary)
        return content

    def approval_summary(self) -> str:
        return "approval"

    def status_summary(self) -> str:
        return "status"

    def tool_summary(self) -> str:
        return "tools"

    def workspace_summary(self) -> str:
        return "workspace"

    def add_workspace_root(self, raw_path: str) -> Path:
        self.calls.append(("add", raw_path))
        return Path(raw_path)

    def remove_workspace_root(self, raw_path: str) -> Path:
        self.calls.append(("remove", raw_path))
        return Path(raw_path)

    def reset_workspace_roots(self) -> None:
        self.calls.append(("reset-roots", None))

    def move_workspace(self, raw_path: str) -> Path:
        self.calls.append(("move", raw_path))
        return Path(raw_path)

    def set_session_approval_mode(self, mode: str) -> None:
        self.calls.append(("mode", mode))

    def set_session_tool_policy(self, tool: str, policy: str) -> None:
        self.calls.append((policy, tool))

    def reset_session_tool_policy(self, tool: str) -> None:
        self.calls.append(("reset-tool", tool))


def _dispatcher(mode: str = "final") -> tuple[CommandDispatcher, _Runtime, ListEventSink]:
    sink = ListEventSink()
    events = EventEmitter(session_id="session-1", sink=sink)
    runtime = _Runtime(events, mode)
    return CommandDispatcher(runtime, events, "session-1"), runtime, sink


class CommandDispatcherTests(unittest.TestCase):
    def test_submit_prompt_correlates_one_complete_turn(self) -> None:
        dispatcher, _, sink = _dispatcher()
        command = new_command("SubmitPrompt", {"prompt": "hello"}, session_id="session-1")

        result = dispatcher.dispatch(command)

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["content"], "done")
        self.assertEqual(result.payload["status"], "completed")
        self.assertTrue(result.payload["delivered"])
        event_types = [event.type for event in sink.events]
        self.assertEqual(event_types.count("TurnStarted"), 1)
        self.assertEqual(event_types.count("TurnFinished"), 1)
        self.assertLess(event_types.index("RunSummary"), event_types.index("TurnFinished"))
        self.assertTrue(all(event.command_id == command.command_id for event in sink.events))
        self.assertTrue(all(event.run_id == result.run_id for event in sink.events))

    def test_terminal_reasons_keep_transport_success_but_report_delivery_status(self) -> None:
        expected = {
            "provider_error": ("error", False, "provider failed"),
            "budget": ("stopped", False, "budget stopped"),
            "length": ("stopped", False, "length stopped"),
        }
        for reason, (status, delivered, content) in expected.items():
            with self.subTest(reason=reason):
                dispatcher, _, sink = _dispatcher(reason)
                result = dispatcher.dispatch(new_command("SubmitPrompt", {"prompt": "hello"}))

                self.assertTrue(result.ok)
                self.assertEqual(result.payload["status"], status)
                self.assertEqual(result.payload["delivered"], delivered)
                self.assertEqual(result.payload["content"], content)
                self.assertEqual([event.type for event in sink.events].count("TurnFinished"), 1)
                self.assertEqual([event.type for event in sink.events].count("TurnStarted"), 1)

    def test_legacy_run_returns_terminal_text_for_non_delivery_stops(self) -> None:
        for reason, content in (("provider_error", "provider failed"), ("budget", "budget stopped"), ("length", "length stopped")):
            with self.subTest(reason=reason):
                dispatcher, _, _ = _dispatcher(reason)
                self.assertEqual(dispatcher.run("hello"), content)

    def test_keyboard_interrupt_closes_one_error_turn_and_run_facade_reraises(self) -> None:
        dispatcher, _, sink = _dispatcher("interrupt")
        result = dispatcher.dispatch(new_command("SubmitPrompt", {"prompt": "hello"}))

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "interrupted")
        self.assertEqual(result.payload["status"], "interrupted")
        self.assertFalse(result.payload["delivered"])
        self.assertEqual([event.type for event in sink.events].count("TurnStarted"), 1)
        self.assertEqual([event.type for event in sink.events].count("TurnFinished"), 1)
        self.assertEqual([event.type for event in sink.events].count("ErrorEvent"), 1)

        facade, _, facade_sink = _dispatcher("interrupt")
        with self.assertRaises(KeyboardInterrupt):
            facade.run("hello")
        self.assertEqual([event.type for event in facade_sink.events].count("TurnFinished"), 1)

    def test_unhandled_runtime_exception_is_a_correlated_error_turn(self) -> None:
        dispatcher, _, sink = _dispatcher("exception")
        command = new_command("SubmitPrompt", {"prompt": "hello"})

        result = dispatcher.dispatch(command)

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "command_execution_error")
        self.assertEqual(result.payload["status"], "error")
        self.assertTrue(all(event.command_id == command.command_id for event in sink.events))
        self.assertEqual([event.type for event in sink.events].count("TurnFinished"), 1)

    def test_run_summary_sink_failure_reuses_committed_terminal_state_once(self) -> None:
        sink = _FailFirstRunSummarySink()
        events = EventEmitter(session_id="session-1", sink=sink)
        runtime = _Runtime(events)
        dispatcher = CommandDispatcher(runtime, events, "session-1")

        result = dispatcher.dispatch(new_command("SubmitPrompt", {"prompt": "hello"}))

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "command_execution_error")
        self.assertEqual(result.payload["reason"], "final")
        self.assertTrue(result.payload["delivered"])
        self.assertEqual([name for name, _ in runtime._session.records].count("final"), 1)
        self.assertEqual([name for name, _ in runtime._session.records].count("run_summary"), 1)
        self.assertEqual([event.type for event in sink.events].count("RunSummary"), 1)
        self.assertEqual([event.type for event in sink.events].count("TurnFinished"), 1)

    def test_return_without_terminal_event_fails_closed(self) -> None:
        dispatcher, _, sink = _dispatcher("unterminated")

        result = dispatcher.dispatch(new_command("SubmitPrompt", {"prompt": "hello"}))

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "missing_turn_terminal")
        self.assertEqual(result.payload["status"], "error")
        self.assertFalse(result.payload["delivered"])
        self.assertNotIn("unreviewed draft", result.payload["content"])
        self.assertEqual([event.type for event in sink.events].count("TurnFinished"), 1)

    def test_run_facade_and_submit_dispatch_have_isomorphic_lifecycle(self) -> None:
        facade, _, facade_sink = _dispatcher()
        direct, _, direct_sink = _dispatcher()

        facade_content = facade.run("hello")
        direct_result = direct.dispatch(new_command("SubmitPrompt", {"prompt": "hello"}))

        self.assertEqual(facade_content, direct_result.payload["content"])
        self.assertEqual([event.type for event in facade_sink.events], [event.type for event in direct_sink.events])
        self.assertEqual(
            [event.payload.get("reason") for event in facade_sink.events],
            [event.payload.get("reason") for event in direct_sink.events],
        )

    def test_invalid_unsupported_and_session_mismatch_fail_closed(self) -> None:
        dispatcher, _, sink = _dispatcher()
        invalid = AgentCommand("c1", None, None, 0.0, "SubmitPrompt", {})
        unknown = AgentCommand("c2", None, None, 0.0, "Unknown", {})
        mismatch = new_command("GetStatus", session_id="another-session")
        unsupported = new_command("CancelRun")

        self.assertEqual(dispatcher.dispatch(invalid).error_code, "invalid_command")
        self.assertEqual(dispatcher.dispatch(unknown).error_code, "invalid_command")
        self.assertEqual(dispatcher.dispatch(mismatch).error_code, "session_mismatch")
        unsupported_result = dispatcher.dispatch(unsupported)
        self.assertEqual(unsupported_result.status, "unsupported")
        self.assertEqual(unsupported_result.error_code, "unsupported_command")
        self.assertEqual([event.type for event in sink.events], ["ErrorEvent"])

    def test_session_commands_route_to_existing_runtime_owners(self) -> None:
        dispatcher, runtime, sink = _dispatcher()

        result = dispatcher.dispatch(new_command("SetToolApproval", {"tool": "shell", "policy": "deny"}))

        self.assertTrue(result.ok)
        self.assertEqual(result.payload, {"text": "approval"})
        self.assertEqual(runtime.calls, [("deny", "shell")])
        self.assertEqual(sink.events, [])


if __name__ == "__main__":
    unittest.main()

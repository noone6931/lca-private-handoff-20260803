from __future__ import annotations

import unittest

from local_agent.protocol.commands import AgentCommand
from local_agent.protocol.commands import CommandResult
from local_agent.protocol.commands import command_validation_error
from local_agent.protocol.commands import new_command
from local_agent.protocol.events import EventEmitter
from local_agent.protocol.events import ListEventSink


class ProtocolTests(unittest.TestCase):
    def test_event_emitter_assigns_run_id_sequence_and_records_events(self) -> None:
        sink = ListEventSink()
        recorded = []
        emitter = EventEmitter(session_id="session-1", sink=sink, recorder=recorded.append)

        session = emitter.emit("SessionStarted", {})
        emitter.begin_command("command-1")
        run_id = emitter.start_run()
        started = emitter.emit("TurnStarted", {})
        first = emitter.emit("UserMessage", {"content": "hello"})
        summary = emitter.emit("RunSummary", {"termination_reason": "final"})
        second = emitter.finish_turn(
            content="done",
            reason="final",
            run_summary={"termination_reason": "final"},
        )
        emitter.end_command("command-1")

        self.assertEqual(first.session_id, "session-1")
        self.assertEqual(first.run_id, run_id)
        self.assertEqual(first.command_id, "command-1")
        self.assertEqual(session.seq, 1)
        self.assertEqual(started.seq, 2)
        self.assertEqual(first.seq, 3)
        self.assertEqual(summary.seq, 4)
        self.assertEqual(second.seq, 5)
        self.assertEqual(sink.events, [session, started, first, summary, second])
        self.assertEqual(recorded, sink.events)
        self.assertEqual(first.to_dict()["payload"], {"content": "hello"})
        self.assertEqual(first.to_dict()["command_id"], "command-1")
        self.assertEqual(second.payload["status"], "completed")
        self.assertTrue(second.payload["delivered"])

    def test_event_emitter_rejects_unknown_event_type(self) -> None:
        emitter = EventEmitter(session_id="session-1")

        with self.assertRaisesRegex(ValueError, "Unknown event type"):
            emitter.emit("SomethingElse", {})

    def test_command_to_dict_shape(self) -> None:
        command = new_command(
            "SetToolApproval",
            {"tool": "shell", "policy": "deny"},
            session_id="session-1",
            run_id="run-1",
        )

        rendered = command.to_dict()
        self.assertEqual(rendered["session_id"], "session-1")
        self.assertEqual(rendered["run_id"], "run-1")
        self.assertEqual(rendered["type"], "SetToolApproval")
        self.assertEqual(rendered["payload"], {"tool": "shell", "policy": "deny"})
        self.assertTrue(rendered["command_id"])

    def test_command_rejects_unknown_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown command type"):
            new_command("DoAnything", {})

    def test_command_validation_rejects_missing_and_extra_payload_fields(self) -> None:
        missing = AgentCommand("c1", None, None, 0.0, "SubmitPrompt", {})
        extra = AgentCommand("c2", None, None, 0.0, "GetStatus", {"prompt": "no"})

        self.assertIn("missing fields: prompt", command_validation_error(missing) or "")
        self.assertIn("unexpected fields: prompt", command_validation_error(extra) or "")

    def test_command_result_is_transport_neutral_and_serializable(self) -> None:
        result = CommandResult("c1", "s1", "r1", "error", {}, "failed", "nope")

        self.assertFalse(result.ok)
        self.assertEqual(result.to_dict()["error"], {"code": "failed", "message": "nope"})

    def test_workspace_command_and_event_types_are_available(self) -> None:
        command = new_command("AddWorkspaceRoot", {"path": "/repo/frontend"}, session_id="session-1")
        emitter = EventEmitter(session_id="session-1")
        emitter.begin_command(command.command_id)
        event = emitter.emit("WorkspaceRootsChanged", {"revision": 1})
        emitter.end_command(command.command_id)

        self.assertEqual(command.type, "AddWorkspaceRoot")
        self.assertEqual(event.type, "WorkspaceRootsChanged")
        self.assertEqual(event.command_id, command.command_id)


if __name__ == "__main__":
    unittest.main()

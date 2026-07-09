from __future__ import annotations

import unittest

from local_agent.protocol.commands import new_command
from local_agent.protocol.events import EventEmitter
from local_agent.protocol.events import ListEventSink


class ProtocolTests(unittest.TestCase):
    def test_event_emitter_assigns_run_id_sequence_and_records_events(self) -> None:
        sink = ListEventSink()
        recorded = []
        emitter = EventEmitter(session_id="session-1", sink=sink, recorder=recorded.append)

        session = emitter.emit("SessionStarted", {})
        run_id = emitter.start_run()
        first = emitter.emit("UserMessage", {"content": "hello"})
        second = emitter.emit("SessionFinished", {"content": "done"})

        self.assertEqual(first.session_id, "session-1")
        self.assertEqual(first.run_id, run_id)
        self.assertEqual(session.seq, 1)
        self.assertEqual(first.seq, 2)
        self.assertEqual(second.seq, 3)
        self.assertEqual(sink.events, [session, first, second])
        self.assertEqual(recorded, [session, first, second])
        self.assertEqual(first.to_dict()["payload"], {"content": "hello"})

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


if __name__ == "__main__":
    unittest.main()

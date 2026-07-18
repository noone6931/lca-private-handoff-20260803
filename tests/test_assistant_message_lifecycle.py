from __future__ import annotations

import unittest

from local_agent.protocol.events import EventEmitter
from local_agent.protocol.events import ListEventSink
from local_agent.runtime.assistant_message import AssistantMessageLifecycle
from local_agent.runtime.run_output import RunOutputLifecycle


class AssistantMessageLifecycleTests(unittest.TestCase):
    def test_delta_and_final_share_identity_without_exposing_arguments(self) -> None:
        sink = ListEventSink()
        events = EventEmitter(session_id="s1", sink=sink)
        events.begin_command("c1")
        events.start_run()
        lifecycle = AssistantMessageLifecycle(
            events,
            provider="openai-compatible",
            stream_enabled=True,
            message_id_factory=lambda: "m1",
        )

        callback = lifecycle.delta_callback()
        assert callback is not None
        callback("hello", 0)
        finalized = lifecycle.finalize(
            {
                "role": "assistant",
                "content": "hello",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"secret"}'},
                    }
                ],
            },
            finish_reason="tool_calls",
        )

        self.assertEqual(finalized.message_id, "m1")
        self.assertEqual(finalized.content, "hello")
        self.assertEqual([event.type for event in sink.events], ["AssistantDelta", "AssistantMessage"])
        self.assertEqual({event.payload["message_id"] for event in sink.events}, {"m1"})
        self.assertNotIn("arguments", repr(sink.events[-1].payload))
        self.assertEqual(sink.events[-1].payload["finish_reason"], "tool_calls")
        self.assertTrue(sink.events[-1].payload["authoritative"])

    def test_finalization_is_exactly_once_and_returned_message_is_isolated(self) -> None:
        sink = ListEventSink()
        events = EventEmitter(session_id="s1", sink=sink)
        lifecycle = AssistantMessageLifecycle(
            events,
            provider="bailian",
            stream_enabled=False,
            message_id_factory=lambda: "m2",
        )
        source = {"role": "assistant", "content": "final"}

        finalized = lifecycle.finalize(source, finish_reason="stop")
        source["content"] = "mutated"
        projected = finalized.model_message()
        projected["content"] = "also mutated"

        self.assertEqual(finalized.content, "final")
        self.assertEqual(finalized.model_message()["content"], "final")
        self.assertIsNone(lifecycle.delta_callback())
        with self.assertRaisesRegex(RuntimeError, "already closed"):
            lifecycle.finalize({"content": "duplicate"}, finish_reason="stop")
        self.assertEqual([event.type for event in sink.events], ["AssistantMessage"])

    def test_abort_closes_provisional_message_without_leaking_failure_detail(self) -> None:
        sink = ListEventSink()
        events = EventEmitter(session_id="s1", sink=sink)
        lifecycle = AssistantMessageLifecycle(
            events,
            provider="bailian",
            stream_enabled=True,
            message_id_factory=lambda: "m3",
        )

        callback = lifecycle.delta_callback()
        assert callback is not None
        callback("draft", 0)
        lifecycle.abort("provider_error")
        callback("late", 1)

        self.assertEqual([event.type for event in sink.events], ["AssistantDelta", "AssistantMessageAborted"])
        self.assertEqual(
            sink.events[-1].payload,
            {"message_id": "m3", "reason": "provider_error", "origin": "provider", "status": "aborted"},
        )
        with self.assertRaisesRegex(RuntimeError, "already closed"):
            lifecycle.abort("duplicate")

    def test_run_output_correlates_provider_and_runtime_delivery(self) -> None:
        sink = ListEventSink()
        events = EventEmitter(session_id="s1", sink=sink)
        output = RunOutputLifecycle()
        lifecycle = AssistantMessageLifecycle(
            events,
            provider="bailian",
            stream_enabled=False,
            message_id_factory=lambda: "m4",
            observer=output,
        )
        lifecycle.finalize({"content": "answer"}, finish_reason="stop")

        output.emit(
            events,
            content="answer\n\nVerification: tests passed.",
            reason="final",
            run_summary={"termination_reason": "final"},
        )

        finished = sink.events[-1]
        self.assertEqual(finished.type, "TurnFinished")
        self.assertEqual(finished.payload["final_message_id"], "m4")
        self.assertEqual(finished.payload["origin"], "runtime")
        self.assertEqual(finished.payload["output_kind"], "runtime_augmented")
        with self.assertRaisesRegex(RuntimeError, "already finished"):
            output.finish("duplicate")

    def test_run_output_can_abort_an_attached_message_at_command_boundary(self) -> None:
        sink = ListEventSink()
        events = EventEmitter(session_id="s1", sink=sink)
        output = RunOutputLifecycle()
        lifecycle = AssistantMessageLifecycle(
            events,
            provider="bailian",
            stream_enabled=True,
            message_id_factory=lambda: "m5",
            observer=output,
        )
        callback = lifecycle.delta_callback()
        assert callback is not None
        callback("partial", 0)

        self.assertTrue(output.abort_active("command_error"))
        self.assertFalse(output.abort_active("duplicate"))
        output.emit(events, content="Stopped after runtime error.", reason="command_error", run_summary={})

        self.assertEqual(
            [event.type for event in sink.events],
            ["AssistantDelta", "AssistantMessageAborted", "TurnFinished"],
        )
        self.assertEqual(sink.events[-1].payload["output_kind"], "runtime_only")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from local_agent.frontends.tui.mailbox import TuiMailbox
from local_agent.frontends.tui.messages import TuiEvent
from local_agent.frontends.tui.model import TuiEventSink
from local_agent.frontends.tui.model import TuiProjector
from local_agent.frontends.tui.model import TranscriptEntry
from local_agent.frontends.tui.model import TuiState
from local_agent.protocol.events import AgentEvent


class TuiModelTests(unittest.TestCase):
    def test_local_transcript_ids_remain_unique_after_history_is_bounded(self) -> None:
        projector = TuiProjector(
            TuiState(transcript=tuple(TranscriptEntry(f"local:{index}", "system", "old") for index in range(512)))
        )

        first = projector.append_local("system", "same")
        second = projector.append_local("system", "same")

        self.assertEqual(first.transcript[-1].entry_id, "local:512")
        self.assertEqual(second.transcript[-1].entry_id, "local:513")

    def test_sink_removes_tool_arguments_before_cross_thread_queue(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        sink = TuiEventSink(mailbox)

        sink.emit(_event("ToolStarted", {"name": "shell", "arguments": {"secret": "do-not-leak"}}))

        projected = mailbox.get(timeout=0)
        self.assertIsInstance(projected, TuiEvent)
        assert isinstance(projected, TuiEvent)
        self.assertEqual(projected.get("name"), "shell")
        self.assertNotIn("do-not-leak", repr(projected))
        self.assertNotIn("arguments", repr(projected))

    def test_control_sequences_are_removed_at_projection_ingress(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        sink = TuiEventSink(mailbox)

        sink.emit(_event("ErrorEvent", {"message": "safe\x1b[31m\x9bunsafe"}))

        projected = mailbox.get(timeout=0)
        self.assertIsInstance(projected, TuiEvent)
        assert isinstance(projected, TuiEvent)
        self.assertEqual(projected.get("message"), "safe[31munsafe")

    def test_hidden_tools_are_filtered_before_crossing_threads(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        sink = TuiEventSink(mailbox, show_tools=False)

        sink.emit(_event("ToolStarted", {"name": "shell", "arguments": {"secret": "hidden"}}))

        self.assertIsNone(mailbox.get(timeout=0))

    def test_streaming_final_is_not_duplicated(self) -> None:
        projector = TuiProjector()
        projector.apply(_tui_event("TurnStarted"))
        projector.apply(
            _tui_event(
                "AssistantDelta",
                fields=(("message_id", "m1"), ("delta", "hello"), ("delta_index", 0), ("delta_span", 1)),
            )
        )
        projector.apply(
            _tui_event(
                "AssistantMessage",
                seq=3,
                fields=(("message_id", "m1"), ("content", "hello"), ("authoritative", True)),
            )
        )
        state = projector.apply(
            _tui_event("TurnFinished", seq=4, fields=(("content", "hello"), ("reason", "final")))
        )

        self.assertEqual([entry.text for entry in state.transcript], ["hello"])
        self.assertFalse(state.transcript[0].provisional)
        self.assertFalse(state.transcript[0].authoritative)
        self.assertFalse(state.busy)

    def test_session_metadata_projects_without_runtime_access(self) -> None:
        projector = TuiProjector()

        state = projector.apply(
            _tui_event(
                "SessionStarted",
                fields=(("provider", "bailian"), ("workspace", "/tmp/project"), ("continued", True)),
            )
        )

        self.assertEqual(state.provider, "bailian")
        self.assertEqual(state.workspace, "/tmp/project")
        self.assertTrue(state.continued)
        self.assertEqual(state.status, "resumed")

    def test_different_final_is_marked_authoritative(self) -> None:
        projector = TuiProjector()
        projector.apply(_tui_event("TurnStarted"))
        projector.apply(
            _tui_event(
                "AssistantDelta",
                fields=(("message_id", "m1"), ("delta", "draft"), ("delta_index", 0), ("delta_span", 1)),
            )
        )
        state = projector.apply(
            _tui_event(
                "AssistantMessage",
                seq=3,
                fields=(("message_id", "m1"), ("content", "safe final"), ("authoritative", True)),
            )
        )

        self.assertEqual([entry.text for entry in state.transcript], ["safe final"])
        self.assertTrue(state.transcript[-1].authoritative)

    def test_candidate_messages_replace_each_other_until_turn_finished(self) -> None:
        projector = TuiProjector()
        projector.apply(_tui_event("TurnStarted"))
        first = projector.apply(
            _tui_event(
                "AssistantMessage",
                seq=2,
                fields=(("message_id", "m1"), ("content", "yes"), ("phase", "candidate")),
            )
        )
        second = projector.apply(
            _tui_event(
                "AssistantMessage",
                seq=3,
                fields=(("message_id", "m2"), ("content", "checking"), ("phase", "tool_call")),
            )
        )
        third = projector.apply(
            _tui_event(
                "AssistantMessage",
                seq=4,
                fields=(("message_id", "m3"), ("content", "no"), ("phase", "candidate")),
            )
        )

        self.assertEqual([(entry.entry_id, entry.text) for entry in first.transcript], [("m1", "yes")])
        self.assertTrue(first.transcript[0].provisional)
        self.assertEqual([(entry.entry_id, entry.text) for entry in second.transcript], [("m2", "checking")])
        self.assertTrue(second.transcript[0].provisional)
        self.assertEqual([(entry.entry_id, entry.text) for entry in third.transcript], [("m3", "no")])
        self.assertTrue(third.transcript[0].provisional)

        finished = projector.apply(
            _tui_event(
                "TurnFinished",
                seq=5,
                fields=(
                    ("content", "no"),
                    ("reason", "final"),
                    ("final_message_id", "m3"),
                    ("origin", "provider"),
                ),
            )
        )

        self.assertEqual([(entry.entry_id, entry.text) for entry in finished.transcript], [("m3", "no")])
        self.assertFalse(finished.transcript[0].provisional)

    def test_turn_finished_discards_non_final_provisional_candidate(self) -> None:
        projector = TuiProjector()
        projector.apply(_tui_event("TurnStarted"))
        projector.apply(
            _tui_event(
                "AssistantMessage",
                seq=2,
                fields=(("message_id", "m1"), ("content", "provider draft"), ("phase", "candidate")),
            )
        )

        state = projector.apply(
            _tui_event(
                "TurnFinished",
                seq=3,
                fields=(
                    ("content", "runtime final"),
                    ("reason", "final"),
                    ("final_message_id", "runtime-message"),
                    ("origin", "runtime"),
                ),
            )
        )

        self.assertEqual([(entry.entry_id, entry.text) for entry in state.transcript], [("runtime-message", "runtime final")])
        self.assertTrue(state.transcript[0].authoritative)

    def test_aborted_message_closes_provisional_before_runtime_delivery(self) -> None:
        projector = TuiProjector()
        projector.apply(_tui_event("TurnStarted"))
        projector.apply(
            _tui_event(
                "AssistantDelta",
                seq=2,
                fields=(("message_id", "m1"), ("delta", "draft"), ("delta_index", 0), ("delta_span", 1)),
            )
        )
        state = projector.apply(
            _tui_event("AssistantMessageAborted", seq=3, fields=(("message_id", "m1"), ("reason", "provider_error")))
        )

        self.assertEqual(state.transcript, ())
        state = projector.apply(
            _tui_event("TurnFinished", seq=4, fields=(("content", "Provider request failed."), ("reason", "provider_error")))
        )
        self.assertEqual([entry.text for entry in state.transcript], ["Provider request failed."])

    def test_turn_finished_preserves_a_distinct_runtime_delivery(self) -> None:
        projector = TuiProjector()
        projector.apply(_tui_event("TurnStarted"))
        projector.apply(
            _tui_event(
                "AssistantMessage",
                seq=2,
                fields=(("message_id", "m1"), ("content", "provider final"), ("authoritative", True)),
            )
        )

        state = projector.apply(
            _tui_event(
                "TurnFinished",
                seq=3,
                fields=(
                    ("content", "runtime wrapper"),
                    ("reason", "final"),
                    ("final_message_id", "m1"),
                    ("origin", "runtime"),
                    ("output_kind", "runtime_replaced"),
                ),
            )
        )

        self.assertEqual([entry.text for entry in state.transcript], ["runtime wrapper"])
        self.assertTrue(state.transcript[-1].authoritative)

    def test_mailbox_preserves_assistant_completion_under_backpressure(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        for seq in range(8):
            mailbox.put(_tui_event("TurnStarted", seq=seq))

        accepted = mailbox.put(
            _tui_event("AssistantMessage", seq=9, fields=(("message_id", "m1"), ("content", "done")))
        )

        self.assertTrue(accepted)
        self.assertIn("AssistantMessage", [item.type for item in mailbox.drain() if isinstance(item, TuiEvent)])

    def test_late_delta_and_duplicate_terminal_do_not_reopen_closed_turn(self) -> None:
        projector = TuiProjector()
        projector.apply(_tui_event("TurnStarted", seq=1))
        projector.apply(
            _tui_event(
                "AssistantDelta",
                seq=2,
                fields=(("message_id", "m1"), ("delta", "done"), ("delta_index", 0), ("delta_span", 1)),
            )
        )
        projector.apply(
            _tui_event(
                "AssistantMessage",
                seq=3,
                fields=(("message_id", "m1"), ("content", "done"), ("authoritative", True)),
            )
        )
        projector.apply(_tui_event("TurnFinished", seq=4, fields=(("content", "done"), ("reason", "final"))))

        projector.apply(
            _tui_event(
                "AssistantDelta",
                seq=5,
                fields=(("message_id", "m1"), ("delta", "late"), ("delta_index", 1), ("delta_span", 1)),
            )
        )
        state = projector.apply(
            _tui_event("TurnFinished", seq=6, fields=(("content", "duplicate"), ("reason", "final")))
        )

        self.assertEqual([entry.text for entry in state.transcript], ["done"])

    def test_mailbox_coalesces_contiguous_deltas(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        mailbox.put(
            _tui_event(
                "AssistantDelta",
                fields=(("message_id", "m1"), ("delta", "a"), ("delta_index", 0), ("delta_span", 1)),
            )
        )
        mailbox.put(
            _tui_event(
                "AssistantDelta",
                seq=2,
                fields=(("message_id", "m1"), ("delta", "b"), ("delta_index", 1), ("delta_span", 1)),
            )
        )

        event = mailbox.get(timeout=0)
        self.assertIsInstance(event, TuiEvent)
        assert isinstance(event, TuiEvent)
        self.assertEqual(event.get("delta"), "ab")
        self.assertEqual(event.get("delta_span"), 2)
        self.assertIsNone(mailbox.get(timeout=0))

    def test_mailbox_evicts_low_priority_event_for_terminal_lifecycle(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        for seq in range(8):
            event_type = "ContextUpdated" if seq == 0 else "TurnStarted"
            mailbox.put(_tui_event(event_type, seq=seq))

        accepted = mailbox.put(_tui_event("TurnFinished", seq=9))
        drained = mailbox.drain()

        self.assertTrue(accepted)
        self.assertEqual(len(drained), 8)
        self.assertIn("TurnFinished", [message.type for message in drained if isinstance(message, TuiEvent)])
        self.assertEqual(mailbox.dropped_count, 1)

    def test_tool_lifecycle_is_one_row_with_safe_error_preview(self) -> None:
        projector = TuiProjector()
        projector.apply(_tui_event("ToolStarted", seq=1, fields=(("name", "shell"),)))
        projector.apply(
            _tui_event("ToolFailed", seq=2, fields=(("name", "shell"), ("detail", "12 chars")))
        )
        state = projector.apply(
            _tui_event("ToolOutput", seq=3, fields=(("name", "shell"), ("detail", "denied")))
        )

        self.assertEqual(len(state.tools), 1)
        self.assertEqual(state.tools[0].status, "failed")
        self.assertEqual(state.tools[0].detail, "denied")

    def test_tool_error_preview_is_single_line_at_projection_ingress(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        sink = TuiEventSink(mailbox)

        sink.emit(
            _event(
                "ToolOutput",
                {
                    "name": "run_tests",
                    "is_error": True,
                    "content_preview": "\n[stderr]\r\nboom",
                },
            )
        )

        projected = mailbox.get(timeout=0)
        self.assertIsInstance(projected, TuiEvent)
        assert isinstance(projected, TuiEvent)
        self.assertEqual(projected.get("detail"), " [stderr] boom")


def _event(event_type: str, payload: dict) -> AgentEvent:
    return AgentEvent("e1", "s1", "r1", 1, 0.0, event_type, payload, "c1")


def _tui_event(
    event_type: str,
    *,
    seq: int = 1,
    fields: tuple[tuple[str, str | int | float | bool | None], ...] = (),
) -> TuiEvent:
    return TuiEvent(event_type, seq, "s1", "r1", "c1", fields)


if __name__ == "__main__":
    unittest.main()

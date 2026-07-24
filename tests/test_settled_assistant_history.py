from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_agent.session.assistant_history import ASSISTANT_SETTLEMENT_EVENT
from local_agent.session.assistant_history import AssistantHistoryReplay
from local_agent.session.assistant_history import AssistantSettlement
from local_agent.session.assistant_history import MESSAGE_ID_KEY, ORIGIN_KEY, OUTPUT_KIND_KEY
from local_agent.session.assistant_history import PHASE_KEY, RUN_ID_KEY
from local_agent.session.assistant_history import SETTLED_DELIVERY_PHASE
from local_agent.session.assistant_history import TOOL_CALL_PHASE, UNSETTLED_CANDIDATE_PHASE
from local_agent.session.assistant_history import annotate_provider_message
from local_agent.session.assistant_history import checkpoint_messages
from local_agent.session.assistant_history import has_unsettled_candidate
from local_agent.session.assistant_history import messages_for_active_run
from local_agent.session.assistant_history import project_live_settlement
from local_agent.session.jsonl_store import JsonlSessionStore


def _candidate(run_id: str, message_id: str, content: str) -> dict:
    return annotate_provider_message(
        {"role": "assistant", "content": content},
        message_id=message_id,
        run_id=run_id,
    )


def _tool_call(run_id: str, message_id: str, call_id: str = "call-1") -> dict:
    return annotate_provider_message(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
                }
            ],
        },
        message_id=message_id,
        run_id=run_id,
    )


class SettledAssistantHistoryTests(unittest.TestCase):
    def test_candidate_rewrite_settles_only_final_provider_message(self) -> None:
        messages = [
            {"role": "user", "content": "request"},
            _candidate("run-1", "m1", "rejected draft"),
            {"role": "user", "content": "runtime rewrite instruction"},
            _candidate("run-1", "m2", "accepted final"),
        ]
        settlement = AssistantSettlement.create(
            run_id="run-1",
            final_message_id="m2",
            origin="provider",
            output_kind="provider_message",
            content="accepted final",
        )

        projected = project_live_settlement(messages, settlement)

        assistant = [message for message in projected if message.get("role") == "assistant"]
        self.assertEqual([message["content"] for message in assistant], ["accepted final"])
        self.assertEqual(assistant[0][MESSAGE_ID_KEY], "m2")
        self.assertEqual(assistant[0][PHASE_KEY], SETTLED_DELIVERY_PHASE)

    def test_runtime_output_kinds_restore_only_actual_terminal_content(self) -> None:
        cases = (
            ("runtime_augmented", "draft\n\nruntime report", "m1"),
            ("runtime_replaced", "safe typed terminal", "m1"),
            ("runtime_only", "provider failed before a complete message", None),
        )
        for output_kind, content, final_message_id in cases:
            with self.subTest(output_kind=output_kind):
                messages = [{"role": "user", "content": "request"}]
                if final_message_id is not None:
                    messages.append(_candidate("run-1", final_message_id, "draft"))
                settlement = AssistantSettlement.create(
                    run_id="run-1",
                    final_message_id=final_message_id,
                    origin="runtime",
                    output_kind=output_kind,
                    content=content,
                )

                projected = project_live_settlement(messages, settlement)
                assistant = [message for message in projected if message.get("role") == "assistant"]

                self.assertEqual([message["content"] for message in assistant], [content])
                self.assertEqual(assistant[0][ORIGIN_KEY], "runtime")
                self.assertEqual(assistant[0][OUTPUT_KIND_KEY], output_kind)
                self.assertEqual(assistant[0][PHASE_KEY], SETTLED_DELIVERY_PHASE)

    def test_tool_call_and_result_pair_survive_candidate_settlement(self) -> None:
        tool_call = _tool_call("run-1", "m-tool")
        messages = [
            {"role": "user", "content": "request"},
            tool_call,
            {"role": "tool", "tool_call_id": "call-1", "content": "README"},
            _candidate("run-1", "m-final", "done"),
        ]

        projected = project_live_settlement(
            messages,
            AssistantSettlement.create(
                run_id="run-1",
                final_message_id="m-final",
                origin="provider",
                output_kind="provider_message",
                content="done",
            ),
        )

        self.assertIn(tool_call, projected)
        self.assertTrue(any(message.get("tool_call_id") == "call-1" for message in projected))
        self.assertEqual(projected[-1]["content"], "done")

    def test_active_history_keeps_only_current_draft_and_protocol_messages(self) -> None:
        settled = project_live_settlement(
            [_candidate("old-run", "old-final", "settled")],
            AssistantSettlement.create(
                run_id="old-run",
                final_message_id="old-final",
                origin="provider",
                output_kind="provider_message",
                content="settled",
            ),
        )[0]
        current = _candidate("current-run", "current", "current draft")
        prior = _candidate("prior-run", "prior", "rejected prior draft")
        tool_call = _tool_call("current-run", "tool")

        projected = messages_for_active_run(
            [settled, prior, current, tool_call],
            active_run_id="current-run",
        )

        self.assertEqual([message.get("content") for message in projected], ["settled", "current draft", None])
        self.assertTrue(has_unsettled_candidate(projected, run_id="current-run"))
        self.assertFalse(has_unsettled_candidate(projected, run_id="prior-run"))

    def test_replay_missing_malformed_and_duplicate_settlement_fail_closed(self) -> None:
        candidate = _candidate("run-1", "m1", "draft")
        valid = AssistantSettlement.create(
            run_id="run-1",
            final_message_id="m1",
            origin="provider",
            output_kind="provider_message",
            content="draft",
        ).to_payload()
        malformed = {**valid, "content_sha256": "wrong"}
        for settlements in ((), (malformed,), (valid, valid)):
            with self.subTest(settlements=len(settlements), malformed=bool(settlements and settlements[0] is malformed)):
                replay = AssistantHistoryReplay()
                replay.append_user({"content": "request"})
                replay.append_assistant(candidate)
                for payload in settlements:
                    replay.append_settlement(payload)

                self.assertEqual(replay.messages(), [{"role": "user", "content": "request"}])

    def test_checkpoint_requires_typed_settled_history(self) -> None:
        settled = project_live_settlement(
            [_candidate("run-1", "m1", "final")],
            AssistantSettlement.create(
                run_id="run-1",
                final_message_id="m1",
                origin="provider",
                output_kind="provider_message",
                content="final",
            ),
        )
        self.assertIsNone(checkpoint_messages([{"role": "assistant", "content": "legacy candidate"}]))
        self.assertIsNone(checkpoint_messages([_candidate("run-2", "m2", "draft")]))
        self.assertIsNone(checkpoint_messages([{"role": "unknown", "content": "invalid"}]))
        self.assertIsNone(checkpoint_messages(["not a message"]))
        self.assertEqual(checkpoint_messages(settled), settled)

        replay = AssistantHistoryReplay()
        self.assertFalse(replay.install_checkpoint({"version": 1, "messages": settled}))
        self.assertTrue(replay.install_checkpoint({"version": 2, "messages": settled}))
        self.assertEqual(replay.messages(), settled)

    def test_checkpoint_rejects_malformed_settlement_semantics(self) -> None:
        provider = project_live_settlement(
            [_candidate("provider-run", "m1", "provider final")],
            AssistantSettlement.create(
                run_id="provider-run",
                final_message_id="m1",
                origin="provider",
                output_kind="provider_message",
                content="provider final",
            ),
        )[0]
        runtime_augmented = project_live_settlement(
            [_candidate("augmented-run", "m2", "draft")],
            AssistantSettlement.create(
                run_id="augmented-run",
                final_message_id="m2",
                origin="runtime",
                output_kind="runtime_augmented",
                content="draft\n\nruntime report",
            ),
        )[0]
        runtime_only = project_live_settlement(
            [],
            AssistantSettlement.create(
                run_id="runtime-run",
                final_message_id=None,
                origin="runtime",
                output_kind="runtime_only",
                content="runtime final",
            ),
        )[0]
        malformed = (
            {**provider, ORIGIN_KEY: "runtime"},
            {**runtime_augmented, ORIGIN_KEY: "provider"},
            {**runtime_only, MESSAGE_ID_KEY: "provider-message-id"},
            {**provider, MESSAGE_ID_KEY: "runtime:provider-run"},
            {key: value for key, value in provider.items() if key != MESSAGE_ID_KEY},
            {key: value for key, value in provider.items() if key != RUN_ID_KEY},
        )
        for message in malformed:
            with self.subTest(message=message):
                replay = AssistantHistoryReplay()

                self.assertIsNone(checkpoint_messages([message]))
                self.assertFalse(replay.install_checkpoint({"version": 2, "messages": [message]}))
                self.assertEqual(replay.messages(), [])

        replay = AssistantHistoryReplay()
        self.assertFalse(replay.install_checkpoint({"version": 2, "messages": [provider, provider]}))

    def test_checkpoint_settlement_identity_rejects_later_duplicates(self) -> None:
        settlements = (
            AssistantSettlement.create(
                run_id="provider-run",
                final_message_id="m1",
                origin="provider",
                output_kind="provider_message",
                content="provider final",
            ),
            AssistantSettlement.create(
                run_id="runtime-run",
                final_message_id=None,
                origin="runtime",
                output_kind="runtime_only",
                content="runtime final",
            ),
        )
        for settlement in settlements:
            candidates = (
                [_candidate(settlement.run_id, "m1", settlement.content)]
                if settlement.final_message_id is not None
                else []
            )
            settled = project_live_settlement(candidates, settlement)
            for duplicate in (
                settlement.to_payload(),
                {**settlement.to_payload(), "content_sha256": "malformed"},
            ):
                with self.subTest(output_kind=settlement.output_kind, malformed=duplicate["content_sha256"] == "malformed"):
                    replay = AssistantHistoryReplay()
                    self.assertTrue(replay.install_checkpoint({"version": 2, "messages": settled}))

                    replay.append_settlement(duplicate)

                    self.assertEqual(replay.messages(), [])

            replay = AssistantHistoryReplay()
            self.assertFalse(replay.install_checkpoint({"version": 2, "messages": settled + settled}))

    def test_jsonl_resume_projects_all_output_kinds_without_candidate_authority(self) -> None:
        cases = (
            ("provider_message", "provider", "draft", "draft", "m1"),
            ("runtime_augmented", "runtime", "draft", "draft\n\nreport", "m1"),
            ("runtime_replaced", "runtime", "draft", "safe terminal", "m1"),
            ("runtime_only", "runtime", None, "provider error terminal", None),
        )
        for output_kind, origin, candidate_content, final_content, final_message_id in cases:
            with self.subTest(output_kind=output_kind), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp).resolve()
                store = JsonlSessionStore(workspace)
                store.append("user", {"content": "request"})
                if candidate_content is not None:
                    store.append("assistant", _candidate("run-1", "m1", candidate_content))
                store.append(
                    ASSISTANT_SETTLEMENT_EVENT,
                    AssistantSettlement.create(
                        run_id="run-1",
                        final_message_id=final_message_id,
                        origin=origin,
                        output_kind=output_kind,
                        content=final_content,
                    ).to_payload(),
                )

                assistant = [
                    message for message in store.load_messages() if message.get("role") == "assistant"
                ]

                self.assertEqual([message["content"] for message in assistant], [final_content])
                self.assertEqual(assistant[0][OUTPUT_KIND_KEY], output_kind)
                self.assertEqual(assistant[0][ORIGIN_KEY], origin)

    def test_session_records_one_settlement_and_reopens_only_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            store = JsonlSessionStore(workspace)
            messages = [
                {"role": "user", "content": "request"},
                _candidate("run-1", "m0", "first rejected"),
                _candidate("run-1", "m1", "rejected"),
                _candidate("run-1", "m2", "final"),
            ]
            store.append("user", {"content": "request"})
            store.append("assistant", messages[1])
            store.append("assistant", messages[2])
            store.append("assistant", messages[3])
            store.record_assistant_settlement(
                messages,
                AssistantSettlement.create(
                    run_id="run-1",
                    final_message_id="m2",
                    origin="provider",
                    output_kind="provider_message",
                    content="final",
                ),
            )

            records = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
            reopened = JsonlSessionStore(workspace, session_id=store.session_id)
            reopened_messages = reopened.load_messages()

        self.assertEqual(sum(record["event"] == ASSISTANT_SETTLEMENT_EVENT for record in records), 1)
        self.assertEqual([message.get("content") for message in messages if message.get("role") == "assistant"], ["final"])
        self.assertEqual(
            [message.get("content") for message in reopened_messages if message.get("role") == "assistant"],
            ["final"],
        )
        self.assertEqual(messages[-1][PHASE_KEY], SETTLED_DELIVERY_PHASE)
        self.assertEqual(messages[-1][RUN_ID_KEY], "run-1")
        self.assertEqual(messages[-1][MESSAGE_ID_KEY], "m2")

    def test_live_correlation_failure_records_no_settlement_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlSessionStore(Path(tmp).resolve())
            messages = [_candidate("run-1", "m1", "draft")]
            settlement = AssistantSettlement.create(
                run_id="run-1",
                final_message_id="missing",
                origin="runtime",
                output_kind="runtime_replaced",
                content="safe terminal",
            )

            recorded = store.record_assistant_settlement(messages, settlement)

            self.assertFalse(recorded)
            self.assertEqual(messages[0]["content"], "draft")
            persisted = store.path.read_text(encoding="utf-8") if store.path.exists() else ""
            self.assertNotIn(ASSISTANT_SETTLEMENT_EVENT, persisted)


if __name__ == "__main__":
    unittest.main()

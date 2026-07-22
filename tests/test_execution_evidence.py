from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.agent import AgentRuntime
from local_agent.cancellation import RunCancelled
from local_agent.config import AgentConfig
from local_agent.session.execution_evidence import INCONCLUSIVE_ATTRIBUTION_LINE
from local_agent.session.execution_evidence import INCONCLUSIVE_REFERENCE
from local_agent.session.execution_evidence import PRIOR_EXECUTION_MESSAGE_KEY
from local_agent.session.execution_evidence import PRIOR_EXECUTION_STATE_KEY
from local_agent.session.execution_evidence import _execution_ref
from local_agent.session.execution_evidence import _fact_from_payload
from local_agent.session.execution_evidence import redact_prior_execution_transcript
from local_agent.session.execution_evidence import trusted_prior_execution_attributions
from local_agent.session.jsonl_store import JsonlSessionStore
from local_agent.steering.final_answer import FinalAnswerContext
from local_agent.steering.final_answer import ToolUsageEvidenceSteerer
from local_agent.steering.final_answer import phantom_tool_evidence_claims
from local_agent.task_contract import generate_requirement_contract
from local_agent.tools.base import ToolContext
from local_agent.tools.shell import run_shell, run_tests


class _ProspectiveReplayClient:
    calls: list[dict[str, object]] = []
    command = "SECRET_TOKEN=credential-value printf execution-output-marker"

    def __init__(self, _config: AgentConfig):
        pass

    def chat(self, messages, tools, **_kwargs):
        type(self).calls.append({"messages": messages, "tools": tools})
        call = len(type(self).calls)
        if call == 1:
            return _response(
                None,
                [
                    {
                        "id": "shell-call-1",
                        "type": "function",
                        "function": {"name": "shell", "arguments": json.dumps({"command": self.command})},
                    }
                ],
            )
        if call == 2:
            return _response("first run complete")
        return _response(_prior_fact_line(messages))


class _NonzeroReplayClient(_ProspectiveReplayClient):
    command = "exit 7"

    def chat(self, messages, tools, **_kwargs):
        type(self).calls.append({"messages": messages, "tools": tools})
        call = len(type(self).calls)
        if call == 1:
            return _response(
                None,
                [
                    {
                        "id": "shell-call-failed",
                        "type": "function",
                        "function": {"name": "shell", "arguments": json.dumps({"command": self.command})},
                    }
                ],
            )
        if call == 2:
            return _response("first run recorded a failure")
        return _response(_prior_fact_line(messages))


class _LegacyReplayClient:
    calls: list[dict[str, object]] = []

    def __init__(self, _config: AgentConfig):
        pass

    def chat(self, messages, tools, **_kwargs):
        type(self).calls.append({"messages": messages, "tools": tools})
        if len(type(self).calls) == 1:
            return _response("shell 未运行，因此 typed exit 0。")
        return _response(f"INCONCLUSIVE {INCONCLUSIVE_REFERENCE}; not current filesystem proof.")


class _NoPriorClient:
    calls: list[dict[str, object]] = []

    def __init__(self, _config: AgentConfig):
        pass

    def chat(self, messages, tools, **_kwargs):
        type(self).calls.append({"messages": messages, "tools": tools})
        return _response("No prospective execution fact is available.")


class _CancellationClient:
    def __init__(self, _config: AgentConfig):
        pass

    def chat(self, _messages, _tools, **_kwargs):
        return _response(
            None,
            [
                {
                    "id": "cancelled-shell-call",
                    "type": "function",
                    "function": {"name": "shell", "arguments": '{"command":"sleep 10"}'},
                }
            ],
        )


class ExecutionMetadataTests(unittest.TestCase):
    def test_shell_exposes_exact_launch_and_bounded_outcome_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            success = run_shell({"command": "printf exact-command"}, ToolContext(workspace, "yolo"))
            failed = run_shell({"command": "exit 9"}, ToolContext(workspace, "yolo"))

        execution = success.metadata["execution_v1"]
        self.assertEqual(execution["command"], {"text": "printf exact-command", "argv": None, "shell": True})
        self.assertEqual(execution["cwd"], str(workspace))
        self.assertEqual(execution["outcome"], {"kind": "exited", "exit_code": 0})
        self.assertEqual(execution["output"]["provenance"], "bounded_process_capture_v1")
        self.assertNotIn("output", execution["output"]["capture"]["stdout"])
        self.assertEqual(failed.metadata["execution_v1"]["outcome"], {"kind": "exited", "exit_code": 9})

    def test_non_exits_never_receive_an_exit_code(self) -> None:
        timeout = subprocess.TimeoutExpired("command", 1, output="partial")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            context = ToolContext(workspace, "yolo")
            with patch("local_agent.tools.shell._run_process", side_effect=timeout):
                timed_out = run_shell({"command": "sleep 10"}, context)
            with patch("local_agent.tools.shell._run_process", side_effect=OSError("spawn failed")):
                spawn_failed = run_shell({"command": "printf never"}, context)
            not_run = run_shell({"command": "rm -rf /"}, context)

        self.assertEqual(timed_out.metadata["execution_v1"]["outcome"], {"kind": "timed_out", "exit_code": None})
        self.assertEqual(spawn_failed.metadata["execution_v1"]["outcome"], {"kind": "spawn_failed", "exit_code": None})
        self.assertEqual(not_run.metadata["execution_v1"]["outcome"], {"kind": "not_run", "exit_code": None})

    def test_run_tests_preserves_existing_metadata_and_adds_same_typed_shape(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="ok\n", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            with patch("local_agent.tools.test_runner_policy.shutil.which", return_value="/usr/bin/true"):
                with patch("local_agent.tools.shell._run_process", return_value=completed):
                    result = run_tests({"command": "python3 -m unittest tests.test_sample"}, ToolContext(workspace, "yolo"))

        self.assertEqual(result.metadata["execution_status"], "succeeded")
        self.assertEqual(result.metadata["exit_code"], 0)
        self.assertEqual(result.metadata["execution_v1"]["outcome"], {"kind": "exited", "exit_code": 0})
        self.assertFalse(result.metadata["execution_v1"]["command"]["shell"])
        self.assertIsInstance(result.metadata["execution_v1"]["command"]["argv"], list)


class ProspectiveExecutionEvidenceTests(unittest.TestCase):
    def test_no_execution_history_redaction_is_byte_equivalent(self) -> None:
        messages = [
            {"role": "user", "content": "Keep this user prompt exactly."},
            {"role": "assistant", "content": "Keep this assistant response exactly."},
            {"role": "user", "content": "Keep this later prompt too."},
            {"role": "assistant", "content": "Keep this later response too."},
        ]
        before = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))

        redacted = redact_prior_execution_transcript(messages, execution_ids={"unmatched-persisted-call"})

        self.assertEqual(redacted, 0)
        self.assertEqual(json.dumps(messages, ensure_ascii=False, separators=(",", ":")), before)

    def test_no_prior_execution_produces_no_projection_or_steering(self) -> None:
        _NoPriorClient.calls = []
        with tempfile.TemporaryDirectory() as tmp, patch(
            "local_agent.agent.OpenAICompatibleClient", _NoPriorClient
        ):
            runtime = AgentRuntime(_config(Path(tmp)), show_tool_logs=False)
            runtime.run("Answer without running any tool.")
            result = runtime.run("Is there a prospective execution fact?")
            summary = dict(runtime._last_run_summary or {})

        self.assertEqual(result, "No prospective execution fact is available.")
        self.assertEqual(len(_NoPriorClient.calls), 2)
        for call in _NoPriorClient.calls:
            self.assertNotIn("<prior_execution_facts_v1>", json.dumps(call["messages"]))
        self.assertIn("No prospective execution fact is available.", json.dumps(_NoPriorClient.calls[1]["messages"]))
        self.assertEqual(summary["tool_calls"], 0)
        self.assertEqual(summary["tool_counts"], {})
        self.assertEqual(summary["steering_counts"].get("tool_usage_evidence", 0), 0)

    def test_fresh_two_run_replay_persists_fact_and_keeps_prior_out_of_current_observations(self) -> None:
        _ProspectiveReplayClient.calls = []
        with tempfile.TemporaryDirectory() as tmp, patch(
            "local_agent.agent.OpenAICompatibleClient", _ProspectiveReplayClient
        ):
            workspace = Path(tmp) / "workspace;current!?"
            workspace.mkdir()
            runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
            first = runtime.run("Run a harmless shell command and report its result.")
            payloads = runtime._session.load_event_payloads("execution_completed_v1")
            second = runtime.run("Cite the prospective prior execution fact without rerunning it.")
            second_summary = dict(runtime._last_run_summary or {})
            second_observed = list(runtime._run.tool_choice_results)
            second_names = list(runtime._run.tool_choice_tool_names)
            second_verification = runtime._run.verification_plan.snapshot()
            session_id = runtime._session.session_id
            reopened = AgentRuntime(_config(workspace), show_tool_logs=False, session_id=session_id)
            reopened_result = reopened.run("Cite the restored prior fact without rerunning it.")
            reopened_summary = dict(reopened._last_run_summary or {})
            reopened_observed = list(reopened._run.tool_choice_results)
            reopened_names = list(reopened._run.tool_choice_tool_names)
            reopened_verification = reopened._run.verification_plan.snapshot()

        self.assertTrue(first.startswith("first run complete\n\n[Runtime operation provenance]"))
        self.assertIn("patch_transaction_writes: none recorded", first)
        self.assertIn("passed shell exit=0", first)
        self.assertEqual(len(payloads), 1)
        fact = payloads[0]
        self.assertEqual(fact["tool"], "shell")
        self.assertEqual(fact["tool_call_id"], "shell-call-1")
        self.assertEqual(fact["outcome"], {"status": "exited", "exit_code": 0})
        self.assertEqual(fact["command"]["text"], _ProspectiveReplayClient.command)
        self.assertGreater(fact["event_seq"], 0)
        self.assertGreater(fact["event_time"], 0)
        self.assertEqual(fact["output"]["provenance"], "bounded_process_capture_v1")
        self.assertIn("[prior-execution:execv1_", second)
        self.assertEqual(second_summary["tool_calls"], 0)
        self.assertEqual(second_summary["tool_counts"], {})
        self.assertEqual(second_observed, [])
        self.assertEqual(second_names, [])
        self.assertNotIn(fact["execution_ref"], json.dumps(second_verification))
        self.assertIn(f"[prior-execution:{fact['execution_ref']}]", reopened_result)
        self.assertEqual(reopened_summary["tool_calls"], 0)
        self.assertEqual(reopened_summary["tool_counts"], {})
        self.assertEqual(reopened_observed, [])
        self.assertEqual(reopened_names, [])
        self.assertNotIn(fact["execution_ref"], json.dumps(reopened_verification))
        self.assertEqual(reopened._evidence_phase._execution_evidence.snapshot()["facts"], 1)
        projected_messages = json.dumps(_ProspectiveReplayClient.calls[-1]["messages"], ensure_ascii=False)
        self.assertNotIn("credential-value", projected_messages)
        self.assertNotIn("execution-output-marker", projected_messages)
        self.assertIn("command_digest=sha256:", projected_messages)
        self.assertIn("not rerun in current run, not current filesystem proof", projected_messages)
        projected_line = _prior_fact_line(_ProspectiveReplayClient.calls[-1]["messages"])
        self.assertEqual(re.split(r"[\n。！？!?;]+", projected_line), [projected_line])
        self.assertIn("cwd=", projected_line)
        self.assertIn("%3Bcurrent%21%3F", projected_line)

    def test_legacy_replay_is_inconclusive_and_cannot_backfill_from_transcript(self) -> None:
        _LegacyReplayClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            state_dir = workspace / ".state"
            store = JsonlSessionStore(workspace, state_dir=state_dir)
            store.append(
                "event_v1",
                _legacy_event(
                    store.session_id,
                    1,
                    "SessionStarted",
                    {"workspace": str(workspace)},
                ),
            )
            store.append("user", {"content": "Implement bubble sort"})
            store.append(
                "assistant",
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "legacy-shell",
                            "type": "function",
                            "function": {
                                "name": "shell",
                                "arguments": '{"command":"SECRET_TOKEN=legacy-credential pytest"}',
                            },
                        }
                    ],
                },
            )
            store.append(
                "event_v1",
                _legacy_event(
                    store.session_id,
                    2,
                    "ToolStarted",
                    {"name": "shell", "arguments": '{"command":"pytest"}'},
                ),
            )
            store.append(
                "event_v1",
                _legacy_event(
                    store.session_id,
                    3,
                    "ToolFinished",
                    {"name": "shell", "content_length": 24},
                ),
            )
            store.append(
                "tool_result",
                {"tool_call_id": "legacy-shell", "name": "shell", "content": "all passed\n[exit_code] 0"},
            )
            store.append(
                "event_v1",
                _legacy_event(
                    store.session_id,
                    4,
                    "ToolOutput",
                    {"name": "shell", "content_preview": "all passed\n[exit_code] 0"},
                ),
            )
            store.append("assistant", {"role": "assistant", "content": "Bubble sort verified with typed exit 0."})
            store.append("user", {"content": "Please confirm whether that historical command really ran."})
            store.append(
                "assistant",
                {
                    "role": "assistant",
                    "content": "Later confirmation command: cd /legacy/workspace && python bubble_sort.py",
                },
            )
            store.append("user", {"content": "What output did the confirmation show?"})
            store.append(
                "assistant",
                {
                    "role": "assistant",
                    "content": "Later confirmation output: sorted=[11, 12, 22, 25, 34, 64, 90] exit=0",
                },
            )
            store.append(
                "assistant",
                {
                    "role": "assistant",
                    "content": "Second assistant-only confirmation: the shell output proved success.",
                },
            )
            config = _config(workspace, state_dir=state_dir)
            with patch("local_agent.agent.OpenAICompatibleClient", _LegacyReplayClient):
                runtime = AgentRuntime(config, show_tool_logs=False, session_id=store.session_id)
                result = runtime.run("What prospective execution fact exists for the original run?")

        self.assertIn("INCONCLUSIVE", result)
        self.assertNotIn("未运行", result)
        self.assertNotIn("exit 0", result.lower())
        self.assertEqual(len(_LegacyReplayClient.calls), 2)
        first_context = json.dumps(_LegacyReplayClient.calls[0]["messages"], ensure_ascii=False)
        self.assertIn("state=INCONCLUSIVE", first_context)
        self.assertNotIn("legacy-credential", first_context)
        self.assertNotIn("all passed", first_context)
        self.assertNotIn("Bubble sort verified", first_context)
        self.assertNotIn("Later confirmation command", first_context)
        self.assertNotIn("Later confirmation output", first_context)
        self.assertNotIn("Second assistant-only confirmation", first_context)
        self.assertIn("Please confirm whether that historical command really ran.", first_context)
        self.assertIn("What output did the confirmation show?", first_context)
        self.assertEqual(runtime._session.load_event_payloads("execution_completed_v1"), [])
        self.assertEqual(runtime._last_run_summary["tool_calls"], 0)
        self.assertEqual(runtime._last_run_summary["tool_counts"], {})
        self.assertEqual(runtime._run.tool_choice_results, [])
        self.assertEqual(runtime._run.tool_choice_tool_names, [])
        self.assertNotIn("execv1_", json.dumps(runtime._run.verification_plan.snapshot()))

    def test_cancellation_never_reaches_the_execution_fact_join(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "local_agent.agent.OpenAICompatibleClient", _CancellationClient
        ), patch(
            "local_agent.tools.shell._run_process",
            side_effect=RunCancelled("cancelled during process execution"),
        ):
            runtime = AgentRuntime(_config(Path(tmp)), show_tool_logs=False)
            with self.assertRaises(KeyboardInterrupt):
                runtime.run("Run a shell command until cancelled.")

        self.assertEqual(runtime._session.load_event_payloads("execution_completed_v1"), [])
        self.assertEqual(runtime._evidence_phase._execution_evidence.snapshot()["facts"], 0)
        self.assertEqual(runtime._run.tool_choice_results, [])

    def test_timeout_spawn_failure_and_denial_project_their_typed_outcomes(self) -> None:
        cases = (
            ("timed_out", "sleep 10", subprocess.TimeoutExpired("sleep 10", 1)),
            ("spawn_failed", "printf never", OSError("spawn failed")),
            ("not_run", "rm -rf /", None),
        )
        for status, command, failure in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                _NonzeroReplayClient.calls = []
                _NonzeroReplayClient.command = command
                process_patch = (
                    patch("local_agent.tools.shell._run_process", side_effect=failure)
                    if failure is not None
                    else patch("local_agent.tools.shell._run_process")
                )
                with patch("local_agent.agent.OpenAICompatibleClient", _NonzeroReplayClient), process_patch:
                    runtime = AgentRuntime(_config(Path(tmp)), show_tool_logs=False)
                    runtime.run("Attempt the requested shell command.")
                    runtime.run("Report the prospective execution evidence state.")
                    payload = runtime._session.load_event_payloads("execution_completed_v1")[0]
                    block = _prior_block(_NonzeroReplayClient.calls[-1]["messages"])

                self.assertEqual(payload["outcome"], {"status": status, "exit_code": None})
                self.assertIn("state=ATTRIBUTED", block)
                self.assertIn(f"result_status={status}", block)
                self.assertNotIn(" exit=", block)

    def test_nonzero_exit_projects_exact_failure_without_supporting_success_paraphrase(self) -> None:
        _NonzeroReplayClient.calls = []
        with tempfile.TemporaryDirectory() as tmp, patch(
            "local_agent.agent.OpenAICompatibleClient", _NonzeroReplayClient
        ):
            root = Path(tmp).resolve()
            runtime = AgentRuntime(_config(root), show_tool_logs=False)
            runtime.run("Run a shell command that may fail.")
            runtime.run("Report only a prospective successful prior execution fact.")
            block = _prior_block(_NonzeroReplayClient.calls[-1]["messages"])
            payload = runtime._session.load_event_payloads("execution_completed_v1")[0]
            prior = trusted_prior_execution_attributions(_NonzeroReplayClient.calls[-1]["messages"])

        self.assertIn("state=ATTRIBUTED", block)
        self.assertIn("result_status=exited exit=7", block)
        self.assertNotIn("exit=0", block)
        self.assertEqual(payload["outcome"], {"status": "exited", "exit_code": 7})
        self.assertEqual(
            phantom_tool_evidence_claims(
                f"shell ran successfully [prior-execution:{payload['execution_ref']}]",
                [],
                prior,
            ),
            ("shell",),
        )

    def test_foreign_session_fact_does_not_create_projection_or_observation(self) -> None:
        _ProspectiveReplayClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            source, target = base / "source", base / "target"
            source.mkdir()
            target.mkdir()
            with patch("local_agent.agent.OpenAICompatibleClient", _ProspectiveReplayClient):
                source_runtime = AgentRuntime(_config(source), show_tool_logs=False)
                source_runtime.run("Run a harmless shell command.")
                foreign_fact = source_runtime._session.load_event_payloads("execution_completed_v1")[0]

            target_state = target / ".state"
            target_store = JsonlSessionStore(target, state_dir=target_state)
            target_store.append("execution_completed_v1", foreign_fact)
            _NoPriorClient.calls = []
            with patch("local_agent.agent.OpenAICompatibleClient", _NoPriorClient):
                target_runtime = AgentRuntime(
                    _config(target, state_dir=target_state),
                    show_tool_logs=False,
                    session_id=target_store.session_id,
                )
                target_runtime.run("Report any prospective fact.")

        self.assertNotIn("<prior_execution_facts_v1>", json.dumps(_NoPriorClient.calls[0]["messages"]))
        self.assertEqual(target_runtime._evidence_phase._execution_evidence.snapshot()["facts"], 0)
        self.assertEqual(target_runtime._run.tool_choice_results, [])
        self.assertEqual(target_runtime._run.tool_choice_tool_names, [])
        self.assertEqual(target_runtime._last_run_summary["tool_counts"], {})

    def test_malformed_current_session_facts_are_inconclusive_without_synthesis(self) -> None:
        _ProspectiveReplayClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            source = base / "source"
            source.mkdir()
            with patch("local_agent.agent.OpenAICompatibleClient", _ProspectiveReplayClient):
                source_runtime = AgentRuntime(_config(source), show_tool_logs=False)
                source_runtime.run("Run a harmless shell command.")
                valid = source_runtime._session.load_event_payloads("execution_completed_v1")[0]

            for missing in ("tool_call_id", "cwd", "exit_code"):
                with self.subTest(missing=missing):
                    target = base / missing
                    target.mkdir()
                    state_dir = target / ".state"
                    store = JsonlSessionStore(target, state_dir=state_dir)
                    malformed = json.loads(json.dumps(valid))
                    malformed["origin_session_id"] = store.session_id
                    if missing == "exit_code":
                        del malformed["outcome"]["exit_code"]
                    else:
                        del malformed[missing]
                    store.append("execution_completed_v1", malformed)
                    _LegacyReplayClient.calls = []
                    with patch("local_agent.agent.OpenAICompatibleClient", _LegacyReplayClient):
                        runtime = AgentRuntime(
                            _config(target, state_dir=state_dir),
                            show_tool_logs=False,
                            session_id=store.session_id,
                        )
                        result = runtime.run("Report the prospective fact state.")

                    self.assertIn("INCONCLUSIVE", result)
                    self.assertIn("state=INCONCLUSIVE", _prior_block(_LegacyReplayClient.calls[0]["messages"]))
                    self.assertEqual(runtime._evidence_phase._execution_evidence.snapshot()["facts"], 0)
                    self.assertEqual(runtime._session.load_event_payloads("execution_completed_v1"), [malformed])

    def test_well_shaped_fact_requires_exact_runtime_event_correlation_on_restore(self) -> None:
        _ProspectiveReplayClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            with patch("local_agent.agent.OpenAICompatibleClient", _ProspectiveReplayClient):
                source = AgentRuntime(_config(workspace), show_tool_logs=False)
                source.run("Run a harmless shell command.")
                template = source._session.load_event_payloads("execution_completed_v1")[0]

            for case in ("missing_events", "missing_output", "wrong_output", "wrong_context"):
                with self.subTest(case=case):
                    state_dir = workspace / f".state-{case}"
                    store = JsonlSessionStore(workspace, state_dir=state_dir)
                    fact = _synthetic_fact(template, store.session_id)
                    self.assertIsNotNone(_fact_from_payload(fact))
                    events = _synthetic_correlation_events(fact, case)
                    for event in events:
                        store.append("event_v1", event)
                    store.append("execution_completed_v1", fact)
                    _LegacyReplayClient.calls = []
                    with patch("local_agent.agent.OpenAICompatibleClient", _LegacyReplayClient):
                        runtime = AgentRuntime(
                            _config(workspace, state_dir=state_dir),
                            show_tool_logs=False,
                            session_id=store.session_id,
                        )
                        result = runtime.run("Report the prospective fact state.")

                    self.assertIn("INCONCLUSIVE", result)
                    self.assertIn("state=INCONCLUSIVE", _prior_block(_LegacyReplayClient.calls[0]["messages"]))
                    self.assertEqual(runtime._evidence_phase._execution_evidence.snapshot()["facts"], 0)
                    self.assertEqual(runtime._run.tool_choice_results, [])
                    self.assertEqual(runtime._run.tool_choice_tool_names, [])
                    self.assertNotIn(fact["execution_ref"], json.dumps(runtime._run.verification_plan.snapshot()))
                    self.assertEqual(runtime._last_run_summary["tool_counts"], {})

    def test_duplicate_reference_and_root_reset_invalidate_projection(self) -> None:
        _ProspectiveReplayClient.calls = []
        with tempfile.TemporaryDirectory() as tmp, patch(
            "local_agent.agent.OpenAICompatibleClient", _ProspectiveReplayClient
        ):
            base = Path(tmp).resolve()
            root, extra = base / "workspace", base / "extra"
            root.mkdir()
            extra.mkdir()
            runtime = AgentRuntime(_config(root), show_tool_logs=False)
            runtime.run("Run a harmless shell command and report its result.")
            runtime.add_workspace_root(str(extra))
            runtime.reset_workspace_roots()
            runtime._session.append(
                "execution_completed_v1",
                runtime._session.load_event_payloads("execution_completed_v1")[0],
            )
            session_id = runtime._session.session_id
            _LegacyReplayClient.calls = []
            reopened = AgentRuntime(
                _config(root), show_tool_logs=False, session_id=session_id
            )
            reopened._client = _LegacyReplayClient(_config(root))
            result = reopened.run("Report the prior execution evidence state.")

        self.assertIn("INCONCLUSIVE", result)
        block = _prior_block(_LegacyReplayClient.calls[0]["messages"])
        self.assertIn("state=INCONCLUSIVE", block)
        self.assertEqual(reopened._evidence_phase._execution_evidence.snapshot()["facts"], 0)

    def test_root_change_replaces_an_earlier_projected_attribution_with_inconclusive(self) -> None:
        _ProspectiveReplayClient.calls = []
        with tempfile.TemporaryDirectory() as tmp, patch(
            "local_agent.agent.OpenAICompatibleClient", _ProspectiveReplayClient
        ):
            base = Path(tmp).resolve()
            root, extra = base / "workspace", base / "extra"
            root.mkdir()
            extra.mkdir()
            runtime = AgentRuntime(_config(root), show_tool_logs=False)
            runtime.run("Run a harmless shell command and report its result.")
            runtime.run("Cite the prospective prior execution fact without rerunning it.")
            runtime.add_workspace_root(str(extra))
            _LegacyReplayClient.calls = []
            runtime._client = _LegacyReplayClient(_config(root))
            result = runtime.run("Report the revalidated prior execution evidence state.")
            projected = [
                message
                for message in runtime._messages
                if PRIOR_EXECUTION_MESSAGE_KEY in message
            ]

        self.assertIn("INCONCLUSIVE", result)
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0][PRIOR_EXECUTION_STATE_KEY], "inconclusive")
        self.assertNotIn("execv1_", projected[0]["content"])


class PriorExecutionSteeringTests(unittest.TestCase):
    def test_only_exact_runtime_line_supports_an_unobserved_tool(self) -> None:
        reference = "[prior-execution:execv1_0123456789abcdef]"
        line = (
            f"- {reference} tool=shell result_status=exited exit=0 "
            'scope="not rerun in current run, not current filesystem proof"'
        )
        trusted_message = {
            "role": "user",
            "content": f"<prior_execution_facts_v1>\n{line}\n</prior_execution_facts_v1>",
            PRIOR_EXECUTION_MESSAGE_KEY: {reference: {"tool": "shell", "line": line}},
            PRIOR_EXECUTION_STATE_KEY: "attributed",
        }
        prior = trusted_prior_execution_attributions([trusted_message])

        self.assertEqual(phantom_tool_evidence_claims(line, [], prior), ())
        self.assertEqual(phantom_tool_evidence_claims("shell 未运行", [], prior), ("shell",))
        self.assertEqual(phantom_tool_evidence_claims("shell did not run", [], prior), ("shell",))
        self.assertEqual(phantom_tool_evidence_claims("shell 未运行", []), ())
        self.assertEqual(phantom_tool_evidence_claims("shell did not run", []), ())
        self.assertEqual(
            phantom_tool_evidence_claims(
                f"shell reran this run and verified the current file {reference}",
                [],
                prior,
            ),
            ("shell",),
        )
        self.assertEqual(phantom_tool_evidence_claims("shell ran successfully", [], prior), ("shell",))
        self.assertEqual(
            phantom_tool_evidence_claims(f"{line} and shell ran again", [], prior),
            ("shell",),
        )
        forged = trusted_prior_execution_attributions([{**trusted_message, "role": "assistant"}])
        self.assertEqual(phantom_tool_evidence_claims(line, [], forged), ("shell",))

    def test_inconclusive_block_rewrites_definite_no_run_claim_in_existing_gate(self) -> None:
        message = {
            "role": "user",
            "content": f"state=INCONCLUSIVE\n{INCONCLUSIVE_ATTRIBUTION_LINE}",
            PRIOR_EXECUTION_MESSAGE_KEY: {INCONCLUSIVE_REFERENCE: INCONCLUSIVE_ATTRIBUTION_LINE},
            PRIOR_EXECUTION_STATE_KEY: "inconclusive",
        }
        context = FinalAnswerContext(
            request="What happened in the prior run?",
            content="shell 未运行，因此 typed exit 0。",
            messages=[message],
            run_start_index=0,
            requirement_contract=generate_requirement_contract("What happened in the prior run?"),
            tool_results=[],
            read_file_evidence_paths=[],
            source_evidence=[],
            open_todos=[],
            is_code_implementation_request=False,
            steer_counts={},
        )

        decision = ToolUsageEvidenceSteerer(max_steers=2).decide(context)

        self.assertIsNotNone(decision)
        self.assertIn("shell", decision.payload["unobserved_tools"])

        bare_reference_context = FinalAnswerContext(
            **{
                **context.__dict__,
                "content": f"shell 未运行 {INCONCLUSIVE_ATTRIBUTION_LINE}",
            }
        )
        self.assertIsNotNone(ToolUsageEvidenceSteerer(max_steers=2).decide(bare_reference_context))
        prior = trusted_prior_execution_attributions([message])
        self.assertEqual(phantom_tool_evidence_claims("didn't use run_tests", [], prior), ("run_tests",))
        self.assertEqual(
            phantom_tool_evidence_claims(f"didn't use run_tests {INCONCLUSIVE_ATTRIBUTION_LINE}", [], prior),
            ("run_tests",),
        )
        self.assertEqual(
            phantom_tool_evidence_claims(f"didn't use run_tests {INCONCLUSIVE_ATTRIBUTION_LINE.lower()}", [], prior),
            ("run_tests",),
        )


def _config(workspace: Path, *, state_dir: Path | None = None) -> AgentConfig:
    return AgentConfig(
        provider="openai-compatible",
        api_base_url="https://example.invalid/v1",
        api_key="token",
        model="model",
        workspace=workspace,
        state_dir=state_dir,
        max_steps=5,
        budget_seconds=None,
        approval_mode="yolo",
    )


def _response(content: str | None, tool_calls: list[dict[str, object]] | None = None):
    return type("Response", (), {"message": {"role": "assistant", "content": content, "tool_calls": tool_calls or []}})()


def _legacy_event(session_id: str, seq: int, event_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "event_id": f"legacy-event-{seq}",
        "session_id": session_id,
        "run_id": "legacy-run",
        "command_id": "legacy-command",
        "seq": seq,
        "timestamp": 1_700_000_000.0 + seq,
        "type": event_type,
        "payload": payload,
    }


def _prior_fact_line(messages: object) -> str:
    return next(line for line in _prior_block(messages).splitlines() if line.startswith("- [prior-execution:"))


def _synthetic_fact(template: dict[str, object], session_id: str) -> dict[str, object]:
    fact = json.loads(json.dumps(template))
    fact["origin_session_id"] = session_id
    fact["origin_run_id"] = "synthetic-run"
    fact["origin_command_id"] = "synthetic-command"
    fact["tool_call_id"] = "synthetic-tool-call"
    command = str(fact["command"]["text"])
    fact["execution_ref"] = _execution_ref(
        session_id,
        str(fact["origin_run_id"]),
        str(fact["origin_command_id"]),
        str(fact["tool_call_id"]),
        str(fact["tool"]),
        str(fact["cwd"]),
        command,
    )
    return fact


def _synthetic_correlation_events(fact: dict[str, object], case: str) -> list[dict[str, object]]:
    output = _fact_event(
        fact,
        int(fact["event_seq"]) - 1,
        "ToolOutput",
        {
            "tool_call_id": "wrong-tool-call" if case == "wrong_output" else fact["tool_call_id"],
            "name": fact["tool"],
        },
    )
    context = _fact_event(
        fact,
        int(fact["event_seq"]),
        "ContextUpdated",
        {
            "kind": "execution_completed_v1",
            "execution_ref": fact["execution_ref"],
            "tool": fact["tool"],
            "status": "timed_out" if case == "wrong_context" else fact["outcome"]["status"],
        },
        timestamp=float(fact["event_time"]),
    )
    if case == "missing_events":
        return []
    if case == "missing_output":
        return [context]
    return [output, context]


def _fact_event(
    fact: dict[str, object],
    seq: int,
    event_type: str,
    payload: dict[str, object],
    *,
    timestamp: float | None = None,
) -> dict[str, object]:
    return {
        "event_id": f"synthetic-{event_type}-{seq}",
        "session_id": fact["origin_session_id"],
        "run_id": fact["origin_run_id"],
        "command_id": fact["origin_command_id"],
        "seq": seq,
        "timestamp": timestamp if timestamp is not None else float(fact["event_time"]) - 0.01,
        "type": event_type,
        "payload": payload,
    }


def _prior_block(messages: object) -> str:
    assert isinstance(messages, list)
    return next(
        str(message.get("content") or "")
        for message in messages
        if isinstance(message, dict) and "<prior_execution_facts_v1>" in str(message.get("content") or "")
    )

from __future__ import annotations

import json
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.agent import AgentRuntime
from local_agent.config import AgentConfig, ConfigError, load_config
from local_agent.cli import main
from local_agent.explore_subagent import (
    EXPLORE_TOOL_NAMES,
    ExploreSubagentRunner,
    MAX_CHILD_TOOL_CALLS,
    MAX_CHILD_TRANSCRIPT_CHARS,
    MAX_HANDOFF_JSON_CHARS,
    MAX_LIMITATIONS,
    delegate_explore_tool,
)
from local_agent.llm import ChatResponse
from local_agent.protocol.events import ListEventSink
from local_agent.protocol.commands import CommandResult
from local_agent.run_collector import RunCollector
from local_agent.tools import create_default_registry, create_runtime_registry
from local_agent.tools.base import Tool, ToolContext, ToolRegistry, ToolResult


def _call(name: str, arguments: dict[str, object], call_id: str = "call-1") -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _response(*calls: dict[str, object], content: str | None = None) -> ChatResponse:
    message: dict[str, object] = {"content": content}
    if calls:
        message["tool_calls"] = list(calls)
    return ChatResponse(message=message)


def _yield_call(
    *,
    status: str = "completed",
    summary: str = "Located the narrow implementation candidate.",
    files: list[dict[str, str]] | None = None,
    architecture: str = "The entrypoint delegates to a small owner.",
    limitations: list[str] | None = None,
) -> dict[str, object]:
    return _call(
        "submit_explore_yield",
        {
            "status": status,
            "summary": summary,
            "files": files or [],
            "architecture": architecture,
            "limitations": limitations or [],
        },
        "yield-1",
    )


class _ScriptedClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def chat(self, messages, tools, *, timeout=None, model=None, tool_choice=None):
        self.calls.append(
            {
                "messages": json.loads(json.dumps(messages)),
                "tools": [schema["function"]["name"] for schema in tools],
                "timeout": timeout,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response()
        return response


class _CliRuntime:
    config: AgentConfig | None = None

    def __init__(self, config: AgentConfig, **_kwargs: object) -> None:
        type(self).config = config
        self.commands = self

    def dispatch(self, command: object) -> CommandResult:
        return CommandResult(command.command_id, "session", "run", "ok", {"content": "done"})


def _context(
    root: Path,
    events: list[tuple[str, dict[str, object]]],
    *,
    run_id: str = "run-1",
    tool_call_id: str = "parent-call-1",
    deadline: float | None = None,
    tool_approval: dict[str, str] | None = None,
) -> ToolContext:
    return ToolContext(
        workspace=root,
        approval_mode="always-ask",
        run_id=run_id,
        tool_call_id=tool_call_id,
        workspace_revision=7,
        deadline_monotonic=deadline,
        tool_approval=tool_approval,
        event_callback=lambda kind, payload: events.append((kind, dict(payload))),
    )


class ExploreSubagentTests(unittest.TestCase):
    @staticmethod
    def _read_registry(handler) -> ToolRegistry:
        return ToolRegistry(
            (
                Tool(
                    name="read_file",
                    description="Read one test file.",
                    tier="read",
                    input_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                    handler=handler,
                ),
            )
        )

    def test_config_defaults_disabled_and_accepts_cli_json_env_with_bounded_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = dict(
                config_path=None,
                cwd=tmp,
                provider="bailian",
                api_base_url=None,
                api_key=None,
                model=None,
                max_steps=None,
                budget_seconds=None,
                approval_mode=None,
            )
            with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "token"}, clear=True):
                default = load_config(**base)
                explicit = load_config(**base, enable_subagents=True, subagent_budget_seconds=45)
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps({"enable_subagents": True, "subagent_budget_seconds": 75}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "token"}, clear=True):
                from_json = load_config(**{**base, "config_path": str(config_path)})
            with patch.dict(
                os.environ,
                {
                    "DASHSCOPE_API_KEY": "token",
                    "LCA_ENABLE_SUBAGENTS": "yes",
                    "LCA_SUBAGENT_BUDGET_SECONDS": "30",
                },
                clear=True,
            ):
                from_env = load_config(**base)
                with self.assertRaisesRegex(ConfigError, "between 5 and 300"):
                    load_config(**base, subagent_budget_seconds=301)

        self.assertFalse(default.enable_subagents)
        self.assertEqual(default.subagent_budget_seconds, 60)
        self.assertTrue(explicit.enable_subagents)
        self.assertEqual(explicit.subagent_budget_seconds, 45)
        self.assertEqual((from_json.enable_subagents, from_json.subagent_budget_seconds), (True, 75))
        self.assertEqual((from_env.enable_subagents, from_env.subagent_budget_seconds), (True, 30))

    def test_registry_exposes_delegate_only_when_explicitly_enabled(self) -> None:
        disabled = create_runtime_registry(object(), False, 60)
        enabled = create_runtime_registry(object(), True, 60)

        self.assertNotIn("delegate_explore", disabled.tool_names())
        self.assertEqual(enabled.tool_names().count("delegate_explore"), 1)

    def test_cli_projects_explicit_enable_and_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with (
                patch.dict(os.environ, {"DASHSCOPE_API_KEY": "token"}, clear=True),
                patch("local_agent.cli.AgentRuntime", _CliRuntime),
                patch("sys.stdout", output),
            ):
                exit_code = main(
                    [
                        "--cwd", tmp,
                        "--provider", "bailian",
                        "--enable-subagents",
                        "--subagent-budget-seconds", "25",
                        "hello",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertTrue(_CliRuntime.config.enable_subagents)
        self.assertEqual(_CliRuntime.config.subagent_budget_seconds, 25)

    def test_typed_yield_uses_exact_readonly_tools_and_bounded_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "src" / "owner.py"
            source.parent.mkdir()
            source.write_text("class Owner:\n    pass\n", encoding="utf-8")
            client = _ScriptedClient(
                [
                    _response(_call("read_file", {"path": "src/owner.py"})),
                    _response(_yield_call(files=[{"path": "src/owner.py", "description": "Owner candidate"}])),
                ]
            )
            runner = ExploreSubagentRunner(client, create_default_registry(), budget_seconds=60)
            events: list[tuple[str, dict[str, object]]] = []

            result = runner.run("Locate the owner.", "Only inspect the entrypoint.", _context(root, events))
            payload = json.loads(result.content)

        self.assertFalse(result.is_error)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["files"][0]["path"], str(source))
        self.assertEqual(payload["files"][0]["provenance"], f"subagent:{payload['child_id']}/read_file")
        self.assertEqual(payload["provenance"], f"subagent:{payload['child_id']}")
        exposed = set(client.calls[0]["tools"])
        self.assertEqual(exposed - {"submit_explore_yield"}, set(EXPLORE_TOOL_NAMES))
        self.assertNotIn("delegate_explore", exposed)
        self.assertFalse({"shell", "run_tests", "apply_patch", "write_file", "ask_user", "memory_write"} & exposed)
        child_user = str(client.calls[0]["messages"][1]["content"])
        self.assertIn('"workspace_revision": 7', child_user)
        self.assertIn(str(root), child_user)
        self.assertEqual([kind for kind, _ in events], ["SubagentStarted", "SubagentFinished"])
        for _, event in events:
            self.assertNotIn("assignment", event)
            self.assertNotIn("content", event)
            self.assertNotIn("arguments", event)

    def test_one_call_per_run_resets_on_new_parent_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            client = _ScriptedClient([_response(_yield_call()), _response(_yield_call())])
            runner = ExploreSubagentRunner(client, create_default_registry(), budget_seconds=60)
            events: list[tuple[str, dict[str, object]]] = []

            first = json.loads(runner.run("First", "", _context(root, events)).content)
            second = json.loads(
                runner.run("Second", "", _context(root, events, tool_call_id="parent-call-2")).content
            )
            third = json.loads(
                runner.run(
                    "Third",
                    "",
                    _context(root, events, run_id="run-2", tool_call_id="parent-call-3"),
                ).content
            )

        self.assertEqual((first["status"], second["status"], third["status"]), ("completed", "failed", "completed"))
        self.assertEqual(len(client.calls), 2)
        self.assertIn("one explore subtask", second["limitations"][0].lower())

    def test_policy_deny_and_path_escape_are_partial_without_hidden_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            client = _ScriptedClient(
                [
                    _response(_call("read_file", {"path": "/outside/secret.txt"})),
                    _response(_yield_call()),
                ]
            )
            runner = ExploreSubagentRunner(client, create_default_registry(), budget_seconds=60)
            events: list[tuple[str, dict[str, object]]] = []
            payload = json.loads(
                runner.run(
                    "Inspect a candidate.",
                    "",
                    _context(root, events, tool_approval={"read_file": "prompt"}),
                ).content
            )

        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["tool_calls"], 1)
        self.assertEqual(payload["tool_errors"], 1)
        self.assertFalse(any(kind.startswith("Approval") for kind, _ in events))
        tool_result = client.calls[1]["messages"][-1]
        self.assertIn("requires approval", str(tool_result["content"]))

    def test_malformed_or_missing_yield_fails_without_repair(self) -> None:
        cases = (
            _response(content="I found a file."),
            _response(_call("submit_explore_yield", {"summary": "missing fields"}, "bad-yield")),
        )
        for response in cases:
            with self.subTest(response=response):
                with tempfile.TemporaryDirectory() as tmp:
                    client = _ScriptedClient([response])
                    runner = ExploreSubagentRunner(client, create_default_registry(), budget_seconds=60)
                    events: list[tuple[str, dict[str, object]]] = []
                    result = runner.run("Locate one file.", "", _context(Path(tmp).resolve(), events))
                    payload = json.loads(result.content)

                self.assertTrue(result.is_error)
                self.assertEqual(payload["status"], "failed")
                self.assertEqual(len(client.calls), 1)

    def test_failed_typed_yield_preserves_prior_child_tool_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "owner.py").write_text("pass\n", encoding="utf-8")
            client = _ScriptedClient(
                [
                    _response(_call("read_file", {"path": "owner.py"})),
                    _response(_call("submit_explore_yield", {"summary": "missing fields"}, "bad-yield")),
                ]
            )
            runner = ExploreSubagentRunner(client, create_default_registry(), budget_seconds=60)
            events: list[tuple[str, dict[str, object]]] = []
            result = runner.run("Locate one file.", "", _context(root, events))
            payload = json.loads(result.content)

        self.assertTrue(result.is_error)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["tool_calls"], 1)
        self.assertEqual(payload["tool_errors"], 0)
        finished = next(event for kind, event in events if kind == "SubagentFinished")
        self.assertEqual((finished["tool_calls"], finished["tool_errors"]), (1, 0))
        collector = RunCollector()
        collector.start("run-1", "locate", 1.0, guard_start={}, steer_start={})
        for kind, event in events:
            collector.record_event(kind, event)
        summary = collector.finish("final", guard_values={}, steering_values={})
        self.assertEqual(summary["subagents"]["tool_calls"], 1)
        self.assertEqual(summary["subagents"]["statuses"], {"failed": 1})

    def test_completed_yield_with_limitation_is_downgraded_to_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _ScriptedClient([_response(_yield_call(limitations=["A source remained unlocated."]))])
            runner = ExploreSubagentRunner(client, create_default_registry(), budget_seconds=60)
            payload = json.loads(runner.run("Locate.", "", _context(Path(tmp).resolve(), [])).content)

        self.assertEqual(payload["status"], "partial")

    def test_oversized_parallel_batch_is_rejected_before_any_tool_executes(self) -> None:
        executed: list[str] = []
        registry = self._read_registry(
            lambda args, _context: executed.append(str(args["path"])) or ToolResult("content")
        )
        calls = tuple(
            _call("read_file", {"path": f"file-{index}.py"}, f"call-{index}")
            for index in range(MAX_CHILD_TOOL_CALLS + 1)
        )
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExploreSubagentRunner(_ScriptedClient([_response(*calls)]), registry, budget_seconds=60)
            payload = json.loads(runner.run("Locate.", "", _context(Path(tmp).resolve(), [])).content)

        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["tool_calls"], 0)
        self.assertEqual(executed, [])

    def test_mixed_batch_with_unavailable_capability_executes_nothing(self) -> None:
        executed: list[str] = []
        registry = self._read_registry(
            lambda args, _context: executed.append(str(args["path"])) or ToolResult("content")
        )
        client = _ScriptedClient(
            [_response(_call("read_file", {"path": "safe.py"}), _call("shell", {"command": "false"}, "bad"))]
        )
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExploreSubagentRunner(client, registry, budget_seconds=60)
            payload = json.loads(runner.run("Locate.", "", _context(Path(tmp).resolve(), [])).content)

        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["tool_calls"], 0)
        self.assertEqual(executed, [])

    def test_cumulative_tool_result_transcript_stops_at_global_budget(self) -> None:
        registry = self._read_registry(lambda _args, _context: ToolResult("x" * 16000))
        client = _ScriptedClient(
            [
                _response(*(_call("read_file", {"path": f"file-{index}.py"}, f"call-{index}") for index in range(3))),
                _response(_call("read_file", {"path": "overflow.py"}, "call-overflow")),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExploreSubagentRunner(client, registry, budget_seconds=60)
            payload = json.loads(runner.run("Locate.", "", _context(Path(tmp).resolve(), [])).content)

        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["tool_calls"], 4)
        injected = sum(
            len(str(message.get("content") or ""))
            for message in client.calls[1]["messages"]
            if message.get("role") == "tool"
        )
        self.assertEqual(injected, MAX_CHILD_TRANSCRIPT_CHARS)

    def test_adversarial_encoded_handoff_and_path_remain_bounded_valid_json(self) -> None:
        cases = (
            _yield_call(
                summary="\x00" * 3000,
                architecture="\x00" * 2400,
                limitations=["\x00" * 500 for _ in range(MAX_LIMITATIONS)],
            ),
            _yield_call(files=[{"path": "a" * 2000, "description": "long path"}]),
        )
        for response in cases:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as tmp:
                runner = ExploreSubagentRunner(
                    _ScriptedClient([_response(response)]), create_default_registry(), budget_seconds=60
                )
                result = runner.run("Locate.", "", _context(Path(tmp).resolve(), []))

            self.assertLessEqual(len(result.content), MAX_HANDOFF_JSON_CHARS)
            self.assertEqual(json.loads(result.content)["status"], "failed")

    def test_max_limitations_plus_tool_error_stays_within_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            client = _ScriptedClient(
                [
                    _response(_call("read_file", {"path": "missing.py"})),
                    _response(_yield_call(limitations=[f"limit-{index}" for index in range(MAX_LIMITATIONS)])),
                ]
            )
            runner = ExploreSubagentRunner(client, create_default_registry(), budget_seconds=60)
            payload = json.loads(
                runner.run(
                    "Locate.", "", _context(root, [], tool_approval={"read_file": "prompt"})
                ).content
            )

        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["tool_errors"], 1)
        self.assertEqual(len(payload["limitations"]), MAX_LIMITATIONS)

    def test_oversized_valid_yield_is_deterministically_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "owner.py"
            source.write_text("pass\n", encoding="utf-8")
            client = _ScriptedClient(
                [
                    _response(_call("read_file", {"path": "owner.py"})),
                    _response(
                        _yield_call(
                            summary="s" * 5000,
                            files=[{"path": "owner.py", "description": "d" * 800}] * 25,
                            architecture="a" * 4000,
                            limitations=["l" * 800] * 20,
                        )
                    )
                ]
            )
            runner = ExploreSubagentRunner(client, create_default_registry(), budget_seconds=60)
            payload = json.loads(runner.run("Locate.", "", _context(root, [])).content)

        self.assertEqual(payload["status"], "partial")
        self.assertLessEqual(len(payload["summary"]), 3000)
        self.assertLessEqual(len(payload["architecture"]), 2400)
        self.assertLessEqual(len(payload["files"]), 20)
        self.assertLessEqual(len(payload["limitations"]), 12)
        self.assertIn("deterministically truncated", payload["limitations"][-1])

    def test_yield_file_must_be_bound_to_a_child_tool_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "invented.py").write_text("pass\n", encoding="utf-8")
            client = _ScriptedClient(
                [_response(_yield_call(files=[{"path": "invented.py", "description": "Unobserved"}]))]
            )
            runner = ExploreSubagentRunner(client, create_default_registry(), budget_seconds=60)
            result = runner.run("Locate.", "", _context(root, []))

        self.assertTrue(result.is_error)
        self.assertEqual(json.loads(result.content)["status"], "failed")

    def test_parent_deadline_times_out_once_and_ignores_late_response(self) -> None:
        def late_response() -> ChatResponse:
            time.sleep(0.12)
            return _response(_yield_call())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            client = _ScriptedClient([late_response])
            runner = ExploreSubagentRunner(client, create_default_registry(), budget_seconds=60)
            events: list[tuple[str, dict[str, object]]] = []
            result = runner.run(
                "Locate.",
                "",
                _context(root, events, deadline=time.monotonic() + 1.03),
            )
            count_after_timeout = len(events)
            time.sleep(0.15)

        payload = json.loads(result.content)
        self.assertEqual(payload["status"], "timeout")
        self.assertEqual(len(events), count_after_timeout)
        self.assertEqual([kind for kind, _ in events], ["SubagentStarted", "SubagentFinished"])

    def test_delegate_tool_redacts_arguments_and_marks_handoff_non_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            client = _ScriptedClient([_response(_yield_call())])
            registry = create_default_registry(
                (delegate_explore_tool(client, create_default_registry(), budget_seconds=60),)
            )
            events: list[tuple[str, dict[str, object]]] = []
            context = _context(root, events)

            result = registry.execute("delegate_explore", {"assignment": "secret assignment"}, context)

        self.assertEqual(registry.telemetry_arguments("delegate_explore", {"assignment": "secret"}), "[redacted by tool owner]")
        self.assertFalse(result.metadata["evidence_eligible"])
        self.assertTrue(result.metadata["redact_output_event"])


class _IntegratedSubagentClient:
    instance: "_IntegratedSubagentClient | None" = None

    def __init__(self, _config: AgentConfig) -> None:
        type(self).instance = self
        self.main_calls = 0
        self.child_calls = 0
        self.calls: list[dict[str, object]] = []

    def chat(self, messages, tools, *, timeout=None, model=None, tool_choice=None):
        names = [schema["function"]["name"] for schema in tools]
        child = "submit_explore_yield" in names
        self.calls.append({"child": child, "messages": json.loads(json.dumps(messages)), "tools": names})
        if child:
            self.child_calls += 1
            if self.child_calls == 1:
                return _response(
                    _call("search_code", {"pattern": "CHILD_ONLY_PATTERN_7f4a", "path": "."}, "child-search")
                )
            return _response(_yield_call(summary="Child located a candidate; parent verification is required."))
        self.main_calls += 1
        if self.main_calls == 1:
            return _response(_call("delegate_explore", {"assignment": "Find the parser owner."}, "delegate-1"))
        return _response(content="The scout returned candidate guidance; no direct repository fact is claimed.")


class ExploreSubagentRuntimeTests(unittest.TestCase):
    def test_parent_turn_has_one_lifecycle_and_child_transcript_is_not_parent_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            sink = ListEventSink()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=root,
                state_dir=root / "state",
                max_steps=4,
                budget_seconds=None,
                approval_mode="always-ask",
                workflow_profile="coding",
                enable_subagents=True,
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _IntegratedSubagentClient):
                runtime = AgentRuntime(config, show_tool_logs=False, event_sink=sink)
                content = runtime.run("Use one explicit scout, then report only candidate guidance.")

            client = _IntegratedSubagentClient.instance
            self.assertIsNotNone(client)
            self.assertEqual((client.main_calls, client.child_calls), (2, 2))
            self.assertIn("candidate guidance", content)
            child_messages = next(call["messages"] for call in client.calls if call["child"])
            self.assertNotIn("Use one explicit scout", json.dumps(child_messages))
            event_types = [event.type for event in sink.events]
            self.assertEqual(event_types.count("TurnStarted"), 1)
            self.assertEqual(event_types.count("TurnFinished"), 1)
            self.assertEqual(event_types.count("SubagentStarted"), 1)
            self.assertEqual(event_types.count("SubagentFinished"), 1)
            subagent_events = [event for event in sink.events if event.type.startswith("Subagent")]
            self.assertEqual({event.command_id for event in subagent_events}, {sink.events[1].command_id})
            self.assertEqual({event.run_id for event in subagent_events}, {runtime._last_run_summary["run_id"]})
            tool_started = next(event for event in sink.events if event.type == "ToolStarted")
            tool_output = next(event for event in sink.events if event.type == "ToolOutput")
            self.assertEqual(tool_started.payload["arguments"], "[redacted by tool owner]")
            self.assertEqual(tool_output.payload["content_preview"], "[redacted by tool owner]")
            self.assertEqual(runtime._last_run_summary["subagents"]["calls"], 1)
            self.assertEqual(runtime._last_run_summary["subagents"]["statuses"], {"completed": 1})
            self.assertNotIn("summary", runtime._last_run_summary["subagents"])
            self.assertIn("subagents: enabled, budget=60s", runtime.status_summary())
            self.assertEqual(runtime._run.tool_choice_results, [])
            self.assertEqual(runtime._run.evidence.records, [])
            parent_tool_results = [message for message in runtime._messages if message.get("role") == "tool"]
            self.assertEqual(len(parent_tool_results), 1)
            self.assertIn('"provenance": "subagent:', parent_tool_results[0]["content"])
            self.assertFalse(any(message.get("name") == "read_file" for message in parent_tool_results))
            session_text = runtime._session.path.read_text(encoding="utf-8")
            self.assertNotIn("Find the parser owner", session_text)
            self.assertNotIn("CHILD_ONLY_PATTERN_7f4a", session_text)

    def test_feature_off_keeps_default_schema_and_chat_only_client_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=1,
                budget_seconds=None,
                approval_mode="yolo",
            )
            runtime = AgentRuntime(config, show_tool_logs=False)

        self.assertNotIn("delegate_explore", runtime._registry.tool_names())
        self.assertIn("subagents: disabled", runtime.status_summary())


if __name__ == "__main__":
    unittest.main()

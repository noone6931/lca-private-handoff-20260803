from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.agent import AgentRuntime
from local_agent.config import AgentConfig
from local_agent.execution_policy import evaluate_execution_policy
from local_agent.execution_policy import execution_action
from local_agent.protocol.events import ListEventSink
from local_agent.run_collector import RunCollector
from local_agent.tools.base import Tool
from local_agent.tools.base import ToolContext
from local_agent.tools.base import ToolRegistry
from local_agent.tools.base import ToolResult
from local_agent.tools.shell import shell_tools


class _PolicyRuntimeClient:
    calls = 0

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None, model=None, tool_choice=None):
        type(self).calls += 1
        if type(self).calls == 1:
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "read-policy-fixture",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": "README.md"}),
                                },
                            }
                        ],
                    }
                },
            )()
        return type(
            "Response",
            (),
            {"message": {"content": "Observed `README.md:1`: policy fixture. No files were changed."}},
        )()


class ExecutionPolicyTests(unittest.TestCase):
    def test_characterization_matrix_preserves_mode_override_and_session_precedence(self) -> None:
        cases = (
            ("always-ask", "read", None, None, False, True, "allow", "approval_mode", False),
            ("always-ask", "state", None, None, False, True, "allow", "approval_mode", False),
            ("always-ask", "interaction", None, None, False, True, "allow", "approval_mode", False),
            ("always-ask", "network", None, None, False, True, "allow", "approval_mode", False),
            ("always-ask", "write", None, None, False, True, "prompt", "approval_mode", True),
            ("always-ask", "exec", None, None, False, True, "prompt", "approval_mode", False),
            ("write", "write", None, None, False, True, "allow", "approval_mode", False),
            ("write", "exec", None, None, False, True, "prompt", "approval_mode", False),
            ("yolo", "exec", None, None, False, False, "allow", "approval_mode", False),
            ("yolo", "exec", "deny", None, False, True, "deny", "config_per_tool", False),
            ("yolo", "read", "prompt", "allow_always", False, True, "prompt", "config_per_tool", False),
            ("yolo", "exec", "allow", "reject_always", False, True, "deny", "session_per_tool", False),
            ("yolo", "read", "allow", "prompt", False, True, "prompt", "session_per_tool", True),
            ("yolo", "exec", None, "prompt", False, True, "prompt", "session_per_tool", False),
            ("always-ask", "exec", None, "allow_always", False, True, "allow", "session_per_tool", False),
            ("always-ask", "write", "allow", None, False, True, "allow", "config_per_tool", False),
            ("always-ask", "write", None, None, True, True, "allow", "auto_approve", False),
            ("always-ask", "write", "deny", None, True, True, "deny", "config_per_tool", False),
            ("always-ask", "exec", None, None, False, False, "deny", "non_interactive", False),
            ("auto-read", "write", None, None, False, True, "prompt", "approval_mode", True),
        )
        for mode, tier, config, session, auto, interactive, outcome, source, cache in cases:
            with self.subTest(
                mode=mode,
                tier=tier,
                config=config,
                session=session,
                auto=auto,
                interactive=interactive,
            ):
                decision = evaluate_execution_policy(
                    execution_action("sample", tier),
                    approval_mode=mode,
                    config_policy=config,
                    session_policy=session,
                    auto_approved=auto,
                    interactive_available=interactive,
                )
                self.assertEqual(decision.outcome, outcome)
                self.assertEqual(decision.source, source)
                self.assertEqual(decision.session_cache_allowed, cache)

    def test_action_and_sandbox_taxonomy_is_static_and_truthful(self) -> None:
        shell = evaluate_execution_policy(execution_action("shell", "exec"), approval_mode="yolo")
        tests = evaluate_execution_policy(execution_action("run_tests", "exec"), approval_mode="yolo")
        read = evaluate_execution_policy(execution_action("read_file", "read"), approval_mode="yolo")
        state = evaluate_execution_policy(execution_action("todo_update", "state"), approval_mode="yolo")
        network = evaluate_execution_policy(execution_action("web_search", "network"), approval_mode="yolo")

        self.assertEqual(shell.action.capability_class, "process_exec")
        self.assertEqual(shell.sandbox_state, "unsandboxed")
        self.assertEqual(tests.sandbox_state, "unsandboxed")
        self.assertEqual(read.sandbox_state, "none")
        self.assertEqual(state.sandbox_state, "none")
        self.assertEqual(network.action.capability_class, "network_read")
        self.assertEqual(network.sandbox_state, "none")

    def test_registry_execute_and_preapproval_use_the_same_evaluator(self) -> None:
        tool = _tool("sample_read", "read")
        registry = ToolRegistry([tool])
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="yolo")
            with patch(
                "local_agent.tools.base.evaluate_execution_policy",
                wraps=evaluate_execution_policy,
            ) as evaluator:
                self.assertTrue(registry.is_preapproved("sample_read", context))
                result = registry.execute("sample_read", {"private": "not-in-schema"}, context)

        self.assertTrue(result.is_error)
        self.assertEqual(evaluator.call_count, 2)

    def test_policy_events_are_once_per_execution_and_never_include_arguments(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        secret = "PRIVATE_COMMAND_AND_ENV_TOKEN"
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="yolo",
                tool_approval={"shell": "deny", "run_tests": "deny"},
                event_callback=lambda event_type, payload: events.append((event_type, payload)),
            )
            registry = ToolRegistry(shell_tools())
            registry.execute("shell", {"command": f"echo {secret}"}, context)
            registry.execute("run_tests", {"command": f"TOKEN={secret} python3 -m unittest"}, context)

        policy_events = [payload for event_type, payload in events if event_type == "ExecutionPolicyEvaluated"]
        self.assertEqual(len(policy_events), 2)
        self.assertEqual({payload["tool"] for payload in policy_events}, {"shell", "run_tests"})
        self.assertTrue(all(payload["outcome"] == "deny" for payload in policy_events))
        self.assertTrue(all(payload["sandbox_state"] == "unsandboxed" for payload in policy_events))
        self.assertNotIn(secret, json.dumps(policy_events, sort_keys=True))
        self.assertFalse(any(key in payload for payload in policy_events for key in ("arguments", "command", "env")))

    def test_unknown_and_runtime_restricted_tools_are_not_policy_denials(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        registry = ToolRegistry([_tool("sample_read", "read")])
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="yolo",
                runtime_tool_allowlist=frozenset({"read_file"}),
                event_callback=lambda event_type, payload: events.append((event_type, payload)),
            )
            unknown = registry.execute("missing_tool", {}, context)
            restricted = registry.execute("sample_read", {}, context)

        self.assertTrue(unknown.metadata["unknown_tool"])
        self.assertTrue(restricted.metadata["provider_schema_violation"])
        self.assertFalse(any(event_type == "ExecutionPolicyEvaluated" for event_type, _payload in events))

    def test_collector_bounds_policy_taxonomy_and_summarizes_sources(self) -> None:
        collector = RunCollector()
        collector.start("run-policy", "test", 1.0, guard_start={}, steer_start={})
        for decision in (
            evaluate_execution_policy(execution_action("read_file", "read"), approval_mode="yolo"),
            evaluate_execution_policy(
                execution_action("shell", "exec"),
                approval_mode="always-ask",
                interactive_available=True,
            ),
            evaluate_execution_policy(
                execution_action("run_tests", "exec"),
                approval_mode="always-ask",
                interactive_available=False,
            ),
        ):
            collector.record_event("ExecutionPolicyEvaluated", decision.event_payload())
        summary = collector.finish("final", guard_values={}, steering_values={})

        self.assertEqual(
            summary["execution_policy"],
            {
                "evaluated": 3,
                "allow": 1,
                "prompt": 1,
                "deny": 1,
                "unsandboxed_exec_evaluations": 2,
                "invalid_events": 0,
                "sources": {"approval_mode": 2, "non_interactive": 1},
            },
        )
        collector.record_event(
            "ExecutionPolicyEvaluated",
            {"outcome": "allow", "source": "raw-model-text", "sandbox_state": "none"},
        )
        after_invalid = collector.finish("final", guard_values={}, steering_values={})
        self.assertEqual(after_invalid["execution_policy"]["invalid_events"], 1)
        self.assertEqual(after_invalid["execution_policy"]["evaluated"], 3)

    def test_runtime_records_policy_event_session_summary_and_status(self) -> None:
        _PolicyRuntimeClient.calls = 0
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "README.md").write_text("policy fixture\n", encoding="utf-8")
            sink = ListEventSink()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
                workflow_profile="coding",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _PolicyRuntimeClient):
                runtime = AgentRuntime(config, show_tool_logs=False, event_sink=sink)
                result = runtime.run("Read README.md and report the observed evidence without modifying files.")
            records = [json.loads(line) for line in runtime._session.path.read_text(encoding="utf-8").splitlines()]

        self.assertIn("policy fixture", result)
        events = [event for event in sink.events if event.type == "ExecutionPolicyEvaluated"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["tool"], "read_file")
        self.assertEqual(events[0].payload["source"], "approval_mode")
        self.assertEqual(runtime._last_run_summary["execution_policy"]["evaluated"], 1)
        self.assertEqual(runtime._last_run_summary["execution_policy"]["allow"], 1)
        self.assertIn("execution_policy: evaluated=1, allow=1, prompt=0, deny=0", runtime.status_summary())
        self.assertIn("unsandboxed_exec_evaluations=0, invalid_events=0", runtime.status_summary())
        self.assertTrue(
            any(
                record.get("event") == "event_v1"
                and record.get("payload", {}).get("type") == "ExecutionPolicyEvaluated"
                for record in records
            )
        )


def _tool(name: str, tier: str) -> Tool:
    return Tool(
        name=name,
        description="test-only tool",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        tier=tier,
        handler=lambda _arguments, _context: ToolResult("ok"),
    )


if __name__ == "__main__":
    unittest.main()

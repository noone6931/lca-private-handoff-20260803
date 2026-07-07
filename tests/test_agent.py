from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.agent import AgentRuntime
from local_agent.config import AgentConfig


class _FailingClient:
    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        raise AssertionError("LLM should not be called after the budget is exhausted")


class _FinalClient:
    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        return type("Response", (), {"message": {"content": "done"}})()


class _TimeoutRecordingClient:
    timeouts: list[float] = []

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        self.timeouts.append(timeout)
        return type("Response", (), {"message": {"content": "done"}})()


class _MessageRecordingClient:
    messages: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        type(self).messages = messages
        return type("Response", (), {"message": {"content": "done"}})()


class _TwoToolClient:
    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "unknown_one", "arguments": "{}"},
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "unknown_two", "arguments": "{}"},
                        },
                    ],
                }
            },
        )()


class _LengthToolClient:
    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        return type(
            "Response",
            (),
            {
                "finish_reason": "length",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "shell", "arguments": "{\"command\":\"unterminated"},
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "run_tests", "arguments": "{}"},
                        },
                    ],
                },
            },
        )()


class _InterruptingRegistry:
    def schemas(self):
        return []

    def execute(self, name, raw_arguments, context):
        raise KeyboardInterrupt


class _UnexpectedRegistry:
    def schemas(self):
        return []

    def execute(self, name, raw_arguments, context):
        raise AssertionError("Tool should not execute after finish_reason=length")


class AgentRuntimeTests(unittest.TestCase):
    def test_budget_seconds_stops_before_next_llm_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=100,
                budget_seconds=1,
                approval_mode="yolo",
            )
            with (
                patch("local_agent.agent.OpenAICompatibleClient", _FailingClient),
                patch("local_agent.agent.time.monotonic", side_effect=[0.0, 2.0]),
            ):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("hello")

        self.assertEqual(result, "Stopped after reaching budget_seconds=1.")

    def test_zero_max_steps_means_unlimited_not_zero_iterations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _FinalClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("hello")

        self.assertEqual(result, "done")

    def test_llm_timeout_is_clamped_to_remaining_budget(self) -> None:
        _TimeoutRecordingClient.timeouts = []
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                request_timeout=120,
                max_steps=0,
                budget_seconds=10,
                approval_mode="yolo",
            )
            with (
                patch("local_agent.agent.OpenAICompatibleClient", _TimeoutRecordingClient),
                patch("local_agent.agent.time.monotonic", side_effect=[100.0, 101.0, 102.0]),
            ):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("hello")

        self.assertEqual(result, "done")
        self.assertEqual(_TimeoutRecordingClient.timeouts, [8.0])

    def test_budget_stop_synthesizes_remaining_tool_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=0,
                budget_seconds=1,
                approval_mode="yolo",
            )
            with (
                patch("local_agent.agent.OpenAICompatibleClient", _TwoToolClient),
                patch(
                    "local_agent.agent.time.monotonic",
                    side_effect=[0.0, 0.1, 0.2, 0.3, 2.0],
                ),
            ):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("hello")

        tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        self.assertEqual(result, "Stopped after reaching budget_seconds=1.")
        self.assertEqual([message["tool_call_id"] for message in tool_messages], ["call_1", "call_2"])
        self.assertIn("Tool call was not executed", tool_messages[1]["content"])

    def test_length_finish_reason_synthesizes_tool_results_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with (
                patch("local_agent.agent.OpenAICompatibleClient", _LengthToolClient),
                patch("local_agent.agent.create_default_registry", return_value=_UnexpectedRegistry()),
            ):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("hello")

        tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        self.assertIn("finish_reason=length", result)
        self.assertEqual([message["tool_call_id"] for message in tool_messages], ["call_1", "call_2"])
        self.assertTrue(all("output token limit" in message["content"] for message in tool_messages))

    def test_keyboard_interrupt_synthesizes_remaining_tool_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with (
                patch("local_agent.agent.OpenAICompatibleClient", _TwoToolClient),
                patch("local_agent.agent.create_default_registry", return_value=_InterruptingRegistry()),
            ):
                runtime = AgentRuntime(config, show_tool_logs=False)
                with self.assertRaises(KeyboardInterrupt):
                    runtime.run("hello")
            records = [json.loads(line) for line in runtime._session.path.read_text(encoding="utf-8").splitlines()]

        tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        self.assertEqual([message["tool_call_id"] for message in tool_messages], ["call_1", "call_2"])
        self.assertTrue(all("interrupted execution" in message["content"] for message in tool_messages))
        self.assertTrue(
            any(
                record.get("event") == "final"
                and record.get("payload", {}).get("content") == "Stopped after user interrupt."
                for record in records
            )
        )

    def test_context_compaction_injects_summary_and_open_todos(self) -> None:
        _MessageRecordingClient.messages = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
                context_char_budget=1200,
                context_recent_messages=4,
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _MessageRecordingClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                todo_path = workspace / ".local-agent" / "todos" / f"{runtime._session.session_id}.json"
                todo_path.parent.mkdir(parents=True)
                todo_path.write_text(
                    json.dumps(
                        [
                            {"id": "T1", "task": "Finish compaction", "status": "in_progress", "note": "keep this"},
                            {"id": "T2", "task": "Already done", "status": "done", "note": ""},
                        ]
                    ),
                    encoding="utf-8",
                )
                runtime._messages.extend(
                    [
                        {"role": "user", "content": "old request " + ("x" * 500)},
                        {"role": "assistant", "content": "old answer " + ("y" * 500)},
                        {"role": "user", "content": "recent request"},
                    ]
                )
                result = runtime.run("new request " + ("details " * 80) + "FINAL_MARKER_DO_NOT_DROP")

        sent = _MessageRecordingClient.messages
        sent_system_messages = [message for message in sent if message.get("role") == "system"]
        self.assertEqual(result, "done")
        self.assertEqual(len(sent_system_messages), 1)
        self.assertIn("You are a local coding agent", sent_system_messages[0].get("content", ""))
        self.assertIn("[Local context compaction]", sent_system_messages[0].get("content", ""))
        self.assertIn("Current user request:", sent_system_messages[0].get("content", ""))
        self.assertIn("FINAL_MARKER_DO_NOT_DROP", sent_system_messages[0].get("content", ""))
        self.assertTrue(any("Earlier conversation was compacted" in m.get("content", "") for m in sent))
        self.assertTrue(any("T1: Finish compaction" in m.get("content", "") for m in sent))
        self.assertFalse(any("T2: Already done" in m.get("content", "") for m in sent))

    def test_context_compaction_truncates_large_recent_tool_outputs_for_model_only(self) -> None:
        _MessageRecordingClient.messages = []
        large_tool_output = "tool-output-" + ("z" * 10000)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
                context_char_budget=3000,
                context_recent_messages=4,
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _MessageRecordingClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                runtime._messages.extend(
                    [
                        {"role": "user", "content": "old request " + ("x" * 3000)},
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "shell", "arguments": "{}"},
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "call_1",
                            "content": large_tool_output,
                        },
                    ]
                )
                result = runtime.run("new request")

        sent_tool_messages = [message for message in _MessageRecordingClient.messages if message.get("role") == "tool"]
        stored_tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        self.assertEqual(result, "done")
        self.assertEqual(len(sent_tool_messages), 1)
        self.assertIn("...<truncated", sent_tool_messages[0]["content"])
        self.assertLess(len(sent_tool_messages[0]["content"]), len(large_tool_output))
        self.assertEqual(stored_tool_messages[0]["content"], large_tool_output)

    def test_session_tool_policy_rejects_unknown_tool_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _FinalClient):
                runtime = AgentRuntime(config, show_tool_logs=False)

        with self.assertRaisesRegex(ValueError, "unknown tool: run_test"):
            runtime.set_session_tool_policy("run_test", "allow")


if __name__ == "__main__":
    unittest.main()

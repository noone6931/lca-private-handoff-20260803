from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()

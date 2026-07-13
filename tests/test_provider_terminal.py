from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from local_agent.agent import AgentRuntime
from local_agent.config import AgentConfig
from local_agent.finalization import FinalizationCoordinator
from local_agent.provider_terminal import assess_terminal_content
from local_agent.steering.final_answer import SteeringDecision


class _TerminalRecoveryClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config
        self._index = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        replies = ("_nulla_", "A draft answer with useful context.", "_nulla_", "The final answer is complete and useful.")
        response = replies[self._index]
        self._index += 1
        return type("Response", (), {"message": {"content": response}})()


class ProviderTerminalTests(unittest.TestCase):
    def test_rejects_contentless_and_detached_single_token_shapes(self) -> None:
        for content in (None, "", "   ", "...", "_nulla_"):
            with self.subTest(content=content):
                self.assertFalse(assess_terminal_content(content, request="summarize the workspace").substantive)
        self.assertFalse(assess_terminal_content(" nulla", request="summarize the workspace", forced_final=True).substantive)
        self.assertTrue(assess_terminal_content("recovered", request="summarize the workspace").substantive)

    def test_preserves_normal_null_discussion_and_code(self) -> None:
        examples = (
            "null is a JSON literal and differs from an omitted field.",
            "`null`",
            "```json\nnull\n```",
            "null",
        )
        for content in examples:
            with self.subTest(content=content):
                self.assertTrue(assess_terminal_content(content, request="explain null in JSON").substantive)

    def test_forced_final_recovery_is_bounded_and_keeps_terminal_only_mode(self) -> None:
        coordinator = FinalizationCoordinator()
        self.assertTrue(coordinator.request(kind="completion_audit").accepted)
        self.assertTrue(coordinator.begin_forced_final_turn())
        for attempt in range(1, 4):
            outcome = coordinator.observe_non_substantive_response(forced_final=True, kind="detached_single_token")
            self.assertTrue(outcome.retry)
            self.assertEqual(outcome.attempt, attempt)
            self.assertTrue(coordinator.pending_force_final)
            self.assertTrue(coordinator.begin_forced_final_turn())
        exhausted = coordinator.observe_non_substantive_response(forced_final=True, kind="detached_single_token")
        self.assertFalse(exhausted.retry)
        self.assertEqual(coordinator.terminal_response_snapshot(), {"non_substantive_retries": 3, "non_substantive_exhausted": 1})

    def test_runtime_recovers_once_in_ordinary_and_forced_final_phases_without_persisting_placeholders(self) -> None:
        _TerminalRecoveryClient.calls = []
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
            with patch("local_agent.agent.OpenAICompatibleClient", _TerminalRecoveryClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                with patch.object(
                    runtime,
                    "_decide_final_answer_steering",
                    side_effect=(SteeringDecision(kind="completion_audit", message="rewrite", payload={}), None),
                ):
                    answer = runtime.run("Please give a concise answer.")
                records = runtime._session.path.read_text(encoding="utf-8")

        self.assertEqual(answer, "The final answer is complete and useful.")
        self.assertEqual(len(_TerminalRecoveryClient.calls), 4)
        self.assertEqual(_TerminalRecoveryClient.calls[2]["tools"], [])
        self.assertEqual(_TerminalRecoveryClient.calls[3]["tools"], [])
        self.assertNotIn("nulla", records)
        self.assertEqual(runtime._last_run_summary["provider_terminal"], {"non_substantive_retries": 2, "non_substantive_exhausted": 0})


if __name__ == "__main__":
    unittest.main()

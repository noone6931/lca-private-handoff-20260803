from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_runtime_orchestrator_stays_within_line_budget(self) -> None:
        agent = ROOT / "src/local_agent/agent.py"
        self.assertLessEqual(len(agent.read_text(encoding="utf-8").splitlines()), 2500)

    def test_final_answer_facade_stays_thin(self) -> None:
        facade = ROOT / "src/local_agent/steering/final_answer.py"
        self.assertLessEqual(len(facade.read_text(encoding="utf-8").splitlines()), 600)

    def test_runtime_does_not_reintroduce_migrated_domain_helpers(self) -> None:
        content = (ROOT / "src/local_agent/agent.py").read_text(encoding="utf-8")
        for helper in (
            "def _messages_with_runtime_context",
            "def _messages_to_memory_transcript",
            "def _tool_choice_steering_message",
        ):
            self.assertNotIn(helper, content)


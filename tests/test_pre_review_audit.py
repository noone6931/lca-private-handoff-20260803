from __future__ import annotations

import unittest

from local_agent.steering.final_answer import FinalAnswerContext
from local_agent.steering.final_answer import FinalAnswerSteeringSeverity
from local_agent.steering.final_answer import SteeringDecision
from local_agent.steering.pre_review import collect_pre_review_audit
from local_agent.task_contract import RequirementContract
from local_agent.tool_observation import ToolResultSummary


class _DecisionSteerer:
    def __init__(self, kind: str, *, tools: set[str] | None = None) -> None:
        self.kind = kind
        self._tools = tools

    def decide(self, _context: FinalAnswerContext) -> SteeringDecision:
        return SteeringDecision(
            kind=self.kind,
            message=self.kind,
            payload={"issues": [self.kind]},
            force_final_answer_without_tools=self._tools is None,
            temporary_tool_allowlist=self._tools,
            severity=FinalAnswerSteeringSeverity.HARD,
        )


def _context(content: str, *, revision: int = 1) -> FinalAnswerContext:
    contract = RequirementContract(
        objective="review owner evidence",
        scope="workspace",
        acceptance_items=[],
        evidence_requirements=[],
        verification_requirements=[],
        risk_notes=[],
        task_kind="read-only",
        read_only_review_profile="design",
    )
    return FinalAnswerContext(
        request="只读分析设计 owner 和状态码",
        content=content,
        messages=[],
        run_start_index=0,
        requirement_contract=contract,
        tool_results=[ToolResultSummary(name="read_file", path=f"src/Owner{revision}.java")],
        read_file_evidence_paths=[],
        source_evidence=[],
        open_todos=[],
        is_code_implementation_request=False,
        steer_counts={},
        read_only_explore_finalized=True,
    )


class PreReviewAuditTests(unittest.TestCase):
    def test_merges_hard_categories_into_one_no_tool_directive_after_explore(self) -> None:
        outcome = collect_pre_review_audit(
            _context("unsafe draft"),
            (
                _DecisionSteerer("source_grounded_numeric"),
                _DecisionSteerer("negative_existence", tools={"glob_files"}),
                _DecisionSteerer("completion_audit"),
            ),
        )

        assert outcome is not None
        self.assertEqual(
            outcome.categories,
            ("source_grounded_numeric", "negative_existence", "completion_audit"),
        )
        self.assertIn("Use only collected evidence", outcome.render())
        self.assertIn("[negative_existence]", outcome.render())

    def test_collection_is_pure_and_does_not_own_retry_state(self) -> None:
        steerers = (_DecisionSteerer("source_grounded_numeric"), _DecisionSteerer("negative_existence"))
        first = collect_pre_review_audit(_context("draft"), steerers)
        repeated = collect_pre_review_audit(_context("draft"), steerers)
        refreshed = collect_pre_review_audit(_context("draft", revision=2), steerers)

        assert first is not None and repeated is not None and refreshed is not None
        self.assertEqual(first, repeated)
        self.assertEqual(first.categories, refreshed.categories)

    def test_single_hard_finding_keeps_its_precise_owner(self) -> None:
        audit = collect_pre_review_audit(_context("draft"), (_DecisionSteerer("completion_audit"),))

        self.assertIsNone(audit)


if __name__ == "__main__":
    unittest.main()

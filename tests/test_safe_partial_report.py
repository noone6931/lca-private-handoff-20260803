from __future__ import annotations

import unittest

from local_agent.explore_handoff import ClaimEvidenceItem, ExploreHandoff
from local_agent.read_only_reviewer import ReviewerFinding
from local_agent.safe_partial_report import build_safe_partial_report
from local_agent.task_contract import RequirementContract


def _handoff() -> ExploreHandoff:
    contract = RequirementContract(
        objective="inspect a design",
        scope="roots",
        acceptance_items=[],
        evidence_requirements=[],
        verification_requirements=[],
        risk_notes=[],
        task_kind="read-only",
        read_only_review_profile="design",
    )
    return ExploreHandoff(
        request="analyze the design",
        contract=contract,
        items=(
            ClaimEvidenceItem("requirement_fact", "read_file", "docs/requirements.md", "primary", "root_local", "ok", "The request returns to draft."),
            ClaimEvidenceItem("observed_candidate", "read_file", "src/PrepareOrder.java", "service", "root_local", "ok", "A bounded implementation excerpt was read."),
            ClaimEvidenceItem("unlocated", "glob_files", "**/*Settlement*", "service", "root_discovery", "no_match", "No matching file in the bounded scope."),
            ClaimEvidenceItem("inspection_failure", "glob_files", "other-root", "other", "root_discovery", "error", "Tool approval denied."),
        ),
    )


class SafePartialReportTests(unittest.TestCase):
    def test_second_nonpass_keeps_observations_without_leaking_candidate_inventions(self) -> None:
        report = build_safe_partial_report(
            _handoff(),
            (
                ReviewerFinding("c001", "No direct owner binding for the asserted owner.", "Downgrade to unlocated."),
                ReviewerFinding("c002", "Existing endpoint/table is not supported.", "Mark it as a proposal."),
            ),
            reason="second_review_nonpass",
        )

        self.assertIn("docs/requirements.md", report.content)
        self.assertIn("src/PrepareOrder.java", report.content)
        self.assertIn("**/*Settlement*", report.content)
        self.assertIn("检查限制 / 失败", report.content)
        self.assertIn("Tool approval denied", report.content)
        self.assertIn("不是 Owner/现有实现结论", report.content)
        self.assertIn("Owner/调用链缺少显式绑定", report.content)
        self.assertNotIn("DDL", report.content)
        self.assertNotIn("模板/下载", report.content)
        self.assertNotIn("InventedSettlementTable", report.content)
        self.assertEqual(report.observation_count, 2)
        self.assertEqual(report.missing_count, 1)

    def test_zero_observation_report_is_still_safe_and_explicit(self) -> None:
        empty = ExploreHandoff(request="x", contract=_handoff().contract, items=())
        report = build_safe_partial_report(empty, reason="timeout")
        self.assertIn("没有可安全交付", report.content)
        self.assertIn("未完成/未验证", report.content)
        self.assertEqual(report.observation_count, 0)


if __name__ == "__main__":
    unittest.main()

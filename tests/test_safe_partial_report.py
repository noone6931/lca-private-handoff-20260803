from __future__ import annotations

import unittest

from local_agent.explore_handoff import ClaimEvidenceItem, ExploreHandoff
from local_agent.document_consistency import DocumentConsistencyAssessment
from local_agent.read_only_reviewer import ReviewerFinding
from local_agent.safe_partial_report import build_safe_partial_report
from local_agent.explore_handoff import build_explore_handoff
from local_agent.requirement_evidence import RequirementEvidence
from local_agent.task_contract import generate_requirement_contract
from local_agent.tool_choice_queue import ToolResultSummary
from local_agent.steering.models import SourceEvidence
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
    def test_handoff_dedupes_relative_requirement_and_absolute_current_read(self) -> None:
        workspace = "/tmp/lca-doc-root"
        handoff = build_explore_handoff(
            request="compare documents",
            contract=generate_requirement_contract("只根据 Markdown、HTML 和示例图分析需求，不要检查代码。"),
            requirement_evidence=(RequirementEvidence("requirements.md", "Document requirement", root=workspace),),
            source_evidence=(SourceEvidence("requirements.md", "Document requirement", root=workspace),),
            records=(),
            tool_results=(
                ToolResultSummary(
                    "read_file",
                    "Document requirement",
                    path=f"{workspace}/requirements.md",
                    metadata={"evidence_root": workspace, "evidence_root_label": "primary"},
                ),
            ),
        )

        report = build_safe_partial_report(handoff, reason="llm_timeout")

        self.assertEqual(report.observation_count, 1)
        self.assertEqual(report.content.count("requirements.md"), 1)

    def test_multi_root_incomplete_handoff_keeps_observation_and_missing_root_once(self) -> None:
        contract = generate_requirement_contract("只读分析服务 owner 和影响范围，不要修改。")
        handoff = build_explore_handoff(
            request="只读分析服务 owner 和影响范围，不要修改。",
            contract=contract,
            requirement_evidence=(),
            source_evidence=(SourceEvidence("/workspace/service-a/App.java", "class Observed {}", root="/workspace/service-a"),),
            records=(),
            tool_results=(
                ToolResultSummary("read_file", "class Observed {}", path="/workspace/service-a/App.java"),
                ToolResultSummary(
                    "read_only_explore",
                    "No successful direct read covered this root before the bounded exploration phase ended.",
                    useless=True,
                    path="/workspace/service-b",
                    metadata={"read_only_explore_incomplete": True, "evidence_root": "/workspace/service-b"},
                ),
                ToolResultSummary(
                    "search_code",
                    "runtime tool restriction",
                    is_error=True,
                    path="/workspace/service-b",
                    metadata={"evidence_root": "/workspace/service-b"},
                ),
                ToolResultSummary(
                    "glob_files",
                    "runtime tool restriction again",
                    is_error=True,
                    path="/workspace/service-b",
                    metadata={"evidence_root": "/workspace/service-b"},
                ),
            ),
        )

        report = build_safe_partial_report(handoff, reason="second_review_nonpass")

        self.assertIn("/workspace/service-a/App.java", report.content)
        self.assertIn("/workspace/service-b", report.content)
        self.assertNotIn("没有额外的缺失", report.content)
        self.assertEqual(report.content.count("检查限制 / 失败"), 1)
        self.assertIn("同类限制 2 次", report.content)

    def test_second_nonpass_keeps_observations_without_leaking_candidate_inventions(self) -> None:
        report = build_safe_partial_report(
            _handoff(),
            (
                ReviewerFinding(
                    "c001",
                    "No direct owner binding for the asserted owner.",
                    "Downgrade to unlocated.",
                    "The owner is verified.",
                    "candidate_defect",
                ),
                ReviewerFinding(
                    "c002",
                    "Existing endpoint/table is not supported.",
                    "Mark it as a proposal.",
                    "Existing endpoint is ready.",
                    "candidate_defect",
                ),
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

    def test_timeout_partial_is_not_mislabelled_as_a_reviewer_rejection(self) -> None:
        report = build_safe_partial_report(_handoff(), reason="llm_timeout")

        self.assertIn("有界运行提前终止", report.content)
        self.assertIn("termination=llm_timeout", report.content)
        self.assertNotIn("候选草稿未通过独立证据审查", report.content)

    def test_document_partial_keeps_model_generated_visual_observation(self) -> None:
        contract = generate_requirement_contract("只根据 Markdown 和示例图分析资料一致性，不要检查代码。")
        handoff = ExploreHandoff(
            request="compare artifacts",
            contract=contract,
            items=(
                ClaimEvidenceItem("requirement_fact", "read_file", "policy.md", "primary", "root_local", "ok", "Field must remain blank."),
                ClaimEvidenceItem("visual_observation", "inspect_image", "example.png", "primary", "root_local", "ok", "Model observed a visible value."),
            ),
        )
        report = build_safe_partial_report(
            handoff,
            reason="second_review_nonpass",
            document_consistency=DocumentConsistencyAssessment("asserted_reconciled", ("e001", "e002")),
        )

        self.assertIn("policy.md", report.content)
        self.assertIn("example.png", report.content)
        self.assertIn("视觉模型观察", report.content)
        self.assertIn("OCR/识别不确定性", report.content)
        self.assertIn("未消解的资料冲突", report.content)
        self.assertEqual(report.observation_count, 2)


if __name__ == "__main__":
    unittest.main()

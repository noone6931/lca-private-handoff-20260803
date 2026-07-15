from __future__ import annotations

import unittest

from local_agent.document_artifacts import DocumentArtifactRequirement
from local_agent.explore_handoff import ClaimEvidenceItem, ExploreHandoff
from local_agent.document_consistency import DocumentConsistencyAssessment
from local_agent.implementation_readiness import ImplementationReadinessAssessment
from local_agent.implementation_readiness import ImplementationReadinessDimension
from local_agent.read_only_reviewer import ReviewerFinding
from local_agent.safe_partial_report import build_safe_partial_report
from local_agent.explore_handoff import build_explore_handoff
from local_agent.requirement_evidence import RequirementEvidence
from local_agent.read_only_root_coverage import read_only_root_coverage
from local_agent.steering.final_answer import phantom_tool_evidence_claims
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


def _readiness_handoff() -> ExploreHandoff:
    contract = RequirementContract(
        objective="choose a safe implementation slice",
        scope="read-only evidence roots",
        acceptance_items=["Classify implementation readiness without selecting an unsupported slice."],
        evidence_requirements=["Bind every core implementation dependency to requirement or source evidence."],
        verification_requirements=["Verify the test entry and rollback boundary before implementation."],
        risk_notes=["A nearby module is not necessarily the requested owner."],
        task_kind="read-only",
        evidence_domain="repository_code",
        read_only_review_profile="design",
        document_artifacts=(
            DocumentArtifactRequirement("markdown", "requirements.md", exact=True),
            DocumentArtifactRequirement("html", "prototype.html", exact=True),
            DocumentArtifactRequirement("image", "example.png", exact=True),
        ),
        implementation_readiness_required=True,
    )
    return ExploreHandoff(
        request="read-only readiness",
        contract=contract,
        items=(
            ClaimEvidenceItem(
                "requirement_fact", "read_file", "/workspace/docs/requirements.md",
                "/workspace/docs", "root_local", "ok", "Bounded requirement observation.",
            ),
            ClaimEvidenceItem(
                "requirement_fact", "read_file", "/workspace/docs/prototype.html",
                "/workspace/docs", "root_local", "ok", "Bounded prototype observation.",
            ),
            ClaimEvidenceItem(
                "visual_observation", "inspect_image", "/workspace/docs/example.png",
                "/workspace/docs", "root_local", "ok", "Bounded visual observation.",
            ),
            ClaimEvidenceItem(
                "observed_candidate", "search_code", "/workspace/backend/src",
                "/workspace/backend", "root_local", "ok", "One bounded search hit.",
            ),
            ClaimEvidenceItem(
                "observed_candidate", "read_file", "/workspace/backend/src/Analog.java",
                "/workspace/backend", "root_local", "ok", "One bounded source read.",
            ),
            ClaimEvidenceItem(
                "unlocated", "search_code", "/workspace/frontend/src",
                "/workspace/frontend", "root_local", "no_match", "Bounded search found no direct owner.",
            ),
            ClaimEvidenceItem(
                "observed_candidate", "read_file", "/workspace/frontend/src/RouteShell.ts",
                "/workspace/frontend", "root_local", "ok", "One bounded source read.",
            ),
        ),
    )


class SafePartialReportTests(unittest.TestCase):
    def test_readiness_safe_partial_is_typed_blocked_delivery_without_candidate_text(self) -> None:
        report = build_safe_partial_report(
            _readiness_handoff(),
            (
                ReviewerFinding(
                    "c099",
                    "Concrete identifiers have no source provenance.",
                    "Remove the unsupported implementation selection.",
                    "InventedController uses /api/phantom and phantom_table.phantom_field.",
                    "candidate_defect",
                ),
            ),
            reason="pre_review_audit_unverified",
        )

        self.assertEqual(report.delivery_status, "blocked")
        self.assertEqual(report.termination_reason, "pre_review_audit_unverified")
        self.assertIn("Implementation readiness: BLOCKED", report.content)
        self.assertIn("未选择实施切片", report.content)
        for heading in (
            "为什么不能选择实施切片",
            "已完成调查：需求材料",
            "已完成调查：代码根",
            "已验证文件 / 模块与搜索边界",
            "接口与数据契约状态",
            "验收与测试状态",
            "阻塞项与进入实现前需要的信息",
            "安全边界",
        ):
            self.assertIn(heading, report.content)
        for path in (
            "/workspace/docs/requirements.md",
            "/workspace/docs/prototype.html",
            "/workspace/docs/example.png",
            "/workspace/backend/src/Analog.java",
            "/workspace/frontend/src/RouteShell.ts",
        ):
            self.assertIn(path, report.content)
        self.assertNotIn("root=`/workspace/docs`", report.content)
        self.assertIn("search observation", report.content)
        self.assertIn("不自动证明它是目标 Owner", report.content)
        self.assertIn("不提出 API、DDL、表、字段、状态码或 Owner 名称", report.content)
        for sentinel in ("InventedController", "/api/phantom", "phantom_table", "phantom_field"):
            self.assertNotIn(sentinel, report.content)

    def test_readiness_dimensions_use_typed_assessment_and_keep_missing_core_dependencies(self) -> None:
        dimensions = {
            key: ImplementationReadinessDimension(
                "closed" if key == "owner" else "unlocated",
                ("c001",),
            )
            for key in (
                "owner", "data_contract_or_source", "write_target", "test_entry", "rollback_boundary"
            )
        }
        assessment = ImplementationReadinessAssessment("blocked", dimensions, reason="core dependencies remain open")

        report = build_safe_partial_report(
            _readiness_handoff(),
            reason="invalid_output",
            implementation_readiness=assessment,
        )

        self.assertNotIn("[未闭合] Owner / 调用归属", report.content)
        self.assertIn("[未闭合] 数据契约 / 来源", report.content)
        self.assertIn("[未闭合] 写入目标", report.content)
        self.assertIn("[未闭合] 测试入口", report.content)
        self.assertIn("[未闭合] 回滚边界", report.content)

    def test_source_html_keeps_code_root_role_without_material_contract(self) -> None:
        contract = RequirementContract(
            objective="assess implementation readiness",
            scope="read-only source inspection",
            acceptance_items=["Return a blocked result when dependencies remain open."],
            evidence_requirements=["Use typed source observations."],
            verification_requirements=["Keep source and material roles distinct."],
            risk_notes=[],
            task_kind="read-only",
            evidence_domain="repository_code",
            read_only_review_profile="design",
            implementation_readiness_required=True,
        )
        handoff = ExploreHandoff(
            request="read-only readiness",
            contract=contract,
            items=(
                ClaimEvidenceItem(
                    "observed_candidate",
                    "read_file",
                    "/repo/web/page.html",
                    "/repo/web",
                    "root_local",
                    "ok",
                    "A bounded source read.",
                ),
            ),
        )

        report = build_safe_partial_report(handoff, reason="pre_review_audit_unverified")

        self.assertIn("root=`/repo/web`", report.content)
        self.assertIn("direct_read=`/repo/web/page.html`", report.content)
        material_section = report.content.split("### 已完成调查：代码根", 1)[0]
        self.assertNotIn("/repo/web/page.html", material_section)

    def test_readiness_hard_reasons_all_preserve_typed_blocked_terminal(self) -> None:
        for reason in (
            "invalid_output",
            "pre_review_audit_unverified",
            "deadline_or_finalization_budget",
            "provider_error",
        ):
            with self.subTest(reason=reason):
                report = build_safe_partial_report(_readiness_handoff(), reason=reason)
                self.assertEqual(report.delivery_status, "blocked")
                self.assertEqual(report.termination_reason, reason)
                self.assertIn(f"termination={reason}", report.content)
                self.assertIn(f"hard termination reason 保留为 `{reason}`", report.content)

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

    def test_handoff_projects_root_coverage_for_search_without_direct_read(self) -> None:
        contract = generate_requirement_contract("只读分析服务 owner 和影响范围，不要修改。")
        handoff = build_explore_handoff(
            request="只读分析服务 owner 和影响范围，不要修改。",
            contract=contract,
            requirement_evidence=(),
            source_evidence=(),
            records=(),
            tool_results=(
                ToolResultSummary(
                    "search_code",
                    "No matches.",
                    useless=True,
                    path="/workspace/backend",
                    metadata={"evidence_root": "/workspace/backend", "negative_evidence_type": "content_no_match"},
                ),
            ),
        )

        missing = [
            item
            for item in handoff.items
            if item.classification == "unlocated" and item.tool == "read_only_explore"
        ]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].path, "/workspace/backend")
        self.assertIn("searched/no direct read/unlocated", missing[0].summary)
        self.assertIn("successful_searches=1", missing[0].summary)
        self.assertNotIn("out of scope", missing[0].summary.lower())

    def test_root_coverage_owner_is_shared_by_handoff_and_final_audit(self) -> None:
        results = (
            ToolResultSummary(
                "search_code",
                "No matches.",
                useless=True,
                metadata={"evidence_root": "/workspace/backend", "negative_evidence_type": "content_no_match"},
            ),
            ToolResultSummary(
                "read_file",
                "Tool call was not executed.",
                is_error=True,
                metadata={"evidence_root": "/workspace/backend", "suppressed": True},
            ),
        )
        coverage = read_only_root_coverage(results)
        handoff = build_explore_handoff(
            request="locate owner",
            contract=generate_requirement_contract("locate owner"),
            requirement_evidence=(),
            source_evidence=(),
            records=(),
            tool_results=results,
        )
        handoff_roots = {
            item.root
            for item in handoff.items
            if item.classification == "unlocated" and item.tool == "read_only_explore"
        }

        self.assertEqual(len(coverage), 1)
        self.assertTrue(coverage[0].attempted_without_direct_read)
        self.assertTrue(coverage[0].searched_without_direct_read)
        self.assertEqual(coverage[0].successful_direct_reads, 0)
        self.assertEqual(handoff_roots, {coverage[0].root})
        self.assertEqual(
            phantom_tool_evidence_claims("backend was outside inspection scope.", list(results)),
            (f"read_only_explore:{coverage[0].root}",),
        )

    def test_root_coverage_preserves_identity_and_separates_attempts_from_observations(self) -> None:
        suppressed_backend = (
            ToolResultSummary(
                "search_code",
                "Tool call was not executed.",
                is_error=True,
                useless=True,
                metadata={
                    "evidence_root": "/Workspace/Backend",
                    "negative_evidence_type": "content_no_match",
                    "suppressed": True,
                },
            ),
            ToolResultSummary(
                "glob_files",
                "Denied.",
                is_error=True,
                useless=True,
                metadata={"evidence_root": "/Workspace/Backend", "negative_evidence_type": "content_no_match"},
            ),
        )
        frontend_no_match = (
            ToolResultSummary(
                "search_code",
                "No matches.",
                useless=True,
                metadata={"evidence_root": "/Workspace/Frontend", "negative_evidence_type": "content_no_match"},
            ),
        )
        coverage = read_only_root_coverage(suppressed_backend + frontend_no_match)

        by_root = {item.root: item for item in coverage}
        self.assertIn("/Workspace/Backend", by_root)
        self.assertEqual(by_root["/Workspace/Backend"].search_attempts, 1)
        self.assertEqual(by_root["/Workspace/Backend"].successful_searches, 0)
        self.assertEqual(by_root["/Workspace/Backend"].no_match, 0)
        self.assertEqual(by_root["/Workspace/Backend"].failures, 1)
        self.assertEqual(by_root["/Workspace/Backend"].suppressed, 1)
        self.assertFalse(by_root["/Workspace/Backend"].searched_without_direct_read)
        self.assertEqual(by_root["/Workspace/Frontend"].search_attempts, 1)
        self.assertEqual(by_root["/Workspace/Frontend"].successful_searches, 1)
        self.assertEqual(by_root["/Workspace/Frontend"].no_match, 1)
        self.assertTrue(by_root["/Workspace/Frontend"].searched_without_direct_read)
        self.assertEqual(
            phantom_tool_evidence_claims("backend was outside inspection scope.", list(suppressed_backend)),
            (),
        )

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

        self.assertEqual(report.delivery_status, "unverified")
        self.assertEqual(report.termination_reason, "llm_timeout")
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

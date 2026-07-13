from __future__ import annotations

import unittest

from local_agent.task_contract import generate_requirement_contract
from local_agent.task_contract import render_contract_context


class RequirementContractTests(unittest.TestCase):
    def test_read_only_code_evidence_question_contract(self) -> None:
        contract = generate_requirement_contract(
            "只读代码，帮我确认登录密码是否在后端加密，必须给代码证据，不要修改文件。"
        )

        self.assertEqual(contract.task_kind, "read-only")
        self.assertIn("without modifying files", contract.scope)
        self.assertTrue(any("repository-grounded evidence" in item for item in contract.acceptance_items))
        self.assertTrue(any("file paths" in item for item in contract.evidence_requirements))
        self.assertTrue(any("read/search" in item for item in contract.verification_requirements))

        rendered = render_contract_context(contract)

        self.assertIn("Requirement Contract", rendered)
        self.assertIn("Task kind: read-only", rendered)
        self.assertIn("Evidence:", rendered)
        self.assertIn("Verification:", rendered)

    def test_code_implementation_task_contract(self) -> None:
        contract = generate_requirement_contract("请实现用户注册接口的邮箱唯一性校验，并补充单元测试。")

        self.assertEqual(contract.task_kind, "code-implementation")
        self.assertIn("Code implementation work", contract.scope)
        self.assertTrue(any("smallest practical change" in item for item in contract.acceptance_items))
        self.assertTrue(any("modified files" in item for item in contract.evidence_requirements))
        self.assertTrue(any("test command" in item for item in contract.verification_requirements))

    def test_source_qualified_chinese_fix_is_still_an_implementation_contract(self) -> None:
        contract = generate_requirement_contract("仅根据当前源码修复 README 文档里的一个小问题")

        self.assertEqual(contract.task_kind, "code-implementation")

    def test_implementation_that_mentions_a_read_only_literal_is_not_misclassified(self) -> None:
        contract = generate_requirement_contract(
            "请在任务分类器中添加精确标记‘只读核实’，并补充单元测试，断言 task_kind is read-only。"
        )

        self.assertEqual(contract.task_kind, "code-implementation")

    def test_local_readme_docs_exclusion_does_not_override_explicit_patch_workflow(self) -> None:
        contract = generate_requirement_contract(
            "请自行挑选一个极小、低风险的测试改进。先读源码和测试，随后必须 "
            "apply_patch dry_run=true 预览、apply_patch 真正写入、run_tests、git_diff。"
            "不要修改 README 或 docs。"
        )

        self.assertEqual(contract.task_kind, "code-implementation")

    def test_chinese_status_questions_are_read_only(self) -> None:
        for prompt in (
            "这个功能实现了吗？",
            "当前是否支持批量导出？",
            "已经实现用户注册了吗？",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(generate_requirement_contract(prompt).task_kind, "read-only")

    def test_additional_workspace_inventory_is_not_misclassified_as_add_implementation(self) -> None:
        prompt = (
            "帮我分析下现在的项目代码都有哪些。只读：不要运行 shell、测试或写入。"
            "请覆盖当前 primary 和所有已授权 additional workspace root。"
        )

        self.assertEqual(generate_requirement_contract(prompt).task_kind, "read-only")

    def test_service_fee_settlement_design_contract_is_clarification_first(self) -> None:
        contract = generate_requirement_contract("帮我设计服务费结算需求：下单、退款、商家分账都要考虑。")

        self.assertEqual(contract.task_kind, "unclear")
        self.assertIn("Requirements/design clarification", contract.scope)
        self.assertTrue(any("business goal" in item for item in contract.acceptance_items))
        self.assertTrue(any("assumptions" in item for item in contract.evidence_requirements))
        self.assertTrue(any("rounding" in item for item in contract.verification_requirements))
        self.assertTrue(any("Settlement requirements" in item for item in contract.risk_notes))

    def test_global_forbid_edit_keeps_cross_root_design_read_only(self) -> None:
        contract = generate_requirement_contract(
            "这是一次真实只读需求分析，必须读取需求和前后端代码，输出分阶段实现方案；不得修改文件、"
            "运行测试或写 memory。"
        )

        self.assertEqual(contract.task_kind, "read-only")

    def test_very_short_plain_question_contract_is_unclear(self) -> None:
        contract = generate_requirement_contract("能吗？")

        self.assertEqual(contract.task_kind, "unclear")
        self.assertEqual(contract.objective, "Clarify the user's request: 能吗？")
        self.assertIn("Clarification-first task", contract.scope)
        self.assertTrue(any("too short" in item for item in contract.risk_notes))

    def test_generation_is_deterministic(self) -> None:
        prompt = "请实现导出按钮的 loading 状态，并补充测试。"

        self.assertEqual(generate_requirement_contract(prompt), generate_requirement_contract(prompt))

    def test_semantic_only_no_inspection_contract_does_not_upgrade_quoted_code_words(self) -> None:
        contract = generate_requirement_contract(
            "只解释这些句子的语义，不判断仓库，不检查文件：‘没有 Java 源码’和‘no codebase’分别是什么意思？"
        )

        self.assertEqual(contract.task_kind, "read-only")
        self.assertTrue(contract.inspection_forbidden)
        self.assertIn("Do not inspect", contract.scope)
        self.assertTrue(all("repository-grounded" not in item for item in contract.acceptance_items))

    def test_document_only_requirement_analysis_has_its_own_evidence_domain(self) -> None:
        contract = generate_requirement_contract(
            "只根据需求文档 Markdown、原型 HTML 和示例图分析需求；不要检查代码，也不要推测系统归属。"
        )

        self.assertEqual(contract.task_kind, "read-only")
        self.assertEqual(contract.evidence_domain, "requirement_documents")
        self.assertEqual(contract.read_only_review_profile, "document_consistency")
        self.assertEqual(
            tuple(item.kind for item in contract.document_artifacts),
            ("markdown", "html", "image"),
        )
        self.assertIn("Document-only", contract.scope)
        self.assertTrue(any("document path" in item for item in contract.evidence_requirements))

    def test_single_markdown_document_does_not_require_unrequested_artifacts(self) -> None:
        contract = generate_requirement_contract("只根据 `requirements.md` 分析需求；不要检查代码。")

        self.assertEqual(contract.evidence_domain, "requirement_documents")
        self.assertEqual(contract.read_only_review_profile, "none")
        self.assertEqual([(item.kind, item.reference, item.exact) for item in contract.document_artifacts], [("markdown", "requirements.md", True)])

    def test_natural_language_artifact_mentions_use_modal_coverage_not_greedy_filename_matching(self) -> None:
        contract = generate_requirement_contract(
            "只读完成母需求 S1：分析当前目录中的需求文档-拓展服务费结算V1.3.md、对应 HTML 和结算单示例.png。必须实际检查示例图"
        )

        self.assertEqual(
            [(item.kind, item.reference, item.exact) for item in contract.document_artifacts],
            [("markdown", "markdown", False), ("html", "html", False), ("image", "image", False)],
        )

    def test_unread_image_boundary_does_not_request_image_inspection(self) -> None:
        contract = generate_requirement_contract(
            "只根据需求文档 Markdown 和原型 HTML 分析需求；请区分未读取图片的边界，不要检查代码。"
        )

        self.assertEqual(
            tuple(item.kind for item in contract.document_artifacts),
            ("markdown", "html"),
        )

    def test_code_inspection_boundary_does_not_cancel_requested_document_artifacts(self) -> None:
        contract = generate_requirement_contract(
            "只根据 Markdown、HTML 和示例图分析需求，不要检查代码。"
        )

        self.assertEqual(
            tuple(item.kind for item in contract.document_artifacts),
            ("markdown", "html", "image"),
        )

    def test_quoted_or_pathlike_artifacts_remain_exact_without_cross_binding(self) -> None:
        contract = generate_requirement_contract(
            "只根据 `spec-a.md`、./prototypes/spec-b.html 和 `image-c.png` 分析；不要检查代码。"
        )

        self.assertEqual(
            [(item.kind, item.reference, item.exact) for item in contract.document_artifacts],
            [("markdown", "spec-a.md", True), ("html", "spec-b.html", True), ("image", "image-c.png", True)],
        )

    def test_read_only_reviewer_profile_is_typed_by_contract_owner(self) -> None:
        self.assertEqual(
            generate_requirement_contract("只读分析当前服务 owner、调用链和影响范围，不要修改。").read_only_review_profile,
            "owner_impact",
        )
        self.assertEqual(
            generate_requirement_contract("基于源码给出数据模型和状态流转的实施设计草案。").read_only_review_profile,
            "design",
        )
        self.assertEqual(
            generate_requirement_contract("只根据需求文档 Markdown 分析需求，不要检查代码。").read_only_review_profile,
            "none",
        )

    def test_primary_git_metadata_contract_has_a_dedicated_evidence_owner(self) -> None:
        for prompt in (
            "当前 primary workspace 是不是 Git 仓库？",
            "当前primary是不是Git仓库？",
            "当前 primary 是不是 Git仓库？",
            "确认当前 primary 是不是 Git 仓库",
            "Is the current workspace a Git repository?",
        ):
            with self.subTest(prompt=prompt):
                contract = generate_requirement_contract(prompt)
                self.assertEqual(contract.task_kind, "read-only")
                self.assertEqual(contract.workspace_metadata_subject, "git_repository")
                self.assertIn("git_status", contract.scope)

    def test_git_metadata_contract_does_not_capture_implementation_design_or_explanation(self) -> None:
        cases = (
            ("请实现一个检查当前目录是否为 Git 仓库的函数，并补测试。", "code-implementation"),
            ("请新增 Git repository 检测能力。", "code-implementation"),
            ("请设计 Git repository 检测方案。", "unclear"),
            ("解释 Git repository 的概念。", "read-only"),
            ("当前目录的 Git 仓库检测函数如何实现？", "read-only"),
            ("请分析当前 workspace 的 Git repository 检测逻辑如何实现？", "read-only"),
            ("当前目录为何不是 Git 仓库？", "read-only"),
            ("How should the current directory Git repository detection function be implemented?", "read-only"),
            ("Why is the current workspace not a Git repository?", "read-only"),
        )
        for prompt, expected_kind in cases:
            with self.subTest(prompt=prompt):
                contract = generate_requirement_contract(prompt)
                self.assertEqual(contract.task_kind, expected_kind)
                self.assertIsNone(contract.workspace_metadata_subject)


if __name__ == "__main__":
    unittest.main()

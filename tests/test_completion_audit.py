from __future__ import annotations

import unittest

from local_agent.completion_audit import audit_completion
from local_agent.requirement_evidence import RequirementEvidence
from local_agent.task_contract import generate_requirement_contract
from local_agent.test_planner import TestPlan
from local_agent.tool_choice_queue import ToolResultSummary
from local_agent.verification_plan import VerificationPlan


class CompletionAuditTests(unittest.TestCase):
    def test_document_only_requirement_analysis_accepts_document_facts_without_code_inference_labels(self) -> None:
        request = "只根据需求文档 Markdown 分析需求；不要检查代码，也不要推测系统归属。"
        requirement = RequirementEvidence(
            path="requirements.md",
            content="第 12 行：有效结算单为未回退的结算单。",
            root="primary",
        )
        result = audit_completion(
            generate_requirement_contract(request),
            request=request,
            final_content=(
                "需求事实：有效结算单定义为未回退的结算单（requirements.md:12）。"
                "本结论不判断当前系统归属。"
            ),
            tool_results=[ToolResultSummary("read_file", "有效结算单", path="requirements.md")],
            source_paths=["requirements.md"],
            open_todos=[],
            requirement_evidence=[requirement],
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.allowed_tool_names(), ())

    def test_document_only_requirement_analysis_requires_a_document_reference(self) -> None:
        request = "只根据需求文档 Markdown 分析需求；不要检查代码，也不要推测系统归属。"
        result = audit_completion(
            generate_requirement_contract(request),
            request=request,
            final_content="有效结算单定义为未回退的结算单；本结论不判断系统归属。",
            tool_results=[ToolResultSummary("read_file", "有效结算单", path="requirements.md")],
            source_paths=["requirements.md"],
            open_todos=[],
            requirement_evidence=[RequirementEvidence(path="requirements.md", content="有效结算单")],
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("document path" in item.reason for item in result.missing_items))

    def test_document_only_requirement_analysis_rejects_a_code_inspection_detour(self) -> None:
        request = "只根据需求文档 Markdown 分析需求；不要检查代码，也不要推测系统归属。"
        result = audit_completion(
            generate_requirement_contract(request),
            request=request,
            final_content="需求事实：有效结算单为未回退的结算单（requirements.md:1）。",
            tool_results=[
                ToolResultSummary("read_file", "有效结算单", path="requirements.md"),
                ToolResultSummary("search_code", "src/Service.java:1: class Service", path="src/Service.java"),
            ],
            source_paths=["requirements.md"],
            open_todos=[],
            requirement_evidence=[RequirementEvidence(path="requirements.md", content="有效结算单")],
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("repository/code inspection" in item.reason for item in result.missing_items))

    def test_image_metadata_read_does_not_count_as_a_visual_observation(self) -> None:
        request = "只根据需求文档和示例图分析需求；不要检查代码。"
        result = audit_completion(
            generate_requirement_contract(request),
            request=request,
            final_content="示例图展示了结算字段（example.png:1）。",
            tool_results=[
                ToolResultSummary(
                    "read_file",
                    "Image file metadata: example.png (image/png, 1024 bytes). Use inspect_image.",
                    path="example.png",
                    metadata={"image_metadata": True},
                )
            ],
            source_paths=["example.png"],
            open_todos=[],
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("Markdown/HTML" in item.reason for item in result.missing_items))

    def test_successful_image_inspection_is_a_document_domain_observation(self) -> None:
        request = "只根据需求文档和示例图分析需求；不要检查代码。"
        result = audit_completion(
            generate_requirement_contract(request),
            request=request,
            final_content="图片观察：example.png 显示结算字段；本结论不判断系统归属。",
            tool_results=[
                ToolResultSummary(
                    "inspect_image",
                    "[image observation: example.png#tag] Visible settlement fields.",
                    path="example.png",
                    metadata={"image_observation": True},
                )
            ],
            source_paths=["example.png"],
            open_todos=[],
        )

        self.assertTrue(result.passed)

    def test_document_artifact_coverage_requires_html_when_requested_alongside_markdown_and_image(self) -> None:
        request = "只根据 Markdown、HTML 和示例图分析需求；不要检查代码，也不要推测系统归属。"
        contract = generate_requirement_contract(request)
        partial = audit_completion(
            contract,
            request=request,
            final_content="需求事实：requirements.md:1 说明状态规则；example.png 已观察；不判断系统归属。",
            tool_results=[
                ToolResultSummary("read_file", "状态规则", path="requirements.md"),
                ToolResultSummary("inspect_image", "[image observation]", path="example.png", metadata={"image_observation": True}),
            ],
            source_paths=["requirements.md", "example.png"],
            open_todos=[],
            requirement_evidence=[RequirementEvidence(path="requirements.md", content="状态规则")],
        )

        self.assertFalse(partial.passed)
        self.assertTrue(any("html" in item.reason for item in partial.missing_items))
        self.assertIn("read_file", partial.allowed_tool_names())

        complete = audit_completion(
            contract,
            request=request,
            final_content="需求事实：requirements.md:1 说明状态规则；prototype.html:1 展示原型；example.png 已观察；不判断系统归属。",
            tool_results=[
                ToolResultSummary("read_file", "状态规则", path="requirements.md"),
                ToolResultSummary("read_file", "原型", path="prototype.html"),
                ToolResultSummary("inspect_image", "[image observation]", path="example.png", metadata={"image_observation": True}),
            ],
            source_paths=["requirements.md", "prototype.html", "example.png"],
            open_todos=[],
            requirement_evidence=[RequirementEvidence(path="requirements.md", content="状态规则")],
        )
        self.assertTrue(complete.passed)

    def test_typed_unavailable_image_accepts_artifact_bound_unavailable_status(self) -> None:
        request = "只根据 Markdown、HTML 和示例图分析需求；不要检查代码。"
        result = audit_completion(
            generate_requirement_contract(request),
            request=request,
            final_content="requirements.md:1 是需求事实；prototype.html:1 是原型观察；图片当前不可用，无法检查其中内容。",
            tool_results=[
                ToolResultSummary("read_file", "规则", path="requirements.md"),
                ToolResultSummary("read_file", "原型", path="prototype.html"),
                ToolResultSummary(
                    "inspect_image",
                    "image inspection unavailable",
                    path="example.png",
                    is_error=True,
                    metadata={"image_inspection_unavailable": True},
                ),
            ],
            source_paths=["requirements.md", "prototype.html"],
            open_todos=[],
            requirement_evidence=[RequirementEvidence(path="requirements.md", content="规则")],
        )

        self.assertTrue(result.passed)

    def test_suppressed_tool_errors_do_not_satisfy_code_evidence(self) -> None:
        request = "只读代码，请根据源码说明登录校验位置。不要修改文件。"
        result = audit_completion(
            generate_requirement_contract(request),
            request=request,
            final_content="登录校验在后端。",
            tool_results=[
                ToolResultSummary(
                    "read_file",
                    "Tool call was not executed because the bounded exploration phase ended.",
                    is_error=True,
                    path="src/LoginController.java",
                ),
                ToolResultSummary(
                    "search_code",
                    "Tool call was not executed because the bounded exploration phase ended.",
                    is_error=True,
                    path="src/LoginController.java",
                ),
            ],
            source_paths=["src/LoginController.java"],
            open_todos=[],
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("no successful" in item.reason for item in result.missing_items))

    def test_read_only_answer_requires_traceable_evidence_and_status_labels(self) -> None:
        contract = generate_requirement_contract("只读代码，请根据源码证据说明登录密码在哪里校验。不要修改文件。")

        result = audit_completion(
            contract,
            request="只读代码，请根据源码证据说明登录密码在哪里校验。不要修改文件。",
            final_content="密码在后端校验。",
            tool_results=[ToolResultSummary("read_file", "class LoginController {}", path="src/LoginController.java")],
            source_paths=["src/LoginController.java"],
            open_todos=[],
        )

        self.assertFalse(result.passed)
        missing_requirements = {item.requirement for item in result.missing_items}
        self.assertTrue(any("repository-grounded evidence" in item for item in missing_requirements))
        self.assertTrue(any("verified facts" in item for item in missing_requirements))
        self.assertEqual(result.allowed_tool_names(), ())

    def test_read_only_answer_passes_when_evidence_status_and_path_are_present(self) -> None:
        contract = generate_requirement_contract("只读代码，请根据源码证据说明登录密码在哪里校验。不要修改文件。")

        result = audit_completion(
            contract,
            request="只读代码，请根据源码证据说明登录密码在哪里校验。不要修改文件。",
            final_content="已验证：src/LoginController.java 调用 PasswordUtil.check。推断：前端加密方式未在已读代码中确认。",
            tool_results=[ToolResultSummary("read_file", "class LoginController {}", path="src/LoginController.java")],
            source_paths=["src/LoginController.java"],
            open_todos=[],
        )

        self.assertTrue(result.passed)

    def test_bare_evidence_word_does_not_satisfy_structured_status_labels(self) -> None:
        contract = generate_requirement_contract(
            "只读代码，请根据源码证据说明登录密码在哪里校验，并按需求事实、源码事实、设计建议、待确认标注。不要修改文件。"
        )
        result = audit_completion(
            contract,
            request=(
                "只读代码，请根据源码证据说明登录密码在哪里校验，"
                "并按需求事实、源码事实、设计建议、待确认标注。不要修改文件。"
            ),
            final_content="src/LoginController.java: 建议查看 evidence，密码校验可能在后端。",
            tool_results=[ToolResultSummary("read_file", "class LoginController {}", path="src/LoginController.java")],
            source_paths=["src/LoginController.java"],
            open_todos=[],
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("verified facts" in item.reason for item in result.missing_items))

    def test_explicit_evidence_status_categories_satisfy_structured_status_labels(self) -> None:
        contract = generate_requirement_contract(
            "只读代码，请根据源码证据说明登录密码在哪里校验，并按需求事实、源码事实、设计建议、待确认标注。不要修改文件。"
        )
        result = audit_completion(
            contract,
            request=(
                "只读代码，请根据源码证据说明登录密码在哪里校验，"
                "并按需求事实、源码事实、设计建议、待确认标注。不要修改文件。"
            ),
            final_content=(
                "需求事实：用户要求只读分析。\n"
                "源码事实：src/LoginController.java:1 包含 LoginController。\n"
                "设计建议：如需确认调用链，应继续定位 PasswordUtil。\n"
                "待确认：前端加密方式未在已读代码中确认。"
            ),
            tool_results=[ToolResultSummary("read_file", "class LoginController {}", path="src/LoginController.java")],
            source_paths=["src/LoginController.java"],
            open_todos=[],
        )

        self.assertTrue(result.passed)

    def test_semantic_only_no_inspection_task_does_not_reopen_code_evidence(self) -> None:
        request = "只解释这句话的语义，不判断仓库，不检查文件：‘没有 Java 源码’是什么意思？"
        result = audit_completion(
            generate_requirement_contract(request),
            request=request,
            final_content="这句话表达的是一个缺失断言；未检查仓库，因此不把它当作已验证的仓库事实。",
            tool_results=[],
            source_paths=[],
            open_todos=[],
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.allowed_tool_names(), ())

    def test_semantic_only_task_requires_an_actual_explanation_and_marks_requested_repository_facts_unverified(self) -> None:
        request = "只解释句子语义，不检查文件；同时告诉我当前仓库有没有 Java。"
        contract = generate_requirement_contract(request)
        empty = audit_completion(
            contract,
            request=request,
            final_content="",
            tool_results=[],
            source_paths=[],
            open_todos=[],
        )
        unsupported = audit_completion(
            contract,
            request=request,
            final_content="这句话表示 Java 缺失；当前仓库是否有 Java 我已经确认。",
            tool_results=[],
            source_paths=[],
            open_todos=[],
        )
        explicit = audit_completion(
            contract,
            request=request,
            final_content="这句话表达 Java 缺失；未检查仓库，因此当前仓库是否有 Java 未验证。",
            tool_results=[],
            source_paths=[],
            open_todos=[],
        )

        self.assertFalse(empty.passed)
        self.assertFalse(unsupported.passed)
        self.assertTrue(explicit.passed)

    def test_primary_non_repository_git_probe_is_sufficient_metadata_evidence(self) -> None:
        request = "当前 primary workspace 是不是 Git 仓库？"
        result = audit_completion(
            generate_requirement_contract(request),
            request=request,
            final_content="已验证：当前 primary workspace 不是 Git 仓库；附加 root 需要先 /move 才能判断。",
            tool_results=[
                ToolResultSummary(
                    "git_status",
                    "not a git repository",
                    is_error=True,
                    metadata={
                        "git_repository": False,
                        "git_probe_root": "/tmp/primary",
                        "evidence_root_label": "primary",
                    },
                )
            ],
            source_paths=[],
            open_todos=[],
        )

        self.assertTrue(result.passed)

    def test_git_metadata_conclusion_ignores_unresolved_other_root_questions(self) -> None:
        request = "当前primary是不是Git仓库？"
        result = audit_completion(
            generate_requirement_contract(request),
            request=request,
            final_content=(
                "已验证：**当前 primary 工作区 `/private/tmp/lca-gapfill/primary` 不是 Git 仓库**。\n"
                "当前 primary 工作区 **不是Git仓库**；其他路径需要检查是否为 Git 仓库，本次未检查。"
            ),
            tool_results=[
                ToolResultSummary(
                    "git_status",
                    "not a git repository",
                    is_error=True,
                    metadata={
                        "git_repository": False,
                        "git_probe_root": "/tmp/primary",
                        "evidence_root_label": "primary",
                    },
                )
            ],
            source_paths=[],
            open_todos=[],
        )

        self.assertTrue(result.passed)

    def test_git_metadata_conclusion_accepts_primary_workspace_synonym_only_for_matching_probe(self) -> None:
        request = "当前primary是不是Git仓库？"
        final_content = (
            "**不是**，当前 primary 工作空间 **不是 Git 仓库**。\n"
            "证据：git_status 返回 non-repository。此结论仅适用于 primary 工作空间。"
            "若存在其他工作空间根（secondary roots），需切换（/move）后单独检查。"
        )
        primary_false = ToolResultSummary(
            "git_status",
            "not a git repository",
            is_error=True,
            metadata={
                "git_repository": False,
                "git_probe_root": "/tmp/primary",
                "evidence_root_label": "primary",
            },
        )
        primary_true = ToolResultSummary(
            "git_status",
            "clean",
            metadata={
                "git_repository": True,
                "git_probe_root": "/tmp/primary",
                "evidence_root_label": "primary",
            },
        )

        self.assertTrue(
            audit_completion(
                generate_requirement_contract(request),
                request=request,
                final_content=final_content,
                tool_results=[primary_false],
                source_paths=[],
                open_todos=[],
            ).passed
        )
        self.assertFalse(
            audit_completion(
                generate_requirement_contract(request),
                request=request,
                final_content=final_content,
                tool_results=[primary_true],
                source_paths=[],
                open_todos=[],
            ).passed
        )

    def test_git_metadata_conclusion_requires_one_primary_declarative_polarity(self) -> None:
        request = "当前primary是不是Git仓库？"
        probe = ToolResultSummary(
            "git_status",
            "not a git repository",
            is_error=True,
            metadata={
                "git_repository": False,
                "git_probe_root": "/tmp/primary",
                "evidence_root_label": "primary",
            },
        )

        self.assertFalse(
            audit_completion(
                generate_requirement_contract(request),
                request=request,
                final_content="当前primary不是Git仓库；当前 primary 是 Git 仓库。",
                tool_results=[probe],
                source_paths=[],
                open_todos=[],
            ).passed
        )
        self.assertTrue(
            audit_completion(
                generate_requirement_contract(request),
                request=request,
                final_content="Verified: the current workspace is not a Git repository.",
                tool_results=[probe],
                source_paths=[],
                open_todos=[],
            ).passed
        )

    def test_git_metadata_conclusion_handles_inline_path_for_both_polarities(self) -> None:
        request = "当前primary是不是Git仓库？"
        primary_true = ToolResultSummary(
            "git_status",
            "clean",
            metadata={
                "git_repository": True,
                "git_probe_root": "/tmp/primary",
                "evidence_root_label": "primary",
            },
        )
        primary_false = ToolResultSummary(
            "git_status",
            "not a git repository",
            is_error=True,
            metadata={
                "git_repository": False,
                "git_probe_root": "/tmp/primary",
                "evidence_root_label": "primary",
            },
        )

        self.assertTrue(
            audit_completion(
                generate_requirement_contract(request),
                request=request,
                final_content="**当前 primary 工作区 `/tmp/primary` 是 Git 仓库**。",
                tool_results=[primary_true],
                source_paths=[],
                open_todos=[],
            ).passed
        )
        self.assertFalse(
            audit_completion(
                generate_requirement_contract(request),
                request=request,
                final_content="**当前 primary 工作区 `/tmp/primary` 不是 Git 仓库**；当前primary是Git仓库。",
                tool_results=[primary_false],
                source_paths=[],
                open_todos=[],
            ).passed
        )

    def test_git_metadata_probe_requires_primary_provenance_and_matching_conclusion(self) -> None:
        request = "当前 primary workspace 是不是 Git 仓库？"
        contract = generate_requirement_contract(request)
        primary_false = ToolResultSummary(
            "git_status",
            "not a git repository",
            is_error=True,
            metadata={"git_repository": False, "git_probe_root": "/tmp/primary", "evidence_root_label": "primary"},
        )
        wrong_root = ToolResultSummary(
            "git_status",
            "not a git repository",
            is_error=True,
            metadata={"git_repository": False, "git_probe_root": "/tmp/add", "evidence_root_label": "additional"},
        )
        missing_label = ToolResultSummary(
            "git_status",
            "not a git repository",
            is_error=True,
            metadata={"git_repository": False, "git_probe_root": "/tmp/primary"},
        )
        positive = ToolResultSummary(
            "git_status",
            "clean",
            metadata={"git_repository": True, "git_probe_root": "/tmp/primary", "evidence_root_label": "primary"},
        )

        self.assertFalse(
            audit_completion(
                contract,
                request=request,
                final_content="已验证：当前 primary workspace 是 Git 仓库。",
                tool_results=[primary_false],
                source_paths=[],
                open_todos=[],
            ).passed
        )
        self.assertFalse(
            audit_completion(
                contract,
                request=request,
                final_content="已验证：当前 primary workspace 不是 Git 仓库。",
                tool_results=[wrong_root],
                source_paths=[],
                open_todos=[],
            ).passed
        )
        self.assertFalse(
            audit_completion(
                contract,
                request=request,
                final_content="已验证：当前 primary workspace 不是 Git 仓库。",
                tool_results=[missing_label],
                source_paths=[],
                open_todos=[],
            ).passed
        )
        self.assertFalse(
            audit_completion(
                contract,
                request=request,
                final_content="已验证：当前 primary workspace 不是 Git 仓库。",
                tool_results=[positive],
                source_paths=[],
                open_todos=[],
            ).passed
        )
        self.assertTrue(
            audit_completion(
                contract,
                request=request,
                final_content="已验证：当前 primary workspace 是 Git 仓库。",
                tool_results=[positive],
                source_paths=[],
                open_todos=[],
            ).passed
        )

    def test_generic_git_error_is_not_accepted_as_repository_metadata_evidence(self) -> None:
        request = "当前 primary workspace 是不是 Git 仓库？"
        result = audit_completion(
            generate_requirement_contract(request),
            request=request,
            final_content="未验证：git 命令执行失败。",
            tool_results=[
                ToolResultSummary(
                    "git_status",
                    "git executable failed",
                    is_error=True,
                    metadata={"git_repository": None, "git_probe_root": "/tmp/primary", "evidence_root_label": "primary"},
                )
            ],
            source_paths=[],
            open_todos=[],
        )

        self.assertFalse(result.passed)

    def test_implementation_answer_requires_tests_and_diff_after_write(self) -> None:
        contract = generate_requirement_contract("请实现用户注册接口的邮箱唯一性校验，并补充单元测试。")

        result = audit_completion(
            contract,
            request="请实现用户注册接口的邮箱唯一性校验，并补充单元测试。",
            final_content="已完成用户注册接口邮箱唯一性校验。",
            tool_results=[
                ToolResultSummary("read_file", "class UserService {}", path="src/UserService.java"),
                ToolResultSummary("apply_patch", "Applied patch. Patch id: p1", changed=True),
            ],
            source_paths=["src/UserService.java"],
            open_todos=[],
        )

        self.assertFalse(result.passed)
        allowed = set(result.allowed_tool_names())
        self.assertIn("run_tests", allowed)
        self.assertIn("git_diff", allowed)

    def test_implementation_answer_passes_with_tests_diff_and_modified_file_summary(self) -> None:
        contract = generate_requirement_contract("请实现用户注册接口的邮箱唯一性校验，并补充单元测试。")

        result = audit_completion(
            contract,
            request="请实现用户注册接口的邮箱唯一性校验，并补充单元测试。",
            final_content=(
                "已修改：src/UserService.java 增加邮箱唯一性校验。\n"
                "验证：run_tests 通过，git_diff 已检查。"
            ),
            tool_results=[
                ToolResultSummary("read_file", "class UserService {}", path="src/UserService.java"),
                ToolResultSummary("apply_patch", "Applied patch. Patch id: p1", changed=True),
                ToolResultSummary("run_tests", "OK"),
                ToolResultSummary("git_diff", "diff --git a/src/UserService.java b/src/UserService.java"),
            ],
            source_paths=["src/UserService.java"],
            open_todos=[],
        )

        self.assertTrue(result.passed)

    def test_implementation_verification_must_follow_the_last_workspace_write(self) -> None:
        request = "请实现用户注册接口的邮箱唯一性校验，并补充单元测试。"
        result = audit_completion(
            generate_requirement_contract(request),
            request=request,
            final_content="已修改：src/UserService.java。验证：测试和 diff 已完成。",
            tool_results=[
                ToolResultSummary("apply_patch", "Applied first patch", changed=True),
                ToolResultSummary("run_tests", "OK"),
                ToolResultSummary("git_diff", "diff --git a/src/UserService.java b/src/UserService.java"),
                ToolResultSummary("apply_patch", "Applied final patch", changed=True),
            ],
            source_paths=["src/UserService.java"],
            open_todos=[],
        )

        self.assertFalse(result.passed)
        allowed = set(result.allowed_tool_names())
        self.assertIn("git_diff", allowed)
        self.assertIn("run_tests", allowed)

    def test_runtime_plan_gates_delivery_checks_but_not_unverified_business_items(self) -> None:
        request = "请实现用户注册接口的邮箱唯一性校验，并补充单元测试。"
        contract = generate_requirement_contract(request)
        plan = VerificationPlan.from_contract(contract)
        results = [
            ToolResultSummary("read_file", "class UserService {}", path="src/UserService.java"),
            ToolResultSummary("apply_patch", "Applied patch", changed=True, path="src/UserService.java"),
            ToolResultSummary("run_tests", "OK"),
            ToolResultSummary("git_diff", "diff --git a/src/UserService.java b/src/UserService.java"),
        ]
        plan.observe(results, test_plan=TestPlan("mvn test", "project fallback", "project"))
        plan.record_patch_review(passed=True, reason="review passed", refs=["git_diff:post-write"])

        result = audit_completion(
            contract,
            request=request,
            final_content="已完成实现。",
            tool_results=results,
            source_paths=["src/UserService.java"],
            open_todos=[],
            verification_plan=plan,
        )

        self.assertTrue(result.passed)
        self.assertTrue(all("business-level" not in item.reason for item in result.items))

    def test_blocked_runtime_check_is_not_a_successful_delivery(self) -> None:
        request = "请实现用户注册接口的邮箱唯一性校验，并补充单元测试。"
        contract = generate_requirement_contract(request)
        plan = VerificationPlan.from_contract(contract)
        results = [
            ToolResultSummary("read_file", "class UserService {}", path="src/UserService.java"),
            ToolResultSummary("apply_patch", "Applied patch", changed=True, path="src/UserService.java"),
            ToolResultSummary("git_diff", "diff --git a/src/UserService.java b/src/UserService.java"),
            ToolResultSummary(
                "run_tests",
                "denied",
                is_error=True,
                metadata={"execution_status": "denied", "denial_kind": "approval"},
            ),
        ]
        plan.observe(results, test_plan=TestPlan("mvn test", "project fallback", "project"))
        plan.record_patch_review(passed=True, reason="review passed", refs=["git_diff:post-write"])

        result = audit_completion(
            contract,
            request=request,
            final_content="测试被拒绝，交付未完成。",
            tool_results=results,
            source_paths=["src/UserService.java"],
            open_todos=[],
            verification_plan=plan,
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("runtime-backed blocked" in item.reason for item in result.missing_items))
        self.assertEqual(result.allowed_tool_names(), ())

    def test_blocked_no_edit_claim_requires_tool_observed_blocking_evidence(self) -> None:
        contract = generate_requirement_contract("请实现用户注册接口的邮箱唯一性校验，并补充单元测试。")

        result = audit_completion(
            contract,
            request="请实现用户注册接口的邮箱唯一性校验，并补充单元测试。",
            final_content="任务 blocked：当前不做修改，也没有需要提交的文件。",
            tool_results=[
                ToolResultSummary("read_file", "class UserService {}", path="src/UserService.java"),
                ToolResultSummary("git_diff", "(empty)"),
            ],
            source_paths=["src/UserService.java"],
            open_todos=[],
        )

        self.assertFalse(result.passed)
        missing = result.missing_items[0]
        self.assertEqual(missing.category, "acceptance")
        self.assertIn("no tool result records", missing.reason)
        self.assertIn("apply_patch", result.allowed_tool_names())

    def test_blocked_no_edit_can_report_a_missing_search_term_without_claiming_the_project_is_absent(self) -> None:
        contract = generate_requirement_contract("请实现用户注册接口的邮箱唯一性校验，并补充单元测试。")

        result = audit_completion(
            contract,
            request="请实现用户注册接口的邮箱唯一性校验，并补充单元测试。",
            final_content=(
                "已验证：search_code 未找到 UserRegistrationService 文本，目标服务是否存在仍未确认；"
                "任务 blocked，未修改文件，也未运行测试。"
            ),
            tool_results=[
                ToolResultSummary("search_code", "(no matches)", useless=True),
                ToolResultSummary("git_diff", "(empty)"),
            ],
            source_paths=[],
            open_todos=[],
        )

        self.assertTrue(result.passed)

    def test_content_search_no_match_cannot_prove_java_files_are_absent(self) -> None:
        request = "只读分析当前仓库有哪些 Java 代码，不要修改文件。"
        result = audit_completion(
            generate_requirement_contract(request),
            request=request,
            final_content="已验证：search_code 未找到 \\.java$，因此当前仓库没有 Java 文件。推断：无需继续检查。",
            tool_results=[
                ToolResultSummary(
                    "search_code",
                    "No matches.",
                    useless=True,
                    metadata={
                        "negative_evidence_type": "content_no_match",
                        "pattern": r"\\.java$",
                        "complete": True,
                    },
                )
            ],
            source_paths=[],
            open_todos=[],
        )

        self.assertFalse(result.passed)
        self.assertIn("glob_files", result.allowed_tool_names())

    def test_truncated_directory_listing_cannot_prove_src_is_absent(self) -> None:
        request = "只读确认 src 是否存在，不要修改文件。"
        result = audit_completion(
            generate_requirement_contract(request),
            request=request,
            final_content="已验证：目录列表没有展示 src，因此 src 不存在。推断：无需继续检查。",
            tool_results=[
                ToolResultSummary(
                    "list_files",
                    "... truncated after 30 files",
                    metadata={
                        "negative_evidence_type": "incomplete",
                        "truncated": True,
                        "complete": False,
                    },
                )
            ],
            source_paths=[],
            open_todos=[],
        )

        self.assertFalse(result.passed)
        self.assertIn("glob_files", result.allowed_tool_names())

    def test_complete_glob_no_match_can_support_java_file_absence(self) -> None:
        request = "只读确认当前仓库是否有 Java 文件，不要修改文件。"
        result = audit_completion(
            generate_requirement_contract(request),
            request=request,
            final_content="已验证：glob_files 在当前 workspace 的 **/*.java 完整扫描未发现 Java 文件。推断：当前范围内没有 Java 源码。",
            tool_results=[
                ToolResultSummary(
                    "glob_files",
                    "{...}",
                    useless=True,
                    metadata={
                        "negative_evidence_type": "path_no_match",
                        "patterns": ["**/*.java"],
                        "complete": True,
                        "truncated": False,
                        "result_limit_reached": False,
                        "evidence_root_label": "primary",
                    },
                )
            ],
            source_paths=[],
            open_todos=[],
        )

        self.assertTrue(result.passed)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from local_agent.completion_audit import audit_completion
from local_agent.task_contract import generate_requirement_contract
from local_agent.tool_choice_queue import ToolResultSummary


class CompletionAuditTests(unittest.TestCase):
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
                    },
                )
            ],
            source_paths=[],
            open_todos=[],
        )

        self.assertTrue(result.passed)


if __name__ == "__main__":
    unittest.main()

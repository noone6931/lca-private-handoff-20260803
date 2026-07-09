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


if __name__ == "__main__":
    unittest.main()

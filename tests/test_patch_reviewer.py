from __future__ import annotations

import unittest

from local_agent.patch_reviewer import review_input_summary
from local_agent.patch_reviewer import review_patch
from local_agent.task_contract import generate_requirement_contract
from local_agent.tool_choice_queue import ToolResultSummary


def _java_diff(*, test_changed: bool = False, public_api: bool = False) -> str:
    method = "public String normalize(String value)" if public_api else "private String normalize(String value)"
    diff = (
        "diff --git a/src/UserService.java b/src/UserService.java\n"
        "--- a/src/UserService.java\n"
        "+++ b/src/UserService.java\n"
        "@@ -1,3 +1,3 @@\n"
        "-    private String normalize(String value) { return value; }\n"
        f"+    {method} {{ return value == null ? \"\" : value.trim(); }}\n"
    )
    if test_changed:
        diff += (
            "diff --git a/src/test/UserServiceTest.java b/src/test/UserServiceTest.java\n"
            "--- a/src/test/UserServiceTest.java\n"
            "+++ b/src/test/UserServiceTest.java\n"
            "@@ -1,2 +1,3 @@\n"
            "+    @Test void normalizesWhitespace() {}\n"
        )
    return diff + "\n[diff summary]\n- Total: 1 file(s), +1 -1, 1 hunk(s).\n"


class PatchReviewerTests(unittest.TestCase):
    def test_requested_tests_must_appear_in_reviewed_diff(self) -> None:
        contract = generate_requirement_contract("请实现用户名规范化，并补充单元测试。")
        result = review_patch(
            contract,
            request="请实现用户名规范化，并补充单元测试。",
            tool_results=[
                ToolResultSummary("apply_patch", "Applied patch", path="src/UserService.java", changed=True),
                ToolResultSummary("git_diff", _java_diff()),
            ],
        )

        self.assertFalse(result.passed)
        self.assertIn("requested_test_missing", {finding.code for finding in result.findings})
        self.assertIn("apply_patch", result.allowed_tool_names())

    def test_requested_tests_pass_when_test_diff_exists(self) -> None:
        contract = generate_requirement_contract("请实现用户名规范化，并补充单元测试。")
        result = review_patch(
            contract,
            request="请实现用户名规范化，并补充单元测试。",
            tool_results=[
                ToolResultSummary("apply_patch", "Applied patch", path="src/UserService.java", changed=True),
                ToolResultSummary("git_diff", _java_diff(test_changed=True)),
            ],
        )

        self.assertTrue(result.passed)

    def test_public_api_change_requires_post_write_call_site_evidence(self) -> None:
        contract = generate_requirement_contract("请实现用户名规范化功能。")
        result = review_patch(
            contract,
            request="请实现用户名规范化功能。",
            tool_results=[
                ToolResultSummary("apply_patch", "Applied patch", path="src/UserService.java", changed=True),
                ToolResultSummary("git_diff", _java_diff(public_api=True)),
            ],
        )

        self.assertIn("call_site_review_missing", {finding.code for finding in result.findings})

    def test_post_write_reference_search_satisfies_public_api_review(self) -> None:
        contract = generate_requirement_contract("请实现用户名规范化功能。")
        result = review_patch(
            contract,
            request="请实现用户名规范化功能。",
            tool_results=[
                ToolResultSummary("apply_patch", "Applied patch", path="src/UserService.java", changed=True),
                ToolResultSummary("search_code", "src/UserController.java: userService.normalize(name)"),
                ToolResultSummary("git_diff", _java_diff(public_api=True)),
            ],
        )

        self.assertTrue(result.passed)

    def test_diff_reviewer_warning_becomes_runtime_review_finding(self) -> None:
        contract = generate_requirement_contract("请实现导入校验功能。")
        diff = _java_diff() + "\n[diff reviewer]\n- Potential implementation-quality warning: comment/documentation-only\n"
        result = review_patch(
            contract,
            request="请实现导入校验功能。",
            tool_results=[
                ToolResultSummary("apply_patch", "Applied patch", path="src/UserService.java", changed=True),
                ToolResultSummary("git_diff", diff),
            ],
        )

        self.assertIn("comment_only_implementation", {finding.code for finding in result.findings})
        self.assertIn("rollback_patch", result.allowed_tool_names())

    def test_large_git_diff_summary_preserves_review_sections(self) -> None:
        raw = "diff --git a/src/App.java b/src/App.java\n" + ("+value\n" * 2000)
        content = raw + "\n[diff summary]\n- Total: 1 file(s).\n\n[diff reviewer]\n- Potential relevance warning\n"

        result = review_input_summary("git_diff", content, max_chars=800)

        self.assertIn("[diff summary]", result)
        self.assertIn("[diff reviewer]", result)
        self.assertLessEqual(len(result), 800)


if __name__ == "__main__":
    unittest.main()

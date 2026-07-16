from __future__ import annotations

import unittest

from local_agent.patch_reviewer import review_input_summary
from local_agent.patch_reviewer import review_input_metadata
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
    TEST_CHANGE_REQUESTS = (
        "Write tests for the new parser.",
        "Add a unit test for this boundary.",
        "Please add tests and make them pass.",
        "Increase test coverage for the cache fix.",
        "请补充单元测试。",
        "请新增测试用例。",
        "修复登录功能，并加一个测试。",
        "Add tests for the bug; do not rewrite unrelated existing tests.",
        "Add tests, but do not rewrite existing tests.",
        "Do not rewrite existing tests; add a regression test.",
        "请补充边界测试，但不要修改其他现有测试。",
    )
    NO_TEST_CHANGE_REQUESTS = (
        "Fix the boundary bug so the existing tests pass. Preserve behavior. Do not rewrite tests.",
        "Do not change tests.",
        "Do not write tests.",
        "Without modifying tests, make the existing tests pass.",
        "Run the unit tests.",
        "Existing tests must pass.",
        "Tests should pass after the fix.",
        "不要修改测试。",
        "不要重写测试。",
        "让现有测试通过。",
        "运行单元测试并确认通过。",
        "测试必须通过。",
        "不需要新增测试。",
        "不要求添加测试。",
        "请勿编写测试。",
        "无须补充测试。",
        "No need to add tests.",
        "Need not write tests.",
        "Do not, however, add tests.",
    )

    def test_explicit_test_change_requests_require_a_test_diff(self) -> None:
        for request in self.TEST_CHANGE_REQUESTS:
            with self.subTest(request=request):
                result = _review_request(request, test_changed=False)
                self.assertIn("requested_test_missing", {finding.code for finding in result.findings})

    def test_explicit_test_change_requests_pass_when_a_test_diff_exists(self) -> None:
        for request in self.TEST_CHANGE_REQUESTS:
            with self.subTest(request=request):
                result = _review_request(request, test_changed=True)
                self.assertNotIn("requested_test_missing", {finding.code for finding in result.findings})

    def test_test_verification_and_local_negations_do_not_require_a_test_diff(self) -> None:
        for request in self.NO_TEST_CHANGE_REQUESTS:
            with self.subTest(request=request):
                result = _review_request(request, test_changed=False)
                self.assertNotIn("requested_test_missing", {finding.code for finding in result.findings})

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

    def test_chinese_add_one_test_request_requires_test_diff(self) -> None:
        request = "请修复登录功能，并加一个测试。"
        contract = generate_requirement_contract(request)
        result = review_patch(
            contract,
            request=request,
            tool_results=[
                ToolResultSummary("apply_patch", "Applied patch", path="src/LoginService.java", changed=True),
                ToolResultSummary("git_diff", _java_diff()),
            ],
        )

        self.assertFalse(result.passed)
        self.assertIn("requested_test_missing", {finding.code for finding in result.findings})

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

    def test_pre_write_diff_does_not_review_a_later_workspace_write(self) -> None:
        contract = generate_requirement_contract("请实现用户名规范化功能。")
        result = review_patch(
            contract,
            request="请实现用户名规范化功能。",
            tool_results=[
                ToolResultSummary("apply_patch", "Applied first patch", changed=True),
                ToolResultSummary("git_diff", _java_diff()),
                ToolResultSummary("apply_patch", "Applied final patch", changed=True),
            ],
        )

        self.assertIn("git_diff_missing", {finding.code for finding in result.findings})

    def test_useless_or_unrelated_search_does_not_satisfy_call_site_review(self) -> None:
        contract = generate_requirement_contract("请实现用户名规范化功能。")
        result = review_patch(
            contract,
            request="请实现用户名规范化功能。",
            tool_results=[
                ToolResultSummary("apply_patch", "Applied patch", changed=True),
                ToolResultSummary("search_code", "(no matches)", useless=True),
                ToolResultSummary(
                    "read_file",
                    "class AuditService { void record() {} }",
                    path="src/AuditService.java",
                ),
                ToolResultSummary("git_diff", _java_diff(public_api=True)),
            ],
        )

        self.assertIn("call_site_review_missing", {finding.code for finding in result.findings})

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

    def test_large_diff_metadata_preserves_late_test_paths_for_reviewer(self) -> None:
        request = "请实现用户名规范化，并补充单元测试。"
        source_only = _java_diff().split("\n[diff summary]", 1)[0]
        test_only = _java_diff(test_changed=True).split("diff --git a/src/test/UserServiceTest.java", 1)[1]
        raw = source_only + ("+filler\n" * 2000) + "diff --git a/src/test/UserServiceTest.java" + test_only
        summary = review_input_summary("git_diff", raw, max_chars=800)
        result = review_patch(
            generate_requirement_contract(request),
            request=request,
            tool_results=[
                ToolResultSummary("apply_patch", "Applied patch", changed=True),
                ToolResultSummary(
                    "git_diff",
                    summary,
                    metadata=review_input_metadata("git_diff", raw),
                ),
            ],
        )

        self.assertNotIn("requested_test_missing", {finding.code for finding in result.findings})


def _review_request(request: str, *, test_changed: bool):
    contract = generate_requirement_contract("Implement a source code boundary fix.")
    return review_patch(
        contract,
        request=request,
        tool_results=[
            ToolResultSummary("apply_patch", "Applied patch", path="src/UserService.java", changed=True),
            ToolResultSummary("git_diff", _java_diff(test_changed=test_changed)),
        ],
    )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from local_agent.tool_choice_queue import READ_ONLY_FORBIDDEN_TOOL_NAMES
from local_agent.tool_choice_queue import CANDIDATE_DELIVERY_TOOL_NAMES
from local_agent.tool_choice_queue import CANDIDATE_REMEDIATION_TOOL_NAMES
from local_agent.tool_choice_queue import PLANNER_EXPLORE_TOOL_NAMES
from local_agent.tool_choice_queue import POST_DIFF_REMEDIATION_TOOL_NAMES
from local_agent.tool_choice_queue import ToolChoiceQueue
from local_agent.tool_choice_queue import ToolResultSummary
from local_agent.tool_choice_queue import evaluate_tool_choice_state


class ToolChoiceQueueTests(unittest.TestCase):
    def test_read_only_evidence_question_requires_code_evidence_tool(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="read_only",
            prompt="不要推测，请给出代码证据说明登录密码在哪里校验。",
            tool_names=[],
            tool_results=[],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "code_evidence")
        self.assertEqual(decision.missing_requirements, ("code_evidence",))
        self.assertIn("read_file", decision.allowed_tool_names)
        self.assertIn("search_code", decision.allowed_tool_names)
        self.assertFalse(READ_ONLY_FORBIDDEN_TOOL_NAMES.intersection(decision.allowed_tool_names))

    def test_implementation_task_missing_diff_is_steered_to_git_diff(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="implementation",
            prompt="实现 MiniToolChoiceQueue 原型。",
            tool_names=["read_file", "apply_patch", "run_tests"],
            tool_results=[
                ToolResultSummary("apply_patch", "Applied patch. Patch id: p1", changed=True),
                ToolResultSummary("run_tests", "OK"),
            ],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "implementation_final_hygiene")
        self.assertEqual(decision.missing_requirements, ("git_diff",))
        self.assertEqual(decision.allowed_tool_names, frozenset({"git_diff"}))

    def test_implementation_task_requires_explore_before_write_tools(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="code-implementation",
            prompt="请实现用户注册接口邮箱唯一性校验，并补充测试。",
            tool_names=[],
            tool_results=[],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "implementation_explore")
        self.assertEqual(decision.missing_requirements, ("planner_explore_evidence",))
        self.assertEqual(decision.allowed_tool_names, PLANNER_EXPLORE_TOOL_NAMES)
        self.assertNotIn("apply_patch", decision.allowed_tool_names)
        self.assertNotIn("write_file", decision.allowed_tool_names)

    def test_implementation_task_before_write_does_not_force_final_hygiene(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="code-implementation",
            prompt="请实现 MiniToolChoiceQueue 原型。",
            tool_names=["read_file"],
            tool_results=[ToolResultSummary("read_file", "src/local_agent/tool_choice_queue.py")],
        )

        self.assertFalse(decision.steering_required)
        self.assertIn("apply_patch", decision.allowed_tool_names)

    def test_autonomous_small_change_candidate_stops_broad_exploration_after_source_and_test_reads(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="code-implementation",
            prompt="请自己找一个极小的代码改进，并补充测试。",
            tool_results=[
                ToolResultSummary("read_file", "class UserService {}", path="src/UserService.java"),
                ToolResultSummary("read_file", "class UserServiceTest {}", path="tests/UserServiceTest.java"),
            ],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "autonomous_small_change_candidate")
        self.assertEqual(decision.allowed_tool_names, CANDIDATE_DELIVERY_TOOL_NAMES)
        self.assertNotIn("list_files", decision.allowed_tool_names)
        self.assertNotIn("search_code", decision.allowed_tool_names)

    def test_scoped_docs_exclusion_keeps_autonomous_candidate_delivery_enabled(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="code-implementation",
            prompt=(
                "请自行挑选一个极小、低风险的测试改进；随后必须 apply_patch dry_run=true 预览、"
                "apply_patch 真正写入、run_tests、git_diff。不要修改 README 或 docs。"
            ),
            tool_results=[
                ToolResultSummary("read_file", "class TerminalIo {}", path="src/local_agent/terminal_io.py"),
                ToolResultSummary("read_file", "class TerminalIoTests {}", path="tests/test_terminal_io.py"),
            ],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "autonomous_small_change_candidate")
        self.assertEqual(decision.allowed_tool_names, CANDIDATE_DELIVERY_TOOL_NAMES)

    def test_candidate_preview_error_allows_exact_read_remediation(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="code-implementation",
            prompt="请自己找一个极小的代码改进，并补充测试。",
            tool_results=[
                ToolResultSummary("read_file", "class UserService {}", path="src/UserService.java"),
                ToolResultSummary("read_file", "class UserServiceTest {}", path="tests/UserServiceTest.java"),
                ToolResultSummary("apply_patch", "Hash mismatch", is_error=True),
            ],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.allowed_tool_names, CANDIDATE_REMEDIATION_TOOL_NAMES)
        self.assertIn("read_file", decision.allowed_tool_names)

    def test_implementation_task_with_diff_and_tests_is_complete(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="implementation",
            prompt="实现 MiniToolChoiceQueue 原型。",
            tool_names=["read_file", "apply_patch", "run_tests", "git_diff"],
            tool_results=[
                ToolResultSummary("apply_patch", "Applied patch. Patch id: p1", changed=True),
                ToolResultSummary("run_tests", "OK"),
                ToolResultSummary("git_diff", "diff --git a/a.py b/a.py"),
            ],
        )

        self.assertFalse(decision.steering_required)
        self.assertEqual(decision.missing_requirements, ())
        self.assertIn("apply_patch", decision.allowed_tool_names)
        self.assertIn("run_tests", decision.allowed_tool_names)

    def test_implementation_verification_must_follow_the_last_workspace_write(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="implementation",
            prompt="实现用户名规范化，并补充测试。",
            tool_names=["apply_patch", "run_tests", "git_diff"],
            tool_results=[
                ToolResultSummary("apply_patch", "Applied first patch", changed=True),
                ToolResultSummary("run_tests", "OK"),
                ToolResultSummary("git_diff", "diff --git a/src/UserService.java b/src/UserService.java"),
                ToolResultSummary("apply_patch", "Applied final patch", changed=True),
            ],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "implementation_final_hygiene")
        self.assertEqual(
            decision.missing_requirements,
            ("git_diff", "run_tests_or_cannot_test_explanation"),
        )
        self.assertEqual(decision.allowed_tool_names, frozenset({"git_diff", "run_tests"}))

    def test_post_diff_pending_tests_keeps_focused_repair_tools_available(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="implementation",
            prompt="实现用户名规范化，并补充单元测试。",
            tool_names=["read_file", "apply_patch", "git_diff"],
            tool_results=[
                ToolResultSummary("read_file", "class UserService {}", path="src/UserService.java"),
                ToolResultSummary("apply_patch", "Applied patch", changed=True),
                ToolResultSummary("git_diff", "diff --git a/src/UserService.java b/src/UserService.java"),
            ],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "implementation_final_hygiene")
        self.assertIn("run_tests_or_cannot_test_explanation", decision.missing_requirements)
        self.assertEqual(decision.allowed_tool_names, POST_DIFF_REMEDIATION_TOOL_NAMES)
        self.assertIn("apply_patch", decision.allowed_tool_names)
        self.assertIn("read_file", decision.allowed_tool_names)
        self.assertIn("run_tests", decision.allowed_tool_names)

    def test_requirement_doc_task_must_read_doc_before_full_toolset(self) -> None:
        before_read = evaluate_tool_choice_state(
            task_kind="allowed_dir",
            prompt="根据 allowed-dir 里的需求文档完成改造。",
            tool_names=[],
            tool_results=[],
        )

        self.assertTrue(before_read.steering_required)
        self.assertEqual(before_read.rule_id, "requirement_document_read")
        self.assertEqual(before_read.missing_requirements, ("requirement_document_read",))
        self.assertIn("read_file", before_read.allowed_tool_names)
        self.assertNotIn("apply_patch", before_read.allowed_tool_names)

        after_read = evaluate_tool_choice_state(
            task_kind="allowed_dir",
            prompt="根据 allowed-dir 里的需求文档完成改造。",
            tool_names=["read_file"],
            tool_results=[
                {
                    "name": "read_file",
                    "path": "/tmp/allowed-dir/需求文档.md",
                    "content": "需求：实现队列规则。",
                }
            ],
        )

        self.assertFalse(after_read.steering_required)
        self.assertIn("apply_patch", after_read.allowed_tool_names)
        self.assertIn("run_tests", after_read.allowed_tool_names)

    def test_requirement_prompt_does_not_treat_source_read_as_requirement_evidence(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="allowed_dir",
            prompt="根据 allowed-dir 里的需求文档完成改造。",
            tool_results=[
                ToolResultSummary(
                    "read_file",
                    "public class PaymentService {}",
                    path="/workspace/backend/src/PaymentService.java",
                )
            ],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "requirement_document_read")

    def test_read_only_task_filters_write_and_exec_tools(self) -> None:
        decision = ToolChoiceQueue().evaluate(
            task_kind="read_only",
            prompt="只读分析这个模块，不要修改。",
            tool_names=["read_file"],
            tool_results=[ToolResultSummary("read_file", "src/local_agent/agent.py:1:from __future__")],
        )

        self.assertFalse(decision.steering_required)
        self.assertFalse(READ_ONLY_FORBIDDEN_TOOL_NAMES.intersection(decision.allowed_tool_names))
        self.assertIn("read_file", decision.allowed_tool_names)
        self.assertIn("search_code", decision.allowed_tool_names)

    def test_cross_root_design_requires_a_code_read_from_each_root(self) -> None:
        backend = "/workspace/backend"
        frontend = "/workspace/frontend"
        after_backend_read = evaluate_tool_choice_state(
            task_kind="read_only",
            prompt="请只读设计前后端改造方案，不要修改文件。",
            tool_results=[ToolResultSummary("read_file", "class Backend {}", path=f"{backend}/src/App.java")],
            design_evidence_roots=(backend, frontend),
        )

        self.assertTrue(after_backend_read.steering_required)
        self.assertEqual(after_backend_read.rule_id, f"cross_root_design_evidence:{frontend}")
        self.assertEqual(after_backend_read.missing_requirements, (f"code_read:{frontend}",))
        self.assertIn("read_file", after_backend_read.allowed_tool_names)
        self.assertNotIn("apply_patch", after_backend_read.allowed_tool_names)

        after_frontend_read = evaluate_tool_choice_state(
            task_kind="read_only",
            prompt="请只读设计前后端改造方案，不要修改文件。",
            tool_results=[
                ToolResultSummary("read_file", "class Backend {}", path=f"{backend}/src/App.java"),
                ToolResultSummary("read_file", "export default {}", path=f"{frontend}/src/views/List.vue"),
            ],
            design_evidence_roots=(backend, frontend),
        )

        self.assertFalse(after_frontend_read.steering_required)


if __name__ == "__main__":
    unittest.main()

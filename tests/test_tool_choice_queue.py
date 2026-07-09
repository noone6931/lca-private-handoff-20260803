from __future__ import annotations

import unittest

from local_agent.tool_choice_queue import READ_ONLY_FORBIDDEN_TOOL_NAMES
from local_agent.tool_choice_queue import PLANNER_EXPLORE_TOOL_NAMES
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


if __name__ == "__main__":
    unittest.main()

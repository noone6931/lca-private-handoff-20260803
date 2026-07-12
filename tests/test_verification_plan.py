from __future__ import annotations

import unittest

from local_agent.task_contract import generate_requirement_contract
from local_agent.test_planner import TestPlan
from local_agent.tool_choice_queue import ToolResultSummary
from local_agent.verification_plan import VerificationPlan


class VerificationPlanTests(unittest.TestCase):
    def _plan(self) -> VerificationPlan:
        return VerificationPlan.from_contract(
            generate_requirement_contract("请实现邮箱唯一性校验，并补充单元测试。")
        )

    def test_runtime_facts_do_not_mark_business_acceptance_passed(self) -> None:
        plan = self._plan()
        results = [
            ToolResultSummary("read_file", "class UserService {}", path="src/UserService.java"),
            ToolResultSummary("apply_patch", "Applied patch", changed=True, path="src/UserService.java"),
            ToolResultSummary("run_tests", "OK"),
            ToolResultSummary("git_diff", "diff --git a/src/UserService.java b/src/UserService.java"),
        ]

        plan.observe(results, test_plan=TestPlan("mvn test", "project fallback", "project"))
        plan.record_patch_review(passed=True, reason="review passed", refs=["git_diff:post-write"])

        contract_items = [item for item in plan.items if not item.enforce_delivery]
        self.assertTrue(contract_items)
        self.assertTrue(all(item.status == "pending" for item in contract_items))
        self.assertEqual(plan.coverage(delivery_only=True)["passed"], 4)
        self.assertGreater(plan.business_acceptance_summary()["unverified"], 0)

    def test_rollback_and_empty_diff_do_not_prove_delivery(self) -> None:
        plan = self._plan()
        results = [
            ToolResultSummary("apply_patch", "Applied patch", changed=True, path="src/UserService.java"),
            ToolResultSummary("rollback_patch", "Rollback applied", changed=True, path="src/UserService.java"),
            ToolResultSummary("git_diff", "(empty diff)"),
        ]

        plan.observe(results, test_plan=TestPlan("mvn test", "project fallback", "project"))

        self.assertEqual(_item(plan, "runtime-current-diff").status, "pending")
        self.assertEqual(_item(plan, "runtime-post-write-test").status, "pending")

    def test_unrelated_read_does_not_prove_code_evidence_for_changed_file(self) -> None:
        plan = self._plan()
        results = [
            ToolResultSummary("read_file", "# README", path="README.md"),
            ToolResultSummary("apply_patch", "Applied patch", changed=True, path="src/App.py"),
            ToolResultSummary("git_diff", "diff --git a/src/App.py b/src/App.py"),
        ]

        plan.observe(results, test_plan=TestPlan("python -m unittest", "project fallback", "project"))

        self.assertEqual(_item(plan, "runtime-code-evidence").status, "pending")

    def test_reading_changed_file_proves_path_related_code_evidence(self) -> None:
        plan = self._plan()
        results = [
            ToolResultSummary("read_file", "def app(): pass", path="src/App.py"),
            ToolResultSummary("apply_patch", "Applied patch", changed=True, path="src/App.py"),
            ToolResultSummary("git_diff", "diff --git a/src/App.py b/src/App.py"),
        ]

        plan.observe(results, test_plan=TestPlan("python -m unittest", "project fallback", "project"))

        self.assertEqual(_item(plan, "runtime-code-evidence").status, "passed")

    def test_dirty_diff_after_rollback_does_not_prove_runtime_current_diff(self) -> None:
        plan = self._plan()
        results = [
            ToolResultSummary("apply_patch", "Applied patch", changed=True, path="src/App.py"),
            ToolResultSummary("rollback_patch", "Rollback applied", changed=True, path="src/App.py"),
            ToolResultSummary(
                "git_diff",
                "diff --git a/README.md b/README.md",
                metadata={"patch_review": {"changed_paths": ["README.md"]}},
            ),
        ]

        plan.observe(results, test_plan=TestPlan("python -m unittest", "project fallback", "project"))

        self.assertEqual(_item(plan, "runtime-current-diff").status, "pending")

    def test_test_before_last_write_does_not_satisfy_post_write_check(self) -> None:
        plan = self._plan()
        results = [
            ToolResultSummary("run_tests", "OK"),
            ToolResultSummary("apply_patch", "Applied patch", changed=True, path="src/UserService.java"),
            ToolResultSummary("git_diff", "diff --git a/src/UserService.java b/src/UserService.java"),
        ]

        plan.observe(results, test_plan=TestPlan("mvn test", "project fallback", "project"))

        self.assertEqual(_item(plan, "runtime-post-write-test").status, "pending")

    def test_structured_approval_denial_blocks_test_without_passing_it(self) -> None:
        plan = self._plan()
        results = [
            ToolResultSummary("apply_patch", "Applied patch", changed=True, path="src/UserService.java"),
            ToolResultSummary(
                "run_tests",
                "User denied tool execution: run_tests",
                is_error=True,
                metadata={"execution_status": "denied", "denial_kind": "approval"},
            ),
        ]

        plan.observe(results, test_plan=TestPlan("mvn test", "project fallback", "project"))

        self.assertEqual(_item(plan, "runtime-post-write-test").status, "blocked")
        self.assertNotEqual(_item(plan, "runtime-post-write-test").status, "passed")

    def test_reviewer_status_is_explicit(self) -> None:
        plan = self._plan()

        plan.record_patch_review(passed=True, reason="review passed", refs=["git_diff:post-write"])
        self.assertEqual(_item(plan, "runtime-review").status, "passed")
        plan.record_patch_review(passed=False, reason="missing caller evidence", refs=["steerer:patch_reviewer"])
        self.assertEqual(_item(plan, "runtime-review").status, "failed")

    def test_incomplete_terminal_reports_non_passed_delivery_checks(self) -> None:
        plan = self._plan()
        plan.record_patch_review(passed=None, reason="review cap reached", refs=[])

        terminal = plan.render_incomplete_terminal()

        self.assertIn("未完成/未验证", terminal)
        self.assertIn("review cap reached", terminal)


def _item(plan: VerificationPlan, item_id: str):
    return next(item for item in plan.items if item.id == item_id)

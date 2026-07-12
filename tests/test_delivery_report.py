from __future__ import annotations

import unittest

from local_agent.delivery_report import render_delivery_report
from local_agent.task_contract import generate_requirement_contract
from local_agent.test_planner import TestPlan
from local_agent.tool_choice_queue import ToolResultSummary
from local_agent.verification_plan import VerificationPlan


class DeliveryReportTests(unittest.TestCase):
    def test_success_report_contains_runtime_facts_even_without_model_summary(self) -> None:
        plan, results = _successful_delivery()

        report = render_delivery_report(plan, results)

        self.assertIn("src/math.py", report)
        self.assertIn("PYTHONPATH=. python3 -m unittest tests.test_math", report)
        self.assertIn("passed=4", report)
        self.assertIn("business_acceptance_unverified", report)
        self.assertIn("remaining_delivery_checks: none", report)

    def test_failed_report_retains_changed_path_and_actual_failed_command(self) -> None:
        plan = VerificationPlan.from_contract(generate_requirement_contract("修复 subtract 并运行测试。"))
        results = [
            ToolResultSummary("read_file", "def subtract(a, b): return a + b", path="src/math.py"),
            ToolResultSummary("apply_patch", "Applied patch", changed=True, path="src/math.py"),
            ToolResultSummary(
                "run_tests",
                "AssertionError",
                is_error=True,
                metadata={
                    "executed_command": "PYTHONPATH=. python3 -m unittest tests.test_math",
                    "execution_status": "failed",
                },
            ),
            ToolResultSummary("git_diff", "diff --git a/src/math.py b/src/math.py"),
        ]
        plan.observe(results, test_plan=TestPlan("python3 -m unittest discover", "project fallback", "project"))
        plan.record_patch_review(passed=True, reason="review passed", refs=["git_diff:post-write"])

        report = render_delivery_report(plan, results)

        self.assertIn("src/math.py", report)
        self.assertIn("PYTHONPATH=. python3 -m unittest tests.test_math", report)
        self.assertIn("failed", report)
        self.assertIn("runtime-post-write-test", report)


def _successful_delivery() -> tuple[VerificationPlan, list[ToolResultSummary]]:
    plan = VerificationPlan.from_contract(generate_requirement_contract("修复 subtract 并运行测试。"))
    results = [
        ToolResultSummary("read_file", "def subtract(a, b): return a + b", path="src/math.py"),
        ToolResultSummary("apply_patch", "Applied patch", changed=True, path="src/math.py"),
        ToolResultSummary(
            "run_tests",
            "OK",
            metadata={
                "executed_command": "PYTHONPATH=. python3 -m unittest tests.test_math",
                "execution_status": "succeeded",
            },
        ),
        ToolResultSummary("git_diff", "diff --git a/src/math.py b/src/math.py"),
    ]
    plan.observe(results, test_plan=TestPlan("python3 -m unittest discover", "project fallback", "project"))
    plan.record_patch_review(passed=True, reason="review passed", refs=["git_diff:post-write"])
    return plan, results

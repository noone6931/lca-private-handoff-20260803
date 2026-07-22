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

    def test_not_run_command_is_a_failed_delivery_check_not_test_evidence(self) -> None:
        plan = VerificationPlan.from_contract(generate_requirement_contract("修复 subtract 并运行测试。"))
        results = [
            ToolResultSummary("read_file", "def subtract(a, b): return a + b", path="src/math.py"),
            ToolResultSummary("apply_patch", "Applied patch", changed=True, path="src/math.py"),
            ToolResultSummary(
                "run_tests",
                "shell syntax rejected",
                is_error=True,
                metadata={
                    "executed_command": "python3 -m unittest tests.test_math || true",
                    "execution_status": "not_run",
                    "exit_code": None,
                },
            ),
            ToolResultSummary("git_diff", "diff --git a/src/math.py b/src/math.py"),
        ]
        plan.observe(results, test_plan=TestPlan("python3 -m unittest discover", "project fallback", "project"))
        plan.record_patch_review(passed=True, reason="review passed", refs=["git_diff:post-write"])

        report = render_delivery_report(plan, results)

        self.assertIn("not_run", report)
        self.assertIn("failed=1", report)
        self.assertIn("runtime-post-write-test", report)

    def test_post_write_shell_is_reported_without_satisfying_run_tests_gate(self) -> None:
        plan = VerificationPlan.from_contract(
            generate_requirement_contract("请修改 bubble_sort.py 参数并运行脚本验证。")
        )
        results = [
            ToolResultSummary("read_file", "VALUES = [3, 1]", path="bubble_sort.py"),
            ToolResultSummary("apply_patch", "Applied patch", changed=True, path="bubble_sort.py"),
            ToolResultSummary(
                "shell",
                "sorted=[1, 3]\n[exit_code] 0",
                metadata={
                    "execution_v1": {
                        "version": 1,
                        "command": {"text": "python3 bubble_sort.py", "argv": None, "shell": True},
                        "cwd": "/workspace",
                        "outcome": {"kind": "exited", "exit_code": 0},
                        "output": {"provenance": "bounded_process_capture_v1", "bounded": True, "capture": {}},
                    }
                },
            ),
            ToolResultSummary("git_diff", "diff --git a/bubble_sort.py b/bubble_sort.py"),
        ]
        plan.observe(results, test_plan=TestPlan("python3 -m unittest", "project fallback", "project"))
        plan.record_patch_review(passed=True, reason="review passed", refs=["git_diff:post-write"])

        report = render_delivery_report(plan, results)

        self.assertIn("no post-write run_tests command was recorded", report)
        self.assertIn("other_post_write_executions (not counted as the run_tests gate)", report)
        self.assertIn("passed shell exit=0", report)
        self.assertIn("command_sha256=", report)
        self.assertNotIn("python3 bubble_sort.py", report)
        self.assertEqual(_item(plan, "runtime-post-write-test").status, "pending")

    def test_shell_only_side_effect_is_not_attributed_to_patch_transaction(self) -> None:
        plan = VerificationPlan.from_contract(generate_requirement_contract("请删除 bubble_sort.py。"))
        result = ToolResultSummary(
            "shell",
            "[exit_code] 0",
            metadata={
                "execution_v1": {
                    "version": 1,
                    "command": {"text": "rm bubble_sort.py", "argv": None, "shell": True},
                    "cwd": "/workspace",
                    "outcome": {"kind": "exited", "exit_code": 0},
                    "output": {"provenance": "bounded_process_capture_v1", "bounded": True, "capture": {}},
                }
            },
        )

        report = render_delivery_report(plan, [result])

        self.assertIn("[Runtime operation provenance]", report)
        self.assertIn("patch_transaction_writes: none recorded", report)
        self.assertIn("not patch-journal mutation evidence", report)
        self.assertIn("passed shell exit=0", report)
        self.assertNotIn("rm bubble_sort.py", report)

    def test_shell_report_rejects_malformed_or_unbounded_execution_metadata(self) -> None:
        plan = VerificationPlan.from_contract(generate_requirement_contract("运行脚本。"))
        valid = {
            "version": 1,
            "command": {"text": "python3 script.py", "argv": None, "shell": True},
            "cwd": "/workspace",
            "outcome": {"kind": "exited", "exit_code": 0},
            "output": {"provenance": "bounded_process_capture_v1", "bounded": True, "capture": {}},
        }
        malformed = (
            {key: value for key, value in valid.items() if key != "version"},
            {**valid, "version": 2},
            {**valid, "command": {**valid["command"], "shell": False}},
            {**valid, "command": {**valid["command"], "argv": ["python3", "script.py"]}},
            {**valid, "output": {"provenance": "raw", "bounded": False}},
            {**valid, "output": {"provenance": "bounded_process_capture_v1", "bounded": True}},
        )

        for execution in malformed:
            with self.subTest(execution=execution):
                result = ToolResultSummary("shell", "[exit_code] 0", metadata={"execution_v1": execution})
                self.assertEqual(render_delivery_report(plan, [result]), "")


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


def _item(plan: VerificationPlan, item_id: str):
    return next(item for item in plan.items if item.id == item_id)

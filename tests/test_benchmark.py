from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_agent.benchmark import DEFAULT_TASKS_DIR
from local_agent.benchmark import BenchmarkResult
from local_agent.benchmark import _acceptance_for_mode
from local_agent.benchmark import _matches_answer_regex
from local_agent.benchmark import load_benchmark_tasks
from local_agent.benchmark import run_benchmark_suite
from local_agent.benchmark import write_benchmark_reports


class BenchmarkTests(unittest.TestCase):
    def test_default_task_catalog_covers_six_representative_workflows(self) -> None:
        tasks = load_benchmark_tasks()
        identifiers = {task.identifier for task in tasks}

        self.assertEqual(len(tasks), 6)
        self.assertEqual(
            identifiers,
            {
                "single-root-readonly-discovery",
                "multi-root-code-inventory",
                "scoped-negative-source-evidence",
                "small-code-change-test-diff",
                "denied-tool-schema",
                "budget-exhausted-incomplete",
            },
        )
        self.assertTrue(DEFAULT_TASKS_DIR.is_dir())

    def test_deterministic_suite_writes_json_and_markdown_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "reports"
            results = run_benchmark_suite(output_dir=output_dir)
            payload = json.loads((output_dir / "benchmark-report.json").read_text(encoding="utf-8"))
            markdown = (output_dir / "benchmark-report.md").read_text(encoding="utf-8")

        self.assertEqual(len(results), 6)
        self.assertTrue(all(result.passed for result in results))
        self.assertEqual(payload["passed"], 6)
        self.assertEqual(payload["failed"], 0)
        self.assertIn("small-code-change-test-diff", markdown)
        self.assertIn("budget-exhausted-incomplete", markdown)

    def test_live_inventory_acceptance_uses_semantic_terms_instead_of_fixed_wording(self) -> None:
        tasks = {task.identifier: task for task in load_benchmark_tasks()}
        inventory = _acceptance_for_mode(tasks["multi-root-code-inventory"].acceptance, "live")
        negative = _acceptance_for_mode(tasks["scoped-negative-source-evidence"].acceptance, "live")

        self.assertEqual(inventory["answer_contains"], [])
        self.assertTrue(_matches_answer_regex(inventory["answer_regex"][0], "Java/Maven 基础骨架项目"))
        self.assertEqual(negative["answer_contains"], [])
        self.assertTrue(
            _matches_answer_regex(
                negative["answer_regex"][0],
                "当前 primary workspace 不存在 Java 源码，结论只覆盖当前 primary。",
            )
        )

    def test_report_includes_bounded_diagnostics_and_run_identity(self) -> None:
        result = BenchmarkResult(
            identifier="failed-fixture",
            title="Failed fixture",
            passed=False,
            mode="live",
            answer="",
            elapsed_ms=10,
            run_summary={"run_id": "run-1", "tool_errors": 1, "compactions": 1, "zero_gain_compactions": 1},
            acceptance=(),
            changed_files=(),
            test_evidence=(),
            residual_risk="fixture only",
            session_id="session-1",
            run_id="run-1",
            tool_error_summaries=({"tool": "run_tests", "summary": "command not found"},),
        )
        with tempfile.TemporaryDirectory() as tmp:
            _, markdown_path = write_benchmark_reports((result,), Path(tmp))
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertIn("Session/run: session-1 / run-1", markdown)
        self.assertIn("Tool error [run_tests]: command not found", markdown)
        self.assertIn("Compaction effectiveness", markdown)


if __name__ == "__main__":
    unittest.main()

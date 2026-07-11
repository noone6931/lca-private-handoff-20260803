from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_agent.benchmark import DEFAULT_TASKS_DIR
from local_agent.benchmark import load_benchmark_tasks
from local_agent.benchmark import run_benchmark_suite


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


if __name__ == "__main__":
    unittest.main()

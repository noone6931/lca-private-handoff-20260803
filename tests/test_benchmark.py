from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_agent.benchmark import DEFAULT_TASKS_DIR
from local_agent.benchmark import BenchmarkResult
from local_agent.benchmark import ScriptedBenchmarkClient
from local_agent.benchmark import _acceptance_for_mode
from local_agent.benchmark import _matches_answer_regex
from local_agent.benchmark import load_benchmark_tasks
from local_agent.benchmark import run_benchmark_suite
from local_agent.benchmark import write_benchmark_reports


class BenchmarkTests(unittest.TestCase):
    def test_default_task_catalog_covers_representative_workflows(self) -> None:
        tasks = load_benchmark_tasks()
        identifiers = {task.identifier for task in tasks}

        self.assertEqual(len(tasks), 60)
        self.assertEqual(
            identifiers,
            {
                "single-root-readonly-discovery",
                "multi-root-code-inventory",
                "scoped-negative-source-evidence",
                "small-code-change-test-diff",
                "denied-tool-schema",
                "budget-exhausted-incomplete",
                "small-code-test-failure-incomplete",
                "session-evidence-followup",
                "qualified-negative-observation",
                "forced-final-protocol-artifact",
                "bare-observed-no-match",
                "document-analysis-boundary",
                "readonly-owner-review",
                "readonly-design-review",
                "readonly-owner-explore-budget",
                "readonly-reviewer-last-gate",
                "readonly-pre-review-audit",
                "readonly-safe-partial-delivery",
                "document-artifact-consistency",
                "readonly-multiroot-safe-partial",
                "document-reconciliation-stance",
                "readonly-exact-toolchoice",
                "readonly-reviewer-nine-findings",
                "readonly-multiroot-explore-directive",
                "document-final-submit-recovery",
                "readonly-inventory-provenance",
                "readonly-open-explore-soft-preference",
                "readonly-transport-recovery",
                "readonly-rewrite-verification-closure",
                "readonly-design-proposal-semantics",
                "document-artifact-synthesis-reviewer-recovery",
                "document-transport-scoped-consistency",
                "document-then-repository-owner",
                "reviewer-finding-idempotent-replay",
                "document-unresolved-difference-word-order",
                "readonly-first-miss-fallback",
                "generic-technical-design-review",
                "readonly-fallback-argument-exhaustion",
                "reviewer-provable-finding-boundary",
                "readonly-initial-precise-glob",
                "readonly-cross-root-exact-path-retry",
                "readonly-cross-root-precise-filename-retry",
                "readonly-cross-root-mixed-exact-retry",
                "readonly-glob-then-bounded-source-read",
                "readonly-unlocated-certainty-rewrite",
                "readonly-parallel-root-glob",
                "readonly-primary-miss-code-root-rebase",
                "readonly-nested-source-fact-transport",
                "readonly-explicit-source-artifact-closure",
                "readonly-explicit-source-candidate-priority",
                "readonly-transport-residual-projection",
                "readonly-post-review-transport-projection",
                "readonly-reviewer-claim-role-ownership",
                "readonly-reviewer-advisory-closure",
                "readonly-root-targeted-explore-directive",
                "readonly-semantic-candidate-commit",
                "readonly-workspace-evidence-root-projection",
                "readonly-implementation-readiness-blocked",
                "readonly-required-material-candidate-recovery",
                "readonly-readiness-final-repair",
            },
        )
        self.assertTrue(DEFAULT_TASKS_DIR.is_dir())

    def test_deterministic_suite_writes_json_and_markdown_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "reports"
            results = run_benchmark_suite(output_dir=output_dir)
            payload = json.loads((output_dir / "benchmark-report.json").read_text(encoding="utf-8"))
            markdown = (output_dir / "benchmark-report.md").read_text(encoding="utf-8")

            self.assertEqual(len(results), 60)
        self.assertTrue(all(result.passed for result in results))
        self.assertEqual(payload["passed"], 60)
        self.assertEqual(payload["failed"], 0)
        self.assertIn("small-code-change-test-diff", markdown)
        self.assertIn("budget-exhausted-incomplete", markdown)
        self.assertIn("small-code-test-failure-incomplete", markdown)
        self.assertIn("session-evidence-followup", markdown)
        self.assertIn("qualified-negative-observation", markdown)
        self.assertIn("forced-final-protocol-artifact", markdown)
        self.assertIn("bare-observed-no-match", markdown)
        self.assertIn("document-analysis-boundary", markdown)
        self.assertIn("readonly-owner-review", markdown)
        self.assertIn("readonly-design-review", markdown)
        self.assertIn("readonly-owner-explore-budget", markdown)
        self.assertIn("readonly-reviewer-last-gate", markdown)
        self.assertIn("readonly-pre-review-audit", markdown)
        self.assertIn("readonly-safe-partial-delivery", markdown)
        self.assertIn("document-artifact-consistency", markdown)
        self.assertIn("readonly-multiroot-safe-partial", markdown)
        self.assertIn("document-reconciliation-stance", markdown)
        self.assertIn("readonly-exact-toolchoice", markdown)
        self.assertIn("readonly-reviewer-nine-findings", markdown)
        self.assertIn("readonly-multiroot-explore-directive", markdown)
        self.assertIn("readonly-inventory-provenance", markdown)
        self.assertIn("readonly-open-explore-soft-preference", markdown)
        self.assertIn("readonly-parallel-root-glob", markdown)
        self.assertIn("readonly-transport-recovery", markdown)
        self.assertIn("readonly-rewrite-verification-closure", markdown)
        self.assertIn("readonly-design-proposal-semantics", markdown)
        self.assertIn("document-artifact-synthesis-reviewer-recovery", markdown)
        self.assertIn("document-transport-scoped-consistency", markdown)
        self.assertIn("document-then-repository-owner", markdown)
        self.assertIn("reviewer-finding-idempotent-replay", markdown)
        self.assertIn("document-unresolved-difference-word-order", markdown)
        self.assertIn("readonly-first-miss-fallback", markdown)
        self.assertIn("generic-technical-design-review", markdown)
        self.assertIn("readonly-fallback-argument-exhaustion", markdown)
        self.assertIn("reviewer-provable-finding-boundary", markdown)
        self.assertIn("readonly-initial-precise-glob", markdown)
        self.assertIn("readonly-cross-root-exact-path-retry", markdown)
        self.assertIn("readonly-cross-root-precise-filename-retry", markdown)
        self.assertIn("readonly-cross-root-mixed-exact-retry", markdown)
        self.assertIn("readonly-glob-then-bounded-source-read", markdown)
        self.assertIn("readonly-unlocated-certainty-rewrite", markdown)
        self.assertIn("readonly-primary-miss-code-root-rebase", markdown)
        self.assertIn("readonly-nested-source-fact-transport", markdown)
        self.assertIn("readonly-explicit-source-artifact-closure", markdown)
        self.assertIn("readonly-explicit-source-candidate-priority", markdown)
        self.assertIn("readonly-transport-residual-projection", markdown)
        self.assertIn("readonly-post-review-transport-projection", markdown)
        self.assertIn("readonly-reviewer-claim-role-ownership", markdown)
        self.assertIn("readonly-reviewer-advisory-closure", markdown)
        self.assertIn("readonly-root-targeted-explore-directive", markdown)
        self.assertIn("readonly-semantic-candidate-commit", markdown)
        self.assertIn("readonly-workspace-evidence-root-projection", markdown)
        self.assertIn("readonly-implementation-readiness-blocked", markdown)
        self.assertIn("readonly-required-material-candidate-recovery", markdown)
        self.assertIn("readonly-readiness-final-repair", markdown)

    def test_scripted_tool_call_can_emit_raw_malformed_arguments(self) -> None:
        client = ScriptedBenchmarkClient(
            (
                {
                    "tool_calls": [
                        {
                            "name": "submit_read_only_review",
                            "arguments_raw": "{",
                        }
                    ]
                },
            ),
            workspace=Path("/tmp/workspace"),
            named_roots={},
        )
        response = client.chat([], [])
        tool_call = response.message["tool_calls"][0]

        self.assertEqual(tool_call["function"]["name"], "submit_read_only_review")
        self.assertEqual(tool_call["function"]["arguments"], "{")

    def test_mapping_acceptance_requires_explicit_metric_values(self) -> None:
        from local_agent.benchmark import _mapping_integer_values_match

        self.assertTrue(_mapping_integer_values_match({"tool_calls": 0}, {"tool_calls": 0}))
        self.assertFalse(_mapping_integer_values_match({}, {"tool_calls": 0}))
        self.assertFalse(_mapping_integer_values_match({"tool_calls": "0"}, {"tool_calls": 0}))

    def test_task_loader_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            (task_dir / "01-duplicate.json").write_text(
                """{
  "id": "duplicate-key-fixture",
  "id": "silently-overwritten",
  "title": "Duplicate key fixture",
  "prompt": "read",
  "workspace_files": {},
  "scripted_responses": [],
  "acceptance": {}
}
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicate JSON key 'id'"):
                load_benchmark_tasks(task_dir)

    def test_scripted_image_observation_is_task_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            (task_dir / "01-image.json").write_text(
                """{
  "id": "image-fixture",
  "title": "Image fixture",
  "prompt": "inspect image",
  "workspace_files": {"example.png": "GIF89a\\n", "second.gif": "GIF89a\\n"},
  "scripted_image_observations": [
    {
      "observations": ["First generic value."],
      "uncertainties": [],
      "inferences": []
    },
    {
      "observations": ["Second generic value."],
      "uncertainties": [],
      "inferences": []
    }
  ],
  "scripted_responses": [
    {"tool_calls": [
      {"name": "inspect_image", "arguments": {"path": "example.png"}},
      {"name": "inspect_image", "arguments": {"path": "second.gif"}}
    ]},
    {"content": "Image observations say First generic value and Second generic value."}
  ],
  "acceptance": {
    "required_tools": ["inspect_image"],
    "answer_all_of": ["First generic value", "Second generic value"],
    "termination_reason": "final",
    "run_summary": {"tool_errors": 0}
  }
}
""",
                encoding="utf-8",
            )
            results = run_benchmark_suite(tasks_dir=task_dir)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].passed)

    def test_scripted_image_without_fixture_fails_without_global_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            (task_dir / "01-image.json").write_text(
                """{
  "id": "image-no-fixture",
  "title": "Image no fixture",
  "prompt": "inspect image",
  "workspace_files": {"example.png": "GIF89a\\n"},
  "scripted_responses": [
    {"tool_calls": [{"name": "inspect_image", "arguments": {"path": "example.png"}}]},
    {"content": "done"}
  ],
  "acceptance": {
    "required_tools": ["inspect_image"],
    "termination_reason": "final",
    "run_summary": {"tool_errors": 0}
  }
}
""",
                encoding="utf-8",
            )
            results = run_benchmark_suite(tasks_dir=task_dir)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].passed)
        self.assertEqual(results[0].run_summary["tool_errors"], 1)

    def test_task_loader_requires_scripted_image_observation_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            (task_dir / "01-image.json").write_text(
                """{
  "id": "image-invalid-fixture",
  "title": "Image invalid fixture",
  "prompt": "inspect image",
  "workspace_files": {"example.png": "GIF89a\\n"},
  "scripted_image_observations": {},
  "scripted_responses": [],
  "acceptance": {}
}
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "scripted_image_observations must be a list of objects"):
                load_benchmark_tasks(task_dir)

    def test_live_inventory_acceptance_uses_semantic_terms_instead_of_fixed_wording(self) -> None:
        tasks = {task.identifier: task for task in load_benchmark_tasks()}
        inventory = _acceptance_for_mode(tasks["multi-root-code-inventory"].acceptance, "live")
        negative = _acceptance_for_mode(tasks["scoped-negative-source-evidence"].acceptance, "live")
        forced_final = _acceptance_for_mode(tasks["forced-final-protocol-artifact"].acceptance, "live")

        self.assertEqual(inventory["answer_contains"], [])
        self.assertTrue(_matches_answer_regex(inventory["answer_regex"][0], "Java/Maven 基础骨架项目"))
        self.assertEqual(negative["answer_contains"], [])
        self.assertTrue(
            _matches_answer_regex(
                negative["answer_regex"][0],
                "当前 primary workspace 不存在 Java 源码，结论只覆盖当前 primary。",
            )
        )
        self.assertEqual(forced_final["termination_reason_any_of"], ["final", "forced_final_protocol_violation"])
        self.assertEqual(forced_final["run_summary"], {"tool_calls": 1, "tool_errors": 0})

    def test_report_includes_bounded_diagnostics_and_run_identity(self) -> None:
        result = BenchmarkResult(
            identifier="failed-fixture",
            title="Failed fixture",
            passed=False,
            mode="live",
            answer="",
            elapsed_ms=10,
            run_summary={
                "run_id": "run-1",
                "tool_errors": 1,
                "compactions": 1,
                "zero_gain_compactions": 1,
                "provider_schema_violations": 1,
                "finalization_attempts": 2,
            },
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
        self.assertIn("provider_schema_violations=1", markdown)
        self.assertIn("finalization_attempts=2", markdown)


if __name__ == "__main__":
    unittest.main()

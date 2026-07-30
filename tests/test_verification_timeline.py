from __future__ import annotations

import unittest

from local_agent.tool_choice_queue import ToolResultSummary
from local_agent.verification_timeline import effective_workspace_write_paths
from local_agent.verification_timeline import last_workspace_write_index
from local_agent.verification_timeline import successful_tool_after_last_write
from local_agent.verification_timeline import successful_nonempty_git_diff_after_last_write
from local_agent.verification_timeline import workspace_write_happened


class VerificationTimelineTests(unittest.TestCase):
    def test_uses_the_last_real_write_not_a_dry_run_preview(self) -> None:
        results = [
            ToolResultSummary("apply_patch", "dry run", changed=False),
            ToolResultSummary("apply_patch", "Applied patch", changed=True),
            ToolResultSummary("run_tests", "OK"),
        ]

        self.assertTrue(workspace_write_happened(results))
        self.assertEqual(last_workspace_write_index(results), 1)
        self.assertTrue(successful_tool_after_last_write(results, "run_tests"))
        self.assertFalse(successful_tool_after_last_write(results, "git_diff"))

    def test_empty_diff_after_rollback_does_not_prove_current_delivery(self) -> None:
        results = [
            ToolResultSummary("apply_patch", "Applied patch", changed=True, path="src/App.py"),
            ToolResultSummary("rollback_patch", "Rollback applied", changed=True, path="src/App.py"),
            ToolResultSummary("git_diff", "(empty diff)"),
        ]

        self.assertIsNone(successful_nonempty_git_diff_after_last_write(results))

    def test_nonempty_dirty_diff_requires_current_runtime_write_path(self) -> None:
        results = [
            ToolResultSummary("apply_patch", "Applied patch", changed=True, path="src/App.py"),
            ToolResultSummary("rollback_patch", "Rollback applied", changed=True, path="src/App.py"),
            ToolResultSummary(
                "git_diff",
                "diff --git a/README.md b/README.md",
                metadata={"patch_review": {"changed_paths": ["README.md"]}},
            ),
        ]

        self.assertIsNone(successful_nonempty_git_diff_after_last_write(results))

    def test_not_run_test_result_is_not_successful_tool_evidence(self) -> None:
        results = [
            ToolResultSummary("apply_patch", "Applied patch", changed=True, path="src/App.py"),
            ToolResultSummary(
                "run_tests",
                "shell syntax rejected",
                is_error=True,
                metadata={"execution_status": "not_run", "exit_code": None},
            ),
        ]

        self.assertFalse(successful_tool_after_last_write(results, "run_tests"))

    def test_typed_staged_exec_transaction_is_a_real_write_even_on_nonzero_exit(
        self,
    ) -> None:
        transaction_id = "20260729T010203000000Z"
        result = ToolResultSummary(
            "shell",
            "[exit_code] 7",
            is_error=True,
            metadata={
                "workspace_transaction_id": transaction_id,
                "workspace_mutation_source": "container_staged_copy",
                "workspace_changed": True,
                "transaction_status": "committed",
                "changed_paths": ["src/main.py"],
                "effective_changed_paths": ["src/main.py"],
                "isolation": {
                    "workspace_transport": "staged-copy",
                    "workspace_output_commit": {
                        "state": "committed",
                        "transaction_id": transaction_id,
                    },
                },
            },
        )

        self.assertTrue(workspace_write_happened([result]))
        self.assertEqual(last_workspace_write_index([result]), 0)
        self.assertEqual(
            effective_workspace_write_paths([result]),
            ("src/main.py",),
        )

    def test_exec_metadata_cannot_forge_write_without_exact_commit_correlation(
        self,
    ) -> None:
        base = {
            "workspace_transaction_id": "tx",
            "workspace_mutation_source": "container_staged_copy",
            "workspace_changed": True,
            "transaction_status": "committed",
            "changed_paths": ["src/main.py"],
            "isolation": {
                "workspace_transport": "staged-copy",
                "workspace_output_commit": {
                    "state": "restored",
                    "transaction_id": "tx",
                },
            },
        }
        results = [
            ToolResultSummary("shell", "claimed write", metadata=base),
            ToolResultSummary(
                "run_tests",
                "claimed write",
                metadata={**base, "workspace_transaction_id": "other"},
            ),
            ToolResultSummary(
                "current_time",
                "claimed write",
                metadata={
                    **base,
                    "isolation": {
                        "workspace_transport": "staged-copy",
                        "workspace_output_commit": {
                            "state": "committed",
                            "transaction_id": "tx",
                        },
                    },
                },
            ),
        ]

        self.assertFalse(workspace_write_happened(results))
        self.assertEqual(effective_workspace_write_paths(results), ())

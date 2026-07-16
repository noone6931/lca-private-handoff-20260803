from __future__ import annotations

import unittest

from local_agent.tool_choice_queue import ToolResultSummary
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

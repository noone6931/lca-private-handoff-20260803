from __future__ import annotations

import unittest

from local_agent.tool_choice_queue import ToolResultSummary
from local_agent.verification_timeline import last_workspace_write_index
from local_agent.verification_timeline import successful_tool_after_last_write
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

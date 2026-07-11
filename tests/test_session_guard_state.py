from __future__ import annotations

import unittest

from local_agent.session_guard_state import SessionGuardState
from local_agent.tools.base import ToolResult


class SessionGuardStateTests(unittest.TestCase):
    def test_duplicate_tool_call_is_blocked_after_recent_window_threshold(self) -> None:
        state = SessionGuardState()

        for _ in range(3):
            self.assertIsNone(
                state.before_tool(
                    read_file_key=None,
                    signature="read_file:README.md",
                    search_pattern_key=None,
                    lsp_symbol_query_key=None,
                    semantic_exploration_key=None,
                )
            )
        decision = state.before_tool(
            read_file_key=None,
            signature="read_file:README.md",
            search_pattern_key=None,
            lsp_symbol_query_key=None,
            semantic_exploration_key=None,
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.kind, "duplicate_tool")
        self.assertEqual(decision.prior_count, 3)
        self.assertEqual(state.hit_count("duplicate_tool"), 1)

    def test_successful_lsp_result_clears_prior_useless_query_window(self) -> None:
        state = SessionGuardState()
        for index in range(12):
            state.record_result(
                search_pattern_key=None,
                lsp_symbol_query_key=f"missing-{index}",
                result=ToolResult("No matches", useless=True),
            )

        blocked = state.before_tool(
            read_file_key=None,
            signature="lsp_symbols:missing-final",
            search_pattern_key=None,
            lsp_symbol_query_key="missing-final",
            semantic_exploration_key=None,
        )
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.kind, "useless_lsp_symbol")

        state.record_result(
            search_pattern_key=None,
            lsp_symbol_query_key="real-symbol",
            result=ToolResult("src/App.java:1:class App {}"),
        )
        allowed = state.before_tool(
            read_file_key=None,
            signature="lsp_symbols:real-symbol",
            search_pattern_key=None,
            lsp_symbol_query_key="real-symbol",
            semantic_exploration_key=None,
        )
        self.assertIsNone(allowed)

    def test_repeated_unknown_tool_is_blocked_after_prior_actionable_errors(self) -> None:
        state = SessionGuardState()
        for _ in range(2):
            self.assertIsNone(
                state.before_tool(
                    read_file_key=None,
                    signature="run_shell:{}",
                    search_pattern_key=None,
                    lsp_symbol_query_key=None,
                    semantic_exploration_key=None,
                    unknown_tool_name="run_shell",
                )
            )
            state.record_result(
                search_pattern_key=None,
                lsp_symbol_query_key=None,
                unknown_tool_name="run_shell",
                result=ToolResult(
                    "Unknown tool: run_shell. Available related tools: shell.",
                    is_error=True,
                    metadata={"unknown_tool": True, "suggested_tools": ["shell"]},
                ),
            )

        decision = state.before_tool(
            read_file_key=None,
            signature="run_shell:{}",
            search_pattern_key=None,
            lsp_symbol_query_key=None,
            semantic_exploration_key=None,
            unknown_tool_name="run_shell",
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.kind, "unknown_tool")
        self.assertEqual(decision.prior_count, 2)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from local_agent.steering.tool_loop import ToolLoopSignals
from local_agent.steering.tool_loop import ToolLoopSteeringRegistry
from local_agent.steering.tool_loop import is_filename_search_misuse


def _signals(**overrides: object) -> ToolLoopSignals:
    values: dict[str, object] = {
        "duplicate_skipped": False,
        "duplicate_tool_name": "read_file",
        "duplicate_guard_hits": 0,
        "useless_search_skipped": False,
        "useless_search_guard_hits": 0,
        "useless_lsp_skipped": False,
        "useless_lsp_guard_hits": 0,
        "repeated_read_skipped": False,
        "repeated_read_guard_hits": 0,
        "semantic_exploration_skipped": False,
        "semantic_exploration_guard_hits": 0,
        "read_file_evidence": "",
        "request_summary": "",
    }
    values.update(overrides)
    return ToolLoopSignals(**values)  # type: ignore[arg-type]


class ToolLoopSteeringRegistryTests(unittest.TestCase):
    def test_classifies_filename_search_misuse_for_telemetry_without_blocking_search(self) -> None:
        self.assertTrue(is_filename_search_misuse("search_code", {"pattern": r"\.java$"}))
        self.assertTrue(is_filename_search_misuse("search_code", {"pattern": "src/main/java"}))
        self.assertTrue(is_filename_search_misuse("search_code", {"pattern": "**/*.vue"}))
        self.assertFalse(is_filename_search_misuse("search_code", {"pattern": "settlementStatus"}))
        self.assertFalse(is_filename_search_misuse("glob_files", {"paths": ["**/*.java"]}))

    def test_uses_explicit_priority_when_multiple_guards_fire(self) -> None:
        registry = ToolLoopSteeringRegistry()

        decision = registry.decide(
            _signals(
                duplicate_skipped=True,
                repeated_read_skipped=True,
                duplicate_guard_hits=1,
                repeated_read_guard_hits=1,
            )
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.kind, "repeated_read_file_final_answer")
        self.assertEqual(registry.count("repeated_read_file_final_answer"), 1)
        self.assertEqual(registry.count("duplicate_tool_final_answer"), 0)

    def test_limits_soft_steers_then_returns_hard_stop_reason(self) -> None:
        registry = ToolLoopSteeringRegistry()
        signals = _signals(repeated_read_skipped=True, repeated_read_guard_hits=4)

        self.assertIsNotNone(registry.decide(signals))
        self.assertIsNotNone(registry.decide(signals))
        self.assertIsNone(registry.decide(signals))
        self.assertEqual(registry.termination_reason(signals), "repeated_read_file_guard")

    def test_hard_stop_priority_matches_steering_priority(self) -> None:
        registry = ToolLoopSteeringRegistry()

        reason = registry.termination_reason(
            _signals(
                duplicate_guard_hits=8,
                repeated_read_guard_hits=4,
            )
        )

        self.assertEqual(reason, "repeated_read_file_guard")

    def test_reset_clears_steer_counts(self) -> None:
        registry = ToolLoopSteeringRegistry()
        registry.decide(_signals(duplicate_skipped=True, duplicate_guard_hits=1))

        registry.reset()

        self.assertEqual(registry.count("duplicate_tool_final_answer"), 0)

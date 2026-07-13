from __future__ import annotations

import unittest
from unittest.mock import patch

from local_agent.run_collector import RunCollector


class RunCollectorTests(unittest.TestCase):
    def test_collects_counts_and_calculates_deltas_from_run_start(self) -> None:
        collector = RunCollector()
        collector.start(
            "run-1",
            "read the project",
            10.0,
            guard_start={"duplicate_tool": 2},
            steer_start={"duplicate_tool_final_answer": 1},
        )
        collector.record_llm_request()
        collector.record_tool_started("read_file")
        collector.record_tool_result(name="read_file", is_error=False, useless=True, metadata={})
        collector.mark_llm_context_summary()
        collector.record_context_compaction(estimated_tokens_before=100, estimated_tokens_after=70)

        with patch("local_agent.run_collector.time.monotonic", return_value=10.125):
            summary = collector.finish(
                "final",
                guard_values={"duplicate_tool": 3},
                steering_values={"duplicate_tool_final_answer": 2, "completion_audit": 1},
            )

        self.assertEqual(summary["elapsed_ms"], 125)
        self.assertEqual(summary["llm_requests"], 1)
        self.assertEqual(summary["tool_calls"], 1)
        self.assertEqual(summary["useless_tool_results"], 1)
        self.assertEqual(summary["compactions"], 1)
        self.assertEqual(summary["effective_compactions"], 1)
        self.assertEqual(summary["zero_gain_compactions"], 0)
        self.assertEqual(summary["compaction_estimated_token_reduction"], 30)
        self.assertEqual(summary["llm_context_summaries"], 1)
        self.assertEqual(summary["guard_hits"], {"duplicate_tool": 1})
        self.assertEqual(
            summary["steering_counts"],
            {"duplicate_tool_final_answer": 1, "completion_audit": 1},
        )

    def test_collects_file_discovery_and_unknown_tool_metrics(self) -> None:
        collector = RunCollector()
        collector.start("run-1", "inventory", 1.0, guard_start={}, steer_start={})
        collector.record_tool_started("glob_files")
        collector.record_tool_result(
            name="glob_files",
            is_error=False,
            useless=False,
            metadata={"complete": False, "negative_evidence_type": "path_no_match"},
        )
        collector.record_tool_started("run_shell")
        collector.record_tool_result(
            name="run_shell",
            is_error=True,
            useless=False,
            metadata={
                "unknown_tool": True,
                "suggested_tools": ["shell"],
                "filename_search_misuse": True,
            },
        )

        summary = collector.finish("final", guard_values={}, steering_values={})

        self.assertEqual(summary["file_discovery_calls"], 1)
        self.assertEqual(summary["file_discovery_incomplete_results"], 1)
        self.assertEqual(summary["file_discovery_no_match_results"], 1)
        self.assertEqual(summary["unknown_tool_calls"], 1)
        self.assertEqual(summary["unknown_tool_suggestions"], 1)
        self.assertEqual(summary["filename_search_misuse_calls"], 1)

    def test_start_replaces_prior_run_counters_and_pending_summary_mode(self) -> None:
        collector = RunCollector()
        collector.start("run-1", "first", 1.0, guard_start={}, steer_start={})
        collector.record_llm_request()
        collector.mark_local_context_summary()

        collector.start("run-2", "second", 2.0, guard_start={}, steer_start={})
        collector.record_context_compaction()

        summary = collector.finish("final", guard_values={}, steering_values={})
        self.assertEqual(summary["run_id"], "run-2")
        self.assertEqual(summary["llm_requests"], 0)
        self.assertEqual(summary["local_context_summaries"], 0)

    def test_tracks_consecutive_zero_gain_compactions(self) -> None:
        collector = RunCollector()
        collector.start("run-1", "compact", 1.0, guard_start={}, steer_start={})
        collector.record_context_compaction(estimated_tokens_before=100, estimated_tokens_after=100)
        collector.record_context_compaction(estimated_tokens_before=100, estimated_tokens_after=101)
        collector.record_context_compaction(estimated_tokens_before=120, estimated_tokens_after=80)
        collector.record_context_compaction(estimated_tokens_before=90, estimated_tokens_after=90)

        summary = collector.finish("final", guard_values={}, steering_values={})

        self.assertEqual(summary["compactions"], 4)
        self.assertEqual(summary["effective_compactions"], 1)
        self.assertEqual(summary["zero_gain_compactions"], 3)
        self.assertEqual(summary["max_consecutive_zero_gain_compactions"], 2)
        self.assertEqual(summary["compaction_estimated_token_reduction"], 40)

    def test_records_pre_review_audit_exhaustion(self) -> None:
        collector = RunCollector()
        collector.start("run-1", "review design", 1.0, guard_start={}, steer_start={})
        collector.record_pre_review_audit(
            categories=("completion_audit", "negative_existence"),
            exhausted=False,
        )
        collector.record_pre_review_audit(
            categories=("completion_audit",),
            exhausted=True,
        )

        summary = collector.finish("pre_review_audit_unverified", guard_values={}, steering_values={})

        self.assertEqual(summary["pre_review_audit"], {
            "rounds": 2,
            "categories": {"completion_audit": 2, "negative_existence": 1},
            "exhausted": 1,
        })

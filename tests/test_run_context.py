from __future__ import annotations

import unittest

from local_agent.run_context import EvidenceRecord
from local_agent.run_context import RunContext
from local_agent.task_contract import generate_requirement_contract
from local_agent.tool_choice_queue import ToolResultSummary


class RunContextTests(unittest.TestCase):
    def test_begin_replaces_task_state_and_preserves_explicit_run_metadata(self) -> None:
        context = RunContext()
        context.read_file_range_counts[("src/Old.java", 1, "tag")] = 3
        context.read_file_evidence_paths.append("src/Old.java")
        context.evidence_records.append(EvidenceRecord("read_file", "src/Old.java", "old"))
        context.tool_choice_results.append(ToolResultSummary("read_file"))
        context.final_answer_steers["completion_audit"] = 2
        context.tool_loop_steering.decide(
            _duplicate_tool_signals()
        )

        contract = generate_requirement_contract("请只读分析当前项目。")
        context.begin(
            run_id="run-2",
            started_monotonic=10.0,
            deadline_monotonic=20.0,
            run_start_index=7,
            git_baseline={"head": "abc"},
            prompt="请只读分析当前项目。",
            requirement_contract=contract,
            requirement_contract_context="contract context",
            design_evidence_roots=("/workspace/backend", "/workspace/frontend"),
        )

        self.assertEqual(context.run_id, "run-2")
        self.assertEqual(context.deadline_monotonic, 20.0)
        self.assertEqual(context.run_start_index, 7)
        self.assertEqual(context.git_baseline, {"head": "abc"})
        self.assertIs(context.requirement_contract, contract)
        self.assertEqual(context.design_evidence_coverage.roots, ("/workspace/backend", "/workspace/frontend"))
        self.assertEqual(context.read_file_range_counts, {})
        self.assertEqual(context.read_file_evidence_paths, [])
        self.assertEqual(context.evidence_records, [])
        self.assertEqual(context.tool_choice_results, [])
        self.assertEqual(context.final_answer_steers, {})
        self.assertEqual(context.tool_loop_steering.count("duplicate_tool_final_answer"), 0)


def _duplicate_tool_signals():
    from local_agent.steering.tool_loop import ToolLoopSignals

    return ToolLoopSignals(
        duplicate_skipped=True,
        duplicate_tool_name="read_file",
        duplicate_guard_hits=1,
        useless_search_skipped=False,
        useless_search_guard_hits=0,
        useless_lsp_skipped=False,
        useless_lsp_guard_hits=0,
        repeated_read_skipped=False,
        repeated_read_guard_hits=0,
        semantic_exploration_skipped=False,
        semantic_exploration_guard_hits=0,
        read_file_evidence="",
        request_summary="",
    )

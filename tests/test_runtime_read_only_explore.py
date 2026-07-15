from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from local_agent.design_evidence import DesignEvidenceCoverageSteerer
from local_agent.run_collector import RunCollector
from local_agent.runtime_read_only_explore import RuntimeReadOnlyExplorePhase
from local_agent.runtime_tool_choice_directive import RuntimeToolChoiceDirectivePhase
from local_agent.task_contract import generate_requirement_contract
from local_agent.tool_choice_directive import ToolChoiceDirectiveOwner
from local_agent.tool_choice_queue import ToolChoiceDecision
from local_agent.tool_observation import ToolResultSummary


class _Session:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def append(self, kind: str, payload: dict[str, object]) -> None:
        self.events.append((kind, payload))


class RuntimeReadOnlyExploreTests(unittest.TestCase):
    def _runtime(self, root: Path, *, readiness: bool) -> tuple[SimpleNamespace, RuntimeReadOnlyExplorePhase]:
        source = root / "src" / "Target.java"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("class Target {}\n", encoding="utf-8")
        contract = generate_requirement_contract(
            "只读进行技术设计，选择可实施切片；若核心依赖未闭合则 blocked；不写代码。"
            if readiness
            else "请实现一个源文件变更。"
        )
        coverage = DesignEvidenceCoverageSteerer()
        coverage.reset((str(root),))
        collector = RunCollector()
        collector.start("run-1", "bounded candidate", 1.0, guard_start={}, steer_start={})
        run = SimpleNamespace(
            requirement_contract=contract,
            design_evidence_coverage=coverage,
            tool_choice_results=[
                ToolResultSummary(
                    "search_code",
                    f"{source}:1: class Target",
                    metadata={"evidence_root": str(root), "evidence_paths": [str(source)]},
                )
            ],
            read_only_explore_finalized=False,
            tool_choice_directive=ToolChoiceDirectiveOwner(),
            collector=collector,
        )
        runtime = SimpleNamespace(_run=run, _session=_Session())
        phase = RuntimeReadOnlyExplorePhase(runtime)
        runtime._read_only_explore_phase = phase
        return runtime, phase

    @staticmethod
    def _candidate_decision(path: str) -> ToolChoiceDecision:
        return ToolChoiceDecision(
            steering_required=True,
            allowed_tool_names=frozenset({"read_file"}),
            reason="bounded candidate read",
            rule_id="read_only_profile_explore_candidate_read",
            requirement_identity=f"candidate:{path}",
            missing_requirements=("code_read",),
            preferred_tool_names=("read_file",),
            scoped_read_paths=(path,),
            scoped_read_budget=1,
            read_only_unlocated_on_exhaustion=True,
        )

    def test_readiness_candidate_error_exhaustion_marks_root_unlocated_and_finalizes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            runtime, _ = self._runtime(root, readiness=True)
            path = str(root / "src" / "Target.java")
            directive = RuntimeToolChoiceDirectivePhase(runtime)
            decision = self._candidate_decision(path)

            self.assertEqual(directive.begin_decision(decision).kind, "none")
            for index in range(3):
                runtime._run.tool_choice_results.append(
                    ToolResultSummary("read_file", "permission denied", is_error=True, path=path, metadata={"resolved_path": path})
                )
                outcome = directive.begin_decision(decision)
                self.assertEqual(outcome.kind, "force")
                self.assertEqual(outcome.requeue_required, index == 2)

        self.assertTrue(runtime._run.read_only_explore_finalized)
        self.assertTrue(
            any(
                result.metadata.get("candidate_read_exhausted") and result.metadata.get("read_only_explore_unlocated")
                for result in runtime._run.tool_choice_results
            )
        )
        self.assertTrue(
            any(payload.get("event") == "candidate_read_exhausted_finalized" for _, payload in runtime._session.events)
        )

    def test_non_readiness_candidate_exhaustion_remains_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            runtime, _ = self._runtime(root, readiness=False)
            path = str(root / "src" / "Target.java")
            directive = RuntimeToolChoiceDirectivePhase(runtime)
            decision = self._candidate_decision(path)

            self.assertEqual(directive.begin_decision(decision).kind, "none")
            for expected in ("force", "force", "exhausted"):
                runtime._run.tool_choice_results.append(
                    ToolResultSummary("read_file", "permission denied", is_error=True, path=path, metadata={"resolved_path": path})
                )
                outcome = directive.begin_decision(decision)
                self.assertEqual(outcome.kind, expected)

        self.assertFalse(runtime._run.read_only_explore_finalized)
        self.assertEqual(outcome.terminal_reason, "tool_choice_exact_exhausted")

"""Runtime facade for typed, bounded read-only exploration."""
from __future__ import annotations

from typing import Any, Protocol

from .read_only_explore import OBSERVATION_TOOLS
from .read_only_explore import ReadOnlyExploreDecision
from .read_only_explore import evaluate_read_only_explore
from .tool_observation import ToolResultSummary


class ReadOnlyExploreRuntimePort(Protocol):
    _run: Any
    _session: Any
    _workspace_context: Any


class RuntimeReadOnlyExplorePhase:
    """Enforce the typed explore cap at the tool-result boundary.

    Queue projection owns which tools are visible for the next model turn. This
    facade owns the in-flight batch boundary so a provider cannot overshoot the
    same hard budget by returning many otherwise-valid calls at once.
    """

    def __init__(self, runtime: ReadOnlyExploreRuntimePort) -> None:
        self._runtime = runtime

    def after_tool_result(self, tool_name: str) -> ReadOnlyExploreDecision | None:
        if tool_name not in OBSERVATION_TOOLS:
            return None
        runtime = self._runtime
        if runtime._run.read_only_explore_finalized:
            return None
        contract = runtime._run.requirement_contract
        if contract is None:
            return None
        decision = evaluate_read_only_explore(
            profile=contract.read_only_review_profile,
            tool_results=runtime._run.tool_choice_results,
            code_roots=runtime._run.design_evidence_coverage.roots or tuple(
                str(root) for root in runtime._workspace_context.all_roots
            ),
        )
        if not decision.is_applicable:
            return None
        if decision.action == "precise" and decision.read_candidates:
            runtime._session.append(
                "read_only_explore",
                {
                    "event": "direct_read_transition",
                    "observations": decision.observation_calls,
                    "successful_observations": decision.successful_observations,
                    "soft_budget": decision.soft_budget,
                    "missing_roots": list(decision.missing_roots),
                    "read_candidates": list(decision.read_candidates),
                },
            )
            return decision
        if decision.action != "finalize":
            return None
        runtime._run.read_only_explore_finalized = True
        self._record_missing_root_observations(decision)
        runtime._session.append(
            "read_only_explore",
            {
                "event": "hard_budget_reached",
                "observations": decision.observation_calls,
                "successful_observations": decision.successful_observations,
                "hard_budget": decision.hard_budget,
                "missing_roots": list(decision.missing_roots),
            },
        )
        return decision

    def record_suppressed_calls(self, count: int, decision: ReadOnlyExploreDecision) -> None:
        if count <= 0:
            return
        runtime = self._runtime
        runtime._run.collector.record_suppressed_tool_executions(count)
        runtime._session.append(
            "read_only_explore",
            {
                "event": "batch_calls_suppressed",
                "count": count,
                "observations": decision.observation_calls,
                "successful_observations": decision.successful_observations,
                "hard_budget": decision.hard_budget,
                "transition": "direct_read" if decision.action == "precise" else "finalize",
            },
        )

    def suppression_message(self, decision: ReadOnlyExploreDecision) -> str:
        if decision.action == "precise":
            return "Skipped because typed search/LSP evidence identified bounded direct-read candidates; read those candidates before further discovery."
        return "Skipped because the bounded read-only exploration budget was reached; produce a scoped candidate final answer."

    def _record_missing_root_observations(self, decision: ReadOnlyExploreDecision) -> None:
        """Keep an explicit incomplete coverage fact for the reviewer/report owner."""

        runtime = self._runtime
        for root in decision.missing_roots:
            runtime._run.tool_choice_results.append(
                ToolResultSummary(
                    "read_only_explore",
                    (
                        "No successful direct read covered this root before the bounded exploration phase ended; "
                        "the target remains unlocated in this root."
                    ),
                    useless=True,
                    path=root,
                    metadata={
                        "read_only_explore_incomplete": True,
                        "evidence_root": root,
                        "evidence_root_label": root,
                        "evidence_scope": "root_local",
                        "attempts": decision.observation_calls,
                        "successful_observations": decision.successful_observations,
                        "hard_budget": decision.hard_budget,
                    },
                )
            )

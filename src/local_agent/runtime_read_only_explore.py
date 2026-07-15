"""Runtime facade for typed, bounded read-only exploration."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .patch.anchored import PatchError
from .patch.anchored import resolve_workspace_path
from .read_only_explore import OBSERVATION_TOOLS
from .read_only_explore import ReadOnlyExploreDecision
from .read_only_explore import evaluate_read_only_explore
from .tool_observation import ToolResultSummary


class ReadOnlyExploreRuntimePort(Protocol):
    _run: Any
    _session: Any
    _workspace_context: Any


@dataclass(frozen=True)
class ExploreBatchPlan:
    """Same-assistant-turn execution plan after a typed explore transition."""

    suppress_calls: tuple[dict[str, Any], ...] = ()
    continue_batch: bool = False


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
            code_roots=runtime._run.design_evidence_coverage.roots,
            requested_source_artifacts=contract.source_artifacts,
            strict_relevance=contract.implementation_readiness_required,
        )
        if not decision.is_applicable:
            return None
        if decision.action == "precise" and decision.read_candidates:
            runtime._session.append(
                "read_only_explore",
                {
                    "event": "read_candidate_hint",
                    "observations": decision.observation_calls,
                    "successful_observations": decision.successful_observations,
                    "soft_budget": decision.soft_budget,
                    "missing_roots": list(decision.missing_roots),
                    "read_candidates": list(decision.read_candidates),
                },
            )
            return None
        if decision.action != "finalize":
            return None
        self._finalize(decision, event="hard_budget_reached")
        return decision

    def mark_candidate_read_unlocated(self, paths: tuple[str, ...], *, reason: str) -> bool:
        """Close one exhausted readiness read requirement as root-local unlocated.

        This is deliberately narrower than generic exact-tool exhaustion.  The
        primary model still had to attempt the typed candidate through the
        bounded directive; only a read-only readiness investigation may turn
        its exhausted candidate into an honest coverage boundary.
        """

        runtime = self._runtime
        contract = runtime._run.requirement_contract
        if (
            contract is None
            or not contract.implementation_readiness_required
            or contract.read_only_review_profile not in {"owner_impact", "design"}
        ):
            return False
        roots = tuple(runtime._run.design_evidence_coverage.roots)
        if not roots:
            return False
        selected_roots = tuple(
            dict.fromkeys(
                root
                for path in paths
                for root in roots
                if path == root or path.startswith(root.rstrip("/") + "/")
            )
        )
        if len(selected_roots) != 1:
            return False
        root = selected_roots[0]
        if any(
            result.name == "read_only_explore"
            and result.metadata.get("candidate_read_exhausted")
            and result.metadata.get("evidence_root") == root
            for result in runtime._run.tool_choice_results
        ):
            self._finalize_if_complete(event="candidate_read_exhausted_finalized")
            return True
        runtime._run.tool_choice_results.append(
            ToolResultSummary(
                "read_only_explore",
                (
                    "The bounded candidate read requirement exhausted after provider detours or invalid arguments; "
                    "the target remains unlocated in this root."
                ),
                useless=True,
                path=root,
                metadata={
                    "read_only_explore_incomplete": True,
                    "read_only_explore_unlocated": True,
                    "candidate_read_exhausted": True,
                    "evidence_root": root,
                    "evidence_root_label": root,
                    "evidence_scope": "root_local",
                    "reason": reason,
                    "scoped_read_paths": list(paths),
                },
            )
        )
        runtime._session.append(
            "read_only_explore",
            {
                "event": "candidate_read_exhausted_unlocated",
                "root": root,
                "reason": reason,
                "scoped_read_paths": list(paths),
            },
        )
        self._finalize_if_complete(event="candidate_read_exhausted_finalized")
        return True

    def _finalize_if_complete(self, *, event: str) -> None:
        runtime = self._runtime
        contract = runtime._run.requirement_contract
        if contract is None:
            return
        decision = evaluate_read_only_explore(
            profile=contract.read_only_review_profile,
            tool_results=runtime._run.tool_choice_results,
            code_roots=runtime._run.design_evidence_coverage.roots,
            requested_source_artifacts=contract.source_artifacts,
            strict_relevance=contract.implementation_readiness_required,
        )
        if decision.is_applicable and decision.action == "finalize":
            self._finalize(decision, event=event)

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

    def plan_remaining_batch(
        self,
        decision: ReadOnlyExploreDecision,
        remaining_calls: list[dict[str, Any]],
    ) -> ExploreBatchPlan:
        """Suppress detours while preserving one root-fair precision call.

        The agent loop can only advance to the next tool call in order, so the
        plan returns any calls to pair with synthetic results before the next
        executable call.  This keeps provider transcript pairing intact while
        preventing a duplicate call for one root from hiding a later call for a
        still-uncovered root in the same assistant batch.
        """

        if decision.action == "finalize":
            return ExploreBatchPlan(tuple(remaining_calls), continue_batch=False)
        if decision.action != "precise" or not remaining_calls:
            return ExploreBatchPlan()
        if not decision.read_candidates:
            # Discovery evidence changes the root-local directive for the next
            # turn.  Do not execute a provider's stale parallel glob/search
            # detours before Queue can project that new requirement.
            return ExploreBatchPlan(tuple(remaining_calls), continue_batch=False)
        candidate_roots = self._candidate_roots(decision.read_candidates, decision.missing_roots)
        suppress: list[dict[str, Any]] = []
        kept_roots: set[str] = set()
        for tool_call in remaining_calls:
            function = tool_call.get("function") if isinstance(tool_call, dict) else None
            name = function.get("name") if isinstance(function, dict) else ""
            root = self._tool_call_root(function.get("arguments") if isinstance(function, dict) else "{}", decision.missing_roots)
            if name == "read_file":
                if root is not None and root in candidate_roots and root not in kept_roots:
                    self._runtime._session.append(
                        "read_only_explore",
                        {"event": "batch_root_fair_defer", "tool": name, "root": root},
                    )
                    kept_roots.add(root)
                    return ExploreBatchPlan(tuple(suppress), continue_batch=True)
                suppress.append(tool_call)
                continue
            if name not in OBSERVATION_TOOLS:
                suppress.append(tool_call)
                continue
            if root is not None and root not in candidate_roots and root not in kept_roots:
                self._runtime._session.append(
                    "read_only_explore",
                    {"event": "batch_root_fair_defer", "tool": name, "root": root},
                )
                kept_roots.add(root)
                return ExploreBatchPlan(tuple(suppress), continue_batch=True)
            suppress.append(tool_call)
        return ExploreBatchPlan(tuple(suppress), continue_batch=False)

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

    def _finalize(self, decision: ReadOnlyExploreDecision, *, event: str) -> None:
        """Record the terminal explore handoff once a bounded outcome exists per root."""

        runtime = self._runtime
        runtime._run.read_only_explore_finalized = True
        self._record_missing_root_observations(decision)
        runtime._session.append(
            "read_only_explore",
            {
                "event": event,
                "observations": decision.observation_calls,
                "successful_observations": decision.successful_observations,
                "hard_budget": decision.hard_budget,
                "missing_roots": list(decision.missing_roots),
            },
        )

    @staticmethod
    def _candidate_roots(paths: tuple[str, ...], roots: tuple[str, ...]) -> set[str]:
        covered: set[str] = set()
        for raw in paths:
            try:
                path = str(Path(raw).resolve())
            except OSError:
                path = raw
            for root in roots:
                if path == root or path.startswith(root + "/"):
                    covered.add(root)
        return covered

    def _tool_call_root(self, raw_arguments: Any, roots: tuple[str, ...]) -> str | None:
        try:
            arguments = raw_arguments if isinstance(raw_arguments, dict) else json.loads(raw_arguments or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        raw_path = arguments.get("path") if isinstance(arguments, dict) else None
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        try:
            resolved = resolve_workspace_path(
                self._runtime._workspace_context.primary,
                raw_path,
                self._runtime._workspace_context.additional_roots,
            )
        except PatchError:
            return None
        rendered = str(resolved)
        return next((root for root in roots if rendered == root or rendered.startswith(root + "/")), None)

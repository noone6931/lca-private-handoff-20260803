from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ...evidence.documents import DocumentArtifactRequirement
from ..contracts import is_inspection_forbidden
from .decision import CODE_EVIDENCE_ALLOWED_TOOL_NAMES
from .decision import CODE_EVIDENCE_TOOL_NAMES
from .decision import DEFAULT_TOOL_NAMES
from .decision import DOCUMENT_ONLY_TOOL_NAMES
from .decision import MAX_WORKSPACE_INVENTORY_DISCOVERY_CALLS
from .decision import MAX_WORKSPACE_INVENTORY_DISCOVERY_CALLS_PER_ROOT
from .decision import PLANNER_EXPLORE_TOOL_NAMES
from .decision import READ_ONLY_FORBIDDEN_TOOL_NAMES
from .decision import REQUIREMENT_DOC_TOOL_NAMES
from .decision import SoftToolDirective
from .decision import ToolChoiceDecision
from .decision import WORKSPACE_INVENTORY_TOOL_NAMES
from .decision import WORKSPACE_INVENTORY_DISCOVERY_TOOL_NAMES
from .decision import _available_tool_names
from .decision import _normalize_tool_result
from .decision import _tool_name_set
from .decision import session_evidence_reuse_directive
from .decision import tool_choice_signature_count
from .decision import tool_choice_steering_identity
from .decision import tool_choice_steering_message
from .decision import tool_choice_steering_signature
from .implementation import CANDIDATE_DELIVERY_TOOL_NAMES
from .implementation import CANDIDATE_DIFF_TOOL_NAMES
from .implementation import CANDIDATE_REMEDIATION_TOOL_NAMES
from .implementation import CANDIDATE_TEST_TOOL_NAMES
from .implementation import MAX_CANDIDATE_PATCH_PREVIEW_FAILURES
from .implementation import MAX_CANDIDATE_READ_REVISITS
from .implementation import POST_DIFF_REMEDIATION_TOOL_NAMES
from .implementation import autonomous_small_change_candidate_paths
from .implementation import evaluate_implementation_phase
from .read_only import _preferred_evidence_tools
from .read_only import evaluate_read_only_phase
from .classification import is_read_only_task
from ...tools.observation import ToolResultSummary
from ...evidence.timeline import WRITE_TOOL_NAMES


class RequiredToolGate:
    def evaluate(
        self,
        *,
        task_kind: str,
        prompt: str,
        tool_names: Iterable[str] | None = None,
        tool_results: Iterable[ToolResultSummary | Mapping[str, Any] | str] | None = None,
        available_tool_names: Iterable[str] | None = None,
        design_evidence_roots: Iterable[str] | None = None,
        workspace_roots: Iterable[str] | None = None,
        evidence_domain: str | None = None,
        read_only_review_profile: str | None = None,
        implementation_readiness_required: bool = False,
        document_artifacts: Iterable[DocumentArtifactRequirement] = (),
        source_artifacts: Iterable[str] = (),
    ) -> ToolChoiceDecision:
        return evaluate_tool_choice_state(
            task_kind=task_kind,
            prompt=prompt,
            tool_names=tool_names,
            tool_results=tool_results,
            available_tool_names=available_tool_names,
            design_evidence_roots=design_evidence_roots,
            workspace_roots=workspace_roots,
            evidence_domain=evidence_domain,
            read_only_review_profile=read_only_review_profile,
            implementation_readiness_required=implementation_readiness_required,
            document_artifacts=document_artifacts,
            source_artifacts=source_artifacts,
        )


class ToolChoiceQueue(RequiredToolGate):
    """Stable facade for the deterministic tool-choice phase owners."""


def evaluate_tool_choice_state(
    *,
    task_kind: str,
    prompt: str,
    tool_names: Iterable[str] | None = None,
    tool_results: Iterable[ToolResultSummary | Mapping[str, Any] | str] | None = None,
    available_tool_names: Iterable[str] | None = None,
    design_evidence_roots: Iterable[str] | None = None,
    workspace_roots: Iterable[str] | None = None,
    evidence_domain: str | None = None,
    read_only_review_profile: str | None = None,
    implementation_readiness_required: bool = False,
    document_artifacts: Iterable[DocumentArtifactRequirement] = (),
    source_artifacts: Iterable[str] = (),
) -> ToolChoiceDecision:
    if is_inspection_forbidden(prompt):
        return ToolChoiceDecision(
            steering_required=False,
            allowed_tool_names=frozenset(),
            reason="inspection_forbidden: semantic-only task must not inspect the workspace.",
            rule_id="inspection_forbidden",
        )
    results = tuple(_normalize_tool_result(result) for result in (tool_results or ()))
    seen_tool_names = _tool_name_set(tool_names, results)
    all_tools = _available_tool_names(available_tool_names)
    read_only = is_read_only_task(task_kind, prompt)
    allowed_tools = all_tools - READ_ONLY_FORBIDDEN_TOOL_NAMES if read_only else all_tools

    read_only_decision = evaluate_read_only_phase(
        task_kind=task_kind,
        prompt=prompt,
        results=results,
        seen_tool_names=seen_tool_names,
        allowed_tools=allowed_tools,
        read_only=read_only,
        design_evidence_roots=design_evidence_roots,
        workspace_roots=workspace_roots,
        evidence_domain=evidence_domain,
        read_only_review_profile=read_only_review_profile,
        implementation_readiness_required=implementation_readiness_required,
        document_artifacts=tuple(document_artifacts),
        source_artifacts=source_artifacts,
    )
    if read_only_decision is not None:
        return read_only_decision

    evidence_preferred = _preferred_evidence_tools(results)
    implementation_decision = evaluate_implementation_phase(
        task_kind=task_kind,
        prompt=prompt,
        seen_tool_names=seen_tool_names,
        results=results,
        allowed_tools=allowed_tools,
        evidence_preferred=evidence_preferred,
    )
    if implementation_decision is not None:
        return implementation_decision

    reason = "all required tool-choice gates satisfied."
    if read_only:
        reason = "read_only restrictions applied; all required tool-choice gates satisfied."
    elif evidence_preferred:
        reason = "code evidence gate satisfied; read_file remains preferred when a candidate file exists."
    return ToolChoiceDecision(
        steering_required=False,
        allowed_tool_names=allowed_tools,
        reason=reason,
        preferred_tool_names=evidence_preferred,
    )


__all__ = [
    "CODE_EVIDENCE_ALLOWED_TOOL_NAMES",
    "CODE_EVIDENCE_TOOL_NAMES",
    "DOCUMENT_ONLY_TOOL_NAMES",
    "CANDIDATE_DELIVERY_TOOL_NAMES",
    "CANDIDATE_DIFF_TOOL_NAMES",
    "CANDIDATE_REMEDIATION_TOOL_NAMES",
    "CANDIDATE_TEST_TOOL_NAMES",
    "DEFAULT_TOOL_NAMES",
    "MAX_CANDIDATE_READ_REVISITS",
    "MAX_CANDIDATE_PATCH_PREVIEW_FAILURES",
    "PLANNER_EXPLORE_TOOL_NAMES",
    "POST_DIFF_REMEDIATION_TOOL_NAMES",
    "READ_ONLY_FORBIDDEN_TOOL_NAMES",
    "REQUIREMENT_DOC_TOOL_NAMES",
    "WORKSPACE_INVENTORY_TOOL_NAMES",
    "WORKSPACE_INVENTORY_DISCOVERY_TOOL_NAMES",
    "MAX_WORKSPACE_INVENTORY_DISCOVERY_CALLS_PER_ROOT",
    "MAX_WORKSPACE_INVENTORY_DISCOVERY_CALLS",
    "RequiredToolGate",
    "SoftToolDirective",
    "ToolChoiceDecision",
    "ToolChoiceQueue",
    "ToolResultSummary",
    "WRITE_TOOL_NAMES",
    "autonomous_small_change_candidate_paths",
    "evaluate_tool_choice_state",
    "session_evidence_reuse_directive",
    "tool_choice_signature_count",
    "tool_choice_steering_identity",
    "tool_choice_steering_message",
    "tool_choice_steering_signature",
]

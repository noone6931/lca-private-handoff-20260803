"""Typed workflow-profile selection and capability projection."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from .contracts import RequirementContract


WorkflowProfileSelector = Literal["auto", "coding", "enterprise-evidence", "readiness-audit"]
ResolvedWorkflowProfile = Literal["coding", "enterprise-evidence", "readiness-audit"]

WORKFLOW_PROFILE_SELECTORS = (
    "auto",
    "coding",
    "enterprise-evidence",
    "readiness-audit",
)

_BASE_CAPABILITIES = (
    "agent_loop",
    "tools",
    "approval",
    "requirement_contract",
    "evidence_ledger",
    "tool_choice_queue",
    "completion_audit",
    "patch_reviewer",
    "verification_plan",
    "finalization",
)


@dataclass(frozen=True)
class WorkflowCapabilities:
    """Heavy workflow hooks enabled for one typed requirement contract."""

    read_only_explore: bool = False
    isolated_read_only_review: bool = False
    document_consistency_review: bool = False
    implementation_readiness_review: bool = False
    safe_partial_delivery: bool = False

    def enabled_names(self) -> tuple[str, ...]:
        optional = (
            ("read_only_explore", self.read_only_explore),
            ("isolated_read_only_review", self.isolated_read_only_review),
            ("document_consistency_review", self.document_consistency_review),
            ("implementation_readiness_review", self.implementation_readiness_review),
            ("safe_partial_delivery", self.safe_partial_delivery),
        )
        return (*_BASE_CAPABILITIES, *(name for name, enabled in optional if enabled))


@dataclass(frozen=True)
class WorkflowProfileResolution:
    selector: WorkflowProfileSelector
    resolved_profile: ResolvedWorkflowProfile
    reason: str
    capabilities: WorkflowCapabilities

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "resolved_profile": self.resolved_profile,
            "reason": self.reason,
            "enabled_capabilities": list(self.capabilities.enabled_names()),
        }


def normalize_workflow_profile_selector(raw_selector: object) -> WorkflowProfileSelector:
    if not isinstance(raw_selector, str):
        raise ValueError("workflow_profile must be a string.")
    selector = raw_selector.strip().lower()
    if selector not in WORKFLOW_PROFILE_SELECTORS:
        raise ValueError(
            "workflow_profile must be one of: auto, coding, enterprise-evidence, readiness-audit."
        )
    return selector  # type: ignore[return-value]


def resolve_workflow_profile(
    selector: object,
    contract: RequirementContract,
) -> WorkflowProfileResolution:
    normalized = normalize_workflow_profile_selector(selector)
    if normalized == "auto":
        if contract.implementation_readiness_required:
            profile: ResolvedWorkflowProfile = "readiness-audit"
            reason = "auto:typed_implementation_readiness_required"
        elif contract.read_only_review_profile != "none":
            profile = "enterprise-evidence"
            reason = f"auto:typed_read_only_review_profile={contract.read_only_review_profile}"
        else:
            profile = "coding"
            reason = "auto:no_typed_heavy_review_contract"
    else:
        profile = normalized
        reason = f"explicit:{normalized}"

    capabilities, applicability = _capabilities_for(profile, contract)
    if applicability:
        reason = f"{reason};{applicability}"
    return WorkflowProfileResolution(normalized, profile, reason, capabilities)


def workflow_profile_for_run(run: Any) -> WorkflowProfileResolution:
    resolution = getattr(run, "workflow_profile", None)
    if isinstance(resolution, WorkflowProfileResolution):
        return resolution
    contract = getattr(run, "requirement_contract", None)
    if contract is None:
        return WorkflowProfileResolution(
            "auto",
            "coding",
            "pending:typed_requirement_contract_absent",
            WorkflowCapabilities(),
        )
    return resolve_workflow_profile("auto", contract)


def workflow_read_only_explore_enabled(run: Any) -> bool:
    return workflow_profile_for_run(run).capabilities.read_only_explore


def workflow_read_only_review_enabled(run: Any) -> bool:
    return workflow_profile_for_run(run).capabilities.isolated_read_only_review


def workflow_safe_partial_enabled(run: Any) -> bool:
    return workflow_profile_for_run(run).capabilities.safe_partial_delivery


def _capabilities_for(
    profile: ResolvedWorkflowProfile,
    contract: RequirementContract,
) -> tuple[WorkflowCapabilities, str]:
    if profile == "coding":
        return WorkflowCapabilities(), ""
    if profile == "readiness-audit":
        if not contract.implementation_readiness_required:
            return WorkflowCapabilities(), "inactive:typed_readiness_not_required"
        return (
            WorkflowCapabilities(
                read_only_explore=contract.read_only_review_profile in {"owner_impact", "design"},
                isolated_read_only_review=True,
                implementation_readiness_review=True,
                safe_partial_delivery=True,
            ),
            "",
        )
    if contract.implementation_readiness_required:
        return WorkflowCapabilities(), "inactive:readiness_contract_requires_readiness-audit"
    if contract.read_only_review_profile == "none":
        return WorkflowCapabilities(), "inactive:no_typed_enterprise_review_profile"
    return (
        WorkflowCapabilities(
            read_only_explore=contract.read_only_review_profile in {"owner_impact", "design"},
            isolated_read_only_review=True,
            document_consistency_review=contract.read_only_review_profile == "document_consistency",
            safe_partial_delivery=True,
        ),
        "",
    )

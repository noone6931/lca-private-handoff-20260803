from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .evidence.design import DesignEvidenceCoverageSteerer
from .evidence import EvidenceLedger
from .evidence import EvidenceRecord
from .finalization import FINAL_ANSWER_STEERING_PRESENTATION
from .finalization import FINAL_ANSWER_STEERING_HARD
from .finalization import FinalizationCoordinator
from .finalization import MAX_FORCED_FINAL_ANSWER_CONTINUATIONS
from .finalization import UnresolvedFinalAnswerGate
from .run_collector import RunCollector
from .soft_tool_requirement import SoftToolRequirement
from .steering.tool_loop import ToolLoopSteeringRegistry
from .task_contract import RequirementContract
from .tool_choice_directive import ToolChoiceDirectiveOwner
from .tool_choice_queue import ToolChoiceQueue
from .tool_observation import ToolResultSummary
from .evidence.verification import VerificationPlan
from .test_planner import TestPlan
from .session.evidence import SessionEvidenceReuse
from .session.continuity import PendingTaskContinuation
from .temporary_tool_directive import DirectiveTransition
from .temporary_tool_directive import TemporaryToolDirectiveOwner
from .read_only_reviewer import ReadOnlyReviewState
from .runtime.run_output import RunOutputLifecycle
from .workflow_profile import WorkflowProfileResolution
from .workflow_profile import resolve_workflow_profile


@dataclass
class RunContext:
    """All mutable state whose authority ends with the current user task."""

    workflow_profile_selector: str = "auto"
    workflow_profile: WorkflowProfileResolution | None = None
    run_id: str | None = None
    started_monotonic: float | None = None
    deadline_monotonic: float | None = None
    run_start_index: int = 0
    _checkpointed_run_messages: list[dict[str, Any]] = field(default_factory=list)
    git_baseline: dict[str, Any] = field(default_factory=dict)
    current_user_request: str | None = None
    read_file_drift_guard_enabled: bool = False
    finalization: FinalizationCoordinator = field(default_factory=FinalizationCoordinator)
    temporary_tool_directive: TemporaryToolDirectiveOwner = field(default_factory=TemporaryToolDirectiveOwner)
    tool_choice_allowed_tool_names: set[str] | None = None
    tool_choice_read_file_paths: set[str] | None = None
    tool_choice_read_file_remaining: int | None = None
    tool_choice_required_glob_roots: set[str] | None = None
    tool_choice_stop_reason: str | None = None
    tool_choice_steering_signatures: set[str] = field(default_factory=set)
    tool_choice_force_final_signatures: set[str] = field(default_factory=set)
    tool_choice_results: list[ToolResultSummary] = field(default_factory=list)
    tool_choice_tool_names: list[str] = field(default_factory=list)
    requirement_contract: RequirementContract | None = None
    requirement_contract_context: str = ""
    verification_plan: VerificationPlan = field(default_factory=VerificationPlan)
    verification_test_plan: TestPlan | None = None
    design_evidence_coverage: DesignEvidenceCoverageSteerer = field(default_factory=DesignEvidenceCoverageSteerer)
    soft_tool_requirement: SoftToolRequirement | None = None
    read_file_range_counts: dict[tuple[str, int, str], int] = field(default_factory=dict)
    evidence: EvidenceLedger = field(default_factory=EvidenceLedger)
    final_answer_steers: dict[str, int] = field(default_factory=dict)
    tool_loop_steering: ToolLoopSteeringRegistry = field(default_factory=ToolLoopSteeringRegistry)
    tool_choice_queue: ToolChoiceQueue = field(default_factory=ToolChoiceQueue)
    tool_choice_directive: ToolChoiceDirectiveOwner = field(default_factory=ToolChoiceDirectiveOwner)
    collector: RunCollector = field(default_factory=RunCollector)
    session_evidence_reuse: SessionEvidenceReuse = field(default_factory=SessionEvidenceReuse)
    session_evidence_directive_emitted: bool = False
    user_facts_context: str = ""
    negative_claim_metrics: dict[str, int] = field(default_factory=dict)
    read_only_review: ReadOnlyReviewState = field(default_factory=ReadOnlyReviewState)
    read_only_explore_finalized: bool = False
    output: RunOutputLifecycle = field(default_factory=RunOutputLifecycle)

    # Compatibility views keep existing runtime tests and integrations stable while
    # EvidenceLedger becomes the single owner of the mutable evidence state.
    @property
    def read_file_evidence_paths(self) -> list[str]:
        return self.evidence.read_file_paths

    @read_file_evidence_paths.setter
    def read_file_evidence_paths(self, value: list[str]) -> None:
        self.evidence.read_file_paths = value

    @property
    def design_evidence_read_paths(self) -> list[str]:
        return self.evidence.design_read_paths

    @design_evidence_read_paths.setter
    def design_evidence_read_paths(self, value: list[str]) -> None:
        self.evidence.design_read_paths = value

    @property
    def source_evidence(self):
        return self.evidence.source_evidence

    @source_evidence.setter
    def source_evidence(self, value) -> None:
        self.evidence.source_evidence = value

    @property
    def pinned_requirement_evidence(self):
        return self.evidence.pinned_requirement_evidence

    @pinned_requirement_evidence.setter
    def pinned_requirement_evidence(self, value) -> None:
        self.evidence.pinned_requirement_evidence = value

    @property
    def successful_patch_preview_signatures(self) -> set[str]:
        return self.evidence.successful_patch_preview_signatures

    @successful_patch_preview_signatures.setter
    def successful_patch_preview_signatures(self, value: set[str]) -> None:
        self.evidence.successful_patch_preview_signatures = value

    @property
    def strong_relevance_paths(self) -> list[str]:
        return self.evidence.strong_relevance_paths

    @strong_relevance_paths.setter
    def strong_relevance_paths(self, value: list[str]) -> None:
        self.evidence.strong_relevance_paths = value

    @property
    def evidence_records(self) -> list[EvidenceRecord]:
        return self.evidence.records

    @evidence_records.setter
    def evidence_records(self, value: list[EvidenceRecord]) -> None:
        self.evidence.records = value

    @property
    def workspace_root_evidence_recorded(self) -> bool:
        return self.evidence.workspace_root_recorded

    @workspace_root_evidence_recorded.setter
    def workspace_root_evidence_recorded(self, value: bool) -> None:
        self.evidence.workspace_root_recorded = value

    def begin(
        self,
        *,
        run_id: str,
        started_monotonic: float,
        deadline_monotonic: float | None,
        run_start_index: int,
        git_baseline: dict[str, Any],
        prompt: str,
        requirement_contract: RequirementContract,
        requirement_contract_context: str,
        design_evidence_roots: tuple[str, ...],
        pending_task: PendingTaskContinuation | None = None,
    ) -> None:
        self.run_id = run_id
        self.started_monotonic = started_monotonic
        self.deadline_monotonic = deadline_monotonic
        self.run_start_index = run_start_index
        self._checkpointed_run_messages.clear()
        self.git_baseline = dict(git_baseline)
        self.current_user_request = prompt
        self.read_file_drift_guard_enabled = False
        self.finalization.reset()
        self.temporary_tool_directive.reset()
        self.tool_choice_allowed_tool_names = None
        self.tool_choice_read_file_paths = None
        self.tool_choice_read_file_remaining = None
        self.tool_choice_required_glob_roots = None
        self.tool_choice_stop_reason = None
        self.tool_choice_steering_signatures.clear()
        self.tool_choice_force_final_signatures.clear()
        self.tool_choice_results.clear()
        self.tool_choice_tool_names.clear()
        self.requirement_contract = requirement_contract
        self.workflow_profile = resolve_workflow_profile(self.workflow_profile_selector, requirement_contract)
        self.requirement_contract_context = requirement_contract_context
        self.verification_plan = VerificationPlan.from_contract(requirement_contract, pending_task=pending_task)
        self.verification_test_plan = None
        self.design_evidence_coverage.reset(design_evidence_roots)
        self.soft_tool_requirement = None
        self.read_file_range_counts.clear()
        self.evidence.reset()
        self.final_answer_steers.clear()
        self.tool_loop_steering.reset()
        self.tool_choice_directive.reset()
        self.session_evidence_reuse = SessionEvidenceReuse()
        self.session_evidence_directive_emitted = False
        self.user_facts_context = ""
        self.negative_claim_metrics.clear()
        self.read_only_review.reset()
        self.read_only_explore_finalized = False
        self.output.reset()

    def workflow_profile_payload(self) -> dict[str, Any]:
        if self.workflow_profile is None:
            return {
                "selector": self.workflow_profile_selector,
                "resolved_profile": "pending",
                "reason": "awaiting_typed_requirement_contract",
                "enabled_capabilities": [],
            }
        return self.workflow_profile.to_dict()

    def workflow_profile_status(self) -> str:
        payload = self.workflow_profile_payload()
        return (
            "- workflow_profile: "
            f"selector={payload['selector']}, resolved={payload['resolved_profile']}, reason={payload['reason']}"
        )

    def active_design_evidence_roots(self) -> tuple[str, ...]:
        if self.workflow_profile is not None and self.workflow_profile.capabilities.read_only_explore:
            return self.design_evidence_coverage.roots
        return ()

    def checkpoint_active_messages(self, messages: list[dict[str, Any]], next_message_start: int) -> None:
        """Preserve this run's pre-checkpoint suffix before history is replaced."""

        self._checkpointed_run_messages.extend(dict(message) for message in messages[self.run_start_index :])
        self.run_start_index = next_message_start

    def current_run_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the complete current run across any compaction checkpoints."""

        return [*self._checkpointed_run_messages, *messages[self.run_start_index :]]

    def record_negative_claim_metrics(self, metrics: dict[str, int]) -> None:
        """Accumulate taxonomy observations without making model text authoritative."""
        for key, value in metrics.items():
            if value:
                self.negative_claim_metrics[key] = self.negative_claim_metrics.get(key, 0) + int(value)

    @property
    def temporary_tool_allowlist(self) -> set[str] | None:
        """Compatibility view; lifecycle ownership remains in the directive owner."""

        return self.temporary_tool_directive.active_allowed_tools

    def activate_temporary_tool_directive(
        self,
        source_kind: str,
        allowed_tools: set[str] | frozenset[str],
    ) -> DirectiveTransition:
        return self.temporary_tool_directive.activate(source_kind, allowed_tools)

    def begin_temporary_tool_directive_turn(self) -> DirectiveTransition | None:
        return self.temporary_tool_directive.begin_turn()

    def reserve_temporary_tool_directive_attempt(self, tool_name: str) -> DirectiveTransition | None:
        return self.temporary_tool_directive.reserve_attempt(tool_name)

    def record_temporary_tool_directive_attempt(
        self,
        transition: DirectiveTransition | None,
        *,
        is_error: bool,
    ) -> DirectiveTransition | None:
        return self.temporary_tool_directive.record_attempt_outcome(transition, is_error=is_error)

    def finish_temporary_tool_directive_turn(self) -> DirectiveTransition | None:
        return self.temporary_tool_directive.finish_turn()

    def close_temporary_tool_directive(self, reason: str) -> DirectiveTransition | None:
        return self.temporary_tool_directive.close_for_terminal(reason)

    def update_tool_choice_read_scope(
        self,
        paths: tuple[str, ...],
        budget: int | None,
    ) -> None:
        next_paths = set(paths)
        if not next_paths:
            self.tool_choice_read_file_paths = None
            self.tool_choice_read_file_remaining = None
            return
        if self.tool_choice_read_file_paths != next_paths:
            self.tool_choice_read_file_paths = next_paths
            self.tool_choice_read_file_remaining = budget

    def consume_tool_choice_read(self, name: str, *, canonical_path: str | None = None) -> None:
        if name != "read_file" or self.tool_choice_read_file_remaining is None:
            return
        if not canonical_path or self.tool_choice_read_file_paths is None or canonical_path not in self.tool_choice_read_file_paths:
            return
        self.tool_choice_read_file_remaining = max(0, self.tool_choice_read_file_remaining - 1)

    def can_queue_forced_final_answer(self) -> bool:
        return self.finalization.can_queue()

    def finalization_rewrite_skip_reason(self, *, now: float | None = None) -> str | None:
        return self.finalization.rewrite_skip_reason(
            deadline_monotonic=self.deadline_monotonic,
            run_started_monotonic=self.started_monotonic,
            now=now,
        )

    def finalization_reserve_required(self, *, now: float | None = None) -> bool:
        return self.finalization_rewrite_skip_reason(now=now) == "deadline_reserve"

    def queue_finalization_rewrite(
        self,
        *,
        kind: str,
        severity: str = FINAL_ANSWER_STEERING_HARD,
        now: float | None = None,
    ) -> bool:
        return self.finalization.request(
            kind=kind,
            severity=severity,
            deadline_monotonic=self.deadline_monotonic,
            run_started_monotonic=self.started_monotonic,
            now=now,
        ).accepted

    def queue_forced_final_answer(
        self,
        *,
        kind: str = "runtime_forced_final",
        severity: str = FINAL_ANSWER_STEERING_HARD,
    ) -> bool:
        return self.queue_finalization_rewrite(kind=kind, severity=severity)

    def request_forced_final_answer(
        self,
        *,
        kind: str,
        severity: str = FINAL_ANSWER_STEERING_HARD,
    ) -> bool:
        """Record why the next no-tool response is being forced."""

        return self.queue_finalization_rewrite(kind=kind, severity=severity)

    def clear_forced_final_answer_request(self) -> None:
        self.finalization.clear_pending_request()

    def allows_forced_final_draft_fallback(self) -> bool:
        return self.finalization.allows_draft_fallback()

    def block_unverified_final_answer(self, *, kind: str, reason: str) -> None:
        self.finalization.block_unverified(kind=kind, reason=reason)

    def reset_forced_final_answer_continuations(self) -> None:
        # Mirrors OMP's continuation accounting: a real tool turn resets the
        # no-tool resample budget because the agent made observable progress.
        self.finalization.observe_tool_progress()

    @property
    def force_final_answer_without_tools(self) -> bool:
        return self.finalization.pending_force_final

    @force_final_answer_without_tools.setter
    def force_final_answer_without_tools(self, value: bool) -> None:
        self.finalization.pending_force_final = bool(value)

    @property
    def forced_final_answer_continuations(self) -> int:
        return self.finalization.continuations

    @forced_final_answer_continuations.setter
    def forced_final_answer_continuations(self, value: int) -> None:
        self.finalization.continuations = max(0, int(value))

    @property
    def forced_final_answer_kind(self) -> str:
        return self.finalization.kind

    @forced_final_answer_kind.setter
    def forced_final_answer_kind(self, value: str) -> None:
        self.finalization.kind = value or "runtime_forced_final"

    @property
    def forced_final_answer_severity(self) -> str:
        return self.finalization.severity

    @forced_final_answer_severity.setter
    def forced_final_answer_severity(self, value: str) -> None:
        self.finalization.severity = value or FINAL_ANSWER_STEERING_HARD

    @property
    def unresolved_final_answer_gate(self) -> UnresolvedFinalAnswerGate | None:
        return self.finalization.unresolved_gate

    @unresolved_final_answer_gate.setter
    def unresolved_final_answer_gate(self, value: UnresolvedFinalAnswerGate | None) -> None:
        self.finalization.unresolved_gate = value

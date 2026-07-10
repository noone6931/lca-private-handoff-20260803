from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .design_evidence import DesignEvidenceCoverageSteerer
from .evidence import EvidenceLedger
from .evidence import EvidenceRecord
from .run_collector import RunCollector
from .soft_tool_requirement import SoftToolRequirement
from .steering.tool_loop import ToolLoopSteeringRegistry
from .task_contract import RequirementContract
from .tool_choice_queue import ToolChoiceQueue
from .tool_choice_queue import ToolResultSummary


@dataclass
class RunContext:
    """All mutable state whose authority ends with the current user task."""

    run_id: str | None = None
    started_monotonic: float | None = None
    deadline_monotonic: float | None = None
    run_start_index: int = 0
    git_baseline: dict[str, Any] = field(default_factory=dict)
    current_user_request: str | None = None
    read_file_drift_guard_enabled: bool = False
    force_final_answer_without_tools: bool = False
    temporary_tool_allowlist: set[str] | None = None
    tool_choice_allowed_tool_names: set[str] | None = None
    tool_choice_steering_signatures: set[str] = field(default_factory=set)
    tool_choice_results: list[ToolResultSummary] = field(default_factory=list)
    tool_choice_tool_names: list[str] = field(default_factory=list)
    requirement_contract: RequirementContract | None = None
    requirement_contract_context: str = ""
    design_evidence_coverage: DesignEvidenceCoverageSteerer = field(default_factory=DesignEvidenceCoverageSteerer)
    soft_tool_requirement: SoftToolRequirement | None = None
    read_file_range_counts: dict[tuple[str, int, str], int] = field(default_factory=dict)
    evidence: EvidenceLedger = field(default_factory=EvidenceLedger)
    final_answer_steers: dict[str, int] = field(default_factory=dict)
    tool_loop_steering: ToolLoopSteeringRegistry = field(default_factory=ToolLoopSteeringRegistry)
    tool_choice_queue: ToolChoiceQueue = field(default_factory=ToolChoiceQueue)
    collector: RunCollector = field(default_factory=RunCollector)

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
    ) -> None:
        self.run_id = run_id
        self.started_monotonic = started_monotonic
        self.deadline_monotonic = deadline_monotonic
        self.run_start_index = run_start_index
        self.git_baseline = dict(git_baseline)
        self.current_user_request = prompt
        self.read_file_drift_guard_enabled = False
        self.force_final_answer_without_tools = False
        self.temporary_tool_allowlist = None
        self.tool_choice_allowed_tool_names = None
        self.tool_choice_steering_signatures.clear()
        self.tool_choice_results.clear()
        self.tool_choice_tool_names.clear()
        self.requirement_contract = requirement_contract
        self.requirement_contract_context = requirement_contract_context
        self.design_evidence_coverage.reset(design_evidence_roots)
        self.soft_tool_requirement = None
        self.read_file_range_counts.clear()
        self.evidence.reset()
        self.final_answer_steers.clear()
        self.tool_loop_steering.reset()

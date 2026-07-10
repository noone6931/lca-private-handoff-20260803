from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .design_evidence import DesignEvidenceCoverageSteerer
from .requirement_evidence import RequirementEvidence
from .run_collector import RunCollector
from .steering.final_answer import SourceEvidence
from .steering.tool_loop import ToolLoopSteeringRegistry
from .task_contract import RequirementContract
from .tool_choice_queue import ToolChoiceQueue
from .tool_choice_queue import ToolResultSummary


@dataclass
class SoftToolRequirement:
    kind: str
    allowed_dirs: tuple[Path, ...]
    candidate_files: tuple[Path, ...] = ()
    steers: int = 0
    satisfied: bool = False


@dataclass(frozen=True)
class EvidenceRecord:
    tool: str
    subject: str
    summary: str
    status: str = "ok"

    def render(self) -> str:
        return f"- [{self.status}] {self.tool} {self.subject}: {self.summary}"


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
    design_evidence_read_paths: list[str] = field(default_factory=list)
    soft_tool_requirement: SoftToolRequirement | None = None
    read_file_range_counts: dict[tuple[str, int, str], int] = field(default_factory=dict)
    read_file_evidence_paths: list[str] = field(default_factory=list)
    source_evidence: list[SourceEvidence] = field(default_factory=list)
    pinned_requirement_evidence: list[RequirementEvidence] = field(default_factory=list)
    successful_patch_preview_signatures: set[str] = field(default_factory=set)
    strong_relevance_paths: list[str] = field(default_factory=list)
    evidence_records: list[EvidenceRecord] = field(default_factory=list)
    workspace_root_evidence_recorded: bool = False
    final_answer_steers: dict[str, int] = field(default_factory=dict)
    tool_loop_steering: ToolLoopSteeringRegistry = field(default_factory=ToolLoopSteeringRegistry)
    tool_choice_queue: ToolChoiceQueue = field(default_factory=ToolChoiceQueue)
    collector: RunCollector = field(default_factory=RunCollector)

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
        self.design_evidence_read_paths.clear()
        self.soft_tool_requirement = None
        self.read_file_range_counts.clear()
        self.read_file_evidence_paths.clear()
        self.source_evidence.clear()
        self.pinned_requirement_evidence.clear()
        self.successful_patch_preview_signatures.clear()
        self.strong_relevance_paths.clear()
        self.evidence_records.clear()
        self.workspace_root_evidence_recorded = False
        self.final_answer_steers.clear()
        self.tool_loop_steering.reset()

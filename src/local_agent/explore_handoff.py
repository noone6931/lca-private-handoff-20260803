"""Bounded read-only evidence handoff for an isolated reviewer.

This is deliberately not a second agent or search engine.  It turns facts the
primary Runtime already collected into a small, provenance-preserving claim
matrix that another model call can inspect without receiving the transcript.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .evidence import EvidenceRecord
from .requirement_evidence import RequirementEvidence
from .steering.final_answer import SourceEvidence
from .task_contract import RequirementContract
from .tool_observation import ToolResultSummary


MAX_HANDOFF_ITEMS = 18
MAX_HANDOFF_CONTENT_CHARS = 700
MAX_REQUIREMENT_ITEMS = 4
MAX_SOURCE_ITEMS_PER_ROOT = 1
MAX_RECORD_ITEMS_PER_ROOT = 1
MAX_RESULT_ITEMS = 4


@dataclass(frozen=True)
class ClaimEvidenceItem:
    """A bounded observation with an epistemic classification, not a verdict."""

    classification: str
    tool: str
    path: str
    root: str
    scope: str
    outcome: str
    summary: str

    def to_dict(self) -> dict[str, str]:
        return {
            "classification": self.classification,
            "tool": self.tool,
            "path": self.path,
            "root": self.root,
            "scope": self.scope,
            "outcome": self.outcome,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class ExploreHandoff:
    """Structured, isolated context for one reviewer call."""

    request: str
    contract: RequirementContract
    items: tuple[ClaimEvidenceItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request,
            "contract": {
                "task_kind": self.contract.task_kind,
                "evidence_domain": self.contract.evidence_domain,
                "scope": self.contract.scope,
                "acceptance": self.contract.acceptance_items[:4],
                "evidence_requirements": self.contract.evidence_requirements[:4],
            },
            "claim_matrix": [item.to_dict() for item in self.items],
            "review_categories": [
                "direct_binding",
                "analogous_capability",
                "unlocated",
                "requirement_fact",
                "proposal",
            ],
            "limitations": [
                "The matrix is a bounded handoff, not a complete repository inventory.",
                "A path or similarly named capability is not a direct owner unless evidence explicitly binds the requested behavior to a symbol, path, or call chain.",
                "Requirement facts, repository observations, proposals, and unlocated questions must remain distinct.",
            ],
        }


def build_explore_handoff(
    *,
    request: str,
    contract: RequirementContract,
    requirement_evidence: Iterable[RequirementEvidence],
    source_evidence: Iterable[SourceEvidence],
    records: Iterable[EvidenceRecord],
    tool_results: Iterable[ToolResultSummary],
) -> ExploreHandoff:
    """Build a conservative matrix from existing Runtime observations only.

    The quotas deliberately preserve requirement evidence and root provenance
    before lower-priority repeated observations.  ``observed_candidate`` means
    raw source evidence, not that the Runtime has semantically classified it as
    analogous capability.  A real ``direct_binding`` needs a request symbol in
    the evidence itself.
    """

    requirements = [
        ClaimEvidenceItem("requirement_fact", "read_file", item.path, item.root or "(unknown)", item.scope or "root_local", "ok", _clip(item.content))
        for item in list(requirement_evidence)[-MAX_REQUIREMENT_ITEMS:]
    ]
    source_items = _bounded_per_root(
        (
            ClaimEvidenceItem(
                "observed_candidate",
                "read_file", item.path, item.root or "(unknown)", item.scope or "root_local", "ok", _head_tail(item.content),
            )
            for item in source_evidence
        ),
        MAX_SOURCE_ITEMS_PER_ROOT,
    )
    record_items = _bounded_per_root(
        (
            ClaimEvidenceItem(
                "unlocated" if item.status in {"content_no_match", "path_no_match", "exact_path_missing", "no_match", "incomplete"} else "inspection_failure" if item.status == "error" else "observed_candidate",
                item.tool, item.subject,
                str(item.details.get("evidence_root_label") or item.details.get("evidence_root") or "(unknown)"),
                str(item.details.get("evidence_scope") or "(unknown)"), item.status, _clip(item.summary),
            )
            for item in records
        ),
        MAX_RECORD_ITEMS_PER_ROOT,
    )
    precise_results = [
        ClaimEvidenceItem(
            (
                "unlocated"
                if item.useless
                else "inspection_failure"
                if item.is_error
                else "direct_binding"
                if item.name in {"lsp_definition", "lsp_references"}
                and bool(item.metadata.get("evidence_paths"))
                else "observed_candidate"
            ),
            item.name, item.path or "(none)",
            str(item.metadata.get("evidence_root_label") or "(unknown)"),
            str(item.metadata.get("evidence_scope") or "(unknown)"),
            "error" if item.is_error else "no_match" if item.useless else "ok", _clip(item.content),
        )
        for item in tool_results
        if item.name.startswith("lsp_") or item.name == "search_code"
    ][-MAX_RESULT_ITEMS:]
    # Requirements and exact navigation/search observations have priority.  A
    # busy generic tool timeline may consume the remainder, never these facts.
    items = [*requirements, *precise_results, *source_items, *record_items]
    return ExploreHandoff(request=request, contract=contract, items=tuple(items[:MAX_HANDOFF_ITEMS]))


def _clip(value: str) -> str:
    compact = " ".join((value or "").split())
    if len(compact) <= MAX_HANDOFF_CONTENT_CHARS:
        return compact
    return compact[: MAX_HANDOFF_CONTENT_CHARS - 1].rstrip() + "..."


def _head_tail(value: str) -> str:
    compact = " ".join((value or "").split())
    if len(compact) <= MAX_HANDOFF_CONTENT_CHARS:
        return compact
    half = MAX_HANDOFF_CONTENT_CHARS // 2
    return compact[:half].rstrip() + " ... [middle omitted] ... " + compact[-half:].lstrip()


def _bounded_per_root(items: Iterable[ClaimEvidenceItem], limit: int) -> list[ClaimEvidenceItem]:
    buckets: dict[str, list[ClaimEvidenceItem]] = {}
    for item in items:
        bucket = buckets.setdefault(item.root, [])
        if len(bucket) < limit:
            bucket.append(item)
        elif item.classification == "direct_binding":
            bucket[-1] = item
    return [item for root in sorted(buckets) for item in buckets[root]]

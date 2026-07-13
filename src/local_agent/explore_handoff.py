"""Bounded read-only evidence handoff for an isolated reviewer.

This is deliberately not a second agent or search engine.  It turns facts the
primary Runtime already collected into a small, provenance-preserving claim
matrix that another model call can inspect without receiving the transcript.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from .document_consistency import explicit_reconciliation_excerpt
from .evidence import EvidenceRecord
from .requirement_evidence import RequirementEvidence
from .steering.final_answer import SourceEvidence
from .task_contract import RequirementContract
from .tool_observation import ToolResultSummary


MAX_HANDOFF_ITEMS = 18
MAX_HANDOFF_CONTENT_CHARS = 700
MAX_VISUAL_OBSERVATION_CHARS = 2400
MAX_REQUIREMENT_ITEMS = 4
MAX_SOURCE_ITEMS_PER_ROOT = 1
MAX_RECORD_ITEMS_PER_ROOT = 1
MAX_RESULT_ITEMS = 4
MAX_DOCUMENT_ARTIFACT_ITEMS = 6


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
    count: int = 1
    evidence_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "tool": self.tool,
            "path": self.path,
            "root": self.root,
            "scope": self.scope,
            "outcome": self.outcome,
            "summary": self.summary,
            "count": self.count,
            "evidence_id": self.evidence_id,
        }


@dataclass(frozen=True)
class ExploreHandoff:
    """Structured, isolated context for one reviewer call."""

    request: str
    contract: RequirementContract
    items: tuple[ClaimEvidenceItem, ...]

    def __post_init__(self) -> None:
        # IDs address only this bounded handoff. They are not filesystem or
        # session identifiers and never expose additional source content.
        object.__setattr__(
            self,
            "items",
            tuple(
                replace(item, evidence_id=item.evidence_id or f"e{index:03d}")
                for index, item in enumerate(self.items, start=1)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request,
            "contract": {
                "task_kind": self.contract.task_kind,
                "evidence_domain": self.contract.evidence_domain,
                "read_only_review_profile": self.contract.read_only_review_profile,
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
                "Visual observations describe what is shown, not an artifact author's intent, lifecycle, precedence, or role.",
            ],
        }

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.items)


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

    results = tuple(tool_results)
    requirement_entries = tuple(requirement_evidence)
    requirements = [
        ClaimEvidenceItem("requirement_fact", "read_file", item.path, item.root or "(unknown)", item.scope or "root_local", "ok", _clip(item.content))
        for item in requirement_entries[-MAX_REQUIREMENT_ITEMS:]
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
    document_items = [
        ClaimEvidenceItem(
            "requirement_fact" if item.name == "read_file" else "visual_observation",
            item.name,
            item.path or "(none)",
            str(item.metadata.get("evidence_root") or item.metadata.get("evidence_root_label") or "(unknown)"),
            str(item.metadata.get("evidence_scope") or "root_local"),
            "error" if item.is_error else "ok",
            _clip(item.content, limit=MAX_VISUAL_OBSERVATION_CHARS) if item.name == "inspect_image" else _clip(item.content),
        )
        for item in results
        if item.name == "inspect_image"
        or (
            item.name == "read_file"
            and str(item.path or "").lower().endswith((".md", ".markdown", ".html", ".htm"))
        )
    ][-MAX_DOCUMENT_ARTIFACT_ITEMS:]
    reconciliation_support_items = _document_reconciliation_support_items(
        requirements=requirement_entries,
        tool_results=results,
    )
    incomplete_root_items = [
        ClaimEvidenceItem(
            "unlocated",
            "read_only_explore",
            item.path or "(unknown root)",
            str(item.metadata.get("evidence_root_label") or item.metadata.get("evidence_root") or item.path or "(unknown)"),
            str(item.metadata.get("evidence_scope") or "root_local"),
            "incomplete",
            _clip(item.content),
        )
        for item in results
        if item.name == "read_only_explore" and item.metadata.get("read_only_explore_incomplete")
    ]
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
            str(item.metadata.get("evidence_root_label") or item.metadata.get("evidence_root") or "(unknown)"),
            str(item.metadata.get("evidence_scope") or "(unknown)"),
            "error" if item.is_error else "no_match" if item.useless else "ok", _clip(item.content),
        )
        for item in results
        if item.name.startswith("lsp_") or item.name == "search_code"
    ][-MAX_RESULT_ITEMS:]
    error_results = [
        ClaimEvidenceItem(
            "inspection_failure",
            item.name,
            item.path or "(none)",
            str(item.metadata.get("evidence_root_label") or item.metadata.get("evidence_root") or "(unknown)"),
            str(item.metadata.get("evidence_scope") or "(unknown)"),
            "error",
            _clip(item.content),
        )
        for item in results
        if item.is_error and not (item.name.startswith("lsp_") or item.name == "search_code")
    ]
    # Direct current-run observations win over session-restored records.  A
    # dedupe pass then keeps one provenance fact per tool/path/outcome and
    # aggregates repeated failures by root/category.
    items = _dedupe_items(
        [
            *reconciliation_support_items,
            *document_items,
            *precise_results,
            *source_items,
            *requirements,
            *incomplete_root_items,
            *error_results,
            *record_items,
        ]
    )
    return ExploreHandoff(request=request, contract=contract, items=tuple(items[:MAX_HANDOFF_ITEMS]))


def _clip(value: str, *, limit: int = MAX_HANDOFF_CONTENT_CHARS) -> str:
    compact = " ".join((value or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "..."


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


def _document_reconciliation_support_items(
    *,
    requirements: Iterable[RequirementEvidence],
    tool_results: Iterable[ToolResultSummary],
) -> list[ClaimEvidenceItem]:
    """Preserve an explicit support excerpt even when the file header is generic."""

    items: list[ClaimEvidenceItem] = []
    for item in requirements:
        excerpt = explicit_reconciliation_excerpt(item.content)
        if excerpt:
            items.append(
                ClaimEvidenceItem(
                    "document_reconciliation_support",
                    "read_file",
                    item.path,
                    item.root or "(unknown)",
                    item.scope or "root_local",
                    "ok",
                    excerpt,
                )
            )
    for item in tool_results:
        if item.name != "read_file" or item.is_error or not str(item.path or "").lower().endswith((".md", ".markdown", ".html", ".htm")):
            continue
        excerpt = explicit_reconciliation_excerpt(item.content)
        if excerpt:
            items.append(
                ClaimEvidenceItem(
                    "document_reconciliation_support",
                    "read_file",
                    item.path or "(none)",
                    str(item.metadata.get("evidence_root") or item.metadata.get("evidence_root_label") or "(unknown)"),
                    str(item.metadata.get("evidence_scope") or "root_local"),
                    "ok",
                    excerpt,
                )
            )
    return items[:MAX_REQUIREMENT_ITEMS]


def _dedupe_items(items: Iterable[ClaimEvidenceItem]) -> list[ClaimEvidenceItem]:
    """Prefer early direct evidence and aggregate repeated failure provenance."""

    deduped: dict[tuple[str, ...], ClaimEvidenceItem] = {}
    for item in items:
        key = _handoff_item_identity(item)
        prior = deduped.get(key)
        if prior is None:
            deduped[key] = item
            continue
        deduped[key] = ClaimEvidenceItem(
            prior.classification,
            prior.tool,
            prior.path,
            prior.root,
            prior.scope,
            prior.outcome,
            prior.summary,
            count=prior.count + item.count,
        )
    return list(deduped.values())


def _handoff_item_identity(item: ClaimEvidenceItem) -> tuple[str, ...]:
    """Use artifact identity rather than display spelling for document reads."""

    if item.classification == "inspection_failure":
        return ("failure", item.classification, _root_identity(item.root), item.outcome)
    if item.tool in {"read_file", "inspect_image"} and item.outcome == "ok":
        if item.classification == "document_reconciliation_support":
            return ("document_support", _root_identity(item.root), _artifact_name(item.path), item.summary)
        return ("artifact", item.tool, _root_identity(item.root), _artifact_name(item.path))
    return ("item", item.tool, item.path, item.outcome)


def _root_identity(root: str) -> str:
    value = (root or "").strip()
    if value.startswith("/"):
        try:
            return str(Path(value).resolve())
        except OSError:
            return value
    return value.lower()


def _artifact_name(path: str) -> str:
    return Path(path or "").name.lower() or (path or "").lower()

"""Bounded read-only evidence handoff for an isolated reviewer.

This is deliberately not a second agent or search engine.  It turns facts the
primary Runtime already collected into a small, provenance-preserving claim
matrix that another model call can inspect without receiving the transcript.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import re
from typing import Any, Iterable

from .document_consistency import explicit_reconciliation_excerpt
from .document_identity import canonical_root_identity
from .document_identity import document_artifact_identity
from .evidence import EvidenceRecord
from .requirement_evidence import DocumentLocator
from .requirement_evidence import document_locator_excerpt
from .requirement_evidence import parse_document_line_range
from .requirement_evidence import parse_document_locators
from .requirement_evidence import RequirementEvidence
from .read_only_root_coverage import read_only_root_coverage
from .steering.final_answer import SourceEvidence
from .task_contract import RequirementContract
from .tool_observation import ToolResultSummary


MAX_HANDOFF_ITEMS = 32
MAX_HANDOFF_CONTENT_CHARS = 700
MAX_VISUAL_OBSERVATION_CHARS = 2400
MAX_REQUIREMENT_ITEMS = 4
MAX_SOURCE_ITEMS_PER_ROOT = 1
MAX_RECORD_ITEMS_PER_ROOT = 1
MAX_RESULT_ITEMS = 4
MAX_DOCUMENT_ARTIFACT_ITEMS = 6
MAX_CANDIDATE_LOCATOR_ITEMS = 16
MAX_CANDIDATE_LOCATOR_SUMMARY_CHARS = 2200
MAX_CANDIDATE_LOCATOR_TOTAL_CHARS = 12000
MAX_CANDIDATE_LOCATOR_WINDOW_LINES = 40
MAX_CANDIDATE_LOCATOR_WINDOWS_PER_CITATION = 4
MAX_CANDIDATE_LOCATOR_SOURCE_SPAN_LINES = (
    MAX_CANDIDATE_LOCATOR_WINDOW_LINES * MAX_CANDIDATE_LOCATOR_WINDOWS_PER_CITATION
)
MAX_CANDIDATE_LOCATOR_WINDOW_GAP = 2


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
    identity_path: str = ""
    claim_id: str = ""
    claim_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = {
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
        if self.claim_ids:
            payload["claim_ids"] = list(self.claim_ids)
        elif self.claim_id:
            payload["claim_id"] = self.claim_id
        return payload


@dataclass(frozen=True)
class ExploreHandoff:
    """Structured, isolated context for one reviewer call."""

    request: str
    contract: RequirementContract
    items: tuple[ClaimEvidenceItem, ...]
    transport_omitted_claim_ids: tuple[str, ...] = ()

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
            "transport_omitted_claim_ids": list(self.transport_omitted_claim_ids),
        }

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.items)


@dataclass(frozen=True)
class _DocumentLocatorSource:
    path: str
    content: str
    root: str
    scope: str
    identity_path: str


@dataclass(frozen=True)
class _LineLocatorRequest:
    artifact_key: tuple[str, str]
    source: _DocumentLocatorSource
    start: int
    end: int
    claim_id: str


def build_explore_handoff(
    *,
    request: str,
    contract: RequirementContract,
    requirement_evidence: Iterable[RequirementEvidence],
    source_evidence: Iterable[SourceEvidence],
    records: Iterable[EvidenceRecord],
    tool_results: Iterable[ToolResultSummary],
    candidate: str | None = None,
    claim_units: Iterable[Any] = (),
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
    source_entries = tuple(source_evidence)
    candidate_locator_items, transport_omitted_claim_ids = _candidate_locator_items(
        candidate or "",
        requirement_entries,
        source_entries,
        results,
        claim_units,
    )
    requirements = [
        ClaimEvidenceItem(
            "requirement_fact",
            "read_file",
            item.path,
            item.root or "(unknown)",
            item.scope or "root_local",
            "ok",
            _clip(item.content),
            identity_path=item.path,
        )
        for item in requirement_entries[-MAX_REQUIREMENT_ITEMS:]
    ]
    source_items = _bounded_per_root(
        (
            ClaimEvidenceItem(
                "observed_candidate",
                "read_file", item.path, item.root or "(unknown)", item.scope or "root_local", "ok", _head_tail(item.content),
            )
            for item in source_entries
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
            identity_path=str(item.metadata.get("resolved_path") or item.path or ""),
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
            str(item.metadata.get("evidence_root") or item.metadata.get("evidence_root_label") or item.path or "(unknown)"),
            str(item.metadata.get("evidence_scope") or "root_local"),
            "incomplete",
            _clip(item.content),
            identity_path=str(item.metadata.get("resolved_path") or item.path or ""),
        )
        for item in results
        if item.name == "read_only_explore" and item.metadata.get("read_only_explore_incomplete")
    ]
    coverage_items = _read_only_explore_coverage_items(results)
    record_items = _bounded_per_root(
        (
            ClaimEvidenceItem(
                "unlocated" if item.status in {"content_no_match", "path_no_match", "exact_path_missing", "no_match", "incomplete"} else "inspection_failure" if item.status == "error" else "observed_candidate",
                item.tool, item.subject,
                str(item.details.get("evidence_root") or item.details.get("evidence_root_label") or "(unknown)"),
                str(item.details.get("evidence_scope") or "(unknown)"), item.status, _clip(item.summary),
                identity_path=str(item.details.get("resolved_path") or item.subject or ""),
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
            str(item.metadata.get("evidence_root") or item.metadata.get("evidence_root_label") or "(unknown)"),
            str(item.metadata.get("evidence_scope") or "(unknown)"),
            "error" if item.is_error else "no_match" if item.useless else "ok", _clip(item.content),
            identity_path=str(item.metadata.get("resolved_path") or item.path or ""),
        )
        for item in results
        if item.name.startswith("lsp_") or item.name == "search_code"
    ][-MAX_RESULT_ITEMS:]
    error_results = [
        ClaimEvidenceItem(
            "inspection_failure",
            item.name,
            item.path or "(none)",
            str(item.metadata.get("evidence_root") or item.metadata.get("evidence_root_label") or "(unknown)"),
            str(item.metadata.get("evidence_scope") or "(unknown)"),
            "error",
            _clip(item.content),
            identity_path=str(item.metadata.get("resolved_path") or item.path or ""),
        )
        for item in results
        if item.is_error and not (item.name.startswith("lsp_") or item.name == "search_code")
    ]
    # Direct current-run observations win over session-restored records.  A
    # dedupe pass then keeps one provenance fact per tool/path/outcome and
    # aggregates repeated failures by root/category.
    items = _dedupe_items(
        [
            *candidate_locator_items,
            *reconciliation_support_items,
            *document_items,
            *precise_results,
            *source_items,
            *requirements,
            *coverage_items,
            *incomplete_root_items,
            *error_results,
            *record_items,
        ]
    )
    return ExploreHandoff(
        request=request,
        contract=contract,
        items=tuple(items[:MAX_HANDOFF_ITEMS]),
        transport_omitted_claim_ids=tuple(transport_omitted_claim_ids),
    )


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
                    identity_path=item.path,
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
                    identity_path=str(item.metadata.get("resolved_path") or item.path or ""),
                )
            )
    return items[:MAX_REQUIREMENT_ITEMS]


def _candidate_locator_items(
    candidate: str,
    requirements: Iterable[RequirementEvidence],
    source_evidence: Iterable[SourceEvidence],
    tool_results: Iterable[ToolResultSummary],
    claim_units: Iterable[Any] = (),
) -> tuple[list[ClaimEvidenceItem], tuple[str, ...]]:
    """Project only already-read document excerpts cited by the candidate."""

    if not candidate:
        return [], ()
    buckets: dict[tuple[tuple[str, str], str, str], ClaimEvidenceItem] = {}
    order: list[tuple[tuple[str, str], str, str]] = []
    seen: set[tuple[str, tuple[str, str], str, str]] = set()
    untransportable_claim_ids: list[str] = []
    line_requests: dict[tuple[str, str], list[_LineLocatorRequest]] = {}
    sources = _document_locator_sources(requirements, source_evidence, tool_results)
    claim_texts = _candidate_locator_claim_texts(candidate, claim_units)
    for claim_id, claim_text in claim_texts:
        for source in sources:
            artifact_key = document_artifact_identity(
                root=source.root,
                path=source.path,
                identity_path=source.identity_path,
            )
            for locator in _candidate_locators_for_source(claim_text, source):
                if _locator_is_ambiguous(locator.path, source, sources):
                    continue
                key = (claim_id, artifact_key, locator.kind, locator.value)
                if key in seen:
                    continue
                seen.add(key)
                if locator.kind == "line":
                    line_range = parse_document_line_range(
                        locator.value,
                        max_lines=MAX_CANDIDATE_LOCATOR_SOURCE_SPAN_LINES,
                    )
                    if line_range is None:
                        if parse_document_line_range(locator.value, max_lines=None) is not None:
                            untransportable_claim_ids.append(claim_id)
                        continue
                    line_requests.setdefault(artifact_key, []).append(
                        _LineLocatorRequest(artifact_key, source, line_range[0], line_range[1], claim_id)
                    )
                    continue
                excerpt = document_locator_excerpt(source.content, locator)
                if not excerpt:
                    continue
                summary = _locator_summary(locator, excerpt)
                if not summary:
                    untransportable_claim_ids.append(claim_id)
                    continue
                bucket_key = (artifact_key, locator.kind, locator.value)
                prior = buckets.get(bucket_key)
                claim_ids = (claim_id,) if prior is None else tuple(dict.fromkeys((*prior.claim_ids, claim_id)))
                buckets[bucket_key] = ClaimEvidenceItem(
                    "requirement_locator",
                    "read_file",
                    source.path,
                    source.root,
                    source.scope,
                    "ok",
                    summary,
                    identity_path=source.identity_path,
                    claim_id=claim_ids[0],
                    claim_ids=claim_ids,
                )
                if prior is None:
                    order.append(bucket_key)
    line_buckets, line_order, line_untransportable = _packed_line_locator_items(line_requests)
    for key, item in line_buckets.items():
        buckets[key] = item
    order.extend(line_order)
    untransportable_claim_ids.extend(line_untransportable)
    items, omitted_claim_ids = _fair_candidate_locator_items(buckets, order)
    return items, tuple(dict.fromkeys((*omitted_claim_ids, *untransportable_claim_ids)))


def _packed_line_locator_items(
    requests_by_artifact: dict[tuple[str, str], list[_LineLocatorRequest]],
) -> tuple[
    dict[tuple[tuple[str, str], str, str], ClaimEvidenceItem],
    list[tuple[tuple[str, str], str, str]],
    tuple[str, ...],
]:
    buckets: dict[tuple[tuple[str, str], str, str], ClaimEvidenceItem] = {}
    order: list[tuple[tuple[str, str], str, str]] = []
    untransportable_claim_ids: list[str] = []
    for artifact_key, requests in requests_by_artifact.items():
        segments: list[tuple[int, int, tuple[str, ...], _DocumentLocatorSource]] = []
        for request in requests:
            current = request.start
            while current <= request.end:
                end = min(request.end, current + MAX_CANDIDATE_LOCATOR_WINDOW_LINES - 1)
                segments.append((current, end, (request.claim_id,), request.source))
                current = end + 1
        merged = _merge_line_segments(segments)
        for start, end, claim_ids, source in merged:
            chunks = _line_locator_summary_chunks(source, start, end)
            if not chunks:
                untransportable_claim_ids.extend(claim_ids)
                continue
            for chunk_start, chunk_end, summary in chunks:
                key = (artifact_key, "line", f"{chunk_start}-{chunk_end}")
                buckets[key] = ClaimEvidenceItem(
                    "requirement_locator",
                    "read_file",
                    source.path,
                    source.root,
                    source.scope,
                    "ok",
                    summary,
                    identity_path=source.identity_path,
                    claim_id=claim_ids[0],
                    claim_ids=claim_ids,
                )
                order.append(key)
    return buckets, order, tuple(dict.fromkeys(untransportable_claim_ids))


def _merge_line_segments(
    segments: list[tuple[int, int, tuple[str, ...], _DocumentLocatorSource]],
) -> list[tuple[int, int, tuple[str, ...], _DocumentLocatorSource]]:
    merged: list[tuple[int, int, tuple[str, ...], _DocumentLocatorSource]] = []
    for start, end, claim_ids, source in sorted(segments, key=lambda item: (item[0], item[1], item[2])):
        if not merged:
            merged.append((start, end, claim_ids, source))
            continue
        prior_start, prior_end, prior_claim_ids, prior_source = merged[-1]
        combined_end = max(prior_end, end)
        can_merge = (
            start <= prior_end + MAX_CANDIDATE_LOCATOR_WINDOW_GAP
            and combined_end - prior_start + 1 <= MAX_CANDIDATE_LOCATOR_WINDOW_LINES
            and prior_source == source
        )
        if can_merge:
            merged[-1] = (
                prior_start,
                combined_end,
                tuple(dict.fromkeys((*prior_claim_ids, *claim_ids))),
                prior_source,
            )
            continue
        merged.append((start, end, claim_ids, source))
    return merged


def _line_locator_summary_chunks(
    source: _DocumentLocatorSource,
    start: int,
    end: int,
) -> tuple[tuple[int, int, str], ...]:
    locator = DocumentLocator(source.path, "line", f"{start}-{end}" if start != end else str(start))
    excerpt = document_locator_excerpt(source.content, locator)
    if not excerpt:
        return ()
    rows = _summary_rows_from_excerpt(excerpt)
    if not rows:
        return ()
    chunks: list[tuple[int, int, str]] = []
    current: list[tuple[int, str]] = []
    current_chars = 0
    for number, line in rows:
        if len(line) > _line_summary_body_budget(number, number):
            return ()
        projected_chars = current_chars + len(line) + (1 if current else 0)
        if current and projected_chars > _line_summary_body_budget(current[0][0], number):
            chunks.append(_render_line_summary_chunk(current))
            current = []
            current_chars = 0
        current.append((number, line))
        current_chars += len(line) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append(_render_line_summary_chunk(current))
    return tuple(chunks)


def _read_only_explore_coverage_items(results: Iterable[ToolResultSummary]) -> list[ClaimEvidenceItem]:
    items: list[ClaimEvidenceItem] = []
    for coverage in read_only_root_coverage(tuple(results)):
        if not coverage.attempted_without_direct_read:
            continue
        status = (
            "searched/no direct read/unlocated"
            if coverage.successful_searches > 0
            else "attempted/no direct read/unlocated"
        )
        summary = (
            f"Bounded root coverage: {status}. "
            f"search_attempts={coverage.search_attempts}; successful_searches={coverage.successful_searches}; "
            f"no_match={coverage.no_match}; "
            f"failures={coverage.failures}; suppressed={coverage.suppressed}. "
            "This root was attempted in the authorized scope, but no successful read_file observation covered it."
        )
        items.append(
            ClaimEvidenceItem(
                "unlocated",
                "read_only_explore",
                coverage.root,
                coverage.root,
                "root_local",
                "incomplete",
                summary,
                identity_path=coverage.root,
            )
        )
    return items


def _summary_rows_from_excerpt(excerpt: str) -> tuple[tuple[int, str], ...]:
    rows: list[tuple[int, str]] = []
    for raw_line in (excerpt or "").splitlines():
        match = re.match(r"^(?:.*?:)?(\d{1,6})\s*[:：]\s*(.*)$", raw_line.strip())
        if not match:
            continue
        rows.append((int(match.group(1)), f"{int(match.group(1))}: {match.group(2).strip()}"))
    return tuple(rows)


def _line_summary_body_budget(start: int, end: int) -> int:
    prefix = f"line {start}-{end}:" if start != end else f"line {start}:"
    return MAX_CANDIDATE_LOCATOR_SUMMARY_CHARS - len(prefix) - 1


def _render_line_summary_chunk(rows: list[tuple[int, str]]) -> tuple[int, int, str]:
    start = rows[0][0]
    end = rows[-1][0]
    prefix = f"line {start}-{end}:" if start != end else f"line {start}:"
    return start, end, f"{prefix}\n" + "\n".join(line for _, line in rows)


def _candidate_locator_claim_texts(candidate: str, claim_units: Iterable[Any]) -> tuple[tuple[str, str], ...]:
    items: list[tuple[str, str]] = []
    for index, unit in enumerate(claim_units):
        claim_id = str(getattr(unit, "claim_id", "") or f"candidate-{index + 1:03d}")
        text = str(getattr(unit, "text", "") or "")
        if text:
            context = str(getattr(unit, "locator_context", "") or "")
            items.append((claim_id, f"{context}\n{text}" if context else text))
    return tuple(items) or (("candidate", candidate),)


def _candidate_locators_for_source(candidate: str, source: _DocumentLocatorSource) -> tuple[Any, ...]:
    locators: list[Any] = []
    refs = tuple(
        dict.fromkeys(
            ref
            for ref in (
                source.path,
                source.identity_path if source.identity_path and source.identity_path != source.path else "",
            )
            if ref
        )
    )
    for ref in refs:
        locators.extend(parse_document_locators(candidate, ref))
    return tuple(dict.fromkeys(locators))


def _document_locator_sources(
    requirements: Iterable[RequirementEvidence],
    source_evidence: Iterable[SourceEvidence],
    tool_results: Iterable[ToolResultSummary],
) -> list[_DocumentLocatorSource]:
    sources: dict[tuple[str, str], _DocumentLocatorSource] = {}
    for item in requirements:
        root = item.root or "(unknown)"
        source = _DocumentLocatorSource(
            path=item.path,
            content=item.content,
            root=root,
            scope=item.scope or "root_local",
            identity_path=document_artifact_identity(root=root, path=item.path)[1],
        )
        _store_document_locator_source(sources, source)
    for item in source_evidence:
        if not str(item.path or "").lower().endswith((".md", ".markdown", ".html", ".htm")):
            continue
        source = _DocumentLocatorSource(
            path=item.path,
            content=item.content,
            root=item.root or "(unknown)",
            scope=item.scope or "root_local",
            identity_path=item.path,
        )
        _store_document_locator_source(sources, source)
    for item in tool_results:
        if item.name != "read_file" or item.is_error:
            continue
        if not str(item.path or "").lower().endswith((".md", ".markdown", ".html", ".htm")):
            continue
        source = _DocumentLocatorSource(
            path=item.path or "(none)",
            content=item.content,
            root=str(item.metadata.get("evidence_root") or item.metadata.get("evidence_root_label") or "(unknown)"),
            scope=str(item.metadata.get("evidence_scope") or "root_local"),
            identity_path=str(item.metadata.get("resolved_path") or item.path or ""),
        )
        _store_document_locator_source(sources, source)
    return list(sources.values())


def _store_document_locator_source(
    sources: dict[tuple[str, str], _DocumentLocatorSource],
    source: _DocumentLocatorSource,
) -> None:
    key = document_artifact_identity(root=source.root, path=source.path, identity_path=source.identity_path)
    prior = sources.get(key)
    if prior is None or len(source.content or "") > len(prior.content or ""):
        sources[key] = source


def _locator_is_ambiguous(
    cited_path: str,
    source: _DocumentLocatorSource,
    sources: Iterable[_DocumentLocatorSource],
) -> bool:
    """A citation that resolves to multiple artifacts cannot bind safely."""

    cited = (cited_path or "").replace("\\", "/").strip().strip("`")
    if not cited:
        return False
    identities = {
        document_artifact_identity(root=item.root, path=item.path, identity_path=item.identity_path)
        for item in sources
        if _citation_matches_source(cited, item)
    }
    current = document_artifact_identity(root=source.root, path=source.path, identity_path=source.identity_path)
    return current in identities and len(identities) > 1


def _citation_matches_source(cited_path: str, source: _DocumentLocatorSource) -> bool:
    cited = (cited_path or "").replace("\\", "/").strip().strip("`")
    if not cited:
        return False
    source_identity = document_artifact_identity(root=source.root, path=source.path, identity_path=source.identity_path)
    if "/" not in cited:
        return source.path.replace("\\", "/").rsplit("/", 1)[-1].casefold() == cited.casefold()
    cited_identity = document_artifact_identity(
        root=source.root,
        path=cited,
        identity_path=cited if cited.startswith("/") else "",
    )
    return cited_identity == source_identity


def _fair_candidate_locator_items(
    buckets: dict[tuple[tuple[str, str], str, str], ClaimEvidenceItem],
    order: list[tuple[tuple[str, str], str, str]],
) -> tuple[list[ClaimEvidenceItem], tuple[str, ...]]:
    items: list[ClaimEvidenceItem] = []
    resolved_claim_counts: Counter[str] = Counter(
        claim_id
        for item in buckets.values()
        for claim_id in item.claim_ids
    )
    transported_claim_counts: Counter[str] = Counter()
    total_summary_chars = 0
    by_artifact: dict[tuple[str, str], list[tuple[tuple[str, str], str, str]]] = {}
    artifact_order: list[tuple[str, str]] = []
    for key in order:
        artifact_key = key[0]
        if artifact_key not in by_artifact:
            by_artifact[artifact_key] = []
            artifact_order.append(artifact_key)
        by_artifact[artifact_key].append(key)
    cursors = {artifact_key: 0 for artifact_key in artifact_order}
    while len(items) < MAX_CANDIDATE_LOCATOR_ITEMS:
        made_progress = False
        for artifact_key in artifact_order:
            keys = by_artifact[artifact_key]
            while cursors[artifact_key] < len(keys):
                key = keys[cursors[artifact_key]]
                cursors[artifact_key] += 1
                item = buckets[key]
                if total_summary_chars + len(item.summary) > MAX_CANDIDATE_LOCATOR_TOTAL_CHARS:
                    continue
                items.append(item)
                transported_claim_counts.update(item.claim_ids)
                total_summary_chars += len(item.summary)
                made_progress = True
                break
            if len(items) >= MAX_CANDIDATE_LOCATOR_ITEMS:
                break
        if not made_progress:
            break
    omitted_claim_ids = [
        claim_id
        for claim_id, required_count in sorted(resolved_claim_counts.items())
        if transported_claim_counts[claim_id] < required_count
    ]
    return items, tuple(omitted_claim_ids)


def _locator_summary(locator: Any, excerpt: str) -> str:
    prefix = f"{locator.kind} {locator.value}:"
    budget = MAX_CANDIDATE_LOCATOR_SUMMARY_CHARS - len(prefix) - 1
    if budget <= 120:
        return ""
    body = _bounded_complete_lines(excerpt, budget)
    return f"{prefix}\n{body}" if body else ""


def _bounded_complete_lines(value: str, budget: int) -> str:
    lines = [line.rstrip() for line in (value or "").splitlines() if line.strip()]
    if not lines:
        return ""
    joined = "\n".join(lines)
    if len(joined) <= budget:
        return joined
    head: list[str] = []
    tail: list[str] = []
    used = 0
    marker = "[omitted middle lines]"
    half_budget = max(0, (budget - len(marker) - 2) // 2)
    for line in lines:
        if len(line) > half_budget:
            continue
        if used + len(line) + (1 if head else 0) > half_budget:
            break
        head.append(line)
        used += len(line) + (1 if head else 0)
    used = 0
    for line in reversed(lines):
        if len(line) > half_budget:
            continue
        if used + len(line) + (1 if tail else 0) > half_budget:
            break
        tail.append(line)
        used += len(line) + (1 if tail else 0)
    rendered = [*head, marker, *reversed(tail)]
    if rendered == [marker]:
        return ""
    return "\n".join(rendered)


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
            identity_path=prior.identity_path,
            claim_id=prior.claim_id,
            claim_ids=tuple(dict.fromkeys((*prior.claim_ids, *item.claim_ids))),
        )
    return list(deduped.values())


def _handoff_item_identity(item: ClaimEvidenceItem) -> tuple[str, ...]:
    """Use artifact identity rather than display spelling for document reads."""

    if item.classification == "inspection_failure":
        return ("failure", item.classification, canonical_root_identity(item.root), item.outcome)
    if item.tool in {"read_file", "inspect_image"} and item.outcome == "ok":
        root_identity, artifact_identity = document_artifact_identity(
            root=item.root,
            path=item.path,
            identity_path=item.identity_path,
        )
        if item.classification == "requirement_locator":
            return ("requirement_locator", root_identity, artifact_identity, item.summary)
        if item.classification == "document_reconciliation_support":
            return ("document_support", root_identity, artifact_identity, item.summary)
        return ("artifact", item.tool, root_identity, artifact_identity)
    return ("item", item.tool, item.path, item.outcome)

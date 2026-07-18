from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
import hashlib
import json
import re
from typing import Any, Iterable, Mapping

from ..evidence import EvidenceRecord
from ..requirement_evidence import RequirementEvidence
from ..requirement_evidence import is_requirement_source_path
from ..steering.final_answer import SourceEvidence
from ..tools.observation import ToolResultSummary


MAX_SESSION_EVIDENCE_ENTRIES = 24
MAX_ENTRY_CONTENT_CHARS = 12_000
MAX_ENTRY_TAGGED_PATHS = 32
MAX_SESSION_EVIDENCE_JOURNAL_EVENTS = 96
_CACHEABLE_TOOLS = frozenset(
    {
        "read_file",
        "search_code",
        "lsp_symbols",
        "lsp_workspace_symbols",
        "lsp_document_symbols",
        "lsp_definition",
        "lsp_references",
        "lsp_diagnostics",
    }
)
_TOKEN_STOPWORDS = frozenset(
    {
        "about",
        "analysis",
        "and",
        "code",
        "file",
        "for",
        "from",
        "help",
        "inspect",
        "module",
        "source",
        "the",
        "this",
        "what",
        "with",
        "一下",
        "代码",
        "分析",
        "当前",
        "文件",
        "项目",
        "请",
        "这个",
    }
)
_CHINESE_FRAGMENT_STOPWORDS = frozenset({"需求", "文档", "代码", "项目", "当前", "分析", "功能", "文件", "服务"})


@dataclass(frozen=True)
class CachedEvidenceEntry:
    """A bounded, in-memory positive observation from an earlier chat run."""

    entry_id: str
    tool_result: ToolResultSummary
    record: EvidenceRecord
    source_evidence: SourceEvidence | None
    requirement_evidence: RequirementEvidence | None
    root: str
    workspace_revision: int
    content_tags: Mapping[str, str]
    request_tokens: frozenset[str]
    evidence_tokens: frozenset[str]
    origin_run_id: str


@dataclass(frozen=True)
class SessionEvidenceReuse:
    entries: tuple[CachedEvidenceEntry, ...] = ()
    hit_count: int = 0
    miss_count: int = 0
    stale_count: int = 0
    invalidation_count: int = 0

    @property
    def reused_paths(self) -> tuple[str, ...]:
        paths: list[str] = []
        for entry in self.entries:
            for path in entry.content_tags:
                if path not in paths:
                    paths.append(path)
        return tuple(paths)


class SessionEvidenceCache:
    """Fresh, session-only tool evidence that may be projected into a follow-up run.

    A typed journal may restore entries when a named session is reopened, but
    only this cache can satisfy Runtime evidence gates and every entry is
    revalidated against authorization, workspace revision, and content hashes.
    """

    def __init__(self, *, max_entries: int = MAX_SESSION_EVIDENCE_ENTRIES) -> None:
        self._max_entries = max(1, max_entries)
        self._entries: list[CachedEvidenceEntry] = []
        self._invalidations = 0
        self._last_request_tokens: frozenset[str] = frozenset()
        self._last_origin_run_id: str | None = None

    def remember_request(self, prompt: str, run_id: str) -> None:
        self._last_request_tokens = _meaningful_tokens(prompt)
        self._last_origin_run_id = run_id

    def capture(
        self,
        *,
        tool_result: ToolResultSummary,
        record: EvidenceRecord | None,
        source_evidence: SourceEvidence | None,
        requirement_evidence: RequirementEvidence | None,
        workspace_revision: int,
        request: str,
        run_id: str,
    ) -> CachedEvidenceEntry | None:
        """Store a successful positive observation when it has verifiable file tags."""

        if tool_result.name not in _CACHEABLE_TOOLS or tool_result.is_error or tool_result.useless or record is None:
            return None
        if _is_negative_or_incomplete(tool_result, record):
            return None
        content_tags = _content_tags_for(tool_result, record, source_evidence)
        # A search/LSP result without concrete files is not safe to reuse. It is
        # still available in the transcript, but cannot become current evidence.
        if not content_tags:
            return None
        root_value = record.details.get("evidence_root")
        if not root_value and source_evidence is not None:
            root_value = source_evidence.root
        root = str(root_value or "")
        if not root:
            return None
        cached_path = tool_result.path
        if tool_result.name == "read_file":
            resolved_path = record.details.get("resolved_path")
            if isinstance(resolved_path, str) and resolved_path:
                cached_path = str(Path(resolved_path).resolve())
        normalized_summary = replace(
            tool_result,
            path=cached_path,
            content=tool_result.content[:MAX_ENTRY_CONTENT_CHARS],
            metadata={**dict(tool_result.metadata), "evidence_origin": "current_run"},
        )
        normalized_record = replace(
            record,
            details={**dict(record.details), "evidence_origin": "current_run"},
        )
        if source_evidence is not None:
            source_evidence = replace(source_evidence, origin="current_run")
        if requirement_evidence is not None:
            requirement_evidence = replace(requirement_evidence, origin="current_run")
        entry = CachedEvidenceEntry(
            entry_id=_entry_id(normalized_summary, normalized_record, content_tags),
            tool_result=normalized_summary,
            record=normalized_record,
            source_evidence=source_evidence,
            requirement_evidence=requirement_evidence,
            root=root,
            workspace_revision=workspace_revision,
            content_tags=content_tags,
            request_tokens=_meaningful_tokens(request),
            evidence_tokens=_evidence_tokens(normalized_summary, normalized_record, source_evidence),
            origin_run_id=run_id,
        )
        self._entries = [item for item in self._entries if item.entry_id != entry.entry_id]
        self._entries.append(entry)
        self._entries = self._entries[-self._max_entries :]
        return entry

    def restore_entries(self, payloads: Iterable[Mapping[str, Any]]) -> int:
        """Restore bounded typed observations; freshness is checked on reuse."""

        restored: list[CachedEvidenceEntry] = []
        for payload in payloads:
            entry = deserialize_cached_evidence_entry(payload)
            if entry is None:
                continue
            restored = [item for item in restored if item.entry_id != entry.entry_id]
            restored.append(entry)
        self._entries = restored[-self._max_entries :]
        if self._entries:
            latest = self._entries[-1]
            self._last_request_tokens = latest.request_tokens
            self._last_origin_run_id = latest.origin_run_id
        return len(self._entries)

    def reuse_for_request(
        self,
        *,
        prompt: str,
        workspace_revision: int,
        authorized_roots: Iterable[Path],
    ) -> SessionEvidenceReuse:
        """Return fresh, relevant entries without treating prior text as proof."""

        roots = {str(root.resolve()) for root in authorized_roots}
        prompt_tokens = _meaningful_tokens(prompt)
        reused: list[CachedEvidenceEntry] = []
        retained: list[CachedEvidenceEntry] = []
        misses = 0
        stale = 0
        for entry in self._entries:
            if entry.root not in roots:
                stale += 1
                continue
            if not _content_tags_are_fresh(entry.content_tags):
                stale += 1
                continue
            if entry.workspace_revision != workspace_revision:
                entry = replace(entry, workspace_revision=workspace_revision)
            retained.append(entry)
            if not _is_relevant(entry, prompt_tokens, self._last_request_tokens, self._last_origin_run_id):
                misses += 1
                continue
            reused.append(_as_session_cached(entry))
        evicted = len(self._entries) - len(retained)
        if evicted:
            self._entries = retained
            self._invalidations += evicted
        return SessionEvidenceReuse(
            entries=tuple(reused),
            hit_count=len(reused),
            miss_count=misses,
            stale_count=stale,
            invalidation_count=evicted,
        )

    def invalidate_paths(self, paths: Iterable[Path]) -> int:
        normalized = {str(path.resolve()) for path in paths}
        if not normalized:
            return 0
        before = len(self._entries)
        self._entries = [
            entry
            for entry in self._entries
            if not normalized.intersection(entry.content_tags)
        ]
        removed = before - len(self._entries)
        self._invalidations += removed
        return removed

    def invalidate_workspace_revision(self) -> int:
        removed = len(self._entries)
        self._entries.clear()
        self._invalidations += removed
        return removed

    def revalidate_authorized_roots(
        self,
        *,
        workspace_revision: int,
        authorized_roots: Iterable[Path],
    ) -> int:
        """Evict only no-longer-authorized/stale entries and rebase the rest.

        Session-root changes adjust authorization. Evidence from roots that
        remain authorized is still usable when its content hash is fresh. A
        workspace move is intentionally handled by the workspace lifecycle as a
        conservative full reset because it changes the primary workspace/state
        partition.
        """

        roots = {str(root.resolve()) for root in authorized_roots}
        retained: list[CachedEvidenceEntry] = []
        for entry in self._entries:
            if entry.root not in roots:
                continue
            if not _content_tags_are_fresh(entry.content_tags):
                continue
            retained.append(replace(entry, workspace_revision=workspace_revision))
        removed = len(self._entries) - len(retained)
        self._entries = retained
        if removed:
            self._invalidations += removed
        return removed

    def snapshot(self) -> dict[str, object]:
        return {
            "entries": len(self._entries),
            "invalidations": self._invalidations,
            "paths": list(_cached_paths(self._entries)),
        }


def query_identity(
    name: str,
    arguments: str | Mapping[str, Any],
    *,
    canonical_path: str | None = None,
) -> str:
    """Stable semantic identity for replacing duplicate cross-run observations."""
    parsed = _parse_arguments(arguments)
    if name == "read_file":
        fields = {
            "path": canonical_path or str(parsed.get("path") or ""),
            "start_line": _positive_int_or_default(parsed.get("start_line"), default=1),
            "end_line": _optional_positive_int(parsed.get("end_line")),
        }
    elif name == "search_code":
        fields = {
            "path": canonical_path or str(parsed.get("path") or ""),
            "pattern": str(parsed.get("pattern") or ""),
            "max_results": _optional_positive_int(parsed.get("max_results")),
        }
    elif name.startswith("lsp_"):
        fields = {
            key: (canonical_path if key == "path" and canonical_path else parsed.get(key))
            for key in ("path", "symbol", "query", "line", "character", "max_results")
            if key in parsed
        }
    else:
        fields = parsed
    return json.dumps({"tool": name, "arguments": fields}, ensure_ascii=False, sort_keys=True, default=str)


def _parse_arguments(arguments: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(arguments, Mapping):
        return dict(arguments)
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _positive_int_or_default(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _optional_positive_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _content_tags_for(
    tool_result: ToolResultSummary,
    record: EvidenceRecord,
    source_evidence: SourceEvidence | None,
) -> dict[str, str]:
    paths: list[Path] = []
    root_value = record.details.get("evidence_root")
    root = Path(str(root_value)).resolve() if isinstance(root_value, str) and root_value else None
    resolved = record.details.get("resolved_path")
    if isinstance(resolved, str) and resolved:
        paths.append(Path(resolved))
    for raw_path in tool_result.metadata.get("evidence_paths", ()):
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            if root is None:
                return {}
            candidate = root / candidate
        paths.append(candidate)
    if source_evidence is not None:
        resolved = record.details.get("resolved_path")
        if isinstance(resolved, str) and resolved:
            paths.append(Path(resolved))
    unique_paths: dict[str, Path] = {}
    for path in paths:
        try:
            resolved_path = path.resolve()
        except OSError:
            return {}
        if root is not None:
            try:
                resolved_path.relative_to(root)
            except ValueError:
                return {}
        unique_paths[str(resolved_path)] = resolved_path
    if not unique_paths or len(unique_paths) > MAX_ENTRY_TAGGED_PATHS:
        return {}
    tags: dict[str, str] = {}
    for key, path in unique_paths.items():
        tag = _content_tag(path)
        if tag is None:
            return {}
        tags[key] = tag
    if len(tags) != len(unique_paths):
        return {}
    return tags


def _content_tag(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _content_tags_are_fresh(tags: Mapping[str, str]) -> bool:
    return bool(tags) and all(_content_tag(Path(path)) == tag for path, tag in tags.items())


def _is_negative_or_incomplete(result: ToolResultSummary, record: EvidenceRecord) -> bool:
    metadata = result.metadata
    return bool(
        result.useless
        or metadata.get("truncated")
        or metadata.get("evidence_paths_overflow")
        or metadata.get("complete") is False
        or metadata.get("negative_evidence_type")
        or record.status in {"content_no_match", "exact_path_missing", "no_match", "path_no_match"}
    )


def _is_relevant(
    entry: CachedEvidenceEntry,
    prompt_tokens: frozenset[str],
    previous_request_tokens: frozenset[str],
    previous_run_id: str | None,
) -> bool:
    if prompt_tokens.intersection(entry.evidence_tokens):
        return True
    if prompt_tokens.intersection(entry.request_tokens):
        return True
    # A very short follow-up has no reliable new entity. Continue only the most
    # recent request's subject, rather than opening every cached README/result.
    if (
        not prompt_tokens
        and entry.origin_run_id == previous_run_id
        and previous_request_tokens.intersection(entry.request_tokens)
    ):
        return True
    return False


def _meaningful_tokens(text: str) -> frozenset[str]:
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*|[\u4e00-\u9fff]{2,}", text.lower())
    tokens: set[str] = set()
    for token in raw:
        if token in _TOKEN_STOPWORDS:
            continue
        tokens.add(token)
        tokens.update(part for part in re.split(r"[._-]", token) if len(part) >= 2 and part not in _TOKEN_STOPWORDS)
        if all("\u4e00" <= character <= "\u9fff" for character in token):
            for width in (2, 3):
                tokens.update(
                    token[index : index + width]
                    for index in range(max(0, len(token) - width + 1))
                    if token[index : index + width] not in _CHINESE_FRAGMENT_STOPWORDS
                )
    return frozenset(tokens)


def _evidence_tokens(
    result: ToolResultSummary,
    record: EvidenceRecord,
    source: SourceEvidence | None,
) -> frozenset[str]:
    material = "\n".join(
        part
        for part in (
            result.path or "",
            record.subject,
            source.path if source is not None else "",
            result.content[:2000],
        )
        if part
    )
    return _meaningful_tokens(material)


def _entry_id(result: ToolResultSummary, record: EvidenceRecord, tags: Mapping[str, str]) -> str:
    """Identify a logical observation across runs, not one specific execution."""
    query_identity = result.metadata.get("session_evidence_query_identity")
    fallback_identity = (result.path or "", record.subject)
    payload = repr(
        (
            result.name,
            query_identity if isinstance(query_identity, str) else fallback_identity,
            tuple(sorted(tags)),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _as_session_cached(entry: CachedEvidenceEntry) -> CachedEvidenceEntry:
    return replace(
        entry,
        tool_result=replace(
            entry.tool_result,
            metadata={**dict(entry.tool_result.metadata), "evidence_origin": "session_cached", "cache_entry_id": entry.entry_id},
        ),
        record=replace(entry.record, details={**dict(entry.record.details), "evidence_origin": "session_cached", "cache_entry_id": entry.entry_id}),
        source_evidence=(replace(entry.source_evidence, origin="session_cached") if entry.source_evidence is not None else None),
        requirement_evidence=(replace(entry.requirement_evidence, origin="session_cached") if entry.requirement_evidence is not None else None),
    )


def _cached_paths(entries: Iterable[CachedEvidenceEntry]) -> tuple[str, ...]:
    paths: list[str] = []
    for entry in entries:
        for path in entry.content_tags:
            if path not in paths:
                paths.append(path)
    return tuple(paths)


def serialize_cached_evidence_entry(entry: CachedEvidenceEntry) -> dict[str, Any]:
    """Return the versioned JSON form owned by the session-evidence layer."""

    return {
        "version": 1,
        "tool_result": {
            "name": entry.tool_result.name,
            "content": entry.tool_result.content,
            "is_error": entry.tool_result.is_error,
            "useless": entry.tool_result.useless,
            "path": entry.tool_result.path,
            "changed": entry.tool_result.changed,
            "metadata": _json_value(entry.tool_result.metadata),
        },
        "record": {
            "tool": entry.record.tool,
            "subject": entry.record.subject,
            "summary": entry.record.summary,
            "status": entry.record.status,
            "details": _json_value(entry.record.details),
        },
        "source_evidence": _source_payload(entry.source_evidence),
        "requirement_evidence": _requirement_payload(entry.requirement_evidence),
        "root": entry.root,
        "workspace_revision": entry.workspace_revision,
        "content_tags": dict(entry.content_tags),
        "request_tokens": sorted(entry.request_tokens),
        "evidence_tokens": sorted(entry.evidence_tokens),
        "origin_run_id": entry.origin_run_id,
    }


def is_journal_safe_cached_evidence(entry: CachedEvidenceEntry) -> bool:
    """Return whether an entry can be reconstructed from current file bytes.

    Search/LSP output remains useful inside one Runtime, but it cannot be
    authenticated after restart. A single-file read can be rebuilt exactly
    enough for evidence projection, so it is the only persisted cache form.
    """

    return entry.tool_result.name == "read_file" and len(entry.content_tags) == 1


def deserialize_cached_evidence_entry(payload: Mapping[str, Any]) -> CachedEvidenceEntry | None:
    """Restore only a positive read observation rebuilt from current file bytes."""

    try:
        if payload.get("version") != 1:
            return None
        raw_result = payload["tool_result"]
        raw_record = payload["record"]
        raw_tags = payload["content_tags"]
        if not all(isinstance(value, Mapping) for value in (raw_result, raw_record, raw_tags)):
            return None
        name = raw_result.get("name")
        root = payload.get("root")
        if name != "read_file" or not isinstance(root, str) or not root:
            return None
        root_path = Path(root).resolve()
        tags = {
            str(path): str(tag)
            for path, tag in raw_tags.items()
            if isinstance(path, str) and isinstance(tag, str) and path and tag
        }
        if not tags or len(tags) != len(raw_tags) or len(tags) > MAX_ENTRY_TAGGED_PATHS:
            return None
        for path, tag in tags.items():
            if not re.fullmatch(r"[0-9a-f]{64}", tag):
                return None
            resolved_path = Path(path).resolve()
            try:
                resolved_path.relative_to(root_path)
            except ValueError:
                return None
            if _content_tag(resolved_path) != tag:
                return None
        metadata = raw_result.get("metadata")
        details = raw_record.get("details")
        if not isinstance(metadata, Mapping) or not isinstance(details, Mapping):
            return None
        if _journal_root_mismatch(metadata.get("evidence_root"), root_path) or _journal_root_mismatch(
            details.get("evidence_root"), root_path
        ):
            return None
        tagged_path = Path(next(iter(tags))).resolve()
        restored_content = _read_journal_file_content(tagged_path)
        if restored_content is None:
            return None
        display_path = str(tagged_path.relative_to(root_path))
        tool_result = ToolResultSummary(
            name="read_file",
            content=restored_content,
            path=str(tagged_path),
            metadata={
                "evidence_root": str(root_path),
                "evidence_root_label": str(details.get("evidence_root_label") or "(unknown)"),
                "evidence_scope": "root_local",
                "resolved_path": str(tagged_path),
                "evidence_origin": "session_journal",
            },
        )
        record = EvidenceRecord(
            tool="read_file",
            subject=display_path,
            summary=f"restored read of {display_path}",
            details=dict(tool_result.metadata),
        )
        revision = int(payload.get("workspace_revision", 0))
        if revision < 0:
            return None
        source = SourceEvidence(display_path, restored_content, root=str(root_path), scope="root_local", origin="session_journal")
        requirement = (
            RequirementEvidence(display_path, restored_content, root=str(root_path), scope="root_local", origin="session_journal")
            if isinstance(payload.get("requirement_evidence"), Mapping) and is_requirement_source_path(display_path)
            else None
        )
        entry_id = _entry_id(tool_result, record, tags)
        return CachedEvidenceEntry(
            entry_id=entry_id,
            tool_result=tool_result,
            record=record,
            source_evidence=source,
            requirement_evidence=requirement,
            root=str(root_path),
            workspace_revision=revision,
            content_tags=tags,
            request_tokens=frozenset(),
            evidence_tokens=_evidence_tokens(tool_result, record, source),
            origin_run_id="",
        )
    except (KeyError, TypeError, ValueError):
        return None


def _source_payload(source: SourceEvidence | None) -> dict[str, str | None] | None:
    if source is None:
        return None
    return {"path": source.path, "content": source.content, "root": source.root, "scope": source.scope, "origin": source.origin}


def _requirement_payload(requirement: RequirementEvidence | None) -> dict[str, str | None] | None:
    if requirement is None:
        return None
    return {
        "path": requirement.path,
        "content": requirement.content,
        "root": requirement.root,
        "scope": requirement.scope,
        "origin": requirement.origin,
    }


def _journal_root_mismatch(value: object, root: Path) -> bool:
    if value in {None, ""}:
        return False
    if not isinstance(value, str):
        return True
    try:
        return Path(value).resolve() != root
    except OSError:
        return True


def _read_journal_file_content(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")[:MAX_ENTRY_CONTENT_CHARS]
    except (OSError, UnicodeDecodeError):
        return None


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    return str(value)


__all__ = [
    "CachedEvidenceEntry",
    "MAX_SESSION_EVIDENCE_ENTRIES",
    "MAX_SESSION_EVIDENCE_JOURNAL_EVENTS",
    "SessionEvidenceCache",
    "SessionEvidenceReuse",
    "deserialize_cached_evidence_entry",
    "is_journal_safe_cached_evidence",
    "serialize_cached_evidence_entry",
]

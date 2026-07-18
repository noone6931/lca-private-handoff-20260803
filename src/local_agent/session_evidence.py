"""Compatibility imports for session evidence caching."""

from .session.evidence import (
    CachedEvidenceEntry,
    MAX_SESSION_EVIDENCE_ENTRIES,
    MAX_SESSION_EVIDENCE_JOURNAL_EVENTS,
    SessionEvidenceCache,
    SessionEvidenceReuse,
    deserialize_cached_evidence_entry,
    is_journal_safe_cached_evidence,
    query_identity,
    serialize_cached_evidence_entry,
)

__all__ = [
    "CachedEvidenceEntry",
    "MAX_SESSION_EVIDENCE_ENTRIES",
    "MAX_SESSION_EVIDENCE_JOURNAL_EVENTS",
    "SessionEvidenceCache",
    "SessionEvidenceReuse",
    "deserialize_cached_evidence_entry",
    "is_journal_safe_cached_evidence",
    "query_identity",
    "serialize_cached_evidence_entry",
]

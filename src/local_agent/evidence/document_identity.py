"""Canonical identity helpers for already-authorized document artifacts."""
from __future__ import annotations

from pathlib import Path


def canonical_root_identity(root: str) -> str:
    value = (root or "").strip()
    if value.startswith("/"):
        try:
            return str(Path(value).resolve())
        except OSError:
            return value
    return value.casefold()


def document_artifact_identity(*, root: str, path: str, identity_path: str = "") -> tuple[str, str]:
    """Return a root-aware artifact identity for handoff/deduplication policy.

    A relative path is resolved under a canonical absolute root when available,
    so ``root=/workspace`` + ``docs/a.md`` matches ``/workspace/docs/a.md``.
    Different roots keep same-basename artifacts distinct.
    """

    root_key = canonical_root_identity(root)
    raw_path = (identity_path or path or "").strip()
    if not raw_path:
        return (root_key, "")
    artifact_path = Path(raw_path)
    if artifact_path.is_absolute():
        try:
            return (root_key, str(artifact_path.resolve()))
        except OSError:
            return (root_key, artifact_path.as_posix())
    normalized = artifact_path.as_posix().strip("/")
    if root_key.startswith("/"):
        try:
            return (root_key, str((Path(root_key) / normalized).resolve()))
        except OSError:
            return (root_key, (Path(root_key) / normalized).as_posix())
    return (root_key, normalized.casefold())

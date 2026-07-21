from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence


TransactionStatus = Literal["committed", "stale", "rolled_back", "rollback_failed"]


@dataclass(frozen=True)
class ExistingTextFileChange:
    path: Path
    before_bytes: bytes
    after_bytes: bytes
    before_sha256: str
    after_sha256: str

    @classmethod
    def create(cls, path: Path, before_bytes: bytes, after_bytes: bytes) -> "ExistingTextFileChange":
        return cls(
            path=path,
            before_bytes=before_bytes,
            after_bytes=after_bytes,
            before_sha256=_sha256(before_bytes),
            after_sha256=_sha256(after_bytes),
        )


@dataclass(frozen=True)
class TextTransactionResult:
    status: TransactionStatus
    workspace_changed: bool
    changed_paths: tuple[Path, ...]
    error_kind: str | None = None


def apply_existing_text_transaction(
    changes: Sequence[ExistingTextFileChange],
) -> TextTransactionResult:
    """Apply exact existing-file byte changes with bounded synchronous compensation."""

    normalized, error_kind = _validate_changes(changes)
    if error_kind is not None:
        return TextTransactionResult("stale", False, (), error_kind)
    preflight_error = _preflight(normalized)
    if preflight_error is not None:
        return TextTransactionResult("stale", False, (), preflight_error)

    committed: list[ExistingTextFileChange] = []
    attempted: ExistingTextFileChange | None = None
    try:
        for change in normalized:
            if change.before_bytes == change.after_bytes:
                continue
            if _read_exact(change.path) != change.before_bytes:
                return _compensate(normalized, committed, None, "stale_during_commit")
            attempted = change
            _write_bytes(change.path, change.after_bytes)
            if _read_exact(change.path) != change.after_bytes:
                return _compensate(normalized, committed, attempted, "write_verification_failed")
            committed.append(change)
            attempted = None
    except (OSError, RuntimeError):
        return _compensate(normalized, committed, attempted, "write_failed")

    return TextTransactionResult(
        "committed",
        bool(committed),
        tuple(change.path for change in committed),
    )


def _validate_changes(
    changes: Sequence[ExistingTextFileChange],
) -> tuple[tuple[ExistingTextFileChange, ...], str | None]:
    normalized = tuple(changes)
    if not normalized:
        return (), "empty_transaction"
    canonical_paths: set[Path] = set()
    for change in normalized:
        try:
            canonical = change.path.resolve(strict=True)
        except (OSError, RuntimeError):
            return normalized, "target_unavailable"
        if canonical != change.path or canonical in canonical_paths:
            return normalized, "duplicate_or_noncanonical_target"
        canonical_paths.add(canonical)
        if _sha256(change.before_bytes) != change.before_sha256:
            return normalized, "before_identity_invalid"
        if _sha256(change.after_bytes) != change.after_sha256:
            return normalized, "after_identity_invalid"
    return normalized, None


def _preflight(changes: tuple[ExistingTextFileChange, ...]) -> str | None:
    for change in changes:
        try:
            current = _read_exact(change.path)
        except (OSError, RuntimeError):
            return "target_unavailable"
        if current != change.before_bytes or _sha256(current) != change.before_sha256:
            return "stale_before_image"
    return None


def _compensate(
    all_changes: tuple[ExistingTextFileChange, ...],
    committed: list[ExistingTextFileChange],
    attempted: ExistingTextFileChange | None,
    error_kind: str,
) -> TextTransactionResult:
    compensation_failed = False
    rollback_order = ([attempted] if attempted is not None else []) + list(reversed(committed))
    for change in rollback_order:
        try:
            current = _read_exact(change.path)
            if current == change.before_bytes:
                continue
            if current != change.after_bytes:
                compensation_failed = True
                continue
            _write_bytes(change.path, change.before_bytes)
            if _read_exact(change.path) != change.before_bytes:
                compensation_failed = True
        except (OSError, RuntimeError):
            compensation_failed = True

    changed_paths = _changed_paths(all_changes)
    if compensation_failed or changed_paths:
        return TextTransactionResult("rollback_failed", True, changed_paths, error_kind)
    return TextTransactionResult("rolled_back", False, (), error_kind)


def _changed_paths(changes: tuple[ExistingTextFileChange, ...]) -> tuple[Path, ...]:
    changed: list[Path] = []
    for change in changes:
        try:
            current = _read_exact(change.path)
        except (OSError, RuntimeError):
            changed.append(change.path)
            continue
        if current != change.before_bytes:
            changed.append(change.path)
    return tuple(changed)


def _read_exact(path: Path) -> bytes:
    if not path.is_file():
        raise OSError("transaction target is not a regular file")
    return path.read_bytes()


def _write_bytes(path: Path, content: bytes) -> None:
    path.write_bytes(content)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()

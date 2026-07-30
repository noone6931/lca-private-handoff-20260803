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
    def create(
        cls,
        path: Path,
        before_bytes: bytes,
        after_bytes: bytes,
    ) -> "ExistingTextFileChange":
        return cls(
            path=path,
            before_bytes=before_bytes,
            after_bytes=after_bytes,
            before_sha256=sha256(before_bytes),
            after_sha256=sha256(after_bytes),
        )


@dataclass(frozen=True)
class RootedTextFileChange:
    relative_parts: tuple[str, ...]
    before_bytes: bytes | None
    after_bytes: bytes | None
    before_mode: int | None
    after_mode: int | None
    before_sha256: str | None
    after_sha256: str | None

    @classmethod
    def create(
        cls,
        relative_parts: tuple[str, ...],
        before_bytes: bytes | None,
        after_bytes: bytes | None,
        *,
        before_mode: int | None,
        after_mode: int | None,
    ) -> "RootedTextFileChange":
        return cls(
            relative_parts=relative_parts,
            before_bytes=before_bytes,
            after_bytes=after_bytes,
            before_mode=before_mode,
            after_mode=after_mode,
            before_sha256=optional_sha256(before_bytes),
            after_sha256=optional_sha256(after_bytes),
        )

    def inverse(self) -> "RootedTextFileChange":
        return RootedTextFileChange(
            relative_parts=self.relative_parts,
            before_bytes=self.after_bytes,
            after_bytes=self.before_bytes,
            before_mode=self.after_mode,
            after_mode=self.before_mode,
            before_sha256=self.after_sha256,
            after_sha256=self.before_sha256,
        )


@dataclass(frozen=True)
class TextTransactionResult:
    status: TransactionStatus
    workspace_changed: bool
    changed_paths: tuple[Path, ...]
    error_kind: str | None = None


def validate_existing_changes(
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
        if sha256(change.before_bytes) != change.before_sha256:
            return normalized, "before_identity_invalid"
        if sha256(change.after_bytes) != change.after_sha256:
            return normalized, "after_identity_invalid"
    return normalized, None


def validate_rooted_changes(
    changes: Sequence[RootedTextFileChange],
) -> tuple[tuple[RootedTextFileChange, ...], str | None]:
    normalized = tuple(changes)
    if not normalized:
        return (), "empty_transaction"
    seen: set[tuple[str, ...]] = set()
    for change in normalized:
        if not change.relative_parts or change.relative_parts in seen:
            return normalized, "duplicate_or_invalid_target"
        seen.add(change.relative_parts)
        if any(
            not part or part in {".", ".."} or "/" in part or "\0" in part
            for part in change.relative_parts
        ):
            return normalized, "duplicate_or_invalid_target"
        if (
            optional_sha256(change.before_bytes) != change.before_sha256
            or optional_sha256(change.after_bytes) != change.after_sha256
        ):
            return normalized, "content_identity_invalid"
        if change.before_bytes is None and change.after_bytes is None:
            return normalized, "empty_change"
        if change.before_bytes == change.after_bytes:
            return normalized, "empty_change"
        for content, mode in (
            (change.before_bytes, change.before_mode),
            (change.after_bytes, change.after_mode),
        ):
            if (content is None) != (mode is None):
                return normalized, "state_identity_invalid"
            if mode is not None and not 0 <= mode <= 0o777:
                return normalized, "state_identity_invalid"
            try:
                if content is not None:
                    content.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                return normalized, "content_not_utf8"
        if (
            change.before_bytes is not None
            and change.after_bytes is not None
            and change.before_mode != change.after_mode
        ):
            return normalized, "mode_change_not_supported"
    return normalized, None


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def optional_sha256(content: bytes | None) -> str | None:
    return sha256(content) if content is not None else None


__all__ = [
    "ExistingTextFileChange",
    "RootedTextFileChange",
    "TextTransactionResult",
    "TransactionStatus",
    "optional_sha256",
    "sha256",
    "validate_existing_changes",
    "validate_rooted_changes",
]

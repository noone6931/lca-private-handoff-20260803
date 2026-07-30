from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ..platform.rooted_files import RootedFileError
from ..platform.rooted_files import mutate_rooted_regular
from ..platform.rooted_files import read_rooted_regular
from .transaction_contracts import ExistingTextFileChange
from .transaction_contracts import RootedTextFileChange
from .transaction_contracts import TextTransactionResult
from .transaction_contracts import optional_sha256 as _optional_sha256
from .transaction_contracts import sha256 as _sha256
from .transaction_contracts import validate_existing_changes as _validate_changes
from .transaction_contracts import validate_rooted_changes as _validate_rooted_changes


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
    except BaseException as exc:
        setattr(exc, "text_transaction_result", _compensate(normalized, committed, attempted, "parent_interrupted"))
        raise

    return TextTransactionResult(
        "committed",
        bool(committed),
        tuple(change.path for change in committed),
    )
def apply_rooted_text_transaction(
    root: Path,
    changes: Sequence[RootedTextFileChange],
    *,
    expected_root_identity: tuple[int, int],
) -> TextTransactionResult:
    """Apply one rooted text batch with exact preflight and compensation."""

    normalized, error_kind = _validate_rooted_changes(changes)
    if error_kind is not None:
        return TextTransactionResult("stale", False, (), error_kind)
    preflight_error = _preflight_rooted(
        root,
        normalized,
        expected_root_identity,
    )
    if preflight_error is not None:
        return TextTransactionResult("stale", False, (), preflight_error)

    committed: list[RootedTextFileChange] = []
    attempted: RootedTextFileChange | None = None
    try:
        for change in normalized:
            attempted = change
            mutate_rooted_regular(
                root,
                change.relative_parts,
                before_content=change.before_bytes,
                after_content=change.after_bytes,
                before_mode=change.before_mode,
                after_mode=change.after_mode,
                expected_root_identity=expected_root_identity,
            )
            committed.append(change)
            attempted = None
    except RootedFileError as exc:
        return _compensate_rooted(
            root,
            normalized,
            committed,
            attempted if exc.workspace_changed else None,
            expected_root_identity,
            exc.kind,
        )
    except (OSError, RuntimeError, ValueError):
        return _compensate_rooted(
            root,
            normalized,
            committed,
            attempted,
            expected_root_identity,
            "write_failed",
        )
    except BaseException as exc:
        setattr(exc, "text_transaction_result", _compensate_rooted(root, normalized, committed, attempted, expected_root_identity, "parent_interrupted"))
        raise
    return TextTransactionResult(
        "committed",
        bool(committed),
        tuple(_rooted_path(root, change) for change in committed),
    )
def restore_existing_text_transaction(
    changes: Sequence[ExistingTextFileChange],
    *,
    error_kind: str,
) -> TextTransactionResult:
    """Restore an original transaction, then measure net state against its before images."""

    original = tuple(changes)
    inverse = tuple(
        ExistingTextFileChange.create(change.path, change.after_bytes, change.before_bytes)
        for change in original
    )
    apply_existing_text_transaction(inverse)
    residual = _changed_paths(original)
    if residual:
        return TextTransactionResult("rollback_failed", True, residual, error_kind)
    return TextTransactionResult("rolled_back", False, (), error_kind)
def restore_rooted_text_transaction(
    root: Path,
    changes: Sequence[RootedTextFileChange],
    *,
    expected_root_identity: tuple[int, int],
    error_kind: str,
) -> TextTransactionResult:
    """Restore a rooted transaction, then measure exact residual state."""

    original = tuple(changes)
    apply_rooted_text_transaction(
        root,
        tuple(change.inverse() for change in original),
        expected_root_identity=expected_root_identity,
    )
    residual = _rooted_changed_paths(root, original, expected_root_identity)
    if residual:
        return TextTransactionResult("rollback_failed", True, residual, error_kind)
    return TextTransactionResult("rolled_back", False, (), error_kind)
def rooted_after_mismatch_paths(
    root: Path,
    changes: Sequence[RootedTextFileChange],
    *,
    expected_root_identity: tuple[int, int],
) -> tuple[Path, ...]:
    """Measure paths that do not match a rooted transaction's after images."""

    normalized, error_kind = _validate_rooted_changes(changes)
    if error_kind is not None:
        return tuple(_rooted_path(root, change) for change in normalized)
    return _rooted_changed_paths(
        root,
        tuple(change.inverse() for change in normalized),
        expected_root_identity,
    )


def _preflight(changes: tuple[ExistingTextFileChange, ...]) -> str | None:
    for change in changes:
        try:
            current = _read_exact(change.path)
        except (OSError, RuntimeError):
            return "target_unavailable"
        if current != change.before_bytes or _sha256(current) != change.before_sha256:
            return "stale_before_image"
    return None


def _preflight_rooted(
    root: Path,
    changes: tuple[RootedTextFileChange, ...],
    expected_root_identity: tuple[int, int],
) -> str | None:
    for change in changes:
        try:
            if not _rooted_side_matches(
                root,
                change,
                before=True,
                expected_root_identity=expected_root_identity,
            ):
                return "stale_before_image"
        except RootedFileError as exc:
            return exc.kind
    return None


def _compensate(
    all_changes: tuple[ExistingTextFileChange, ...],
    committed: list[ExistingTextFileChange],
    attempted: ExistingTextFileChange | None,
    error_kind: str,
) -> TextTransactionResult:
    rollback_order = ([attempted] if attempted is not None else []) + list(reversed(committed))
    for change in rollback_order:
        try:
            current = _read_exact(change.path)
            if current == change.before_bytes:
                continue
            if current != change.after_bytes:
                continue
            _write_bytes(change.path, change.before_bytes)
            _read_exact(change.path)
        except (OSError, RuntimeError):
            pass

    changed_paths = _changed_paths(all_changes)
    if changed_paths:
        return TextTransactionResult("rollback_failed", True, changed_paths, error_kind)
    return TextTransactionResult("rolled_back", False, (), error_kind)


def _compensate_rooted(
    root: Path,
    all_changes: tuple[RootedTextFileChange, ...],
    committed: list[RootedTextFileChange],
    attempted: RootedTextFileChange | None,
    expected_root_identity: tuple[int, int],
    error_kind: str,
) -> TextTransactionResult:
    rollback_order = ([attempted] if attempted is not None else []) + list(
        reversed(committed)
    )
    for change in rollback_order:
        try:
            if _rooted_side_matches(
                root,
                change,
                before=True,
                expected_root_identity=expected_root_identity,
            ):
                continue
            if not _rooted_side_matches(
                root,
                change,
                before=False,
                expected_root_identity=expected_root_identity,
            ):
                continue
            inverse = change.inverse()
            mutate_rooted_regular(
                root,
                inverse.relative_parts,
                before_content=inverse.before_bytes,
                after_content=inverse.after_bytes,
                before_mode=inverse.before_mode,
                after_mode=inverse.after_mode,
                expected_root_identity=expected_root_identity,
            )
        except (RootedFileError, OSError, RuntimeError, ValueError):
            pass
    residual = _rooted_changed_paths(root, all_changes, expected_root_identity)
    if residual:
        return TextTransactionResult("rollback_failed", True, residual, error_kind)
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


def _rooted_changed_paths(
    root: Path,
    changes: tuple[RootedTextFileChange, ...],
    expected_root_identity: tuple[int, int],
) -> tuple[Path, ...]:
    changed: list[Path] = []
    for change in changes:
        try:
            matches = _rooted_side_matches(
                root,
                change,
                before=True,
                expected_root_identity=expected_root_identity,
            )
        except (RootedFileError, OSError, RuntimeError, ValueError):
            matches = False
        if not matches:
            changed.append(_rooted_path(root, change))
    return tuple(changed)


def _rooted_side_matches(
    root: Path,
    change: RootedTextFileChange,
    *,
    before: bool,
    expected_root_identity: tuple[int, int],
) -> bool:
    observed = read_rooted_regular(
        root,
        change.relative_parts,
        expected_root_identity=expected_root_identity,
    )
    content = change.before_bytes if before else change.after_bytes
    mode = change.before_mode if before else change.after_mode
    digest = change.before_sha256 if before else change.after_sha256
    return (
        observed.content == content
        and observed.mode == mode
        and _optional_sha256(observed.content) == digest
    )


def _rooted_path(root: Path, change: RootedTextFileChange) -> Path:
    return root.joinpath(*change.relative_parts)


def _read_exact(path: Path) -> bytes:
    if not path.is_file():
        raise OSError("transaction target is not a regular file")
    return path.read_bytes()


def _write_bytes(path: Path, content: bytes) -> None:
    path.write_bytes(content)

from __future__ import annotations

from pathlib import Path

from ..patch.transaction import RootedTextFileChange
from ..patch.transaction import TextTransactionResult
from ..patch.transaction import restore_rooted_text_transaction
from ..patch.transaction import rooted_after_mismatch_paths
from ..workspace.snapshot import WorkspaceSnapshotError
from ..workspace.snapshot import capture_workspace_snapshot
from ..workspace.snapshot_delta import WorkspaceTextMutationPlan
from ..workspace.snapshot_delta import snapshots_match
from .workspace_mutation_contracts import WorkspaceMutationCommitResult


def final_snapshot_error(
    expected,
    *,
    root: Path,
    expected_root_identity: tuple[int, int],
) -> str | None:
    try:
        observed = capture_workspace_snapshot(
            root,
            roots_revision=expected.roots_revision,
            expected_root_identity=expected_root_identity,
        )
    except WorkspaceSnapshotError as exc:
        return f"workspace_final_{exc.kind}"
    return None if snapshots_match(expected, observed) else "workspace_final_mismatch"


def commit_transaction_failure(
    transaction: TextTransactionResult,
    paths: tuple[str, ...],
    plan: WorkspaceTextMutationPlan,
) -> WorkspaceMutationCommitResult:
    if transaction.status == "stale":
        return _failure("stale", paths, transaction.error_kind or "stale", plan)
    if transaction.status == "rolled_back":
        return _failure(
            "restored",
            paths,
            transaction.error_kind or "commit_failed",
            plan,
        )
    changed = _relative_paths(transaction.changed_paths, plan.before.root)
    return WorkspaceMutationCommitResult(
        "indeterminate",
        True,
        paths,
        changed,
        error_kind=transaction.error_kind or "rollback_failed",
        before_manifest_sha256=plan.before.manifest_sha256,
        after_manifest_sha256=plan.after.manifest_sha256,
    )


def commit_recovery_result(
    recovery: TextTransactionResult,
    paths: tuple[str, ...],
    plan: WorkspaceTextMutationPlan,
    expected_root_identity: tuple[int, int],
) -> WorkspaceMutationCommitResult:
    restored = final_snapshot_error(
        plan.before,
        root=plan.before.root,
        expected_root_identity=expected_root_identity,
    ) is None
    if restored:
        return _failure(
            "restored",
            paths,
            recovery.error_kind or "commit_failed",
            plan,
        )
    return WorkspaceMutationCommitResult(
        "indeterminate",
        True,
        paths,
        _relative_paths(recovery.changed_paths, plan.before.root),
        error_kind=recovery.error_kind or "rollback_failed",
        before_manifest_sha256=plan.before.manifest_sha256,
        after_manifest_sha256=plan.after.manifest_sha256,
    )


def attach_commit_transaction_error(
    error: BaseException,
    paths: tuple[str, ...],
    plan: WorkspaceTextMutationPlan,
) -> None:
    transaction = getattr(error, "text_transaction_result", None)
    if isinstance(transaction, TextTransactionResult):
        setattr(error, "workspace_mutation_result", commit_transaction_failure(transaction, paths, plan))


def attach_commit_recovery(
    error: BaseException,
    *,
    root: Path,
    changes: tuple[RootedTextFileChange, ...],
    expected_root_identity: tuple[int, int],
    paths: tuple[str, ...],
    plan: WorkspaceTextMutationPlan,
) -> None:
    recovery = _restore_without_replacing_parent(
        root,
        changes,
        expected_root_identity=expected_root_identity,
    )
    residual = rooted_after_mismatch_paths(
        root,
        tuple(change.inverse() for change in changes),
        expected_root_identity=expected_root_identity,
    )
    restored = not residual and final_snapshot_error(
        plan.before,
        root=root,
        expected_root_identity=expected_root_identity,
    ) is None
    if restored:
        result = _failure("restored", paths, "parent_interrupted", plan)
    else:
        result = WorkspaceMutationCommitResult(
            "indeterminate",
            True,
            paths,
            _relative_paths(residual or recovery.changed_paths, root),
            error_kind="parent_interrupted",
            before_manifest_sha256=plan.before.manifest_sha256,
            after_manifest_sha256=plan.after.manifest_sha256,
        )
    setattr(error, "workspace_mutation_result", result)


def rollback_transaction_result(
    transaction: TextTransactionResult,
    *,
    root: Path,
    transaction_paths: tuple[str, ...],
    error_kind: str | None,
) -> WorkspaceMutationCommitResult:
    if transaction.status == "stale":
        state = "stale"
    elif transaction.status == "rolled_back":
        state = "restored"
    else:
        state = "indeterminate"
    changed = _relative_paths(transaction.changed_paths, root)
    return WorkspaceMutationCommitResult(
        state,
        bool(changed),
        transaction_paths,
        changed,
        error_kind=error_kind,
    )


def attach_rollback_transaction_error(
    error: BaseException,
    *,
    root: Path,
    transaction_paths: tuple[str, ...],
) -> None:
    transaction = getattr(error, "text_transaction_result", None)
    if isinstance(transaction, TextTransactionResult):
        result = rollback_transaction_result(
            transaction,
            root=root,
            transaction_paths=transaction_paths,
            error_kind=transaction.error_kind,
        )
        setattr(error, "workspace_mutation_result", result)


def attach_rollback_recovery(
    error: BaseException,
    *,
    root: Path,
    changes: tuple[RootedTextFileChange, ...],
    expected_root_identity: tuple[int, int],
    transaction_paths: tuple[str, ...],
) -> None:
    recovery = _restore_without_replacing_parent(
        root,
        changes,
        expected_root_identity=expected_root_identity,
    )
    result = rollback_transaction_result(
        recovery,
        root=root,
        transaction_paths=transaction_paths,
        error_kind="parent_interrupted",
    )
    setattr(error, "workspace_mutation_result", result)


def _restore_without_replacing_parent(
    root: Path,
    changes: tuple[RootedTextFileChange, ...],
    *,
    expected_root_identity: tuple[int, int],
) -> TextTransactionResult:
    try:
        return restore_rooted_text_transaction(
            root,
            changes,
            expected_root_identity=expected_root_identity,
            error_kind="parent_interrupted",
        )
    except BaseException as recovery_error:
        transaction = getattr(recovery_error, "text_transaction_result", None)
        if isinstance(transaction, TextTransactionResult):
            return transaction
        return TextTransactionResult(
            "rollback_failed",
            True,
            tuple(root.joinpath(*change.relative_parts) for change in changes),
            "parent_interrupted",
        )


def _failure(
    state,
    paths: tuple[str, ...],
    error_kind: str,
    plan: WorkspaceTextMutationPlan,
) -> WorkspaceMutationCommitResult:
    return WorkspaceMutationCommitResult(
        state,
        False,
        paths,
        (),
        error_kind=error_kind,
        before_manifest_sha256=plan.before.manifest_sha256,
        after_manifest_sha256=plan.after.manifest_sha256,
    )


def _relative_paths(paths: tuple[Path, ...], root: Path) -> tuple[str, ...]:
    return tuple(
        path.relative_to(root).as_posix()
        for path in paths
        if path.is_relative_to(root)
    )


__all__ = [
    "attach_commit_recovery",
    "attach_commit_transaction_error",
    "attach_rollback_recovery",
    "attach_rollback_transaction_error",
    "commit_recovery_result",
    "commit_transaction_failure",
    "final_snapshot_error",
    "rollback_transaction_result",
]

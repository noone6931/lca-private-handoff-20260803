from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ..patch.journal import append_patch_record
from ..patch.journal import patch_journal_path
from ..patch.transaction import RootedTextFileChange
from ..patch.transaction import TextTransactionResult
from ..patch.transaction import apply_rooted_text_transaction
from ..patch.transaction import restore_rooted_text_transaction
from ..patch.transaction import rooted_after_mismatch_paths
from ..workspace.snapshot import WorkspaceSnapshotError
from ..workspace.snapshot import capture_workspace_snapshot
from ..workspace.snapshot_delta import WorkspaceTextMutationPlan
from ..workspace.snapshot_delta import snapshots_match
from .base import ToolContext
from .base import ToolResult
from .base import tool_state_dir
from .workspace_mutation_contracts import ContainerMutationProvenance
from .workspace_mutation_contracts import WorkspaceMutationCommitResult
from .workspace_mutation_journal import container_workspace_journal_record
from .workspace_mutation_journal import workspace_text_change_diff
from .workspace_mutation_record import ParsedContainerWorkspaceRollback
from .workspace_mutation_record import parse_container_workspace_rollback
from .workspace_mutation_recovery import attach_commit_recovery
from .workspace_mutation_recovery import attach_commit_transaction_error
from .workspace_mutation_recovery import attach_rollback_recovery
from .workspace_mutation_recovery import attach_rollback_transaction_error
from .workspace_mutation_recovery import commit_recovery_result
from .workspace_mutation_recovery import commit_transaction_failure
from .workspace_mutation_recovery import final_snapshot_error as _final_snapshot_error
from .workspace_mutation_recovery import rollback_transaction_result

def commit_container_workspace_output(
    *,
    context: ToolContext,
    plan: WorkspaceTextMutationPlan,
    provenance: ContainerMutationProvenance,
) -> WorkspaceMutationCommitResult:
    """Commit one staged-copy output through the patch transaction and journal."""

    validation_error, root_identity = _validate_plan_authority(context, plan)
    paths = tuple(change.relative_path for change in plan.changes)
    if validation_error is not None or root_identity is None:
        return _failure(
            "stale",
            paths,
            validation_error or "workspace_identity_missing",
            plan,
        )
    try:
        current = capture_workspace_snapshot(
            plan.before.root,
            roots_revision=context.workspace_revision,
            expected_root_identity=root_identity,
        )
    except WorkspaceSnapshotError as exc:
        return _failure("stale", paths, f"workspace_preflight_{exc.kind}", plan)
    if not snapshots_match(plan.before, current):
        return _failure("stale", paths, "workspace_preflight_stale", plan)

    changes = tuple(_transaction_change(change) for change in plan.changes)
    try:
        transaction = apply_rooted_text_transaction(
            plan.before.root,
            changes,
            expected_root_identity=root_identity,
        )
    except BaseException as exc:
        attach_commit_transaction_error(exc, paths, plan)
        raise
    if transaction.status != "committed":
        return commit_transaction_failure(transaction, paths, plan)
    try:
        final_error = _final_snapshot_error(
            plan.after,
            root=plan.before.root,
            expected_root_identity=root_identity,
        )
        if final_error is not None:
            recovery = restore_rooted_text_transaction(
                plan.before.root,
                changes,
                expected_root_identity=root_identity,
                error_kind=final_error,
            )
            return commit_recovery_result(recovery, paths, plan, root_identity)
        transaction_id = _transaction_id()
        try:
            append_patch_record(
                patch_journal_path(tool_state_dir(context), context.session_id),
                container_workspace_journal_record(
                    transaction_id=transaction_id,
                    context=context,
                    plan=plan,
                    provenance=provenance,
                    time=datetime.now(timezone.utc).isoformat(),
                ),
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            recovery = restore_rooted_text_transaction(
                plan.before.root,
                changes,
                expected_root_identity=root_identity,
                error_kind="patch_journal_failed",
            )
            return commit_recovery_result(recovery, paths, plan, root_identity)
        return WorkspaceMutationCommitResult(
            "committed",
            True,
            paths,
            paths,
            transaction_id=transaction_id,
            before_manifest_sha256=plan.before.manifest_sha256,
            after_manifest_sha256=plan.after.manifest_sha256,
        )
    except BaseException as exc:
        attach_commit_recovery(
            exc,
            root=plan.before.root,
            changes=changes,
            expected_root_identity=root_identity,
            paths=paths,
            plan=plan,
        )
        raise

def rollback_container_workspace_record(
    record: dict[str, object],
    context: ToolContext,
) -> ToolResult:
    """Roll back one staged-copy journal record through the same transaction owner."""

    identity = (
        (
            context.workspace_identity.device,
            context.workspace_identity.inode,
        )
        if context.workspace_identity is not None
        else None
    )
    parsed = parse_container_workspace_rollback(
        record,
        workspace_revision=context.workspace_revision,
        workspace_identity=identity,
    )
    if isinstance(parsed, str):
        return ToolResult(parsed, is_error=True)
    assert isinstance(parsed, ParsedContainerWorkspaceRollback)
    changes = parsed.changes
    paths = parsed.paths
    root_identity = parsed.root_identity
    try:
        transaction = apply_rooted_text_transaction(
            context.workspace,
            changes,
            expected_root_identity=root_identity,
        )
    except BaseException as exc:
        attach_rollback_transaction_error(
            exc,
            root=context.workspace,
            transaction_paths=paths,
        )
        raise
    if transaction.status != "committed":
        return _rollback_failure_result(
            transaction,
            context=context,
            changes=changes,
            transaction_paths=paths,
            error_kind=transaction.error_kind,
        )
    try:
        append_patch_record(
            patch_journal_path(tool_state_dir(context), context.session_id),
            {
                "event": "rollback",
                "patch_id": str(record["id"]),
                "time": datetime.now(timezone.utc).isoformat(),
                "source": "container_staged_copy",
            },
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        recovery = restore_rooted_text_transaction(
            context.workspace,
            changes,
            expected_root_identity=root_identity,
            error_kind="patch_journal_failed",
        )
        return _rollback_failure_result(
            recovery,
            context=context,
            changes=changes,
            transaction_paths=paths,
            error_kind="patch_journal_failed",
        )
    except BaseException as exc:
        attach_rollback_recovery(
            exc,
            root=context.workspace,
            changes=changes,
            expected_root_identity=root_identity,
            transaction_paths=paths,
        )
        raise
    diff = "".join(
        workspace_text_change_diff(
            path,
            change.before_bytes.decode("utf-8")
            if change.before_bytes is not None
            else None,
            change.after_bytes.decode("utf-8")
            if change.after_bytes is not None
            else None,
        )
        for path, change in zip(paths, changes, strict=True)
    )
    return ToolResult(
        f"Rolled back container workspace transaction {record['id']} across "
        f"{len(paths)} files.\n\n{diff}",
        metadata={
            "changed_paths": list(paths),
            "transaction_paths": list(paths),
            "effective_changed_paths": [],
            "workspace_changed": True,
            "transaction_status": "committed",
            "workspace_state": "committed",
            "rollback_of": str(record["id"]),
            "workspace_mutation_source": "container_staged_copy",
        },
    )
def _validate_plan_authority(
    context: ToolContext,
    plan: WorkspaceTextMutationPlan,
) -> tuple[str | None, tuple[int, int] | None]:
    identity = context.workspace_identity
    if identity is None:
        return "workspace_identity_missing", None
    expected = (identity.device, identity.inode)
    try:
        workspace = context.workspace.resolve(strict=True)
    except (OSError, RuntimeError):
        return "workspace_unavailable", expected
    if (
        workspace != context.workspace
        or plan.before.root != workspace
        or plan.before.root_identity != expected
        or plan.roots_revision != context.workspace_revision
    ):
        return "workspace_plan_authority_mismatch", expected
    return None, expected
def _rollback_failure_result(
    transaction: TextTransactionResult,
    *,
    context: ToolContext,
    changes: tuple[RootedTextFileChange, ...],
    transaction_paths: tuple[str, ...],
    error_kind: str | None,
) -> ToolResult:
    result = rollback_transaction_result(
        transaction,
        root=context.workspace,
        transaction_paths=transaction_paths,
        error_kind=error_kind,
    )
    if result.state == "stale":
        message = "Container workspace rollback refused because an exact file state is stale."
    elif result.state == "restored":
        message = "Container workspace rollback failed, but its attempted changes were restored."
    else:
        message = "Container workspace rollback failed and compensation left residual state."
    assert context.workspace_identity is not None
    effective = tuple(
        path.relative_to(context.workspace).as_posix()
        for path in rooted_after_mismatch_paths(
            context.workspace,
            changes,
            expected_root_identity=(
                context.workspace_identity.device,
                context.workspace_identity.inode,
            ),
        )
    )
    return ToolResult(
        message,
        is_error=True,
        metadata={
            **result.metadata(),
            "workspace_changed": transaction.workspace_changed,
            "effective_changed_paths": list(effective),
            "transaction_status": transaction.status,
        },
    )
def _transaction_change(change) -> RootedTextFileChange:
    return RootedTextFileChange(
        relative_parts=change.relative_parts,
        before_bytes=change.before_bytes,
        after_bytes=change.after_bytes,
        before_mode=change.before_mode,
        after_mode=change.after_mode,
        before_sha256=change.before_sha256,
        after_sha256=change.after_sha256,
    )
def _failure(
    state: Literal["stale", "restored"],
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


def _transaction_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


__all__ = [
    "ContainerMutationProvenance",
    "WorkspaceMutationCommitResult",
    "commit_container_workspace_output",
    "rollback_container_workspace_record",
]

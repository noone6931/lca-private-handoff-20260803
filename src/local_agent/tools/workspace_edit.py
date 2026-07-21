from __future__ import annotations

from pathlib import Path
from typing import Any

from local_agent.lsp import config as lsp_config
from local_agent.lsp.config import server_identity
from local_agent.lsp.workspace_edit_store import WorkspaceEditPlanScope
from local_agent.lsp.workspace_edit_store import WorkspaceEditPlanStoreError
from local_agent.lsp.workspace_edit_store import StoredWorkspaceEditPlan
from local_agent.lsp.workspace_edit_store import default_workspace_edit_plan_store
from local_agent.patch.anchored import PatchError, display_workspace_path, resolve_workspace_path
from local_agent.patch.transaction import ExistingTextFileChange, apply_existing_text_transaction
from local_agent.patch.transaction import restore_existing_text_transaction

from .base import Tool, ToolContext, ToolResult
from .files import record_workspace_edit_patch


def workspace_edit_tools() -> list[Tool]:
    return [
        Tool(
            name="apply_workspace_edit",
            description=(
                "Apply one exact, in-memory semantic rename plan returned by lsp_rename_preview. "
                "The opaque plan is bound to the current session, run, workspace, project, and LSP server; "
                "all files must still match the previewed before images."
            ),
            tier="write",
            input_schema={
                "type": "object",
                "properties": {
                    "plan_id": {
                        "type": "string",
                        "pattern": "^wep_[0-9a-f]{32}$",
                    }
                },
                "required": ["plan_id"],
                "additionalProperties": False,
            },
            handler=apply_workspace_edit,
        )
    ]


def apply_workspace_edit(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    plan_id = str(arguments["plan_id"])
    store = default_workspace_edit_plan_store()
    try:
        scope = WorkspaceEditPlanScope.create(
            session_id=context.session_id,
            run_id=context.run_id,
            workspace=context.workspace,
            allowed_roots=context.allowed_dirs,
        )
        stored = store.get(plan_id, scope=scope)
    except WorkspaceEditPlanStoreError as exc:
        return ToolResult(
            str(exc),
            is_error=True,
            metadata={
                "workspace_changed": False,
                "transaction_status": "stale",
                "workspace_state": "stale",
                "error_kind": exc.kind,
            },
        )

    authorization_error = _plan_authorization_error(stored, context)
    if authorization_error is not None:
        return ToolResult(
            authorization_error,
            is_error=True,
            metadata={
                "workspace_changed": False,
                "transaction_status": "stale",
                "workspace_state": "stale",
                "error_kind": "plan_identity_mismatch",
            },
        )
    changes = tuple(
        ExistingTextFileChange(
            path=file.path,
            before_bytes=file.before_bytes,
            after_bytes=file.after_bytes,
            before_sha256=file.before_sha256,
            after_sha256=file.after_sha256,
        )
        for file in stored.plan.files
    )
    transaction = apply_existing_text_transaction(changes)
    display_paths = _display_paths(transaction.changed_paths, context)
    all_paths = _display_paths(stored.plan.paths, context)
    base_metadata = {
        "plan_id": plan_id,
        "plan_digest": stored.digest,
        "provenance_digest": stored.provenance.digest,
        "server": stored.provenance.server.name,
        "server_fingerprint": stored.provenance.server.fingerprint,
        "project_root": display_workspace_path(
            context.workspace,
            stored.provenance.project_root,
            context.allowed_dirs,
        ),
        "source": stored.source,
        "file_count": len(stored.plan.files),
        "edit_count": stored.plan.edit_count,
        "changed_paths": display_paths,
        "effective_changed_paths": display_paths,
        "transaction_paths": all_paths,
        "workspace_changed": transaction.workspace_changed,
        "transaction_status": transaction.status,
        "error_kind": transaction.error_kind,
    }
    if transaction.status != "committed":
        if transaction.status == "stale":
            state = "stale"
            message = "WorkspaceEdit apply refused before writing because an exact file identity is stale."
        elif transaction.status == "rolled_back":
            state = "restored"
            message = "WorkspaceEdit apply failed, but every attempted write was restored."
        else:
            state = "indeterminate"
            message = "WorkspaceEdit apply failed and compensation did not restore every file."
            store.consume(plan_id, scope=scope)
        return ToolResult(
            message,
            is_error=True,
            metadata={**base_metadata, "workspace_state": state},
        )

    try:
        transaction_id = record_workspace_edit_patch(
            context=context,
            source=stored.source,
            plan_id=plan_id,
            plan_digest=stored.digest,
            provenance_digest=stored.provenance.digest,
            server_name=stored.provenance.server.name,
            server_fingerprint=stored.provenance.server.fingerprint,
            project_root=stored.provenance.project_root,
            files=stored.plan.files,
            diff=stored.plan.unified_diff,
        )
    except OSError:
        recovery = restore_existing_text_transaction(changes, error_kind="patch_journal_failed")
        residual_paths = _display_paths(recovery.changed_paths, context)
        if recovery.workspace_changed:
            store.consume(plan_id, scope=scope)
        return ToolResult(
            (
                "WorkspaceEdit journal failed, but every committed file was restored."
                if not recovery.workspace_changed
                else "WorkspaceEdit journal failed and inverse compensation did not restore every file."
            ),
            is_error=True,
            metadata={
                **base_metadata,
                "changed_paths": residual_paths,
                "effective_changed_paths": residual_paths,
                "workspace_changed": recovery.workspace_changed,
                "transaction_status": recovery.status,
                "workspace_state": "indeterminate" if recovery.workspace_changed else "restored",
                "error_kind": "patch_journal_failed",
            },
        )
    store.consume(plan_id, scope=scope)
    return ToolResult(
        f"Applied semantic rename transaction {transaction_id} across {len(all_paths)} files.\n\n"
        + stored.plan.unified_diff,
        metadata={
            **base_metadata,
            "transaction_id": transaction_id,
            "workspace_state": "committed",
        },
    )


def _plan_authorization_error(stored: StoredWorkspaceEditPlan, context: ToolContext) -> str | None:
    provenance = stored.provenance
    if not lsp_config.external_lsp_enabled():
        return "WorkspaceEdit plan LSP environment is no longer enabled."
    try:
        canonical_target = provenance.target_path.resolve(strict=True)
        if canonical_target != provenance.target_path or not canonical_target.is_file():
            return "WorkspaceEdit plan target identity changed after preview."
        authorization_root = _authorization_root(provenance.target_path, context)
        current_servers = lsp_config.servers_for_path(authorization_root, provenance.target_path)
    except (OSError, RuntimeError, ValueError):
        return "WorkspaceEdit plan server identity can no longer be resolved safely."
    selected = next(
        (server for server in current_servers if server_identity(server) == provenance.server),
        None,
    )
    if selected is None:
        return "WorkspaceEdit plan server identity changed after preview."
    try:
        current_root = lsp_config.root_for_path(authorization_root, provenance.target_path, selected)
        canonical_root = current_root.resolve(strict=True) if current_root is not None else None
    except (OSError, RuntimeError, ValueError):
        return "WorkspaceEdit plan project root can no longer be resolved safely."
    if canonical_root != provenance.project_root:
        return "WorkspaceEdit plan project root changed after preview."
    for path in stored.plan.paths:
        try:
            resolved = resolve_workspace_path(context.workspace, str(path), context.allowed_dirs)
            resolved.relative_to(provenance.project_root)
        except (PatchError, ValueError):
            return "WorkspaceEdit plan no longer belongs to the authorized workspace roots."
        if resolved != path:
            return "WorkspaceEdit plan target identity changed after preview."
    return None


def _authorization_root(path: Path, context: ToolContext) -> Path:
    matches: list[Path] = []
    for root in (context.workspace, *context.allowed_dirs):
        resolved = root.expanduser().resolve()
        try:
            path.relative_to(resolved)
        except ValueError:
            continue
        matches.append(resolved)
    if not matches:
        raise ValueError("target is outside authorized roots")
    return max(matches, key=lambda candidate: len(candidate.parts))


def _display_paths(paths: tuple[Path, ...], context: ToolContext) -> list[str]:
    return [display_workspace_path(context.workspace, path, context.allowed_dirs) for path in paths]

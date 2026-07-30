from __future__ import annotations

import difflib

from ..workspace.snapshot_delta import WorkspaceTextMutationPlan
from .base import ToolContext
from .workspace_mutation_contracts import ContainerMutationProvenance


def container_workspace_journal_record(
    *,
    transaction_id: str,
    context: ToolContext,
    plan: WorkspaceTextMutationPlan,
    provenance: ContainerMutationProvenance,
    time: str,
) -> dict[str, object]:
    entries = []
    diffs = []
    for change in plan.changes:
        before_text = (
            change.before_bytes.decode("utf-8")
            if change.before_bytes is not None
            else None
        )
        after_text = (
            change.after_bytes.decode("utf-8")
            if change.after_bytes is not None
            else None
        )
        diff = workspace_text_change_diff(
            change.relative_path,
            before_text,
            after_text,
        )
        diffs.append(diff)
        entries.append(
            {
                "path": change.relative_path,
                "operation": change.operation,
                "before_exists": before_text is not None,
                "after_exists": after_text is not None,
                "before_text": before_text,
                "after_text": after_text,
                "before_sha256": change.before_sha256,
                "after_sha256": change.after_sha256,
                "before_mode": change.before_mode,
                "after_mode": change.after_mode,
                "diff": diff,
            }
        )
    return {
        "event": "apply",
        "id": transaction_id,
        "transaction_id": transaction_id,
        "time": time,
        "source": "container_staged_copy",
        "session_id": context.session_id,
        "origin_run_id": context.run_id,
        "tool_call_id": context.tool_call_id,
        "attempt_id": provenance.attempt_id,
        "workspace_roots_revision": plan.roots_revision,
        "workspace_root_identity": list(plan.before.root_identity),
        "before_manifest_sha256": plan.before.manifest_sha256,
        "after_manifest_sha256": plan.after.manifest_sha256,
        "image_digest": provenance.image_digest,
        "profile": provenance.profile,
        "workspace_transport": provenance.workspace_transport,
        "files": entries,
        "diff": "".join(diffs),
    }


def workspace_text_change_diff(
    relative_path: str,
    before_text: str | None,
    after_text: str | None,
) -> str:
    return "".join(
        difflib.unified_diff(
            (before_text or "").splitlines(keepends=True),
            (after_text or "").splitlines(keepends=True),
            fromfile=(
                f"a/{relative_path}"
                if before_text is not None
                else "/dev/null"
            ),
            tofile=(
                f"b/{relative_path}"
                if after_text is not None
                else "/dev/null"
            ),
        )
    )


__all__ = [
    "container_workspace_journal_record",
    "workspace_text_change_diff",
]

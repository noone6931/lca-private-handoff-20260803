from __future__ import annotations

import hashlib
import json
import secrets
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import LspServerIdentity
from .workspace_edit import WorkspaceEditPlan


MAX_STORED_WORKSPACE_EDIT_PLANS = 8
MAX_STORED_WORKSPACE_EDIT_BYTES = 64 * 1024 * 1024
WorkspaceEditPlanSource = Literal["rename"]


class WorkspaceEditPlanStoreError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class WorkspaceEditPlanScope:
    session_id: str
    run_id: str
    workspace: Path
    allowed_roots: tuple[Path, ...]

    @classmethod
    def create(
        cls,
        *,
        session_id: str | None,
        run_id: str | None,
        workspace: Path,
        allowed_roots: tuple[Path, ...],
    ) -> "WorkspaceEditPlanScope":
        if not isinstance(session_id, str) or not session_id.strip() or not isinstance(run_id, str) or not run_id.strip():
            raise WorkspaceEditPlanStoreError(
                "plan_scope_missing",
                "WorkspaceEdit plans require an active session and run identity.",
            )
        try:
            canonical_workspace = workspace.expanduser().resolve()
            canonical_roots = tuple(root.expanduser().resolve() for root in allowed_roots)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspaceEditPlanStoreError(
                "plan_scope_invalid",
                "WorkspaceEdit plan scope cannot be resolved safely.",
            ) from exc
        return cls(
            session_id=session_id,
            run_id=run_id,
            workspace=canonical_workspace,
            allowed_roots=canonical_roots,
        )


@dataclass(frozen=True)
class WorkspaceEditPlanProvenance:
    target_path: Path
    project_root: Path
    server: LspServerIdentity
    digest: str

    @classmethod
    def create(
        cls,
        *,
        target_path: Path,
        project_root: Path,
        server: LspServerIdentity,
    ) -> "WorkspaceEditPlanProvenance":
        try:
            target = target_path.expanduser().resolve(strict=True)
            project = project_root.expanduser().resolve(strict=True)
            target.relative_to(project)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspaceEditPlanStoreError(
                "plan_provenance_invalid",
                "WorkspaceEdit plan provenance is not a canonical project target.",
            ) from exc
        digest = hashlib.sha256(
            f"workspace-edit-provenance-v1\n{target}\n{project}\n{server.fingerprint}\n".encode("utf-8")
        ).hexdigest()
        return cls(target_path=target, project_root=project, server=server, digest=digest)


@dataclass(frozen=True)
class StoredWorkspaceEditPlan:
    plan_id: str
    source: WorkspaceEditPlanSource
    scope: WorkspaceEditPlanScope
    provenance: WorkspaceEditPlanProvenance
    plan: WorkspaceEditPlan
    digest: str

    @property
    def stored_bytes(self) -> int:
        return _stored_plan_bytes(self)


class WorkspaceEditPlanStore:
    def __init__(
        self,
        *,
        max_plans: int = MAX_STORED_WORKSPACE_EDIT_PLANS,
        max_bytes: int = MAX_STORED_WORKSPACE_EDIT_BYTES,
    ) -> None:
        if max_plans < 1 or max_bytes < 1:
            raise ValueError("WorkspaceEdit plan store bounds must be positive.")
        self._max_plans = max_plans
        self._max_bytes = max_bytes
        self._plans: OrderedDict[str, StoredWorkspaceEditPlan] = OrderedDict()
        self._stored_bytes = 0

    def register(
        self,
        plan: WorkspaceEditPlan,
        *,
        source: WorkspaceEditPlanSource,
        scope: WorkspaceEditPlanScope,
        provenance: WorkspaceEditPlanProvenance,
    ) -> StoredWorkspaceEditPlan:
        plan_id = "wep_" + secrets.token_hex(16)
        digest = hashlib.sha256(
            f"workspace-edit-plan-v2\n{plan.digest}\n{provenance.digest}\n".encode("ascii")
        ).hexdigest()
        stored = StoredWorkspaceEditPlan(
            plan_id=plan_id,
            source=source,
            scope=scope,
            provenance=provenance,
            plan=plan,
            digest=digest,
        )
        size = stored.stored_bytes
        if size > self._max_bytes:
            raise WorkspaceEditPlanStoreError("plan_too_large", "WorkspaceEdit plan exceeds the in-memory store limit.")
        while self._plans and (len(self._plans) >= self._max_plans or self._stored_bytes + size > self._max_bytes):
            _plan_id, evicted = self._plans.popitem(last=False)
            self._stored_bytes -= evicted.stored_bytes
        self._plans[plan_id] = stored
        self._stored_bytes += size
        return stored

    def get(self, plan_id: str, *, scope: WorkspaceEditPlanScope) -> StoredWorkspaceEditPlan:
        stored = self._plans.get(plan_id)
        if stored is None:
            raise WorkspaceEditPlanStoreError("plan_missing", "WorkspaceEdit plan is missing, expired, or already consumed.")
        if stored.scope != scope:
            raise WorkspaceEditPlanStoreError("plan_scope_mismatch", "WorkspaceEdit plan does not belong to this run and workspace scope.")
        return stored

    def consume(self, plan_id: str, *, scope: WorkspaceEditPlanScope) -> StoredWorkspaceEditPlan:
        stored = self.get(plan_id, scope=scope)
        self._plans.pop(plan_id)
        self._stored_bytes -= stored.stored_bytes
        return stored

    def snapshot(self) -> dict[str, int]:
        return {"plans": len(self._plans), "stored_bytes": self._stored_bytes}

    def clear(self) -> None:
        self._plans.clear()
        self._stored_bytes = 0


_DEFAULT_PLAN_STORE = WorkspaceEditPlanStore()


def default_workspace_edit_plan_store() -> WorkspaceEditPlanStore:
    return _DEFAULT_PLAN_STORE


def _stored_plan_bytes(stored: StoredWorkspaceEditPlan) -> int:
    payload = {
        "plan_id": stored.plan_id,
        "source": stored.source,
        "digest": stored.digest,
        "scope": {
            "session_id": stored.scope.session_id,
            "run_id": stored.scope.run_id,
            "workspace": str(stored.scope.workspace),
            "allowed_roots": [str(root) for root in stored.scope.allowed_roots],
        },
        "provenance": {
            "target_path": str(stored.provenance.target_path),
            "project_root": str(stored.provenance.project_root),
            "digest": stored.provenance.digest,
            "server": {
                "name": stored.provenance.server.name,
                "command": list(stored.provenance.server.command),
                "file_types": list(stored.provenance.server.file_types),
                "root_markers": list(stored.provenance.server.root_markers),
                "language_id": stored.provenance.server.language_id,
                "process_environment": [list(item) for item in stored.provenance.server.process_environment],
                "fingerprint": stored.provenance.server.fingerprint,
            },
        },
        "plan": {
            "edit_count": stored.plan.edit_count,
            "unified_diff": stored.plan.unified_diff,
            "digest": stored.plan.digest,
            "files": [
                {
                    "path": str(file.path),
                    "before_sha256": file.before_sha256,
                    "after_sha256": file.after_sha256,
                    "edit_count": file.edit_count,
                    "unified_diff": file.unified_diff,
                }
                for file in stored.plan.files
            ],
        },
    }
    metadata_bytes = len(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    content_bytes = sum(len(file.before_bytes) + len(file.after_bytes) for file in stored.plan.files)
    return metadata_bytes + content_bytes

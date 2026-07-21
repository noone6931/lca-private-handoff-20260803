from __future__ import annotations

import secrets
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
    session_id: str | None
    run_id: str | None
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
        return cls(
            session_id=session_id,
            run_id=run_id,
            workspace=workspace.expanduser().resolve(),
            allowed_roots=tuple(root.expanduser().resolve() for root in allowed_roots),
        )


@dataclass(frozen=True)
class StoredWorkspaceEditPlan:
    plan_id: str
    source: WorkspaceEditPlanSource
    scope: WorkspaceEditPlanScope
    plan: WorkspaceEditPlan


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
    ) -> StoredWorkspaceEditPlan:
        size = plan.stored_bytes
        if size > self._max_bytes:
            raise WorkspaceEditPlanStoreError("plan_too_large", "WorkspaceEdit plan exceeds the in-memory store limit.")
        while self._plans and (len(self._plans) >= self._max_plans or self._stored_bytes + size > self._max_bytes):
            _plan_id, evicted = self._plans.popitem(last=False)
            self._stored_bytes -= evicted.plan.stored_bytes
        plan_id = "wep_" + secrets.token_hex(16)
        stored = StoredWorkspaceEditPlan(plan_id=plan_id, source=source, scope=scope, plan=plan)
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
        self._stored_bytes -= stored.plan.stored_bytes
        return stored

    def snapshot(self) -> dict[str, int]:
        return {"plans": len(self._plans), "stored_bytes": self._stored_bytes}

    def clear(self) -> None:
        self._plans.clear()
        self._stored_bytes = 0


_DEFAULT_PLAN_STORE = WorkspaceEditPlanStore()


def default_workspace_edit_plan_store() -> WorkspaceEditPlanStore:
    return _DEFAULT_PLAN_STORE

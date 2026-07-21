from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_agent.lsp.workspace_edit import build_workspace_edit_preview
from local_agent.lsp.workspace_edit_store import WorkspaceEditPlanScope
from local_agent.lsp.workspace_edit_store import WorkspaceEditPlanStore
from local_agent.lsp.workspace_edit_store import WorkspaceEditPlanStoreError


def _edit(new_text: str) -> dict[str, object]:
    return {
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 3},
        },
        "newText": new_text,
    }


class WorkspaceEditPlanStoreTests(unittest.TestCase):
    def _plan(self, workspace: Path, target: Path, new_text: str = "new"):
        return build_workspace_edit_preview(
            {"changes": {target.as_uri(): [_edit(new_text)]}},
            workspace=workspace,
            allowed_roots=(),
            project_root=workspace,
        )

    def _scope(
        self,
        workspace: Path,
        *,
        session_id: str = "session",
        run_id: str = "run",
        allowed_roots: tuple[Path, ...] = (),
    ) -> WorkspaceEditPlanScope:
        return WorkspaceEditPlanScope.create(
            session_id=session_id,
            run_id=run_id,
            workspace=workspace,
            allowed_roots=allowed_roots,
        )

    def test_plan_contains_exact_before_after_identity_and_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_bytes(b"old\r\n")
            plan = self._plan(workspace, target)

            self.assertEqual(plan.files[0].before_bytes, b"old\r\n")
            self.assertEqual(plan.files[0].after_bytes, b"new\r\n")
            self.assertEqual(len(plan.files[0].before_sha256), 64)
            self.assertEqual(len(plan.files[0].after_sha256), 64)
            self.assertEqual(len(plan.digest), 64)
            self.assertEqual(target.read_bytes(), b"old\r\n")

    def test_scope_binding_and_one_shot_consume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old\n", encoding="utf-8")
            store = WorkspaceEditPlanStore()
            scope = self._scope(workspace)
            stored = store.register(self._plan(workspace, target), source="rename", scope=scope)

            self.assertEqual(store.get(stored.plan_id, scope=scope), stored)
            for mismatch in (
                self._scope(workspace, session_id="other"),
                self._scope(workspace, run_id="other"),
                self._scope(workspace, allowed_roots=(workspace,)),
            ):
                with self.subTest(mismatch=mismatch):
                    with self.assertRaisesRegex(WorkspaceEditPlanStoreError, "does not belong"):
                        store.get(stored.plan_id, scope=mismatch)
            other_workspace = workspace / "other"
            other_workspace.mkdir()
            with self.assertRaisesRegex(WorkspaceEditPlanStoreError, "does not belong"):
                store.get(stored.plan_id, scope=self._scope(other_workspace))
            self.assertEqual(store.consume(stored.plan_id, scope=scope), stored)
            with self.assertRaisesRegex(WorkspaceEditPlanStoreError, "missing"):
                store.get(stored.plan_id, scope=scope)

    def test_capacity_and_byte_bounds_evict_oldest_without_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            first = workspace / "first.py"
            second = workspace / "second.py"
            first.write_text("old\n", encoding="utf-8")
            second.write_text("old\n", encoding="utf-8")
            scope = self._scope(workspace)
            first_plan = self._plan(workspace, first)
            second_plan = self._plan(workspace, second)
            store = WorkspaceEditPlanStore(max_plans=1, max_bytes=first_plan.stored_bytes + second_plan.stored_bytes)
            first_stored = store.register(first_plan, source="rename", scope=scope)
            second_stored = store.register(second_plan, source="rename", scope=scope)

            with self.assertRaises(WorkspaceEditPlanStoreError):
                store.get(first_stored.plan_id, scope=scope)
            self.assertEqual(store.get(second_stored.plan_id, scope=scope), second_stored)
            self.assertEqual(store.snapshot()["plans"], 1)
            self.assertFalse((workspace / ".local-agent").exists())

            too_small = WorkspaceEditPlanStore(max_plans=1, max_bytes=1)
            with self.assertRaisesRegex(WorkspaceEditPlanStoreError, "store limit"):
                too_small.register(first_plan, source="rename", scope=scope)


if __name__ == "__main__":
    unittest.main()

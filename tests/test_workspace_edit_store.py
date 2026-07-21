from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from local_agent.lsp.config import LspProcessEnvironment
from local_agent.lsp.config import LspServerConfig
from local_agent.lsp.config import server_identity
from local_agent.lsp.workspace_edit import build_workspace_edit_preview
from local_agent.lsp.workspace_edit_store import WorkspaceEditPlanProvenance
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
    def _server(self) -> LspServerConfig:
        return LspServerConfig(
            name="test-lsp",
            command=("/usr/bin/test-lsp", "--stdio"),
            file_types=(".py",),
            root_markers=("project.marker",),
            language_id="python",
            process_environment=LspProcessEnvironment(append=(("TOOLCHAIN", "one"),)),
        )

    def _provenance(self, workspace: Path, target: Path) -> WorkspaceEditPlanProvenance:
        return WorkspaceEditPlanProvenance.create(
            target_path=target,
            project_root=workspace,
            server=server_identity(self._server()),
        )

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
            stored = store.register(
                self._plan(workspace, target),
                source="rename",
                scope=scope,
                provenance=self._provenance(workspace, target),
            )

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

    def test_scope_requires_nonempty_session_and_run_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            for session_id, run_id in (
                (None, "run"),
                ("session", None),
                ("", "run"),
                ("session", ""),
                (" ", "run"),
                ("session", " "),
            ):
                with self.subTest(session_id=session_id, run_id=run_id):
                    with self.assertRaisesRegex(WorkspaceEditPlanStoreError, "active session and run"):
                        WorkspaceEditPlanScope.create(
                            session_id=session_id,
                            run_id=run_id,
                            workspace=workspace,
                            allowed_roots=(),
                        )

    def test_scope_canonicalization_failure_is_typed(self) -> None:
        unavailable = Mock()
        unavailable.expanduser.return_value.resolve.side_effect = OSError("controlled failure")

        with self.assertRaises(WorkspaceEditPlanStoreError) as caught:
            WorkspaceEditPlanScope.create(
                session_id="session",
                run_id="run",
                workspace=unavailable,
                allowed_roots=(),
            )

        self.assertEqual(caught.exception.kind, "plan_scope_invalid")
        self.assertNotIn("controlled failure", str(caught.exception))

    def test_server_identity_fingerprint_covers_all_launch_and_root_fields(self) -> None:
        base = self._server()
        identity = server_identity(base)
        variants = (
            replace(base, name="other"),
            replace(base, command=("/other/server",)),
            replace(base, file_types=(".py", ".pyi")),
            replace(base, root_markers=("other.marker",)),
            replace(base, language_id="other-language"),
            replace(base, process_environment=LspProcessEnvironment(append=(("TOOLCHAIN", "two"),))),
        )
        self.assertEqual(identity, server_identity(base))
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(identity.fingerprint, server_identity(variant).fingerprint)

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
            store = WorkspaceEditPlanStore(max_plans=1)
            first_stored = store.register(
                first_plan,
                source="rename",
                scope=scope,
                provenance=self._provenance(workspace, first),
            )
            second_stored = store.register(
                second_plan,
                source="rename",
                scope=scope,
                provenance=self._provenance(workspace, second),
            )

            with self.assertRaises(WorkspaceEditPlanStoreError):
                store.get(first_stored.plan_id, scope=scope)
            self.assertEqual(store.get(second_stored.plan_id, scope=scope), second_stored)
            self.assertEqual(store.snapshot()["plans"], 1)
            self.assertFalse((workspace / ".local-agent").exists())

            too_small = WorkspaceEditPlanStore(max_plans=1, max_bytes=1)
            with self.assertRaisesRegex(WorkspaceEditPlanStoreError, "store limit"):
                too_small.register(
                    first_plan,
                    source="rename",
                    scope=scope,
                    provenance=self._provenance(workspace, first),
                )

    def test_byte_bound_counts_scope_and_complete_server_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "main.py"
            target.write_text("old\n", encoding="utf-8")
            long_value = "x" * 4096
            server = LspServerConfig(
                name="test-lsp",
                command=(f"/tool/{long_value}", "--stdio"),
                file_types=(".py",),
                root_markers=("project.marker",),
                language_id="python",
                process_environment=LspProcessEnvironment(append=(("TOOLCHAIN", long_value),)),
            )
            scope = self._scope(workspace, allowed_roots=(Path("/tmp") / long_value,))
            provenance = WorkspaceEditPlanProvenance.create(
                target_path=target,
                project_root=workspace,
                server=server_identity(server),
            )
            plan = self._plan(workspace, target)
            measuring = WorkspaceEditPlanStore()
            measured = measuring.register(plan, source="rename", scope=scope, provenance=provenance)

            bounded = WorkspaceEditPlanStore(max_bytes=measured.stored_bytes - 1)
            with self.assertRaisesRegex(WorkspaceEditPlanStoreError, "store limit"):
                bounded.register(plan, source="rename", scope=scope, provenance=provenance)

            evicting = WorkspaceEditPlanStore(max_plans=8, max_bytes=measured.stored_bytes)
            first = evicting.register(plan, source="rename", scope=scope, provenance=provenance)
            second = evicting.register(plan, source="rename", scope=scope, provenance=provenance)
            with self.assertRaises(WorkspaceEditPlanStoreError):
                evicting.get(first.plan_id, scope=scope)
            self.assertEqual(evicting.get(second.plan_id, scope=scope), second)
            self.assertEqual(evicting.snapshot()["stored_bytes"], second.stored_bytes)


if __name__ == "__main__":
    unittest.main()

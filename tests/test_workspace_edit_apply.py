from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from local_agent.agent import AgentRuntime
from local_agent.config import AgentConfig
from local_agent.evidence.timeline import effective_workspace_write_paths
from local_agent.evidence.timeline import result_changed_workspace
from local_agent.lsp.config import LspProcessEnvironment
from local_agent.lsp.config import LspServerConfig
from local_agent.lsp.config import server_identity
from local_agent.lsp.workspace_edit import build_workspace_edit_preview
from local_agent.lsp.workspace_edit_store import WorkspaceEditPlanProvenance
from local_agent.lsp.workspace_edit_store import WorkspaceEditPlanScope
from local_agent.lsp.workspace_edit_store import WorkspaceEditPlanStore
from local_agent.lsp.workspace_edit_store import WorkspaceEditPlanStoreError
from local_agent.tools.base import ToolContext, ToolRegistry, ToolResult
from local_agent.tools.files import rollback_patch
from local_agent.tools.git import _session_patch_paths
from local_agent.tools.observation import ToolResultSummary
from local_agent.tools.workspace_edit import workspace_edit_tools
from local_agent.tools import create_default_registry
from local_agent.workflows.test_planner import plan_narrow_test


def _edit(start: int, end: int, new_text: str) -> dict[str, object]:
    return {
        "range": {
            "start": {"line": 0, "character": start},
            "end": {"line": 0, "character": end},
        },
        "newText": new_text,
    }


class ApplyWorkspaceEditTests(unittest.TestCase):
    def _server(self, *, command: tuple[str, ...] = ("/usr/bin/test-lsp",)) -> LspServerConfig:
        return LspServerConfig(
            name="test-lsp",
            command=command,
            file_types=(".py",),
            root_markers=("project.marker",),
            language_id="python",
            process_environment=LspProcessEnvironment(append=(("TOOLCHAIN", "one"),)),
        )

    def _context(self, workspace: Path, *, state_dir: Path | None = None, **overrides) -> ToolContext:
        values = {
            "workspace": workspace,
            "approval_mode": "yolo",
            "state_dir": state_dir,
            "session_id": "session-1",
            "run_id": "run-1",
        }
        values.update(overrides)
        return ToolContext(**values)

    def _scope(self, context: ToolContext) -> WorkspaceEditPlanScope:
        return WorkspaceEditPlanScope.create(
            session_id=context.session_id,
            run_id=context.run_id,
            workspace=context.workspace,
            allowed_roots=context.allowed_dirs,
        )

    def _register(self, store: WorkspaceEditPlanStore, context: ToolContext, paths: tuple[Path, ...]):
        plan = build_workspace_edit_preview(
            {"changes": {path.as_uri(): [_edit(0, 3, "new")] for path in paths}},
            workspace=context.workspace,
            allowed_roots=context.allowed_dirs,
            project_root=context.workspace,
        )
        return store.register(
            plan,
            source="rename",
            scope=self._scope(context),
            provenance=WorkspaceEditPlanProvenance.create(
                target_path=paths[0],
                project_root=context.workspace,
                server=server_identity(self._server()),
            ),
        )

    def _execute(
        self,
        store: WorkspaceEditPlanStore,
        context: ToolContext,
        plan_id: str,
        *,
        validate_provenance: bool = False,
    ):
        registry = ToolRegistry(workspace_edit_tools())
        validation = (
            patch("local_agent.tools.workspace_edit._plan_authorization_error", return_value=None)
            if not validate_provenance
            else nullcontext()
        )
        with patch("local_agent.tools.workspace_edit.default_workspace_edit_plan_store", return_value=store), validation:
            return registry.execute("apply_workspace_edit", {"plan_id": plan_id}, context)

    def test_two_file_apply_is_one_shot_journaled_and_rollback_is_transactional(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state = root / "state"
            first = root / "first.py"
            second = root / "second.py"
            first.write_bytes(b"old\r\n")
            second.write_bytes(b"\xef\xbb\xbfold\n")
            context = self._context(root, state_dir=state)
            store = WorkspaceEditPlanStore()
            stored = self._register(store, context, (first, second))

            result = self._execute(store, context, stored.plan_id)

            self.assertFalse(result.is_error, result.content)
            self.assertEqual(first.read_bytes(), b"new\r\n")
            self.assertEqual(second.read_bytes(), b"\xef\xbb\xbfnew\n")
            self.assertEqual(result.metadata["source"], "rename")
            self.assertEqual(result.metadata["plan_id"], stored.plan_id)
            self.assertEqual(result.metadata["plan_digest"], stored.digest)
            self.assertEqual(result.metadata["provenance_digest"], stored.provenance.digest)
            self.assertEqual(result.metadata["server_fingerprint"], stored.provenance.server.fingerprint)
            self.assertEqual(result.metadata["changed_paths"], ["first.py", "second.py"])
            self.assertTrue(result.metadata["workspace_changed"])
            transaction_id = result.metadata["transaction_id"]

            replay = self._execute(store, context, stored.plan_id)
            self.assertTrue(replay.is_error)
            self.assertEqual(replay.metadata["error_kind"], "plan_missing")

            journal = state / "patches" / "session-1.jsonl"
            record = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(record["transaction_id"], transaction_id)
            self.assertEqual(record["source"], "rename")
            self.assertEqual(record["plan_digest"], stored.digest)
            self.assertEqual(record["provenance_digest"], stored.provenance.digest)
            self.assertEqual(record["server_fingerprint"], stored.provenance.server.fingerprint)
            self.assertEqual([item["path"] for item in record["files"]], ["first.py", "second.py"])
            self.assertTrue(all("before_text" in item and "after_text" in item for item in record["files"]))
            self.assertEqual(_session_patch_paths(context), {"first.py", "second.py"})

            rolled_back = rollback_patch({"patch_id": transaction_id}, context)
            self.assertFalse(rolled_back.is_error, rolled_back.content)
            self.assertEqual(first.read_bytes(), b"old\r\n")
            self.assertEqual(second.read_bytes(), b"\xef\xbb\xbfold\n")
            self.assertEqual(rolled_back.metadata["changed_paths"], ["first.py", "second.py"])
            self.assertEqual(rolled_back.metadata["effective_changed_paths"], [])

    def test_tool_schema_is_write_tier_opaque_and_registered_once(self) -> None:
        tool = workspace_edit_tools()[0]
        self.assertEqual(tool.name, "apply_workspace_edit")
        self.assertEqual(tool.tier, "write")
        self.assertEqual(set(tool.input_schema["properties"]), {"plan_id"})
        self.assertFalse(tool.input_schema["additionalProperties"])
        self.assertEqual(create_default_registry().tool_names().count("apply_workspace_edit"), 1)

    def test_approval_deny_does_not_consume_plan_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "main.py"
            target.write_text("old\n", encoding="utf-8")
            store = WorkspaceEditPlanStore()
            denied_context = self._context(root, tool_approval={"apply_workspace_edit": "deny"})
            stored = self._register(store, denied_context, (target,))

            denied = self._execute(store, denied_context, stored.plan_id)
            self.assertTrue(denied.is_error)
            self.assertEqual(denied.metadata["execution_status"], "denied")
            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")
            self.assertEqual(store.get(stored.plan_id, scope=self._scope(denied_context)), stored)

            applied = self._execute(store, self._context(root), stored.plan_id)
            self.assertFalse(applied.is_error, applied.content)
            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

    def test_scope_mismatch_and_stale_file_fail_closed_without_consuming_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("old\n", encoding="utf-8")
            second.write_text("old\n", encoding="utf-8")
            context = self._context(root)
            store = WorkspaceEditPlanStore()
            stored = self._register(store, context, (first, second))

            mismatch = self._execute(store, self._context(root, run_id="other"), stored.plan_id)
            self.assertTrue(mismatch.is_error)
            self.assertEqual(mismatch.metadata["error_kind"], "plan_scope_mismatch")
            second.write_text("external\n", encoding="utf-8")
            stale = self._execute(store, context, stored.plan_id)

            self.assertTrue(stale.is_error)
            self.assertEqual(stale.metadata["workspace_state"], "stale")
            self.assertFalse(stale.metadata["workspace_changed"])
            self.assertEqual(first.read_text(encoding="utf-8"), "old\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "external\n")
            self.assertEqual(store.get(stored.plan_id, scope=self._scope(context)), stored)

    def test_apply_revalidates_project_and_complete_server_identity_without_lsp_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "project.marker").write_text("root\n", encoding="utf-8")
            target = root / "main.py"
            target.write_text("old\n", encoding="utf-8")
            context = self._context(root)
            store = WorkspaceEditPlanStore()
            stored = self._register(store, context, (target,))

            with (
                patch("local_agent.tools.workspace_edit.lsp_config.external_lsp_enabled", return_value=True),
                patch(
                    "local_agent.tools.workspace_edit.lsp_config.servers_for_path",
                    return_value=[self._server(command=("/different/server",))],
                ),
            ):
                drift = self._execute(store, context, stored.plan_id, validate_provenance=True)
            self.assertTrue(drift.is_error)
            self.assertIn("server identity changed", drift.content)
            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

            other_root = root / "other"
            other_root.mkdir()
            with (
                patch("local_agent.tools.workspace_edit.lsp_config.external_lsp_enabled", return_value=True),
                patch("local_agent.tools.workspace_edit.lsp_config.servers_for_path", return_value=[self._server()]),
                patch("local_agent.tools.workspace_edit.lsp_config.root_for_path", return_value=other_root),
            ):
                root_drift = self._execute(store, context, stored.plan_id, validate_provenance=True)
            self.assertTrue(root_drift.is_error)
            self.assertIn("project root changed", root_drift.content)
            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

            failing_root = Mock()
            failing_root.resolve.side_effect = OSError("controlled resolve failure")
            with (
                patch("local_agent.tools.workspace_edit.lsp_config.external_lsp_enabled", return_value=True),
                patch("local_agent.tools.workspace_edit.lsp_config.servers_for_path", return_value=[self._server()]),
                patch("local_agent.tools.workspace_edit.lsp_config.root_for_path", return_value=failing_root),
            ):
                root_error = self._execute(store, context, stored.plan_id, validate_provenance=True)
            self.assertTrue(root_error.is_error)
            self.assertIn("can no longer be resolved safely", root_error.content)
            self.assertFalse(root_error.metadata["workspace_changed"])
            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

            with (
                patch("local_agent.tools.workspace_edit.lsp_config.external_lsp_enabled", return_value=True),
                patch("local_agent.tools.workspace_edit.lsp_config.servers_for_path", return_value=[self._server()]),
                patch("local_agent.tools.workspace_edit.lsp_config.root_for_path", return_value=root),
            ):
                applied = self._execute(store, context, stored.plan_id, validate_provenance=True)
            self.assertFalse(applied.is_error, applied.content)
            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

    def test_apply_requires_nonanonymous_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "main.py"
            target.write_text("old\n", encoding="utf-8")
            context = self._context(root)
            store = WorkspaceEditPlanStore()
            stored = self._register(store, context, (target,))

            result = self._execute(store, self._context(root, run_id=None), stored.plan_id)

            self.assertTrue(result.is_error)
            self.assertEqual(result.metadata["error_kind"], "plan_scope_missing")
            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

            unavailable = Mock()
            unavailable.expanduser.return_value.resolve.side_effect = OSError("controlled failure")
            invalid = self._execute(
                store,
                self._context(root, allowed_dirs=(unavailable,)),
                stored.plan_id,
            )
            self.assertTrue(invalid.is_error)
            self.assertEqual(invalid.metadata["error_kind"], "plan_scope_invalid")
            self.assertFalse(invalid.metadata["workspace_changed"])
            self.assertNotIn("controlled failure", invalid.content)
            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

    def test_workspace_edit_journal_failure_restores_and_retains_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("old\n", encoding="utf-8")
            second.write_text("old\n", encoding="utf-8")
            context = self._context(root)
            store = WorkspaceEditPlanStore()
            stored = self._register(store, context, (first, second))

            with patch("local_agent.tools.workspace_edit.record_workspace_edit_patch", side_effect=OSError("full")):
                result = self._execute(store, context, stored.plan_id)

            self.assertTrue(result.is_error)
            self.assertEqual(result.metadata["workspace_state"], "restored")
            self.assertEqual(result.metadata["transaction_status"], "rolled_back")
            self.assertFalse(result.metadata["workspace_changed"])
            self.assertEqual(result.metadata["changed_paths"], [])
            self.assertEqual((first.read_text(), second.read_text()), ("old\n", "old\n"))
            self.assertEqual(store.get(stored.plan_id, scope=self._scope(context)), stored)

    def test_workspace_edit_journal_failure_partial_inverse_reports_final_residual(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("old\n", encoding="utf-8")
            second.write_text("old\n", encoding="utf-8")
            context = self._context(root)
            store = WorkspaceEditPlanStore()
            stored = self._register(store, context, (first, second))
            calls = 0

            def partial_inverse(path: Path, content: bytes) -> None:
                nonlocal calls
                calls += 1
                if calls in {4, 5}:
                    raise OSError("controlled inverse failure")
                path.write_bytes(content)

            with (
                patch("local_agent.tools.workspace_edit.record_workspace_edit_patch", side_effect=OSError("full")),
                patch("local_agent.patch.transaction._write_bytes", side_effect=partial_inverse),
            ):
                result = self._execute(store, context, stored.plan_id)

            self.assertTrue(result.is_error)
            self.assertEqual(result.metadata["workspace_state"], "indeterminate")
            self.assertTrue(result.metadata["workspace_changed"])
            self.assertEqual(result.metadata["changed_paths"], ["second.py"])
            self.assertEqual((first.read_text(), second.read_text()), ("old\n", "new\n"))
            with self.assertRaises(WorkspaceEditPlanStoreError):
                store.get(stored.plan_id, scope=self._scope(context))

    def test_workspace_edit_journal_failure_concurrent_drift_is_not_hidden_by_inverse_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "main.py"
            target.write_text("old\n", encoding="utf-8")
            context = self._context(root)
            store = WorkspaceEditPlanStore()
            stored = self._register(store, context, (target,))

            def drift_then_fail(**_kwargs) -> str:
                target.write_text("external\n", encoding="utf-8")
                raise OSError("concurrent drift")

            with patch("local_agent.tools.workspace_edit.record_workspace_edit_patch", side_effect=drift_then_fail):
                result = self._execute(store, context, stored.plan_id)

            self.assertTrue(result.is_error)
            self.assertEqual(result.metadata["transaction_status"], "rollback_failed")
            self.assertTrue(result.metadata["workspace_changed"])
            self.assertEqual(result.metadata["changed_paths"], ["main.py"])
            self.assertEqual(target.read_text(encoding="utf-8"), "external\n")

    def test_commit_failure_restores_and_compensation_failure_reports_partial_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("old\n", encoding="utf-8")
            second.write_text("old\n", encoding="utf-8")
            context = self._context(root)
            store = WorkspaceEditPlanStore()
            restored_plan = self._register(store, context, (first, second))

            def second_fails(path: Path, content: bytes) -> None:
                if path == second:
                    raise OSError("controlled")
                path.write_bytes(content)

            with patch("local_agent.patch.transaction._write_bytes", side_effect=second_fails):
                restored = self._execute(store, context, restored_plan.plan_id)
            self.assertTrue(restored.is_error)
            self.assertEqual(restored.metadata["workspace_state"], "restored")
            self.assertFalse(restored.metadata["workspace_changed"])
            self.assertEqual((first.read_text(), second.read_text()), ("old\n", "old\n"))
            self.assertEqual(store.get(restored_plan.plan_id, scope=self._scope(context)), restored_plan)

            partial_plan = self._register(store, context, (first, second))

            def rollback_fails(path: Path, content: bytes) -> None:
                if path == second or (path == first and content == b"old\n"):
                    raise OSError("controlled")
                path.write_bytes(content)

            with patch("local_agent.patch.transaction._write_bytes", side_effect=rollback_fails):
                partial = self._execute(store, context, partial_plan.plan_id)
            self.assertTrue(partial.is_error)
            self.assertEqual(partial.metadata["workspace_state"], "indeterminate")
            self.assertTrue(partial.metadata["workspace_changed"])
            self.assertEqual(partial.metadata["changed_paths"], ["first.py"])
            self.assertTrue(result_changed_workspace(_summary(partial)))
            with self.assertRaises(WorkspaceEditPlanStoreError):
                store.get(partial_plan.plan_id, scope=self._scope(context))

    def test_transaction_rollback_stale_and_partial_failure_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state = root / "state"
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("old\n", encoding="utf-8")
            second.write_text("old\n", encoding="utf-8")
            context = self._context(root, state_dir=state)
            store = WorkspaceEditPlanStore()
            stale_plan = self._register(store, context, (first, second))
            applied = self._execute(store, context, stale_plan.plan_id)
            self.assertFalse(applied.is_error)
            second.write_text("external\n", encoding="utf-8")

            stale = rollback_patch({"patch_id": applied.metadata["transaction_id"]}, context)
            self.assertTrue(stale.is_error)
            self.assertEqual(stale.metadata["workspace_state"], "stale")
            self.assertFalse(stale.metadata["workspace_changed"])
            self.assertEqual(first.read_text(), "new\n")
            self.assertEqual(second.read_text(), "external\n")

            first.write_text("old\n", encoding="utf-8")
            second.write_text("old\n", encoding="utf-8")
            fresh_plan = self._register(store, context, (first, second))
            applied = self._execute(store, context, fresh_plan.plan_id)
            self.assertFalse(applied.is_error, applied.content)

            def rollback_compensation_fails(path: Path, content: bytes) -> None:
                if path == second or (path == first and content == b"new\n"):
                    raise OSError("controlled")
                path.write_bytes(content)

            with patch("local_agent.patch.transaction._write_bytes", side_effect=rollback_compensation_fails):
                partial = rollback_patch({"patch_id": applied.metadata["transaction_id"]}, context)
            self.assertTrue(partial.is_error)
            self.assertEqual(partial.metadata["workspace_state"], "indeterminate")
            self.assertEqual(partial.metadata["changed_paths"], ["first.py"])
            self.assertEqual(partial.metadata["effective_changed_paths"], ["second.py"])
            summaries = [
                ToolResultSummary("apply_workspace_edit", metadata=applied.metadata),
                ToolResultSummary("rollback_patch", partial.content, is_error=True, metadata=partial.metadata),
            ]
            self.assertEqual(effective_workspace_write_paths(summaries), ("second.py",))

    def test_multifile_rollback_journal_failure_restores_prior_transaction_and_keeps_record_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state = root / "state"
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("old\n", encoding="utf-8")
            second.write_text("old\n", encoding="utf-8")
            context = self._context(root, state_dir=state)
            store = WorkspaceEditPlanStore()
            stored = self._register(store, context, (first, second))
            applied = self._execute(store, context, stored.plan_id)
            self.assertFalse(applied.is_error, applied.content)

            with patch("local_agent.tools.files._record_rollback", side_effect=OSError("journal unavailable")):
                failed = rollback_patch({"patch_id": applied.metadata["transaction_id"]}, context)

            self.assertTrue(failed.is_error)
            self.assertEqual(failed.metadata["workspace_state"], "restored")
            self.assertFalse(failed.metadata["workspace_changed"])
            self.assertEqual((first.read_text(), second.read_text()), ("new\n", "new\n"))
            retried = rollback_patch({"patch_id": applied.metadata["transaction_id"]}, context)
            self.assertFalse(retried.is_error, retried.content)
            self.assertEqual((first.read_text(), second.read_text()), ("old\n", "old\n"))

    def test_multifile_rollback_journal_partial_truth_separates_operation_and_effective_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state = root / "state"
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("old\n", encoding="utf-8")
            second.write_text("old\n", encoding="utf-8")
            context = self._context(root, state_dir=state)
            store = WorkspaceEditPlanStore()
            stored = self._register(store, context, (first, second))
            applied = self._execute(store, context, stored.plan_id)
            self.assertFalse(applied.is_error, applied.content)
            calls = 0

            def partial_inverse(path: Path, content: bytes) -> None:
                nonlocal calls
                calls += 1
                if calls in {4, 5}:
                    raise OSError("controlled inverse failure")
                path.write_bytes(content)

            with (
                patch("local_agent.tools.files._record_rollback", side_effect=OSError("journal unavailable")),
                patch("local_agent.patch.transaction._write_bytes", side_effect=partial_inverse),
            ):
                failed = rollback_patch({"patch_id": applied.metadata["transaction_id"]}, context)

            self.assertTrue(failed.is_error)
            self.assertEqual((first.read_text(), second.read_text()), ("new\n", "old\n"))
            self.assertEqual(failed.metadata["changed_paths"], ["second.py"])
            self.assertEqual(failed.metadata["effective_changed_paths"], ["first.py"])
            summaries = [
                ToolResultSummary("apply_workspace_edit", metadata=applied.metadata),
                ToolResultSummary("rollback_patch", failed.content, is_error=True, metadata=failed.metadata),
            ]
            self.assertEqual(effective_workspace_write_paths(summaries), ("first.py",))

    def test_multifile_truth_reaches_timeline_and_test_planner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "tests").mkdir()
            result = ToolResultSummary(
                "apply_workspace_edit",
                "committed",
                metadata={
                    "workspace_changed": True,
                    "changed_paths": ["src/one.py", "src/two.py"],
                    "effective_changed_paths": ["src/one.py", "src/two.py"],
                },
            )
            self.assertEqual(effective_workspace_write_paths([result]), ("src/one.py", "src/two.py"))
            self.assertEqual(plan_narrow_test(root, [result]).changed_paths, ("src/one.py", "src/two.py"))

            rolled_back = ToolResultSummary(
                "rollback_patch",
                "restored",
                metadata={
                    "workspace_changed": True,
                    "changed_paths": ["src/one.py", "src/two.py"],
                    "transaction_paths": ["src/one.py", "src/two.py"],
                    "effective_changed_paths": [],
                },
            )
            self.assertEqual(effective_workspace_write_paths([result, rolled_back]), ())

    def test_runtime_invalidates_every_changed_path_even_for_partial_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("one\n", encoding="utf-8")
            second.write_text("two\n", encoding="utf-8")
            runtime = AgentRuntime(
                AgentConfig(
                    provider="openai-compatible",
                    api_base_url="https://example.invalid/v1",
                    api_key="token",
                    model="model",
                    workspace=root,
                    max_steps=0,
                    budget_seconds=None,
                    approval_mode="yolo",
                ),
                show_tool_logs=False,
            )
            runtime._run.run_id = "run-1"
            runtime._run.current_user_request = "inspect both files"
            for path in (first, second):
                result = ToolResult(path.read_text(encoding="utf-8"))
                arguments = {"path": path.name}
                runtime._evidence_phase.record_tool_choice_result("read_file", arguments, result)
                runtime._evidence_phase.record_read_file_evidence("read_file", arguments, result)
                runtime._evidence_phase.record_tool_evidence("read_file", arguments, result)
            self.assertEqual(runtime._session_evidence.snapshot()["entries"], 2)

            partial = ToolResult(
                "compensation incomplete",
                is_error=True,
                metadata={
                    "workspace_changed": True,
                    "changed_paths": ["first.py", "second.py"],
                    "workspace_state": "indeterminate",
                },
            )
            runtime._evidence_phase.invalidate_stale_source_evidence_after_write(
                "apply_workspace_edit",
                {"plan_id": "wep_" + "0" * 32},
                partial,
            )

            self.assertEqual(runtime._session_evidence.snapshot()["entries"], 0)
            self.assertEqual(runtime._run.evidence.source_evidence, [])


def _summary(result) -> ToolResultSummary:
    return ToolResultSummary(
        "apply_workspace_edit",
        result.content,
        is_error=result.is_error,
        metadata=result.metadata,
    )


if __name__ == "__main__":
    unittest.main()

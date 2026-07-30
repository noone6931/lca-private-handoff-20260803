from __future__ import annotations

import errno
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.patch.transaction import RootedTextFileChange
from local_agent.patch.transaction import TextTransactionResult
from local_agent.patch.transaction import apply_rooted_text_transaction
from local_agent.platform.rooted_contracts import RootedRegularSnapshot
from local_agent.platform.rooted_files import RootedFileError
from local_agent.platform.rooted_files import mutate_rooted_regular
from local_agent.tools.base import ToolContext
from local_agent.tools.files import rollback_patch
from local_agent.tools.workspace_mutation import ContainerMutationProvenance
from local_agent.tools.workspace_mutation import commit_container_workspace_output
from local_agent.tools.workspace_mutation import rollback_container_workspace_record
from local_agent.workspace.context import WorkspaceRootIdentity
from local_agent.workspace.snapshot import capture_workspace_snapshot
from local_agent.workspace.snapshot_delta import build_workspace_text_mutation_plan


class RootedTextTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_replace_create_delete_commit_with_exact_modes(self) -> None:
        replaced = self.workspace / "replace.txt"
        deleted = self.workspace / "delete.txt"
        replaced.write_text("before\n", encoding="utf-8")
        deleted.write_text("delete\n", encoding="utf-8")
        replaced.chmod(0o640)
        deleted.chmod(0o600)
        identity = _identity(self.workspace)
        changes = (
            RootedTextFileChange.create(
                ("create.txt",),
                None,
                b"created\n",
                before_mode=None,
                after_mode=0o620,
            ),
            RootedTextFileChange.create(
                ("delete.txt",),
                b"delete\n",
                None,
                before_mode=0o600,
                after_mode=None,
            ),
            RootedTextFileChange.create(
                ("replace.txt",),
                b"before\n",
                b"after\n",
                before_mode=0o640,
                after_mode=0o640,
            ),
        )

        result = apply_rooted_text_transaction(
            self.workspace,
            changes,
            expected_root_identity=identity,
        )

        self.assertEqual(result.status, "committed")
        self.assertEqual(
            tuple(path.name for path in result.changed_paths),
            ("create.txt", "delete.txt", "replace.txt"),
        )
        self.assertEqual((self.workspace / "create.txt").read_text(), "created\n")
        self.assertEqual((self.workspace / "create.txt").stat().st_mode & 0o777, 0o620)
        self.assertFalse(deleted.exists())
        self.assertEqual(replaced.read_text(), "after\n")
        self.assertEqual(replaced.stat().st_mode & 0o777, 0o640)

    def test_preflight_stale_rejects_whole_batch(self) -> None:
        target = self.workspace / "target.txt"
        target.write_text("external\n", encoding="utf-8")
        result = apply_rooted_text_transaction(
            self.workspace,
            (
                RootedTextFileChange.create(
                    ("created.txt",),
                    None,
                    b"created\n",
                    before_mode=None,
                    after_mode=0o600,
                ),
                RootedTextFileChange.create(
                    ("target.txt",),
                    b"expected\n",
                    b"after\n",
                    before_mode=0o644,
                    after_mode=0o644,
                ),
            ),
            expected_root_identity=_identity(self.workspace),
        )

        self.assertEqual(result.status, "stale")
        self.assertFalse(result.workspace_changed)
        self.assertFalse((self.workspace / "created.txt").exists())
        self.assertEqual(target.read_text(), "external\n")

    def test_later_failure_compensates_prior_create_and_replace(self) -> None:
        target = self.workspace / "target.txt"
        target.write_text("before\n", encoding="utf-8")
        changes = (
            RootedTextFileChange.create(
                ("created.txt",),
                None,
                b"created\n",
                before_mode=None,
                after_mode=0o600,
            ),
            RootedTextFileChange.create(
                ("target.txt",),
                b"before\n",
                b"after\n",
                before_mode=0o644,
                after_mode=0o644,
            ),
        )
        from local_agent.patch import transaction as owner

        real_mutate = owner.mutate_rooted_regular

        def fail_second(root, parts, **kwargs):
            if parts == ("target.txt",):
                raise RootedFileError("controlled_failure")
            return real_mutate(root, parts, **kwargs)

        with patch(
            "local_agent.patch.transaction.mutate_rooted_regular",
            side_effect=fail_second,
        ):
            result = apply_rooted_text_transaction(
                self.workspace,
                changes,
                expected_root_identity=_identity(self.workspace),
            )

        self.assertEqual(result.status, "rolled_back")
        self.assertFalse(result.workspace_changed)
        self.assertFalse((self.workspace / "created.txt").exists())
        self.assertEqual(target.read_text(), "before\n")

    def test_keyboard_interrupt_compensates_before_propagating(self) -> None:
        target = self.workspace / "target.txt"
        target.write_text("before\n", encoding="utf-8")
        changes = (
            RootedTextFileChange.create(
                ("created.txt",), None, b"created\n",
                before_mode=None, after_mode=0o600,
            ),
            RootedTextFileChange.create(
                ("target.txt",), b"before\n", b"after\n",
                before_mode=0o644, after_mode=0o644,
            ),
        )
        from local_agent.patch import transaction as owner

        real_mutate = owner.mutate_rooted_regular

        def interrupt_second(root, parts, **kwargs):
            if parts == ("target.txt",):
                raise KeyboardInterrupt()
            return real_mutate(root, parts, **kwargs)

        with (
            patch(
                "local_agent.patch.transaction.mutate_rooted_regular",
                side_effect=interrupt_second,
            ),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            apply_rooted_text_transaction(
                self.workspace,
                changes,
                expected_root_identity=_identity(self.workspace),
            )

        transaction = raised.exception.text_transaction_result
        self.assertEqual(transaction.status, "rolled_back")
        self.assertFalse(transaction.workspace_changed)
        self.assertFalse((self.workspace / "created.txt").exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "before\n")

    def test_keyboard_interrupt_reports_failed_inverse_residual(self) -> None:
        target = self.workspace / "target.txt"
        target.write_text("before\n", encoding="utf-8")
        changes = (
            RootedTextFileChange.create(
                ("created.txt",), None, b"created\n",
                before_mode=None, after_mode=0o600,
            ),
            RootedTextFileChange.create(
                ("target.txt",), b"before\n", b"after\n",
                before_mode=0o644, after_mode=0o644,
            ),
        )
        from local_agent.patch import transaction as owner

        real_mutate = owner.mutate_rooted_regular

        def interrupt_then_fail_inverse(root, parts, **kwargs):
            if parts == ("target.txt",):
                raise KeyboardInterrupt()
            if parts == ("created.txt",) and kwargs["after_content"] is None:
                raise RootedFileError("inverse_failed")
            return real_mutate(root, parts, **kwargs)

        with (
            patch(
                "local_agent.patch.transaction.mutate_rooted_regular",
                side_effect=interrupt_then_fail_inverse,
            ),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            apply_rooted_text_transaction(
                self.workspace,
                changes,
                expected_root_identity=_identity(self.workspace),
            )

        transaction = raised.exception.text_transaction_result
        self.assertEqual(transaction.status, "rollback_failed")
        self.assertTrue(transaction.workspace_changed)
        self.assertEqual(transaction.changed_paths, (self.workspace / "created.txt",))
        self.assertTrue((self.workspace / "created.txt").exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "before\n")

    def test_parent_symlink_is_rejected_without_outside_write(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        (self.workspace / "linked").symlink_to(outside, target_is_directory=True)
        result = apply_rooted_text_transaction(
            self.workspace,
            (
                RootedTextFileChange.create(
                    ("linked", "created.txt"),
                    None,
                    b"created\n",
                    before_mode=None,
                    after_mode=0o600,
                ),
            ),
            expected_root_identity=_identity(self.workspace),
        )

        self.assertEqual(result.status, "stale")
        self.assertFalse(result.workspace_changed)
        self.assertFalse((outside / "created.txt").exists())

    def test_post_write_path_must_still_name_opened_file_identity(self) -> None:
        target = self.workspace / "target.txt"
        target.write_text("before\n", encoding="utf-8")
        target.chmod(0o640)

        with patch(
            "local_agent.platform.rooted_files._read_regular_at",
            return_value=RootedRegularSnapshot(
                ("target.txt",),
                b"after\n",
                0o640,
                (999_999, 999_999),
            ),
        ):
            with self.assertRaises(RootedFileError) as raised:
                mutate_rooted_regular(
                    self.workspace,
                    ("target.txt",),
                    before_content=b"before\n",
                    after_content=b"after\n",
                    before_mode=0o640,
                    after_mode=0o640,
                    expected_root_identity=_identity(self.workspace),
                )

        self.assertEqual(raised.exception.kind, "path_identity_changed")
        self.assertTrue(raised.exception.workspace_changed)

    def test_same_inode_concurrent_change_is_stale_before_truncate(self) -> None:
        target = self.workspace / "replace.txt"
        target.write_text("before\n", encoding="utf-8")
        from local_agent.platform import rooted_mutation as owner

        original = owner.atomic_exchange_at
        injected = False

        def inject_then_exchange(*args, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                target.write_text("concurrent\n", encoding="utf-8")
            return original(*args, **kwargs)

        with (
            patch(
                "local_agent.platform.rooted_mutation.atomic_exchange_at",
                side_effect=inject_then_exchange,
            ),
            self.assertRaises(RootedFileError) as raised,
        ):
            mutate_rooted_regular(
                self.workspace,
                ("replace.txt",),
                before_content=b"before\n",
                after_content=b"after\n",
                before_mode=0o644,
                after_mode=0o644,
                expected_root_identity=_identity(self.workspace),
            )

        self.assertEqual(raised.exception.kind, "stale_before_image")
        self.assertFalse(raised.exception.workspace_changed)
        self.assertEqual(target.read_text(encoding="utf-8"), "concurrent\n")

    def test_create_race_keeps_external_file_and_removes_prepared_file(self) -> None:
        target = self.workspace / "create.txt"
        from local_agent.platform import rooted_mutation as owner

        original = owner.atomic_noreplace_at
        injected = False

        def inject_then_rename(source_fd, source, destination_fd, destination):
            nonlocal injected
            if not injected and destination == target.name:
                injected = True
                target.write_text("external\n", encoding="utf-8")
            return original(source_fd, source, destination_fd, destination)

        with (
            patch(
                "local_agent.platform.rooted_mutation.atomic_noreplace_at",
                side_effect=inject_then_rename,
            ),
            self.assertRaises(RootedFileError) as raised,
        ):
            mutate_rooted_regular(
                self.workspace,
                (target.name,),
                before_content=None,
                after_content=b"created\n",
                before_mode=None,
                after_mode=0o644,
                expected_root_identity=_identity(self.workspace),
            )

        self.assertEqual(raised.exception.kind, "stale_before_image")
        self.assertFalse(raised.exception.workspace_changed)
        self.assertEqual(target.read_text(encoding="utf-8"), "external\n")
        self.assertEqual(_mutation_entries(self.workspace), ())

    def test_delete_same_inode_concurrent_change_is_rolled_back(self) -> None:
        target = self.workspace / "delete.txt"
        target.write_text("before\n", encoding="utf-8")
        from local_agent.platform import rooted_mutation as owner

        original = owner.atomic_noreplace_at
        injected = False

        def inject_then_rename(source_fd, source, destination_fd, destination):
            nonlocal injected
            if not injected and source == target.name:
                injected = True
                target.write_text("concurrent\n", encoding="utf-8")
            return original(source_fd, source, destination_fd, destination)

        with (
            patch(
                "local_agent.platform.rooted_mutation.atomic_noreplace_at",
                side_effect=inject_then_rename,
            ),
            self.assertRaises(RootedFileError) as raised,
        ):
            mutate_rooted_regular(
                self.workspace,
                (target.name,),
                before_content=b"before\n",
                after_content=None,
                before_mode=0o644,
                after_mode=None,
                expected_root_identity=_identity(self.workspace),
            )

        self.assertEqual(raised.exception.kind, "stale_before_image")
        self.assertFalse(raised.exception.workspace_changed)
        self.assertEqual(target.read_text(encoding="utf-8"), "concurrent\n")
        self.assertEqual(_mutation_entries(self.workspace), ())

    def test_delete_fsync_failure_reports_committed_workspace_change(self) -> None:
        target = self.workspace / "delete.txt"
        target.write_text("before\n", encoding="utf-8")

        with (
            patch(
                "local_agent.platform.rooted_mutation.os.fsync",
                side_effect=OSError("controlled fsync failure"),
            ),
            self.assertRaises(RootedFileError) as raised,
        ):
            mutate_rooted_regular(
                self.workspace,
                (target.name,),
                before_content=b"before\n",
                after_content=None,
                before_mode=0o644,
                after_mode=None,
                expected_root_identity=_identity(self.workspace),
            )

        self.assertEqual(raised.exception.kind, "mutation_failed")
        self.assertTrue(raised.exception.workspace_changed)
        self.assertFalse(target.exists())

    def test_atomic_exchange_unsupported_fails_before_publication(self) -> None:
        target = self.workspace / "replace.txt"
        target.write_text("before\n", encoding="utf-8")

        with (
            patch(
                "local_agent.platform.rooted_mutation.atomic_exchange_at",
                side_effect=OSError(
                    errno.ENOTSUP,
                    "atomic exchange unsupported",
                ),
            ),
            self.assertRaises(RootedFileError) as raised,
        ):
            mutate_rooted_regular(
                self.workspace,
                (target.name,),
                before_content=b"before\n",
                after_content=b"after\n",
                before_mode=0o644,
                after_mode=0o644,
                expected_root_identity=_identity(self.workspace),
            )

        self.assertEqual(
            raised.exception.kind,
            "atomic_mutation_unsupported",
        )
        self.assertFalse(raised.exception.workspace_changed)
        self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
        self.assertEqual(_mutation_entries(self.workspace), ())

    def test_atomic_noreplace_unsupported_fails_before_create(self) -> None:
        target = self.workspace / "create.txt"

        with (
            patch(
                "local_agent.platform.rooted_mutation.atomic_noreplace_at",
                side_effect=OSError(
                    errno.EOPNOTSUPP,
                    "atomic no-replace unsupported",
                ),
            ),
            self.assertRaises(RootedFileError) as raised,
        ):
            mutate_rooted_regular(
                self.workspace,
                (target.name,),
                before_content=None,
                after_content=b"created\n",
                before_mode=None,
                after_mode=0o644,
                expected_root_identity=_identity(self.workspace),
            )

        self.assertEqual(
            raised.exception.kind,
            "atomic_mutation_unsupported",
        )
        self.assertFalse(raised.exception.workspace_changed)
        self.assertFalse(target.exists())
        self.assertEqual(_mutation_entries(self.workspace), ())


class ContainerWorkspaceMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.workspace = self.base / "workspace"
        self.staged = self.base / "staged"
        self.state = self.base / "state"
        self.workspace.mkdir()
        self.staged.mkdir()
        for root in (self.workspace, self.staged):
            (root / "delete.txt").write_text("delete\n", encoding="utf-8")
            (root / "replace.txt").write_text("before\n", encoding="utf-8")
        (self.staged / "delete.txt").unlink()
        (self.staged / "replace.txt").write_text("after\n", encoding="utf-8")
        (self.staged / "create.txt").write_text("created\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _plan(self):
        return build_workspace_text_mutation_plan(
            capture_workspace_snapshot(self.workspace, roots_revision=9),
            capture_workspace_snapshot(self.staged, roots_revision=9),
        )

    def _context(self, plan) -> ToolContext:
        return ToolContext(
            self.workspace,
            "yolo",
            state_dir=self.state,
            session_id="session",
            run_id="run",
            tool_call_id="call",
            workspace_revision=9,
            workspace_identity=WorkspaceRootIdentity(*plan.before.root_identity),
        )

    def _provenance(self) -> ContainerMutationProvenance:
        return ContainerMutationProvenance(
            attempt_id="a" * 32,
            image_digest="sha256:" + "b" * 64,
            profile="workspace-write",
            workspace_transport="staged-copy",
        )

    def test_commit_applies_plan_and_writes_one_bounded_journal_record(self) -> None:
        plan = self._plan()
        result = commit_container_workspace_output(
            context=self._context(plan),
            plan=plan,
            provenance=self._provenance(),
        )

        self.assertTrue(result.committed)
        self.assertTrue(result.workspace_changed)
        self.assertEqual(
            result.transaction_paths,
            ("create.txt", "delete.txt", "replace.txt"),
        )
        self.assertEqual((self.workspace / "create.txt").read_text(), "created\n")
        self.assertFalse((self.workspace / "delete.txt").exists())
        self.assertEqual((self.workspace / "replace.txt").read_text(), "after\n")
        records = [
            json.loads(line)
            for line in (self.state / "patches/session.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["source"], "container_staged_copy")
        self.assertEqual(record["attempt_id"], "a" * 32)
        self.assertEqual(
            [item["operation"] for item in record["files"]],
            ["create", "delete", "replace"],
        )
        self.assertNotIn("command", record)
        self.assertNotIn("stdout", record)
        self.assertNotIn("stderr", record)

    def test_full_snapshot_stale_refuses_before_any_write(self) -> None:
        plan = self._plan()
        (self.workspace / "unrelated.txt").write_text("external\n", encoding="utf-8")

        result = commit_container_workspace_output(
            context=self._context(plan),
            plan=plan,
            provenance=self._provenance(),
        )

        self.assertEqual(result.state, "stale")
        self.assertFalse(result.workspace_changed)
        self.assertFalse((self.workspace / "create.txt").exists())
        self.assertTrue((self.workspace / "delete.txt").exists())
        self.assertEqual((self.workspace / "replace.txt").read_text(), "before\n")
        self.assertFalse((self.state / "patches/session.jsonl").exists())

    def test_final_snapshot_or_journal_failure_restores_every_operation(self) -> None:
        for failure in ("final", "journal"):
            with self.subTest(failure=failure):
                plan = self._plan()
                context = self._context(plan)
                if failure == "final":
                    controlled = patch(
                        "local_agent.tools.workspace_mutation._final_snapshot_error",
                        side_effect=["workspace_final_mismatch", None],
                    )
                else:
                    controlled = patch(
                        "local_agent.tools.workspace_mutation.append_patch_record",
                        side_effect=OSError("journal unavailable"),
                    )
                with controlled:
                    result = commit_container_workspace_output(
                        context=context,
                        plan=plan,
                        provenance=self._provenance(),
                    )
                self.assertEqual(result.state, "restored")
                self.assertFalse(result.workspace_changed)
                self.assertFalse((self.workspace / "create.txt").exists())
                self.assertTrue((self.workspace / "delete.txt").exists())
                self.assertEqual(
                    (self.workspace / "replace.txt").read_text(),
                    "before\n",
                )

    def test_journal_keyboard_interrupt_restores_before_propagating(self) -> None:
        plan = self._plan()
        with (
            patch(
                "local_agent.tools.workspace_mutation.append_patch_record",
                side_effect=KeyboardInterrupt(),
            ),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            commit_container_workspace_output(
                context=self._context(plan),
                plan=plan,
                provenance=self._provenance(),
            )

        recovery = raised.exception.workspace_mutation_result
        self.assertEqual(recovery.state, "restored")
        self.assertFalse(recovery.workspace_changed)
        self.assertFalse((self.workspace / "create.txt").exists())
        self.assertTrue((self.workspace / "delete.txt").exists())
        self.assertEqual((self.workspace / "replace.txt").read_text(), "before\n")
        self.assertFalse((self.state / "patches/session.jsonl").exists())

    def test_transaction_interrupt_truth_maps_to_workspace_result(self) -> None:
        plan = self._plan()
        interruption = KeyboardInterrupt()
        interruption.text_transaction_result = TextTransactionResult(
            "rollback_failed",
            True,
            (self.workspace / "create.txt",),
            "parent_interrupted",
        )
        with (
            patch(
                "local_agent.tools.workspace_mutation.apply_rooted_text_transaction",
                side_effect=interruption,
            ),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            commit_container_workspace_output(
                context=self._context(plan),
                plan=plan,
                provenance=self._provenance(),
            )

        result = raised.exception.workspace_mutation_result
        self.assertEqual(result.state, "indeterminate")
        self.assertTrue(result.workspace_changed)
        self.assertEqual(result.changed_paths, ("create.txt",))

    def test_rollback_interrupt_truth_maps_to_workspace_result(self) -> None:
        plan = self._plan()
        context = self._context(plan)
        commit_container_workspace_output(
            context=context,
            plan=plan,
            provenance=self._provenance(),
        )
        record = json.loads(
            (self.state / "patches/session.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        interruption = KeyboardInterrupt()
        interruption.text_transaction_result = TextTransactionResult(
            "rolled_back",
            False,
            (),
            "parent_interrupted",
        )
        with (
            patch(
                "local_agent.tools.workspace_mutation.apply_rooted_text_transaction",
                side_effect=interruption,
            ),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            rollback_container_workspace_record(record, context)

        result = raised.exception.workspace_mutation_result
        self.assertEqual(result.state, "restored")
        self.assertFalse(result.workspace_changed)
        self.assertEqual(result.changed_paths, ())

    def test_post_replace_interrupt_restores_journal_and_workspace(self) -> None:
        from local_agent.patch import journal as journal_owner

        plan = self._plan()
        real_replace = journal_owner.os.replace
        journal_path = self.state / "patches/session.jsonl"

        def replace_then_interrupt(source, destination):
            real_replace(source, destination)
            if Path(destination) == journal_path:
                raise KeyboardInterrupt()

        with (
            patch(
                "local_agent.patch.journal.os.replace",
                side_effect=replace_then_interrupt,
            ),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            commit_container_workspace_output(
                context=self._context(plan),
                plan=plan,
                provenance=self._provenance(),
            )

        self.assertEqual(raised.exception.patch_journal_result.state, "restored")
        self.assertFalse(raised.exception.patch_journal_result.journal_changed)
        self.assertFalse(raised.exception.patch_journal_result.record_persisted)
        self.assertEqual(raised.exception.workspace_mutation_result.state, "restored")
        self.assertFalse((self.state / "patches/session.jsonl").exists())
        self.assertFalse((self.workspace / "create.txt").exists())
        self.assertTrue((self.workspace / "delete.txt").exists())
        self.assertEqual((self.workspace / "replace.txt").read_text(), "before\n")

    def test_rollback_patch_reverses_create_delete_replace_and_journals(self) -> None:
        plan = self._plan()
        context = self._context(plan)
        committed = commit_container_workspace_output(
            context=context,
            plan=plan,
            provenance=self._provenance(),
        )

        rolled_back = rollback_patch(
            {"patch_id": committed.transaction_id},
            context,
        )

        self.assertFalse(rolled_back.is_error)
        self.assertFalse((self.workspace / "create.txt").exists())
        self.assertTrue((self.workspace / "delete.txt").exists())
        self.assertEqual((self.workspace / "replace.txt").read_text(), "before\n")
        records = [
            json.loads(line)
            for line in (self.state / "patches/session.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual([record["event"] for record in records], ["apply", "rollback"])
        self.assertEqual(records[-1]["patch_id"], committed.transaction_id)

    def test_stale_or_unjournaled_rollback_never_hides_current_state(self) -> None:
        plan = self._plan()
        context = self._context(plan)
        committed = commit_container_workspace_output(
            context=context,
            plan=plan,
            provenance=self._provenance(),
        )
        (self.workspace / "replace.txt").write_text("external\n", encoding="utf-8")

        stale = rollback_patch({"patch_id": committed.transaction_id}, context)

        self.assertTrue(stale.is_error)
        self.assertEqual(stale.metadata["workspace_state"], "stale")
        self.assertFalse(stale.metadata["workspace_changed"])
        self.assertEqual((self.workspace / "replace.txt").read_text(), "external\n")

        (self.workspace / "replace.txt").write_text("after\n", encoding="utf-8")
        with patch(
            "local_agent.tools.workspace_mutation.append_patch_record",
            side_effect=OSError("journal unavailable"),
        ):
            unjournaled = rollback_patch(
                {"patch_id": committed.transaction_id},
                context,
            )

        self.assertTrue(unjournaled.is_error)
        self.assertEqual(unjournaled.metadata["workspace_state"], "restored")
        self.assertFalse(unjournaled.metadata["workspace_changed"])
        self.assertEqual((self.workspace / "replace.txt").read_text(), "after\n")
        self.assertTrue((self.workspace / "create.txt").exists())
        self.assertFalse((self.workspace / "delete.txt").exists())


def _identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    return metadata.st_dev, metadata.st_ino


def _mutation_entries(root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            path.name
            for path in root.iterdir()
            if path.name.startswith(".lca-mutation-")
        )
    )


if __name__ == "__main__":
    unittest.main()

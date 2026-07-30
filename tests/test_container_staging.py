from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.execution.container_staging import ContainerStagingError
from local_agent.execution.container_staging import cleanup_staging_attempt
from local_agent.execution.container_staging import observe_staged_workspace_output
from local_agent.execution.container_staging import record_staging_container_absent
from local_agent.execution.container_staging import record_staging_create_possible
from local_agent.execution.container_staging import recover_staging_authority
from local_agent.execution.container_staging import run_staged_workspace_operation
from local_agent.execution.container_staging import stage_workspace_snapshots
from local_agent.execution.container_staging_contracts import (
    ContainerStagingContainerBinding,
)
from local_agent.execution.container_staging_contracts import (
    ContainerStagingContainerRecoveryResult,
)
from local_agent.execution.container_staging_contracts import (
    ContainerStagingWorkspaceBinding,
)
from local_agent.execution.container_staging_lifecycle import (
    acquire_staging_authority,
)
from local_agent.execution.container_staging_lifecycle import (
    mark_staging_allocated,
)
from local_agent.execution.container_staging_lifecycle import (
    reserve_staging_lifecycle,
)
from local_agent.execution.container_types import (
    ContainerDirectoryPathIdentity,
)
from local_agent.execution.container_types import ContainerFileIdentity
from local_agent.execution.container_types import ContainerRootIdentity
from local_agent.workspace.snapshot import capture_workspace_snapshot


ATTEMPT_ID = "a" * 32


class ContainerStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src/main.py").write_text(
            "print('ok')\n",
            encoding="utf-8",
        )
        self.readable = self.root / "readable"
        self.readable.mkdir()
        (self.readable / "requirements.txt").write_text(
            "example==1\n",
            encoding="utf-8",
        )
        self.staging = self.root / "private-staging"
        self.staging.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _snapshots(self):
        return (
            capture_workspace_snapshot(self.workspace, roots_revision=3),
            capture_workspace_snapshot(self.readable, roots_revision=3),
        )

    def _cleanup(self, attempt):
        transition = record_staging_container_absent(attempt)
        self.assertTrue(transition.verified, transition.reason_code)
        return cleanup_staging_attempt(attempt)

    def _reserve_lifecycle(self):
        snapshots = self._snapshots()
        lease = acquire_staging_authority(
            self.staging,
            workspace_roots=(self.workspace, self.readable),
        )
        return reserve_staging_lifecycle(
            lease,
            attempt_id=ATTEMPT_ID,
            workspace_roots_revision=3,
            workspace_roots=tuple(
                ContainerStagingWorkspaceBinding(
                    snapshot.root,
                    snapshot.root_identity[0],
                    snapshot.root_identity[1],
                    snapshot.manifest_sha256,
                )
                for snapshot in snapshots
            ),
        )

    def test_stage_copy_is_verified_and_cleanup_proves_exact_absence(self) -> None:
        snapshots = self._snapshots()
        attempt = stage_workspace_snapshots(
            staging_root=self.staging,
            snapshots=snapshots,
            destinations=(self.workspace, self.readable),
            workspace_roots=(self.workspace, self.readable),
            attempt_id=ATTEMPT_ID,
        )

        self.assertTrue(attempt.authority_is_current())
        self.assertEqual(len(attempt.roots), 2)
        self.assertEqual(
            (attempt.roots[0].staging_path / "src/main.py").read_text(
                encoding="utf-8"
            ),
            "print('ok')\n",
        )
        self.assertEqual(
            attempt.roots[0].manifest_sha256,
            snapshots[0].manifest_sha256,
        )
        self.assertFalse((self.workspace / ".lca-stage-proof").exists())

        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_text("keep", encoding="utf-8")
        (attempt.roots[0].staging_path / "external-link").symlink_to(outside)
        cleanup = self._cleanup(attempt)

        self.assertTrue(cleanup.verified)
        self.assertFalse(cleanup.unresolved)
        self.assertFalse(attempt.attempt_path.exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_staging_authority_must_be_private_and_outside_workspace(self) -> None:
        snapshots = self._snapshots()
        self.staging.chmod(0o755)
        with self.assertRaises(ContainerStagingError) as raised:
            stage_workspace_snapshots(
                staging_root=self.staging,
                snapshots=snapshots,
                destinations=(self.workspace, self.readable),
                workspace_roots=(self.workspace, self.readable),
                attempt_id=ATTEMPT_ID,
            )
        self.assertEqual(raised.exception.kind, "staging_authority_invalid")

    def test_output_plan_accepts_primary_text_changes_only(self) -> None:
        snapshots = self._snapshots()
        attempt = stage_workspace_snapshots(
            staging_root=self.staging,
            snapshots=snapshots,
            destinations=(self.workspace, self.readable),
            workspace_roots=(self.workspace, self.readable),
            attempt_id=ATTEMPT_ID,
        )
        primary = attempt.roots[0].staging_path
        (primary / "src/main.py").write_text("print('changed')\n", encoding="utf-8")
        (primary / "created.txt").write_text("created\n", encoding="utf-8")
        (primary / "src/main.py").chmod(0o644)

        observed = observe_staged_workspace_output(
            attempt,
            profile="workspace-write",
        )

        self.assertTrue(observed.verified)
        self.assertEqual(
            tuple(change.operation for change in observed.plan.changes),
            ("create", "replace"),
        )
        self.assertTrue(self._cleanup(attempt).verified)

    def test_output_rejects_readable_root_and_binary_changes(self) -> None:
        snapshots = self._snapshots()
        attempt = stage_workspace_snapshots(
            staging_root=self.staging,
            snapshots=snapshots,
            destinations=(self.workspace, self.readable),
            workspace_roots=(self.workspace, self.readable),
            attempt_id=ATTEMPT_ID,
        )
        (attempt.roots[1].staging_path / "requirements.txt").write_text(
            "changed\n",
            encoding="utf-8",
        )
        observed = observe_staged_workspace_output(
            attempt,
            profile="workspace-write",
        )
        self.assertEqual(observed.reason_code, "staging_readable_root_changed")
        self.assertTrue(self._cleanup(attempt).verified)

        attempt = stage_workspace_snapshots(
            staging_root=self.staging,
            snapshots=snapshots,
            destinations=(self.workspace, self.readable),
            workspace_roots=(self.workspace, self.readable),
            attempt_id=ATTEMPT_ID,
        )
        (attempt.roots[0].staging_path / "binary.dat").write_bytes(b"\xff")
        observed = observe_staged_workspace_output(
            attempt,
            profile="workspace-write",
        )
        self.assertEqual(
            observed.reason_code,
            "staging_output_changed_file_not_utf8",
        )
        self.assertTrue(self._cleanup(attempt).verified)

    def test_read_only_output_must_be_exact_noop(self) -> None:
        snapshots = self._snapshots()
        attempt = stage_workspace_snapshots(
            staging_root=self.staging,
            snapshots=snapshots,
            destinations=(self.workspace, self.readable),
            workspace_roots=(self.workspace, self.readable),
            attempt_id=ATTEMPT_ID,
        )
        noop = observe_staged_workspace_output(attempt, profile="read-only")
        self.assertTrue(noop.verified)
        self.assertIsNone(noop.plan)
        (attempt.roots[0].staging_path / "src/main.py").write_text(
            "changed\n",
            encoding="utf-8",
        )
        changed = observe_staged_workspace_output(attempt, profile="read-only")
        self.assertEqual(
            changed.reason_code,
            "staging_read_only_workspace_changed",
        )
        self.assertTrue(self._cleanup(attempt).verified)

        nested = self.workspace / "staging"
        nested.mkdir(mode=0o700)
        with self.assertRaises(ContainerStagingError) as raised:
            stage_workspace_snapshots(
                staging_root=nested,
                snapshots=snapshots,
                destinations=(self.workspace, self.readable),
                workspace_roots=(self.workspace, self.readable),
                attempt_id=ATTEMPT_ID,
            )
        self.assertEqual(raised.exception.kind, "staging_authority_invalid")

    def test_existing_attempt_is_never_adopted_or_removed(self) -> None:
        snapshots = self._snapshots()
        first = stage_workspace_snapshots(
            staging_root=self.staging,
            snapshots=snapshots,
            destinations=(self.workspace, self.readable),
            workspace_roots=(self.workspace, self.readable),
            attempt_id=ATTEMPT_ID,
        )
        with self.assertRaises(ContainerStagingError) as raised:
            stage_workspace_snapshots(
                staging_root=self.staging,
                snapshots=snapshots,
                destinations=(self.workspace, self.readable),
                workspace_roots=(self.workspace, self.readable),
                attempt_id=ATTEMPT_ID,
            )
        self.assertEqual(raised.exception.kind, "staging_authority_busy")
        self.assertTrue(first.attempt_path.exists())
        self.assertTrue(self._cleanup(first).verified)

    def test_attempt_directory_replace_restore_invalidates_staging_authority(
        self,
    ) -> None:
        attempt = stage_workspace_snapshots(
            staging_root=self.staging,
            snapshots=self._snapshots(),
            destinations=(self.workspace, self.readable),
            workspace_roots=(self.workspace, self.readable),
            attempt_id=ATTEMPT_ID,
        )
        moved = attempt.attempt_path.with_name(f"{ATTEMPT_ID}-moved")
        time.sleep(0.001)
        attempt.attempt_path.rename(moved)
        attempt.attempt_path.mkdir(mode=0o700)
        attempt.attempt_path.rmdir()
        moved.rename(attempt.attempt_path)

        self.assertFalse(attempt.authority_is_current())
        self.assertFalse(cleanup_staging_attempt(attempt).verified)

    def test_output_observation_parent_exception_still_cleans_staging(self) -> None:
        with (
            patch(
                "local_agent.execution.container_staging."
                "observe_staged_workspace_output",
                side_effect=RuntimeError("injected output observation failure"),
            ),
            self.assertRaises(ContainerStagingError) as raised,
        ):
            run_staged_workspace_operation(
                staging_root=self.staging,
                workspace_roots=(self.workspace, self.readable),
                workspace_roots_revision=3,
                attempt_id=ATTEMPT_ID,
                profile="workspace-write",
                operation=lambda attempt: attempt.attempt_id,
                cleanup_authorized=lambda value: True,
            )

        self.assertEqual(raised.exception.kind, "staging_output_parent_exception")
        self.assertTrue(raised.exception.cleanup_verified)
        self.assertEqual(
            {path.name for path in self.staging.iterdir()},
            {".lca-staging-journal", ".lca-staging.lock"},
        )

    def test_authority_lease_serializes_sibling_attempts(self) -> None:
        first = stage_workspace_snapshots(
            staging_root=self.staging,
            snapshots=self._snapshots(),
            destinations=(self.workspace, self.readable),
            workspace_roots=(self.workspace, self.readable),
            attempt_id=ATTEMPT_ID,
        )

        with self.assertRaises(ContainerStagingError) as raised:
            stage_workspace_snapshots(
                staging_root=self.staging,
                snapshots=self._snapshots(),
                destinations=(self.workspace, self.readable),
                workspace_roots=(self.workspace, self.readable),
                attempt_id="b" * 32,
            )

        self.assertEqual(raised.exception.kind, "staging_authority_busy")
        self.assertTrue(first.authority_is_current())
        self.assertTrue(self._cleanup(first).verified)
        second = stage_workspace_snapshots(
            staging_root=self.staging,
            snapshots=self._snapshots(),
            destinations=(self.workspace, self.readable),
            workspace_roots=(self.workspace, self.readable),
            attempt_id="b" * 32,
        )
        self.assertTrue(self._cleanup(second).verified)

    def test_prepared_restart_record_is_cleaned_without_container_recovery(self) -> None:
        attempt = stage_workspace_snapshots(
            staging_root=self.staging,
            snapshots=self._snapshots(),
            destinations=(self.workspace, self.readable),
            workspace_roots=(self.workspace, self.readable),
            attempt_id=ATTEMPT_ID,
        )
        attempt.lifecycle.lease.close()

        recovered = recover_staging_authority(
            staging_root=self.staging,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
        )

        self.assertTrue(recovered.ready)
        self.assertFalse(recovered.unresolved)
        self.assertEqual(recovered.recovered_attempts, 1)
        self.assertFalse(attempt.attempt_path.exists())

    def test_reserved_restart_without_attempt_removes_only_the_journal(
        self,
    ) -> None:
        lifecycle = self._reserve_lifecycle()
        lifecycle.lease.close()

        recovered = recover_staging_authority(
            staging_root=self.staging,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
        )

        self.assertTrue(recovered.ready)
        self.assertEqual(recovered.recovered_attempts, 1)
        self.assertFalse((self.staging / ATTEMPT_ID).exists())
        self.assertEqual(
            tuple((self.staging / ".lca-staging-journal").iterdir()),
            (),
        )

    def test_reserved_restart_with_unidentified_attempt_fails_closed(
        self,
    ) -> None:
        lifecycle = self._reserve_lifecycle()
        (self.staging / ATTEMPT_ID).mkdir(mode=0o700)
        lifecycle.lease.close()

        recovered = recover_staging_authority(
            staging_root=self.staging,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
        )

        self.assertFalse(recovered.ready)
        self.assertEqual(
            recovered.reason_code,
            "staging_recovery_reserved_attempt_unidentified",
        )
        self.assertTrue((self.staging / ATTEMPT_ID).exists())

    def test_allocated_restart_cleans_exact_partial_attempt(self) -> None:
        lifecycle = self._reserve_lifecycle()
        attempt_path = self.staging / ATTEMPT_ID
        attempt_path.mkdir(mode=0o700)
        metadata = attempt_path.stat(follow_symlinks=False)
        allocated = mark_staging_allocated(
            lifecycle,
            attempt_identity=ContainerRootIdentity.from_stat(metadata),
            attempt_path_identity=ContainerDirectoryPathIdentity.from_stat(
                metadata
            ),
        )
        self.assertTrue(allocated.verified)
        (attempt_path / "partial.txt").write_text(
            "partial\n",
            encoding="utf-8",
        )
        lifecycle.lease.close()

        recovered = recover_staging_authority(
            staging_root=self.staging,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
        )

        self.assertTrue(recovered.ready)
        self.assertEqual(recovered.recovered_attempts, 1)
        self.assertFalse(attempt_path.exists())

    def test_container_closed_restart_record_recovers_exact_attempt(self) -> None:
        attempt = stage_workspace_snapshots(
            staging_root=self.staging,
            snapshots=self._snapshots(),
            destinations=(self.workspace, self.readable),
            workspace_roots=(self.workspace, self.readable),
            attempt_id=ATTEMPT_ID,
        )
        self.assertTrue(record_staging_container_absent(attempt).verified)
        attempt.lifecycle.lease.close()

        recovered = recover_staging_authority(
            staging_root=self.staging,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
        )

        self.assertTrue(recovered.ready)
        self.assertFalse(recovered.unresolved)
        self.assertEqual(recovered.recovered_attempts, 1)
        self.assertFalse(attempt.attempt_path.exists())
        self.assertEqual(
            tuple((self.staging / ".lca-staging-journal").iterdir()),
            (),
        )

    def test_interrupted_cleanup_resumes_by_exact_attempt_identity(self) -> None:
        attempt = stage_workspace_snapshots(
            staging_root=self.staging,
            snapshots=self._snapshots(),
            destinations=(self.workspace, self.readable),
            workspace_roots=(self.workspace, self.readable),
            attempt_id=ATTEMPT_ID,
        )
        self.assertTrue(record_staging_container_absent(attempt).verified)
        with patch(
            "local_agent.execution.container_staging_lifecycle."
            "remove_directory_contents",
            side_effect=OSError("controlled cleanup failure"),
        ):
            cleanup = cleanup_staging_attempt(attempt)
        self.assertFalse(cleanup.verified)
        self.assertTrue(cleanup.unresolved)

        recovered = recover_staging_authority(
            staging_root=self.staging,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
        )
        self.assertTrue(recovered.ready)
        self.assertFalse(recovered.unresolved)
        self.assertFalse(attempt.attempt_path.exists())

    def test_malformed_journal_and_orphan_attempt_fail_closed(self) -> None:
        ready = recover_staging_authority(
            staging_root=self.staging,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
        )
        self.assertTrue(ready.ready)
        journal = self.staging / ".lca-staging-journal" / f"{ATTEMPT_ID}.json"
        journal.write_text("{}\n", encoding="utf-8")
        malformed = recover_staging_authority(
            staging_root=self.staging,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
        )
        self.assertFalse(malformed.ready)
        self.assertTrue(malformed.unresolved)
        journal.unlink()
        (self.staging / ATTEMPT_ID).mkdir(mode=0o700)
        orphan = recover_staging_authority(
            staging_root=self.staging,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
        )
        self.assertFalse(orphan.ready)
        self.assertEqual(
            orphan.reason_code,
            "staging_recovery_orphan_attempt",
        )

    def test_unverified_container_cleanup_retains_prepared_attempt(self) -> None:
        captured = []

        def create_possible(attempt):
            captured.append(attempt)
            transition = record_staging_create_possible(
                attempt,
                self._container_binding(),
            )
            self.assertTrue(transition.verified)
            return "unresolved"

        result = run_staged_workspace_operation(
            staging_root=self.staging,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            attempt_id=ATTEMPT_ID,
            profile="workspace-write",
            operation=create_possible,
            cleanup_authorized=lambda value: False,
        )

        self.assertFalse(result.cleanup.verified)
        self.assertEqual(
            result.output.reason_code,
            "staging_output_container_cleanup_unverified",
        )
        self.assertTrue(captured[0].attempt_path.exists())
        recovered = recover_staging_authority(
            staging_root=self.staging,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
        )
        self.assertFalse(recovered.ready)
        self.assertEqual(
            recovered.reason_code,
            "staging_recovery_container_unresolved",
        )

    def test_create_possible_restart_cleans_only_after_verified_absence(
        self,
    ) -> None:
        attempt = stage_workspace_snapshots(
            staging_root=self.staging,
            snapshots=self._snapshots(),
            destinations=(self.workspace, self.readable),
            workspace_roots=(self.workspace, self.readable),
            attempt_id=ATTEMPT_ID,
        )
        binding = self._container_binding()
        self.assertTrue(
            record_staging_create_possible(attempt, binding).verified
        )
        attempt.lifecycle.lease.close()
        observed = []

        recovered = recover_staging_authority(
            staging_root=self.staging,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            recover_container=lambda current, _state: (
                observed.append(current)
                or ContainerStagingContainerRecoveryResult(
                    "cleanup_verified_absent",
                    True,
                    False,
                )
            ),
        )

        self.assertEqual(observed, [binding])
        self.assertTrue(recovered.ready)
        self.assertEqual(recovered.recovered_attempts, 1)
        self.assertFalse(attempt.attempt_path.exists())

    def test_create_possible_restart_retains_attempt_when_recovery_fails(
        self,
    ) -> None:
        attempt = stage_workspace_snapshots(
            staging_root=self.staging,
            snapshots=self._snapshots(),
            destinations=(self.workspace, self.readable),
            workspace_roots=(self.workspace, self.readable),
            attempt_id=ATTEMPT_ID,
        )
        self.assertTrue(
            record_staging_create_possible(
                attempt,
                self._container_binding(),
            ).verified
        )
        attempt.lifecycle.lease.close()

        recovered = recover_staging_authority(
            staging_root=self.staging,
            workspace_roots=(self.workspace, self.readable),
            workspace_roots_revision=3,
            recover_container=lambda _binding, _state: (
                ContainerStagingContainerRecoveryResult(
                    "staging_recovery_absence_unverified_exhausted",
                    False,
                    True,
                )
            ),
        )

        self.assertFalse(recovered.ready)
        self.assertTrue(recovered.unresolved)
        self.assertEqual(
            recovered.reason_code,
            "staging_recovery_absence_unverified_exhausted",
        )
        self.assertTrue(attempt.attempt_path.exists())

    def _container_binding(self) -> ContainerStagingContainerBinding:
        identity = ContainerFileIdentity.from_stat(self.workspace.stat())
        return ContainerStagingContainerBinding(
            instance_name=f"lca-{ATTEMPT_ID}",
            prep_instance_name=f"lca-{ATTEMPT_ID}-prep",
            volume_names=(
                f"lca-{ATTEMPT_ID}-root-0000",
                f"lca-{ATTEMPT_ID}-root-0001",
            ),
            runtime_image=f"sha256:{'b' * 64}",
            executable=self.workspace,
            executable_sha256="a" * 64,
            socket_path=self.workspace,
            socket_identity=identity,
            client_config_directory=self.readable,
            client_config_identity=ContainerFileIdentity.from_stat(
                self.readable.stat()
            ),
            gate_image_reference=f"sha256:{'b' * 64}",
            gate_image_digest=f"sha256:{'b' * 64}",
        )


if __name__ == "__main__":
    unittest.main()

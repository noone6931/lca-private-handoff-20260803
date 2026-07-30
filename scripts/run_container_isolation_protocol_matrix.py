#!/usr/bin/env python3
"""Run the deterministic T-273 container isolation protocol matrix."""

from __future__ import annotations

import io
import json
import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


CASES = (
    (
        "trusted-authority-effective-group",
        "tests.test_container_isolation_backend.ContainerIsolationBackendTests."
        "test_effective_group_writable_authority_is_rejected_even_when_user_owned",
    ),
    (
        "probe-fail-closed",
        "tests.test_container_isolation_backend.ContainerIsolationBackendTests."
        "test_probe_correlation_timeout_and_identity_change_fail_closed",
    ),
    (
        "inspect-contract-mismatch",
        "tests.test_container_isolation_backend.ContainerIsolationBackendTests."
        "test_running_inspect_rejects_environment_command_mount_and_limits",
    ),
    (
        "ambiguous-create-retains-obligation",
        "tests.test_container_isolation_backend.ContainerIsolationBackendTests."
        "test_ambiguous_create_never_turns_finite_absence_into_cleanup_proof",
    ),
    (
        "create-parent-failure-recovery",
        "tests.test_container_runtime.ContainerRuntimeTests."
        "test_create_parent_exception_recovers_and_closes_owned_instance",
    ),
    (
        "remove-nonzero-exact-absence",
        "tests.test_container_isolation_backend.ContainerIsolationBackendTests."
        "test_cleanup_requires_exact_absence_even_when_remove_fails",
    ),
    (
        "removal-check-parent-failure",
        "tests.test_container_runtime.ContainerRuntimeTests."
        "test_removal_check_parent_failure_is_typed_unresolved",
    ),
    (
        "recovery-exhaustion-unresolved",
        "tests.test_container_isolation_backend.ContainerIsolationBackendTests."
        "test_recovery_retry_exhaustion_keeps_cleanup_unverified",
    ),
    (
        "attempt-directory-aba",
        "tests.test_container_staging.ContainerStagingTests."
        "test_attempt_directory_replace_restore_invalidates_staging_authority",
    ),
    (
        "staging-output-parent-failure",
        "tests.test_container_staging.ContainerStagingTests."
        "test_output_observation_parent_exception_still_cleans_staging",
    ),
    (
        "same-inode-pretruncate-stale",
        "tests.test_rooted_text_transaction.RootedTextTransactionTests."
        "test_same_inode_concurrent_change_is_stale_before_truncate",
    ),
    (
        "parent-error-redaction",
        "tests.test_container_process.ContainerProcessTests."
        "test_parent_failure_is_typed_without_projecting_raw_error",
    ),
)


def main() -> int:
    results: list[dict[str, str]] = []
    loader = unittest.defaultTestLoader
    for name, test_id in CASES:
        started = time.monotonic()
        stream = io.StringIO()
        suite = loader.loadTestsFromName(test_id)
        outcome = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
        passed = (
            outcome.testsRun > 0
            and not outcome.errors
            and not outcome.failures
            and not outcome.skipped
            and not outcome.unexpectedSuccesses
        )
        detail = "verified" if passed else _bounded_detail(stream.getvalue())
        results.append(
            {
                "case": name,
                "evidence_kind": "deterministic-protocol",
                "status": "PASS" if passed else "FAIL",
                "detail": detail,
                "seconds": f"{time.monotonic() - started:.3f}",
            }
        )
    print(json.dumps(results, indent=2, sort_keys=True))
    passed_count = sum(item["status"] == "PASS" for item in results)
    print(
        f"T-273 deterministic protocol matrix: "
        f"{passed_count}/{len(results)} passed"
    )
    return 0 if passed_count == len(results) else 1


def _bounded_detail(value: str) -> str:
    compact = " ".join(value.split())
    return compact[:500] if compact else "test did not produce a passing result"


if __name__ == "__main__":
    raise SystemExit(main())

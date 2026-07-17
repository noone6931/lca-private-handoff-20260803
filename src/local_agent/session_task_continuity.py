from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TYPE_CHECKING

from .patch.anchored import PatchError, hash_text, resolve_workspace_path
from .task_contract import RequirementContract
from .tools.files import session_patch_records
from .tools.git import capture_git_baseline
from .verification_timeline import effective_workspace_write_paths

if TYPE_CHECKING:
    from .session.jsonl_store import JsonlSessionStore
    from .tool_observation import ToolResultSummary
    from .verification_plan import VerificationPlan


CONTINUITY_EVENT = "session_task_continuity"
CONTINUITY_VERSION = 1
MAX_PENDING_WRITES = 32
UNFINISHED_TERMINATIONS = frozenset({"interrupt", "budget", "length", "incomplete_delivery"})


@dataclass(frozen=True)
class PendingWrite:
    path: str
    content_sha256: str


@dataclass(frozen=True)
class PendingTaskContinuation:
    origin_run_id: str
    origin_termination: str
    head_revision: str | None
    writes: tuple[PendingWrite, ...]

    @property
    def write_paths(self) -> tuple[str, ...]:
        return tuple(write.path for write in self.writes)

    def snapshot(self) -> dict[str, object]:
        return {
            "status": "inherited",
            "origin_run_id": self.origin_run_id,
            "origin_termination": self.origin_termination,
            "pending_write_count": len(self.writes),
            "pending_write_paths": list(self.write_paths),
        }


class ContinuityRuntimePort(Protocol):
    _run: Any
    _session: JsonlSessionStore
    _tool_context: Any
    _workspace_context: Any


class SessionTaskContinuityLifecycle:
    """Own typed continuation of unfinished implementation verification obligations."""

    def __init__(self, runtime: ContinuityRuntimePort) -> None:
        self._runtime = runtime

    def resolve(
        self,
        current_contract: RequirementContract,
        git_baseline: dict[str, Any],
    ) -> tuple[RequirementContract, PendingTaskContinuation | None]:
        if current_contract.task_kind != "unclear":
            return current_contract, None
        payloads = self._runtime._session.load_event_payloads(CONTINUITY_EVENT, max_events=1)
        pending = _parse_pending(payloads[-1] if payloads else None)
        if pending is None or not self._pending_is_fresh(pending, git_baseline):
            return current_contract, None
        return _continuation_contract(current_contract), pending

    def revalidate(self, plan: VerificationPlan) -> bool:
        pending = plan.pending_task_continuation
        if pending is None:
            return True
        current = capture_git_baseline(self._runtime._workspace_context.primary)
        if self._pending_is_fresh(pending, current):
            return True
        plan.invalidate_continuation(
            "the carried workspace revision or file content changed after the unfinished run; restart from current evidence"
        )
        return False

    def finish(self, termination_reason: str) -> dict[str, object]:
        runtime = self._runtime
        plan = runtime._run.verification_plan
        contract = runtime._run.requirement_contract
        run_id = runtime._run.run_id or ""
        payload: dict[str, object] = {
            "version": CONTINUITY_VERSION,
            "status": "closed",
            "run_id": run_id,
            "termination_reason": termination_reason,
        }
        if (
            contract is not None
            and contract.task_kind == "code-implementation"
            and termination_reason in UNFINISHED_TERMINATIONS
            and not plan.continuation_invalid_reason
        ):
            writes = self._validated_pending_writes(plan)
            if writes:
                baseline = capture_git_baseline(runtime._workspace_context.primary)
                payload.update(
                    {
                        "status": "pending",
                        "task_kind": "code-implementation",
                        "head_revision": _head_revision(baseline),
                        "writes": [
                            {"path": write.path, "content_sha256": write.content_sha256}
                            for write in writes
                        ],
                    }
                )
        runtime._session.append(CONTINUITY_EVENT, payload)
        return _summary_payload(payload)

    def _validated_pending_writes(self, plan: VerificationPlan) -> tuple[PendingWrite, ...]:
        runtime = self._runtime
        current_paths = effective_workspace_write_paths(runtime._run.tool_choice_results)
        if current_paths:
            current_baseline = capture_git_baseline(runtime._workspace_context.primary)
            if _head_revision(runtime._run.git_baseline) != _head_revision(current_baseline):
                return ()
            pending = plan.pending_task_continuation
            if pending is not None and pending.head_revision != _head_revision(current_baseline):
                return ()
            paths = tuple(dict.fromkeys((*getattr(pending, "write_paths", ()), *current_paths)))
            return _writes_from_patch_records(runtime, paths)
        pending = plan.pending_task_continuation
        if pending is None:
            return ()
        baseline = capture_git_baseline(runtime._workspace_context.primary)
        return pending.writes if self._pending_is_fresh(pending, baseline) else ()

    def _pending_is_fresh(
        self,
        pending: PendingTaskContinuation,
        git_baseline: dict[str, Any],
    ) -> bool:
        current_head = _head_revision(git_baseline)
        if pending.head_revision != current_head:
            return False
        return all(self._write_is_fresh(write) for write in pending.writes)

    def _write_is_fresh(self, write: PendingWrite) -> bool:
        runtime = self._runtime
        try:
            path = resolve_workspace_path(runtime._workspace_context.primary, write.path, runtime._workspace_context.additional_roots)
            return path.is_file() and _sha256(path) == write.content_sha256
        except (OSError, PatchError):
            return False


def _continuation_contract(current: RequirementContract) -> RequirementContract:
    return RequirementContract(
        objective=current.objective,
        scope="Continue the latest validated unfinished code implementation in this session without discarding its workspace change.",
        acceptance_items=["Complete the validated unfinished workspace change against the current user direction."],
        evidence_requirements=["Read current code related to every carried write before delivery."],
        verification_requirements=[
            "Run a current post-write test or record a concrete block.",
            "Inspect the current net diff and complete deterministic patch review before delivery.",
        ],
        risk_notes=["Continuation is valid only while the recorded Git revision and file content hashes remain unchanged."],
        task_kind="code-implementation",
    )


def _parse_pending(payload: dict[str, Any] | None) -> PendingTaskContinuation | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != CONTINUITY_VERSION or payload.get("status") != "pending":
        return None
    if payload.get("task_kind") != "code-implementation":
        return None
    run_id = payload.get("run_id")
    termination = payload.get("termination_reason")
    head_revision = payload.get("head_revision")
    raw_writes = payload.get("writes")
    if not isinstance(run_id, str) or not run_id or termination not in UNFINISHED_TERMINATIONS:
        return None
    if head_revision is not None and not isinstance(head_revision, str):
        return None
    if not isinstance(raw_writes, list) or not 0 < len(raw_writes) <= MAX_PENDING_WRITES:
        return None
    writes: list[PendingWrite] = []
    for raw in raw_writes:
        if not isinstance(raw, dict):
            return None
        path = raw.get("path")
        digest = raw.get("content_sha256")
        if not isinstance(path, str) or not path.strip() or len(path) > 4096:
            return None
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            return None
        writes.append(PendingWrite(path, digest))
    if len({write.path for write in writes}) != len(writes):
        return None
    return PendingTaskContinuation(run_id, str(termination), head_revision, tuple(writes))


def _writes_from_patch_records(
    runtime: ContinuityRuntimePort,
    paths: tuple[str, ...],
) -> tuple[PendingWrite, ...]:
    if len(paths) > MAX_PENDING_WRITES:
        return ()
    records = session_patch_records(runtime._tool_context)[-256:]
    rolled_back = {
        str(record.get("patch_id"))
        for record in records
        if record.get("event") == "rollback" and record.get("patch_id")
    }
    active: dict[Path, dict[str, Any]] = {}
    for record in records:
        if record.get("event") != "apply" or record.get("id") in rolled_back:
            continue
        raw_path = record.get("path")
        if not isinstance(raw_path, str):
            continue
        try:
            resolved = resolve_workspace_path(
                runtime._workspace_context.primary,
                raw_path,
                runtime._workspace_context.additional_roots,
            )
        except PatchError:
            continue
        active[resolved] = record
    writes: list[PendingWrite] = []
    for raw_path in paths:
        try:
            resolved = resolve_workspace_path(
                runtime._workspace_context.primary,
                raw_path,
                runtime._workspace_context.additional_roots,
            )
            record = active.get(resolved)
            if record is None or not resolved.is_file():
                return ()
            text = resolved.read_bytes().decode("utf-8")
            if hash_text(text) != record.get("after_tag"):
                return ()
            writes.append(PendingWrite(raw_path, hashlib.sha256(resolved.read_bytes()).hexdigest()))
        except (OSError, UnicodeDecodeError, PatchError):
            return ()
    return tuple(writes)


def _head_revision(baseline: dict[str, Any]) -> str | None:
    value = baseline.get("head_revision")
    return value if isinstance(value, str) and value else None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _summary_payload(payload: dict[str, object]) -> dict[str, object]:
    writes = payload.get("writes")
    paths = [item.get("path") for item in writes if isinstance(item, dict)] if isinstance(writes, list) else []
    return {
        "status": payload.get("status"),
        "origin_run_id": payload.get("run_id"),
        "origin_termination": payload.get("termination_reason"),
        "pending_write_count": len(paths),
        "pending_write_paths": paths,
    }


__all__ = [
    "CONTINUITY_EVENT",
    "PendingTaskContinuation",
    "PendingWrite",
    "SessionTaskContinuityLifecycle",
]

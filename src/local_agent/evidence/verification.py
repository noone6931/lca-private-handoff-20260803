from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TYPE_CHECKING

from ..task_contract import RequirementContract
from .timeline import results_after_last_write
from .timeline import code_evidence_for_paths
from .timeline import successful_nonempty_git_diff_after_last_write
from .timeline import successful_nonempty_git_diff_for_paths
from .timeline import workspace_write_happened
from .timeline import effective_workspace_write_paths

if TYPE_CHECKING:
    from ..session.continuity import PendingTaskContinuation
    from .test_planner import TestPlan
    from .tool_observation import ToolResultSummary


PlanKind = Literal["acceptance", "evidence", "verification"]
PlanStatus = Literal["pending", "passed", "failed", "blocked", "skipped"]


@dataclass
class VerificationPlanItem:
    id: str
    kind: PlanKind
    description: str
    enforce_delivery: bool = False
    status: PlanStatus = "pending"
    evidence_refs: list[str] = field(default_factory=list)
    attempts: int = 0
    reason: str = "awaiting runtime evidence"

    def snapshot(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "description": self.description,
            "enforce_delivery": self.enforce_delivery,
            "status": self.status,
            "evidence_refs": list(self.evidence_refs),
            "attempt_count": self.attempts,
            "reason": self.reason,
        }


@dataclass
class VerificationPlan:
    """Runtime-owned delivery facts, intentionally distinct from business acceptance."""

    items: list[VerificationPlanItem] = field(default_factory=list)
    observed_results: int = 0
    pending_task_continuation: PendingTaskContinuation | None = None
    continuation_invalid_reason: str | None = None

    @classmethod
    def from_contract(
        cls,
        contract: RequirementContract | None,
        *,
        pending_task: PendingTaskContinuation | None = None,
    ) -> VerificationPlan:
        if contract is None or contract.task_kind != "code-implementation":
            return cls()
        items: list[VerificationPlanItem] = []
        # Contract items remain visible but never become passed from proxy facts.
        for kind, requirements in (
            ("acceptance", contract.acceptance_items),
            ("evidence", contract.evidence_requirements),
            ("verification", contract.verification_requirements),
        ):
            for index, requirement in enumerate(requirements, start=1):
                items.append(
                    VerificationPlanItem(
                        f"contract-{kind}-{index}",
                        kind,  # type: ignore[arg-type]
                        requirement,
                        reason="business-level requirement requires delivery review; runtime facts alone cannot mark it passed",
                    )
                )
        # These are the limited facts Runtime can prove without claiming business semantics.
        items.extend(
            (
                VerificationPlanItem("runtime-code-evidence", "evidence", "Relevant local code evidence was read or located.", True),
                VerificationPlanItem("runtime-current-diff", "verification", "Current net diff exists after the final effective write.", True),
                VerificationPlanItem("runtime-post-write-test", "verification", "A post-write test command completed, or Runtime recorded a concrete block.", True),
                VerificationPlanItem("runtime-review", "verification", "Deterministic post-diff reviewer completed without blocking findings.", True),
            )
        )
        return cls(items=items, pending_task_continuation=pending_task)

    @property
    def active(self) -> bool:
        return bool(self.items)

    @property
    def continuation_write_paths(self) -> tuple[str, ...]:
        paths = getattr(self.pending_task_continuation, "write_paths", ())
        return tuple(paths) if isinstance(paths, tuple) else ()

    def effective_write_paths(self, results: list[ToolResultSummary]) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.continuation_write_paths, *effective_workspace_write_paths(results))))

    def has_effective_write(self, results: list[ToolResultSummary]) -> bool:
        return bool(self.effective_write_paths(results))

    def results_after_effective_write(self, results: list[ToolResultSummary]) -> list[ToolResultSummary]:
        if workspace_write_happened(results):
            return list(results_after_last_write(results))
        return list(results) if self.continuation_write_paths else []

    def invalidate_continuation(self, reason: str) -> None:
        self.continuation_invalid_reason = reason
        for item in self.delivery_items():
            self._set(item.id, "blocked", reason, [])

    def coverage(self, *, delivery_only: bool = False) -> dict[str, int]:
        selected = [item for item in self.items if item.enforce_delivery or not delivery_only]
        counts = {status: 0 for status in ("pending", "passed", "failed", "blocked", "skipped")}
        for item in selected:
            counts[item.status] += 1
        counts["total"] = len(selected)
        return counts

    def business_acceptance_summary(self) -> dict[str, int]:
        """Report contract items separately from machine-verifiable delivery checks."""

        items = [item for item in self.items if not item.enforce_delivery]
        pending = sum(item.status == "pending" for item in items)
        return {"total": len(items), "unverified": pending, "blocked": sum(item.status == "blocked" for item in items)}

    def delivery_items(self) -> tuple[VerificationPlanItem, ...]:
        return tuple(item for item in self.items if item.enforce_delivery)

    def unresolved_delivery_items(self) -> tuple[VerificationPlanItem, ...]:
        return tuple(item for item in self.delivery_items() if item.status != "passed")

    def render_incomplete_terminal(self) -> str:
        unresolved = self.unresolved_delivery_items()
        if not unresolved:
            return ""
        lines = [
            "未完成/未验证：运行时交付检查没有全部闭环，因此不会把本轮描述为已完成。",
            "",
            "未闭环检查：",
        ]
        for item in unresolved:
            lines.append(f"- [{item.status}] {item.description}：{item.reason}")
        lines.append("请保留当前 session 后继续修复、重新验证，或由人工确认可接受的未验证项。")
        return "\n".join(lines)

    def snapshot(self) -> dict[str, object]:
        snapshot = {
            "business_acceptance": self.business_acceptance_summary(),
            "delivery_coverage": self.coverage(delivery_only=True),
            "items": [item.snapshot() for item in self.items],
        }
        if self.pending_task_continuation is not None:
            snapshot["session_task_continuity"] = self.pending_task_continuation.snapshot()
        if self.continuation_invalid_reason:
            snapshot["continuation_invalid_reason"] = self.continuation_invalid_reason
        return snapshot

    def observe(self, results: list[ToolResultSummary], *, test_plan: TestPlan | None = None) -> bool:
        if not self.active:
            return False
        changed = False
        new_results = results[self.observed_results :]
        self.observed_results = len(results)
        for result in new_results:
            changed |= self._record_attempt(result)

        write_paths = self.effective_write_paths(results)
        code_results = code_evidence_for_paths(results, write_paths, code_tool_names=_CODE_EVIDENCE_TOOLS)
        changed |= self._set(
            "runtime-code-evidence",
            "passed" if code_results else "pending",
            "code evidence is path-related to the current effective write"
            if code_results
            else "awaiting successful read/search/LSP evidence related to the current effective write",
            _refs(code_results),
        )

        has_current_write = workspace_write_happened(results)
        has_write = bool(write_paths)
        diff = (
            successful_nonempty_git_diff_after_last_write(results)
            if has_current_write
            else successful_nonempty_git_diff_for_paths(results, self.continuation_write_paths)
        )
        if diff is not None:
            changed |= self._set("runtime-current-diff", "passed", "post-write git_diff reports a non-empty current diff", _refs([diff]))
        elif has_write:
            changed |= self._set("runtime-current-diff", "pending", "awaiting a non-empty post-write git_diff; empty diff cannot prove delivery", [])

        tests = [result for result in self.results_after_effective_write(results) if result.name == "run_tests"]
        if not has_write:
            changed |= self._set("runtime-post-write-test", "pending", "awaiting an effective workspace write", [])
        elif tests:
            latest = tests[-1]
            if latest.is_error:
                status = "blocked" if _is_structured_denial(latest) else "failed"
                reason = "test execution was denied by approval policy" if status == "blocked" else "post-write test command failed"
            else:
                status, reason = "passed", "post-write test command succeeded"
            changed |= self._set("runtime-post-write-test", status, reason, _refs(tests))
        elif test_plan is not None and test_plan.blocked:
            changed |= self._set("runtime-post-write-test", "blocked", test_plan.reason, [])
        else:
            changed |= self._set("runtime-post-write-test", "pending", "awaiting post-write test execution", [])
        return changed

    def record_patch_review(self, *, passed: bool | None, reason: str, refs: list[str]) -> bool:
        status: PlanStatus = "skipped" if passed is None else ("passed" if passed else "failed")
        return self._set("runtime-review", status, reason, refs)

    def render_context(self) -> str:
        if not self.active:
            return ""
        lines = [
            "[Verification plan]",
            "Runtime-owned delivery state. Only tool results, write/diff timeline, and deterministic reviewer results may change statuses.",
            "Contract acceptance items remain pending unless a human or explicit business oracle validates them; do not describe runtime checks as proof of business completion.",
        ]
        if self.continuation_write_paths:
            lines.append(
                "- Session continuation: a prior unfinished write remains pending; old tests are not reused, and current read/test/diff/review evidence is required."
            )
        for item in self.items:
            refs = f" refs={','.join(item.evidence_refs)}" if item.evidence_refs else ""
            prefix = "delivery-check" if item.enforce_delivery else "contract"
            lines.append(f"- [{item.status}] {prefix} {item.id}: {item.description} ({item.reason}){refs}")
        return "\n".join(lines)

    def _record_attempt(self, result: ToolResultSummary) -> bool:
        watched = {
            "read_file": ("runtime-code-evidence",),
            "search_code": ("runtime-code-evidence",),
            "lsp_definition": ("runtime-code-evidence",),
            "lsp_references": ("runtime-code-evidence",),
            "lsp_symbols": ("runtime-code-evidence",),
            "lsp_workspace_symbols": ("runtime-code-evidence",),
            "lsp_document_symbols": ("runtime-code-evidence",),
            "run_tests": ("runtime-post-write-test",),
            "git_diff": ("runtime-current-diff", "runtime-review"),
        }
        changed = False
        for item_id in watched.get(result.name, ()):
            item = self._item(item_id)
            if item is not None:
                item.attempts += 1
                changed = True
        return changed

    def _set(self, item_id: str, status: PlanStatus, reason: str, refs: list[str]) -> bool:
        item = self._item(item_id)
        if item is None:
            return False
        next_refs = refs[-4:]
        if item.status == status and item.reason == reason and item.evidence_refs == next_refs:
            return False
        item.status, item.reason, item.evidence_refs = status, reason, next_refs
        return True

    def _item(self, item_id: str) -> VerificationPlanItem | None:
        return next((item for item in self.items if item.id == item_id), None)


_CODE_EVIDENCE_TOOLS = frozenset({"read_file", "search_code", "lsp_definition", "lsp_references", "lsp_symbols", "lsp_workspace_symbols", "lsp_document_symbols"})


def _refs(results: list[ToolResultSummary]) -> list[str]:
    return [f"{result.name}:{index}" for index, result in enumerate(results, start=1)][-4:]


def _is_structured_denial(result: ToolResultSummary) -> bool:
    return result.metadata.get("execution_status") == "denied" and result.metadata.get("denial_kind") == "approval"


__all__ = ["PlanKind", "PlanStatus", "VerificationPlan", "VerificationPlanItem"]

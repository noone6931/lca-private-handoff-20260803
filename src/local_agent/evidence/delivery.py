from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from ..tools.execution_metadata import ParsedExecutionMetadata
from ..tools.execution_metadata import parse_execution_metadata
from ..tools.observation import ToolResultSummary
from .verification import VerificationPlan


def render_delivery_report(plan: VerificationPlan, results: Sequence[ToolResultSummary]) -> str:
    """Render a terminal delivery record from Runtime facts, never model prose."""

    all_shell_executions = [
        result for result in results if result.name == "shell" and _execution(result) is not None
    ]
    changed_paths = plan.effective_write_paths(list(results))
    if not changed_paths:
        return _render_shell_only_report(all_shell_executions)
    coverage = plan.coverage(delivery_only=True)
    business = plan.business_acceptance_summary()
    lines = [
        "[Runtime delivery report]",
        "- changed_paths: " + ", ".join(changed_paths),
        "- delivery_checks: " + _render_counts(coverage),
        "- git_diff: " + _item_status(plan, "runtime-current-diff"),
        "- reviewer: " + _item_status(plan, "runtime-review"),
        f"- business_acceptance_unverified: {business['unverified']}/{business['total']}",
        "- tests:",
    ]
    tests = [result for result in plan.results_after_effective_write(list(results)) if result.name == "run_tests"]
    if not tests:
        lines.append("  - no post-write run_tests command was recorded")
    else:
        for result in tests[-3:]:
            command = result.metadata.get("executed_command")
            rendered_command = str(command) if isinstance(command, str) and command.strip() else "(command unavailable)"
            lines.append(f"  - {_test_status(result)}: `{rendered_command}`")
    shell_executions = [
        result
        for result in plan.results_after_effective_write(list(results))
        if result.name == "shell" and _execution(result) is not None
    ]
    if shell_executions:
        lines.append("- other_post_write_executions (not counted as the run_tests gate):")
        for result in shell_executions[-3:]:
            lines.append(f"  - {_render_execution(result)}")
    unresolved = plan.unresolved_delivery_items()
    if unresolved:
        lines.append("- remaining_delivery_checks:")
        for item in unresolved:
            lines.append(f"  - [{item.status}] {item.id}: {item.reason}")
    else:
        lines.append("- remaining_delivery_checks: none")
    return "\n".join(lines)


def _render_shell_only_report(executions: list[ToolResultSummary]) -> str:
    if not executions:
        return ""
    lines = [
        "[Runtime operation provenance]",
        "- patch_transaction_writes: none recorded",
        "- boundary: shell executions may have side effects but are not patch-journal mutation evidence",
        "- executions:",
    ]
    lines.extend(f"  - {_render_execution(result)}" for result in executions[-3:])
    return "\n".join(lines)


def _render_counts(counts: dict[str, int]) -> str:
    return ", ".join(
        f"{name}={counts.get(name, 0)}"
        for name in ("passed", "failed", "blocked", "pending", "skipped")
    )


def _item_status(plan: VerificationPlan, item_id: str) -> str:
    item = next((candidate for candidate in plan.delivery_items() if candidate.id == item_id), None)
    if item is None:
        return "not_applicable"
    return f"{item.status} ({item.reason})"


def _test_status(result: ToolResultSummary) -> str:
    status = result.metadata.get("execution_status")
    if status == "denied":
        return "denied"
    if status == "not_run":
        return "not_run"
    if status == "succeeded" or not result.is_error:
        return "passed"
    return "failed"


def _execution(result: ToolResultSummary) -> ParsedExecutionMetadata | None:
    execution = result.metadata.get("execution_v1")
    if not isinstance(execution, Mapping):
        return None
    return parse_execution_metadata(execution, tool_name=result.name)


def _render_execution(result: ToolResultSummary) -> str:
    execution = _execution(result)
    assert execution is not None
    digest = hashlib.sha256(execution.command.encode("utf-8")).hexdigest()[:12]
    status = "passed" if execution.outcome == "exited" and execution.exit_code == 0 else execution.outcome
    exit_text = f" exit={execution.exit_code}" if execution.exit_code is not None else ""
    return f"{status} shell{exit_text}; cwd={execution.cwd}; command_sha256={digest}"


__all__ = ["render_delivery_report"]

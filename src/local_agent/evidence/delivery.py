from __future__ import annotations

from collections.abc import Sequence

from ..tools.observation import ToolResultSummary
from .verification import VerificationPlan


def render_delivery_report(plan: VerificationPlan, results: Sequence[ToolResultSummary]) -> str:
    """Render a terminal delivery record from Runtime facts, never model prose."""

    changed_paths = plan.effective_write_paths(list(results))
    if not changed_paths:
        return ""
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
    unresolved = plan.unresolved_delivery_items()
    if unresolved:
        lines.append("- remaining_delivery_checks:")
        for item in unresolved:
            lines.append(f"  - [{item.status}] {item.id}: {item.reason}")
    else:
        lines.append("- remaining_delivery_checks: none")
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


__all__ = ["render_delivery_report"]

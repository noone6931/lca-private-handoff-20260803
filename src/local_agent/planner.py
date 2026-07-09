from __future__ import annotations

from typing import Literal

from .task_contract import RequirementContract
from .tool_choice_queue import CODE_EVIDENCE_TOOL_NAMES
from .tool_choice_queue import ToolResultSummary
from .tool_choice_queue import WRITE_TOOL_NAMES


PlannerPhase = Literal["not_applicable", "explore", "ready_to_implement", "verify"]


def planner_phase(
    contract: RequirementContract | None,
    *,
    prompt: str | None,
    tool_results: list[ToolResultSummary],
) -> PlannerPhase:
    if contract is None:
        return "not_applicable"
    if contract.task_kind != "code-implementation":
        return "not_applicable"
    if _workspace_write_happened(tool_results):
        return "verify"
    if _has_successful_code_evidence(tool_results):
        return "ready_to_implement"
    return "explore"


def render_planner_explore_context(
    contract: RequirementContract | None,
    *,
    prompt: str | None,
    tool_results: list[ToolResultSummary],
) -> str:
    phase = planner_phase(contract, prompt=prompt, tool_results=tool_results)
    if phase == "not_applicable":
        return ""
    objective = contract.objective if contract is not None else (prompt or "")
    lines = [
        "Planner / Explore",
        f"Current phase: {phase}",
        f"Objective: {objective}",
        "Two-stage delivery rule:",
        "- Explore first: inspect local requirement/code evidence, identify target files, likely tests, and risk before writing.",
        "- Implement second: once evidence is collected, make the smallest relevant patch, then verify and inspect git_diff.",
        "- If evidence shows the task is unsafe, out of scope, or belongs to another service, stop honestly instead of forcing a patch.",
    ]
    if phase == "explore":
        lines.extend(
            [
                "Phase instruction: do not write files yet. Use list_files/read_file/search_code/lsp_* and todo/ask_user when helpful.",
                "Before editing, know which file or behavior the evidence supports.",
            ]
        )
    elif phase == "ready_to_implement":
        lines.extend(
            [
                "Phase instruction: evidence exists. You may now patch only evidence-backed target files.",
                "Preview meaningful existing-file edits with apply_patch dry_run=true unless the user explicitly skipped preview.",
            ]
        )
    else:
        lines.extend(
            [
                "Phase instruction: workspace changes exist. Focus on run_tests or an explicit cannot-test reason, then git_diff.",
                "Final answer must summarize changed files, verification, diff attribution, and remaining risk.",
            ]
        )
    return "\n".join(lines)


def _has_successful_code_evidence(tool_results: list[ToolResultSummary]) -> bool:
    return any(
        result.name in CODE_EVIDENCE_TOOL_NAMES
        and not result.is_error
        for result in tool_results
    )


def _workspace_write_happened(tool_results: list[ToolResultSummary]) -> bool:
    return any(result.name in WRITE_TOOL_NAMES and _changed_workspace(result) for result in tool_results)


def _changed_workspace(result: ToolResultSummary) -> bool:
    if result.is_error:
        return False
    if result.changed is not None:
        return result.changed
    content = (result.content or "").lower()
    return not any(
        marker in content
        for marker in {
            "dry run",
            "dry_run",
            "file not changed",
            "no file changed",
            "not changed",
            "patch preview only",
            "preview only",
            "would be",
        }
    )


__all__ = [
    "PlannerPhase",
    "planner_phase",
    "render_planner_explore_context",
]

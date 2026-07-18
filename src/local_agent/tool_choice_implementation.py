from __future__ import annotations

from collections.abc import Iterable

from .tool_choice_decision import CODE_EVIDENCE_TOOL_NAMES
from .tool_choice_decision import PLANNER_EXPLORE_TOOL_NAMES
from .tool_choice_decision import ToolChoiceDecision
from .tool_choice_decision import _allowed_subset
from .tool_choice_decision import _compact
from .tool_choice_decision import _lower_text
from .tool_choice_decision import _successful_tool_result
from .tool_choice_decision import has_code_evidence
from .tool_observation import ToolResultSummary
from .tool_choice_task_classification import is_implementation_task
from .evidence.timeline import last_workspace_write_index
from .evidence.timeline import result_changed_workspace
from .evidence.timeline import successful_tool_after_last_write
from .evidence.timeline import workspace_write_happened
from .evidence.timeline import WRITE_TOOL_NAMES


CANDIDATE_STATE_TOOL_NAMES = frozenset(
    {
        "todo_add",
        "todo_read",
        "todo_update",
    }
)
CANDIDATE_DELIVERY_TOOL_NAMES = frozenset({"apply_patch", "read_file", *CANDIDATE_STATE_TOOL_NAMES})
CANDIDATE_REMEDIATION_TOOL_NAMES = frozenset({*CANDIDATE_DELIVERY_TOOL_NAMES, "read_file"})
CANDIDATE_TEST_TOOL_NAMES = frozenset({"run_tests", *CANDIDATE_STATE_TOOL_NAMES})
CANDIDATE_DIFF_TOOL_NAMES = frozenset({"git_diff", *CANDIDATE_STATE_TOOL_NAMES})
MAX_CANDIDATE_READ_REVISITS = 4
MAX_CANDIDATE_PATCH_PREVIEW_FAILURES = 3
POST_DIFF_REMEDIATION_TOOL_NAMES = frozenset(
    {
        "apply_patch",
        "git_diff",
        "lsp_definition",
        "lsp_references",
        "read_file",
        "rollback_patch",
        "run_tests",
        "search_code",
        "write_file",
    }
)

AUTONOMOUS_SMALL_CHANGE_KEYWORDS = frozenset(
    {
        "choose a small",
        "choose an extremely small",
        "find a small",
        "pick a small",
        "自行挑选",
        "自己找一个",
        "找一个很小",
        "找一个极小",
        "自主选点",
    }
)

CANNOT_TEST_MARKERS = frozenset(
    {
        "cannot run tests",
        "cannot test",
        "can't run tests",
        "can't test",
        "no test command",
        "tests not run",
        "unable to run tests",
        "没有测试命令",
        "不能测试",
        "未运行测试",
        "无法测试",
        "无法运行测试",
    }
)


def evaluate_implementation_phase(
    *,
    task_kind: str,
    prompt: str,
    seen_tool_names: set[str],
    results: tuple[ToolResultSummary, ...],
    allowed_tools: frozenset[str],
    evidence_preferred: tuple[str, ...],
) -> ToolChoiceDecision | None:
    if _implementation_needs_explore_before_write(task_kind, prompt, seen_tool_names, results):
        return ToolChoiceDecision(
            steering_required=True,
            allowed_tool_names=_allowed_subset(PLANNER_EXPLORE_TOOL_NAMES, allowed_tools),
            reason=(
                "implementation_explore missing: implementation tasks must inspect local requirement/code evidence "
                "and identify target files before write tools are enabled."
            ),
            rule_id="implementation_explore",
            missing_requirements=("planner_explore_evidence",),
            preferred_tool_names=evidence_preferred,
        )

    candidate_paths = autonomous_small_change_candidate_paths(task_kind, prompt, results)
    if candidate_paths:
        wrote_workspace = _workspace_write_happened(seen_tool_names, results)
        preview_succeeded = _has_successful_patch_preview(results)
        preview_failed = any(result.name == "apply_patch" and result.is_error for result in results)
        if not wrote_workspace:
            preview_failure_count = _candidate_patch_failure_count(results)
            if not preview_succeeded and preview_failure_count >= MAX_CANDIDATE_PATCH_PREVIEW_FAILURES:
                return ToolChoiceDecision(
                    steering_required=False,
                    allowed_tool_names=frozenset(),
                    reason="autonomous_small_change_candidate patch retry budget exhausted.",
                    rule_id="autonomous_small_change_patch_retry_exhausted",
                    missing_requirements=("valid_patch_preview",),
                    stop_message=(
                        "Stopped before changing files: the autonomous candidate produced "
                        f"{preview_failure_count} invalid apply_patch attempts without a successful preview. "
                        "No workspace change was applied; rerun with a more specific target or inspect the candidate manually."
                    ),
                )
            if preview_failed and not preview_succeeded:
                allowed = CANDIDATE_REMEDIATION_TOOL_NAMES
                reason = (
                    "autonomous_small_change_candidate preview needs repair: the anchored preview failed. "
                    "Only re-read the exact candidate file if necessary, then retry apply_patch dry_run=true."
                )
                preferred = ("read_file", "apply_patch")
            elif preview_succeeded:
                allowed = CANDIDATE_DELIVERY_TOOL_NAMES
                reason = (
                    "autonomous_small_change_candidate preview succeeded: stop exploration and apply the same "
                    "small patch without dry_run before testing."
                )
                preferred = ("apply_patch",)
            else:
                allowed = CANDIDATE_DELIVERY_TOOL_NAMES
                reason = (
                    "autonomous_small_change_candidate committed: sufficient target and test evidence exists. "
                    "Stop broad exploration and use apply_patch dry_run=true before any write, test, or diff."
                )
                preferred = ("apply_patch",)
            return ToolChoiceDecision(
                steering_required=True,
                allowed_tool_names=_allowed_subset(allowed, allowed_tools),
                reason=reason,
                rule_id="autonomous_small_change_candidate",
                missing_requirements=("patch_preview_or_write",),
                preferred_tool_names=preferred,
                scoped_read_paths=candidate_paths,
                scoped_read_budget=MAX_CANDIDATE_READ_REVISITS,
            )
        if not _has_verification_after_last_write("run_tests", seen_tool_names, results):
            return ToolChoiceDecision(
                steering_required=True,
                allowed_tool_names=_allowed_subset(CANDIDATE_TEST_TOOL_NAMES, allowed_tools),
                reason=(
                    "autonomous_small_change_candidate verification pending: the patch was written. "
                    "Run the focused tests before reading more files or inspecting the diff."
                ),
                rule_id="autonomous_small_change_test",
                missing_requirements=("run_tests",),
                preferred_tool_names=("run_tests",),
            )
        if not _has_verification_after_last_write("git_diff", seen_tool_names, results):
            return ToolChoiceDecision(
                steering_required=True,
                allowed_tool_names=_allowed_subset(CANDIDATE_DIFF_TOOL_NAMES, allowed_tools),
                reason=(
                    "autonomous_small_change_candidate diff pending: tests have run after the write. "
                    "Inspect git_diff before the final report."
                ),
                rule_id="autonomous_small_change_diff",
                missing_requirements=("git_diff",),
                preferred_tool_names=("git_diff",),
            )

    implementation_missing = _implementation_missing_requirements(task_kind, prompt, seen_tool_names, results)
    if implementation_missing:
        allowed = []
        if "git_diff" in implementation_missing:
            allowed.append("git_diff")
        if "run_tests_or_cannot_test_explanation" in implementation_missing:
            # A first git_diff can expose a reviewer finding that requires another focused
            # edit or a test addition. Keep verification pending while allowing that repair;
            # CompletionAudit remains the hard final-answer gate.
            if "git_diff" not in implementation_missing:
                allowed.extend(POST_DIFF_REMEDIATION_TOOL_NAMES)
            else:
                allowed.append("run_tests")
        return ToolChoiceDecision(
            steering_required=True,
            allowed_tool_names=_allowed_subset(allowed, allowed_tools),
            reason=(
                "implementation_final_hygiene missing: implementation tasks need git_diff before final; "
                "after a write they also need run_tests or an explicit cannot-test explanation. Once a diff exists, "
                "focused repair/test tools remain available so post-diff reviewer findings can be fixed before verification."
            ),
            rule_id="implementation_final_hygiene",
            missing_requirements=tuple(implementation_missing),
            preferred_tool_names=tuple(allowed),
        )
    return None


def _implementation_needs_explore_before_write(
    task_kind: str,
    prompt: str,
    seen_tool_names: set[str],
    results: tuple[ToolResultSummary, ...],
) -> bool:
    if not is_implementation_task(task_kind, prompt):
        return False
    if _workspace_write_happened(seen_tool_names, results):
        return False
    return not has_code_evidence(seen_tool_names, results)


def autonomous_small_change_candidate_paths(
    task_kind: str,
    prompt: str,
    results: tuple[ToolResultSummary, ...] | list[ToolResultSummary],
) -> tuple[str, ...]:
    """Return a narrow candidate only for an explicitly autonomous tiny-change task."""

    if not is_implementation_task(task_kind, prompt):
        return ()
    text = _lower_text(prompt)
    if not any(marker in text for marker in AUTONOMOUS_SMALL_CHANGE_KEYWORDS):
        return ()
    paths = []
    for result in results:
        if result.name != "read_file" or result.is_error or not result.path:
            continue
        path = result.path
        if path not in paths:
            paths.append(path)
    has_test = any(_is_test_path(path) for path in paths)
    has_source = any(not _is_test_path(path) for path in paths)
    if not (has_test and has_source):
        return ()
    return tuple(paths[-4:])


def _is_test_path(path: str) -> bool:
    lowered = path.replace("\\", "/").lower()
    name = lowered.rsplit("/", 1)[-1]
    return "/test/" in lowered or "/tests/" in lowered or name.startswith("test_") or name.endswith(("_test.py", "test.java", "test.ts", "test.js"))


def _implementation_missing_requirements(
    task_kind: str,
    prompt: str,
    seen_tool_names: set[str],
    results: tuple[ToolResultSummary, ...],
) -> list[str]:
    if not is_implementation_task(task_kind, prompt):
        return []
    missing: list[str] = []
    if not _workspace_write_happened(seen_tool_names, results):
        return missing
    if not _has_verification_after_last_write("git_diff", seen_tool_names, results):
        missing.append("git_diff")
    if (
        not _has_verification_after_last_write("run_tests", seen_tool_names, results)
        and not _has_cannot_test_explanation_after_last_write(results)
    ):
        missing.append("run_tests_or_cannot_test_explanation")
    return missing


def _has_successful_tool(name: str, seen_tool_names: set[str], results: tuple[ToolResultSummary, ...]) -> bool:
    matching_results = [result for result in results if result.name == name]
    if matching_results:
        return any(_successful_tool_result(result) for result in matching_results)
    return name in seen_tool_names


def _workspace_write_happened(seen_tool_names: set[str], results: tuple[ToolResultSummary, ...]) -> bool:
    if results:
        return workspace_write_happened(results)
    return bool(seen_tool_names.intersection(WRITE_TOOL_NAMES))


def _has_successful_patch_preview(results: tuple[ToolResultSummary, ...]) -> bool:
    return any(
        result.name == "apply_patch" and _successful_tool_result(result) and not result_changed_workspace(result)
        for result in results
    )


def _candidate_patch_failure_count(results: tuple[ToolResultSummary, ...]) -> int:
    return sum(result.name == "apply_patch" and result.is_error for result in results)


def _has_verification_after_last_write(
    name: str,
    seen_tool_names: set[str],
    results: tuple[ToolResultSummary, ...],
) -> bool:
    if last_workspace_write_index(results) >= 0:
        return successful_tool_after_last_write(results, name)
    return _has_successful_tool(name, seen_tool_names, results)


def _has_cannot_test_explanation_after_last_write(results: tuple[ToolResultSummary, ...]) -> bool:
    after_write = results[last_workspace_write_index(results) + 1 :]
    combined = _lower_text("\n".join(result.content for result in after_write))
    return any(marker in combined for marker in CANNOT_TEST_MARKERS)

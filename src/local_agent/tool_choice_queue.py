from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


DEFAULT_TOOL_NAMES = frozenset(
    {
        "apply_patch",
        "ask_user",
        "git_diff",
        "git_status",
        "learn",
        "list_files",
        "lsp_definition",
        "lsp_diagnostics",
        "lsp_document_symbols",
        "lsp_references",
        "lsp_status",
        "lsp_symbols",
        "lsp_workspace_symbols",
        "memory_read",
        "memory_write",
        "read_file",
        "rollback_patch",
        "run_tests",
        "search_code",
        "shell",
        "todo_add",
        "todo_read",
        "todo_update",
        "write_file",
    }
)

LSP_EVIDENCE_TOOL_NAMES = frozenset(
    {
        "lsp_definition",
        "lsp_diagnostics",
        "lsp_document_symbols",
        "lsp_references",
        "lsp_symbols",
        "lsp_workspace_symbols",
    }
)
CODE_EVIDENCE_TOOL_NAMES = frozenset({"read_file", "search_code", *LSP_EVIDENCE_TOOL_NAMES})
CODE_EVIDENCE_ALLOWED_TOOL_NAMES = frozenset({"list_files", *CODE_EVIDENCE_TOOL_NAMES})
REQUIREMENT_DOC_TOOL_NAMES = frozenset({"ask_user", "list_files", "read_file", "search_code"})
PLANNER_EXPLORE_TOOL_NAMES = frozenset(
    {
        "ask_user",
        "git_diff",
        "git_status",
        "list_files",
        "lsp_definition",
        "lsp_diagnostics",
        "lsp_document_symbols",
        "lsp_references",
        "lsp_status",
        "lsp_symbols",
        "lsp_workspace_symbols",
        "read_file",
        "search_code",
        "todo_add",
        "todo_read",
        "todo_update",
    }
)
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
WRITE_TOOL_NAMES = frozenset({"apply_patch", "rollback_patch", "write_file"})
READ_ONLY_FORBIDDEN_TOOL_NAMES = frozenset(
    {
        "apply_patch",
        "learn",
        "memory_write",
        "rollback_patch",
        "run_tests",
        "shell",
        "write_file",
    }
)

NO_SPECULATION_KEYWORDS = frozenset(
    {
        "do not guess",
        "don't guess",
        "no speculation",
        "不靠猜",
        "不要猜",
        "不要推测",
        "不需要推测",
        "别猜",
        "别推测",
    }
)
CODE_EVIDENCE_KEYWORDS = frozenset(
    {
        "code evidence",
        "source evidence",
        "source-code evidence",
        "代码依据",
        "代码里",
        "代码中",
        "代码证据",
        "源码",
        "源码中",
    }
)
READ_ONLY_KEYWORDS = frozenset(
    {
        "do not edit",
        "do not modify",
        "don't edit",
        "don't modify",
        "no changes",
        "no edits",
        "read only",
        "read-only",
        "readonly",
        "不要改",
        "不要修改",
        "不做修改",
        "只分析",
        "只读",
        "禁止修改",
    }
)
IMPLEMENTATION_KEYWORDS = frozenset(
    {
        "bugfix",
        "change",
        "code edit",
        "edit",
        "fix",
        "implement",
        "implementation",
        "modify",
        "patch",
        "修复",
        "修改",
        "实现",
        "改造",
        "新增",
        "编码",
    }
)
REQUIREMENT_DOC_KEYWORDS = frozenset(
    {
        "allowed-dir",
        "allowed directory",
        "allowed_dirs",
        "requirement doc",
        "requirement document",
        "requirements doc",
        "requirements document",
        "spec doc",
        "specification",
        "需求文档",
        "需求说明",
        "需求目录",
        "外部需求",
    }
)
DOC_READ_MARKERS = frozenset(
    {
        "allowed-dir",
        "allowed directory",
        "allowed_dirs",
        "requirement",
        "requirements",
        "spec",
        "specification",
        "需求",
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
NO_WRITE_RESULT_MARKERS = frozenset(
    {
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


@dataclass(frozen=True)
class ToolResultSummary:
    name: str
    content: str = ""
    is_error: bool = False
    useless: bool = False
    path: str | None = None
    changed: bool | None = None


@dataclass(frozen=True)
class ToolChoiceDecision:
    steering_required: bool
    allowed_tool_names: frozenset[str]
    reason: str
    rule_id: str | None = None
    missing_requirements: tuple[str, ...] = ()
    preferred_tool_names: tuple[str, ...] = ()

    @property
    def needs_steering(self) -> bool:
        return self.steering_required

    @property
    def allowed_tools(self) -> frozenset[str]:
        return self.allowed_tool_names


class RequiredToolGate:
    def evaluate(
        self,
        *,
        task_kind: str,
        prompt: str,
        tool_names: Iterable[str] | None = None,
        tool_results: Iterable[ToolResultSummary | Mapping[str, Any] | str] | None = None,
        available_tool_names: Iterable[str] | None = None,
    ) -> ToolChoiceDecision:
        return evaluate_tool_choice_state(
            task_kind=task_kind,
            prompt=prompt,
            tool_names=tool_names,
            tool_results=tool_results,
            available_tool_names=available_tool_names,
        )


class ToolChoiceQueue(RequiredToolGate):
    """Small deterministic gate prototype for future runtime integration."""


def evaluate_tool_choice_state(
    *,
    task_kind: str,
    prompt: str,
    tool_names: Iterable[str] | None = None,
    tool_results: Iterable[ToolResultSummary | Mapping[str, Any] | str] | None = None,
    available_tool_names: Iterable[str] | None = None,
) -> ToolChoiceDecision:
    results = tuple(_normalize_tool_result(result) for result in (tool_results or ()))
    seen_tool_names = _tool_name_set(tool_names, results)
    all_tools = _available_tool_names(available_tool_names)
    read_only = _is_read_only_task(task_kind, prompt)
    allowed_tools = all_tools - READ_ONLY_FORBIDDEN_TOOL_NAMES if read_only else all_tools

    if _requires_requirement_doc_read(task_kind, prompt) and not _has_requirement_doc_read(prompt, results):
        return ToolChoiceDecision(
            steering_required=True,
            allowed_tool_names=_allowed_subset(REQUIREMENT_DOC_TOOL_NAMES, allowed_tools),
            reason=(
                "requirement_document_read missing: requirement/allowed-dir tasks must read the requirement "
                "document before broad tools are enabled."
            ),
            rule_id="requirement_document_read",
            missing_requirements=("requirement_document_read",),
            preferred_tool_names=("read_file",),
        )

    evidence_preferred = _preferred_evidence_tools(results)
    if _needs_code_evidence(task_kind, prompt) and not _has_code_evidence(seen_tool_names, results):
        return ToolChoiceDecision(
            steering_required=True,
            allowed_tool_names=_allowed_subset(CODE_EVIDENCE_ALLOWED_TOOL_NAMES, allowed_tools),
            reason=(
                "code_evidence missing: final answers for code-evidence/no-speculation requests need "
                "search_code, an LSP evidence tool, or read_file evidence; read_file is preferred."
            ),
            rule_id="code_evidence",
            missing_requirements=("code_evidence",),
            preferred_tool_names=evidence_preferred,
        )

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

    reason = "all required tool-choice gates satisfied."
    if read_only:
        reason = "read_only restrictions applied; all required tool-choice gates satisfied."
    elif evidence_preferred:
        reason = "code evidence gate satisfied; read_file remains preferred when a candidate file exists."
    return ToolChoiceDecision(
        steering_required=False,
        allowed_tool_names=allowed_tools,
        reason=reason,
        preferred_tool_names=evidence_preferred,
    )


def _available_tool_names(available_tool_names: Iterable[str] | None) -> frozenset[str]:
    if available_tool_names is None:
        return DEFAULT_TOOL_NAMES
    return frozenset(str(name) for name in available_tool_names if str(name).strip())


def _allowed_subset(candidates: Iterable[str], allowed_tools: frozenset[str]) -> frozenset[str]:
    return frozenset(name for name in candidates if name in allowed_tools)


def _tool_name_set(tool_names: Iterable[str] | None, results: tuple[ToolResultSummary, ...]) -> set[str]:
    names = {str(name) for name in (tool_names or ()) if str(name).strip()}
    names.update(result.name for result in results if result.name)
    return names


def _normalize_tool_result(result: ToolResultSummary | Mapping[str, Any] | str) -> ToolResultSummary:
    if isinstance(result, ToolResultSummary):
        return result
    if isinstance(result, str):
        return _normalize_string_tool_result(result)
    name = str(result.get("tool_name") or result.get("name") or result.get("_lca_tool_name") or "")
    content = str(result.get("content") or result.get("summary") or result.get("result") or "")
    arguments = result.get("arguments") or result.get("args") or {}
    path = result.get("path")
    if path is None and isinstance(arguments, Mapping):
        path = arguments.get("path")
    error_value = result.get("is_error", result.get("error", False))
    changed = result.get("changed", result.get("workspace_changed"))
    return ToolResultSummary(
        name=name,
        content=content,
        is_error=bool(error_value),
        useless=bool(result.get("useless", False)),
        path=str(path) if path is not None else None,
        changed=changed if isinstance(changed, bool) else None,
    )


def _normalize_string_tool_result(result: str) -> ToolResultSummary:
    stripped = result.strip()
    name = ""
    content = stripped
    prefix = stripped.split(":", 1)[0].strip()
    if prefix in DEFAULT_TOOL_NAMES:
        name = prefix
        content = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
    return ToolResultSummary(name=name, content=content)


def _is_read_only_task(task_kind: str, prompt: str) -> bool:
    kind = _compact(task_kind)
    if kind in {"analysis", "question", "read_only", "readonly", "review"}:
        return True
    text = _lower_text(prompt)
    return any(keyword in text for keyword in READ_ONLY_KEYWORDS)


def _is_implementation_task(task_kind: str, prompt: str) -> bool:
    if _is_read_only_task(task_kind, prompt):
        return False
    kind = _compact(task_kind)
    if kind in {"bugfix", "code_edit", "code_implementation", "edit", "feature", "fix", "implementation", "implement"}:
        return True
    if kind and kind not in {"coding", "general", "task", "unknown"}:
        return False
    text = _lower_text(prompt)
    return any(keyword in text for keyword in IMPLEMENTATION_KEYWORDS)


def _requires_requirement_doc_read(task_kind: str, prompt: str) -> bool:
    kind = _compact(task_kind)
    if kind in {"allowed_dir", "alloweddir", "requirement_doc", "requirements", "requirements_doc", "spec"}:
        return True
    text = _lower_text(prompt)
    return any(keyword in text for keyword in REQUIREMENT_DOC_KEYWORDS)


def _needs_code_evidence(task_kind: str, prompt: str) -> bool:
    kind = _compact(task_kind)
    if kind in {"code_evidence", "evidence", "no_speculation", "read_only_evidence"}:
        return True
    text = _lower_text(prompt)
    return any(keyword in text for keyword in CODE_EVIDENCE_KEYWORDS | NO_SPECULATION_KEYWORDS)


def _has_code_evidence(seen_tool_names: set[str], results: tuple[ToolResultSummary, ...]) -> bool:
    if any(_successful_tool_result(result) and result.name in CODE_EVIDENCE_TOOL_NAMES for result in results):
        return True
    return bool(seen_tool_names.intersection(CODE_EVIDENCE_TOOL_NAMES))


def _preferred_evidence_tools(results: tuple[ToolResultSummary, ...]) -> tuple[str, ...]:
    if any(_successful_tool_result(result) and result.name == "read_file" for result in results):
        return ()
    return ("read_file",)


def _has_requirement_doc_read(prompt: str, results: tuple[ToolResultSummary, ...]) -> bool:
    read_results = [result for result in results if _successful_tool_result(result) and result.name == "read_file"]
    if not read_results:
        return False
    prompt_text = _lower_text(prompt)
    for result in read_results:
        result_text = _lower_text(" ".join(part for part in (result.path, result.content) if part))
        if any(marker in result_text for marker in DOC_READ_MARKERS):
            return True
    return any(keyword in prompt_text for keyword in REQUIREMENT_DOC_KEYWORDS)


def _implementation_needs_explore_before_write(
    task_kind: str,
    prompt: str,
    seen_tool_names: set[str],
    results: tuple[ToolResultSummary, ...],
) -> bool:
    if not _is_implementation_task(task_kind, prompt):
        return False
    if _workspace_write_happened(seen_tool_names, results):
        return False
    return not _has_code_evidence(seen_tool_names, results)


def _implementation_missing_requirements(
    task_kind: str,
    prompt: str,
    seen_tool_names: set[str],
    results: tuple[ToolResultSummary, ...],
) -> list[str]:
    if not _is_implementation_task(task_kind, prompt):
        return []
    missing: list[str] = []
    if not _workspace_write_happened(seen_tool_names, results):
        return missing
    if not _has_successful_tool("git_diff", seen_tool_names, results):
        missing.append("git_diff")
    if (
        not _has_successful_tool("run_tests", seen_tool_names, results)
        and not _has_cannot_test_explanation(results)
    ):
        missing.append("run_tests_or_cannot_test_explanation")
    return missing


def _has_successful_tool(name: str, seen_tool_names: set[str], results: tuple[ToolResultSummary, ...]) -> bool:
    matching_results = [result for result in results if result.name == name]
    if matching_results:
        return any(_successful_tool_result(result) for result in matching_results)
    return name in seen_tool_names


def _workspace_write_happened(seen_tool_names: set[str], results: tuple[ToolResultSummary, ...]) -> bool:
    write_results = [result for result in results if result.name in WRITE_TOOL_NAMES]
    if write_results:
        return any(_tool_result_changed_workspace(result) for result in write_results)
    return bool(seen_tool_names.intersection(WRITE_TOOL_NAMES))


def _tool_result_changed_workspace(result: ToolResultSummary) -> bool:
    if result.is_error:
        return False
    if result.changed is not None:
        return result.changed
    content = _lower_text(result.content)
    return not any(marker in content for marker in NO_WRITE_RESULT_MARKERS)


def _has_cannot_test_explanation(results: tuple[ToolResultSummary, ...]) -> bool:
    combined = _lower_text("\n".join(result.content for result in results))
    return any(marker in combined for marker in CANNOT_TEST_MARKERS)


def _successful_tool_result(result: ToolResultSummary) -> bool:
    return bool(result.name) and not result.is_error


def _compact(value: str) -> str:
    return re.sub(r"[\s-]+", "_", (value or "").strip().lower())


def _lower_text(value: str) -> str:
    return (value or "").lower()


__all__ = [
    "CODE_EVIDENCE_ALLOWED_TOOL_NAMES",
    "CODE_EVIDENCE_TOOL_NAMES",
    "DEFAULT_TOOL_NAMES",
    "PLANNER_EXPLORE_TOOL_NAMES",
    "POST_DIFF_REMEDIATION_TOOL_NAMES",
    "READ_ONLY_FORBIDDEN_TOOL_NAMES",
    "REQUIREMENT_DOC_TOOL_NAMES",
    "RequiredToolGate",
    "ToolChoiceDecision",
    "ToolChoiceQueue",
    "ToolResultSummary",
    "WRITE_TOOL_NAMES",
    "evaluate_tool_choice_state",
]

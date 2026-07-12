from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .negative_evidence import allowed_tools_for_negative_claims
from .negative_evidence import render_negative_existence_issues
from .negative_evidence import unsupported_negative_existence_claims
from .task_contract import RequirementContract
from .tool_choice_queue import CODE_EVIDENCE_TOOL_NAMES
from .tool_observation import ToolResultSummary
from .verification_timeline import results_after_last_write
from .verification_timeline import successful_tool_after_last_write
from .verification_timeline import workspace_write_happened
from .verification_plan import VerificationPlan


AuditCategory = Literal["acceptance", "evidence", "verification"]
AuditStatus = Literal["passed", "missing"]


@dataclass(frozen=True)
class GitRepositoryObservation:
    """Typed primary-workspace metadata observed from a Git probe.

    This is a LCA contract-owner fact, not a reconstruction from final-answer
    prose. The prose check below is a bounded LCA audit that verifies the
    answer does not invert this typed observation.
    """

    subject: str
    root_label: str
    repository: bool
    provenance: str


EVIDENCE_TOOLS = frozenset({"glob_files", "list_files", *CODE_EVIDENCE_TOOL_NAMES})
IMPLEMENTATION_EVIDENCE_TOOLS = frozenset({"list_files", *CODE_EVIDENCE_TOOL_NAMES})
IMPLEMENTATION_VERIFICATION_TOOLS = frozenset({"git_diff", "run_tests"})
TODO_TOOLS = frozenset({"todo_read", "todo_update", "todo_add"})

EVIDENCE_STATUS_MARKERS = frozenset(
    {
        "已验证",
        "证据",
        "证据支持",
        "源码事实",
        "推断",
        "未确认",
        "verified",
        "evidence",
        "inferred",
        "inference",
        "unverified",
    }
)
NO_EDIT_MARKERS = frozenset(
    {
        "did not change",
        "did not edit",
        "no changes",
        "no edits",
        "no files changed",
        "read-only",
        "不修改",
        "不需要修改",
        "只读",
        "没有修改",
        "未修改",
        "无改动",
    }
)
BLOCKED_NO_EDIT_MARKERS = frozenset(
    {
        "blocked",
        "cannot safely",
        "can't safely",
        "insufficient evidence",
        "out of scope",
        "target service",
        "不安全",
        "不属于",
        "无法安全",
        "目标服务",
        "证据不足",
    }
)
NEGATIVE_EVIDENCE_MARKERS = frozenset(
    {
        "no matches",
        "not found",
        "not located",
        "未发现",
        "未找到",
        "没有发现",
        "没有找到",
        "找不到",
    }
)
NO_EDIT_BLOCK_EVIDENCE_MARKERS = frozenset(
    {
        *NEGATIVE_EVIDENCE_MARKERS,
        "file not found",
        "outside the workspace",
        "patch relevance gate",
        "refusing real apply_patch",
        "user denied tool execution",
        "permission denied",
    }
)
CANNOT_TEST_MARKERS = frozenset(
    {
        "cannot run tests",
        "cannot test",
        "can't run tests",
        "can't test",
        "tests not run",
        "unable to run tests",
        "未运行测试",
        "无法测试",
        "无法运行测试",
    }
)
INCOMPLETE_FINAL_MARKERS = frozenset(
    {
        "final answer follows",
        "ready to output",
        "ready to provide",
        "准备输出",
        "可以输出",
        "马上输出",
    }
)
CODE_EVIDENCE_REQUEST_MARKERS = frozenset(
    {
        "code evidence",
        "source evidence",
        "代码证据",
        "代码依据",
        "源码",
        "源码中",
        "不需要推测",
        "不要推测",
        "不要猜",
        "怎么处理",
        "怎么解决",
        "如何处理",
        "实现逻辑",
        "处理逻辑",
    }
)


@dataclass(frozen=True)
class CompletionAuditItem:
    category: AuditCategory
    requirement: str
    status: AuditStatus
    reason: str
    allowed_tools: tuple[str, ...] = ()

    @property
    def missing(self) -> bool:
        return self.status == "missing"


@dataclass(frozen=True)
class CompletionAuditResult:
    items: tuple[CompletionAuditItem, ...]

    @property
    def passed(self) -> bool:
        return not self.missing_items

    @property
    def missing_items(self) -> tuple[CompletionAuditItem, ...]:
        return tuple(item for item in self.items if item.missing)

    def allowed_tool_names(self) -> tuple[str, ...]:
        names: set[str] = set()
        for item in self.missing_items:
            names.update(item.allowed_tools)
        return tuple(sorted(names))

    def payload(self, *, max_items: int = 12) -> dict[str, object]:
        missing = self.missing_items[:max_items]
        return {
            "passed": self.passed,
            "missing_count": len(self.missing_items),
            "missing": [
                {
                    "category": item.category,
                    "requirement": item.requirement,
                    "reason": item.reason,
                    "allowed_tools": list(item.allowed_tools),
                }
                for item in missing
            ],
        }


def audit_completion(
    contract: RequirementContract | None,
    *,
    request: str | None,
    final_content: str,
    tool_results: list[ToolResultSummary],
    source_paths: list[str],
    open_todos: list[str],
    verification_plan: VerificationPlan | None = None,
) -> CompletionAuditResult:
    """Check whether a proposed final answer satisfies the current task contract."""

    if contract is None:
        return CompletionAuditResult(())

    mode = _effective_task_kind(contract, request)
    if mode == "read-only":
        return CompletionAuditResult(
            tuple(
                _read_only_items(
                    contract,
                    final_content=final_content,
                    tool_results=tool_results,
                    source_paths=source_paths,
                )
            )
        )
    if mode == "code-implementation":
        if verification_plan is not None and verification_plan.active and workspace_write_happened(tool_results):
            return CompletionAuditResult(tuple(_verification_plan_items(verification_plan)))
        return CompletionAuditResult(
            tuple(
                _implementation_items(
                    contract,
                    final_content=final_content,
                    tool_results=tool_results,
                    source_paths=source_paths,
                    open_todos=open_todos,
                )
            )
        )
    return CompletionAuditResult(())


def _verification_plan_items(plan: VerificationPlan) -> list[CompletionAuditItem]:
    """Use runtime-owned verification facts after a write instead of final-answer wording."""

    items: list[CompletionAuditItem] = []
    for item in plan.delivery_items():
        if item.status == "passed":
            reason = item.reason
            items.append(_passed(item.kind, item.description, reason))
            continue
        if item.status in {"blocked", "skipped"}:
            # A blocked check may close the run as explicitly incomplete, but it
            # must never look like a successful delivery to the audit.
            items.append(_missing(item.kind, item.description, f"runtime-backed {item.status}: {item.reason}"))
            continue
        if item.status == "failed":
            allowed = ("read_file", "search_code", "apply_patch", "run_tests", "git_diff")
        elif item.id == "runtime-current-diff":
            allowed = ("git_diff",)
        elif item.kind == "verification":
            allowed = ("run_tests", "git_diff")
        elif item.kind == "evidence":
            allowed = IMPLEMENTATION_EVIDENCE_TOOLS
        else:
            allowed = ("read_file", "search_code", "apply_patch")
        items.append(_missing(item.kind, item.description, item.reason, allowed))
    return items


def render_completion_audit_message(
    result: CompletionAuditResult,
    *,
    request: str | None,
    final_content: str,
) -> str:
    missing_lines: list[str] = []
    for item in result.missing_items[:10]:
        tool_hint = f" Allowed tools: {', '.join(item.allowed_tools)}." if item.allowed_tools else " No more tools are needed."
        missing_lines.append(f"- [{item.category}] {item.requirement}: {item.reason}.{tool_hint}")
    return (
        "Runtime completion audit failed: the draft final answer does not satisfy the current "
        "RequirementContract. Do not cover gaps with wording. Either gather the missing evidence/verification "
        "using only the allowed tools, or rewrite the final answer to explicitly mark the item as blocked, "
        "unverified, or not run.\n"
        "Missing audit items:\n"
        + "\n".join(missing_lines)
        + "\n\nFinal answer requirements now:\n"
        "- Address acceptance, evidence, and verification items explicitly enough that a reviewer can trace them.\n"
        "- Separate source-backed facts from inference or missing evidence.\n"
        "- Do not claim tests, diffs, edits, or source evidence unless corresponding tools/results exist in this run.\n"
        f"{_request_summary(request)}"
        f"\n\nDraft final answer that failed audit:\n- {_one_line(final_content, max_chars=1200)}"
    )


def _read_only_items(
    contract: RequirementContract,
    *,
    final_content: str,
    tool_results: list[ToolResultSummary],
    source_paths: list[str],
) -> list[CompletionAuditItem]:
    if contract.inspection_forbidden:
        return _inspection_forbidden_items(contract, final_content=final_content)
    if contract.workspace_metadata_subject == "git_repository":
        return _git_metadata_items(contract, final_content=final_content, tool_results=tool_results)
    content = final_content or ""
    code_evidence = _has_successful_code_evidence(tool_results)
    path_discovery_evidence = _has_complete_path_discovery_evidence(tool_results)
    source_ref = _mentions_source_reference(content, source_paths)
    negative_evidence = _has_negative_evidence_result(tool_results)
    unsupported_existence_claims = unsupported_negative_existence_claims(content, tool_results)
    no_edits = not workspace_write_happened(tool_results)
    items: list[CompletionAuditItem] = []

    direct_requirement = _requirement_at(
        contract.acceptance_items,
        0,
        "Answer the user's question directly using repository-grounded evidence.",
    )
    if unsupported_existence_claims:
        items.append(
            _missing(
                "acceptance",
                direct_requirement,
                "negative existence claim lacks type-matched evidence: "
                + "; ".join(render_negative_existence_issues(unsupported_existence_claims)),
                allowed_tools_for_negative_claims(unsupported_existence_claims),
            )
        )
    elif _looks_incomplete(content):
        items.append(_missing("acceptance", direct_requirement, "draft says it is ready instead of answering"))
    elif not code_evidence and not path_discovery_evidence:
        items.append(
            _missing(
                "acceptance",
                direct_requirement,
                "no successful read/search/LSP evidence exists in this run",
                EVIDENCE_TOOLS,
            )
        )
    elif not source_ref and not negative_evidence:
        items.append(_missing("acceptance", direct_requirement, "answer does not cite inspected source path or search evidence"))
    else:
        items.append(_passed("acceptance", direct_requirement, "repository evidence is available and referenced"))

    separation_requirement = _requirement_at(
        contract.acceptance_items,
        1,
        "Separate verified facts from reasonable inference.",
    )
    if _has_evidence_status(content):
        items.append(_passed("acceptance", separation_requirement, "answer labels evidence/inference status"))
    else:
        items.append(
            _missing(
                "acceptance",
                separation_requirement,
                "answer does not label verified facts, inference, or unverified items",
            )
        )

    not_found_requirement = _requirement_at(
        contract.acceptance_items,
        2,
        "Call out any searched-for evidence that was not found.",
    )
    if negative_evidence and not _mentions_negative_evidence(content):
        items.append(_missing("acceptance", not_found_requirement, "negative search/LSP evidence exists but is not reported"))
    else:
        items.append(_passed("acceptance", not_found_requirement, "no unreported negative evidence"))

    evidence_requirement = _requirement_at(
        contract.evidence_requirements,
        0,
        "Cite concrete file paths, symbols, commands, or search terms used as evidence.",
    )
    if (code_evidence or path_discovery_evidence) and (source_ref or negative_evidence):
        items.append(_passed("evidence", evidence_requirement, "source/search evidence is traceable"))
    else:
        items.append(
            _missing(
                "evidence",
                evidence_requirement,
                "final answer lacks concrete path, symbol, command, or search-term evidence",
                EVIDENCE_TOOLS if not code_evidence else (),
            )
        )

    inference_requirement = _requirement_at(
        contract.evidence_requirements,
        1,
        "Mention when a conclusion depends on inference rather than inspected code.",
    )
    if _has_evidence_status(content):
        items.append(_passed("evidence", inference_requirement, "inference status is explicit"))
    else:
        items.append(_missing("evidence", inference_requirement, "inference status is not explicit"))

    verification_requirement = _requirement_at(
        contract.verification_requirements,
        0,
        "Use read/search style inspection before answering code-specific claims.",
    )
    if code_evidence or path_discovery_evidence:
        items.append(_passed("verification", verification_requirement, "read/search style inspection happened"))
    else:
        items.append(
            _missing(
                "verification",
                verification_requirement,
                "no successful read/search/LSP inspection happened",
                EVIDENCE_TOOLS,
            )
        )

    no_edit_requirement = _requirement_at(
        contract.verification_requirements,
        1,
        "Confirm no file edits are needed for the requested answer.",
    )
    if not no_edits:
        items.append(
            _missing(
                "verification",
                no_edit_requirement,
                "read-only task has workspace write tool results",
                ("git_diff",),
            )
        )
    else:
        items.append(_passed("verification", no_edit_requirement, "no workspace write tool results exist"))

    return items


def _inspection_forbidden_items(
    contract: RequirementContract,
    *,
    final_content: str,
) -> list[CompletionAuditItem]:
    content = final_content or ""
    direct = _requirement_at(contract.acceptance_items, 0, "Explain the requested language or semantic meaning directly.")
    provenance = _requirement_at(
        contract.acceptance_items,
        1,
        "Do not present repository facts as verified when inspection is forbidden.",
    )
    items: list[CompletionAuditItem] = []
    if _has_direct_semantic_answer(content):
        items.append(_passed("acceptance", direct, "semantic-only answer directly addresses the requested wording"))
    else:
        items.append(_missing("acceptance", direct, "final answer does not provide a direct semantic explanation"))
    if not contract.inspection_repository_facts_requested or _has_unverified_status(content):
        items.append(_passed("evidence", provenance, "repository facts are labelled or not requested"))
    else:
        items.append(
            _missing(
                "evidence",
                provenance,
                "repository fact was requested while inspection is forbidden; label it unverified instead",
            )
        )
    items.append(
        _passed(
            "verification",
            _requirement_at(contract.verification_requirements, 0, "Do not call repository inspection tools for this task."),
            "inspection is forbidden by the task contract",
        )
    )
    return items


def _git_metadata_items(
    contract: RequirementContract,
    *,
    final_content: str,
    tool_results: list[ToolResultSummary],
) -> list[CompletionAuditItem]:
    requirement = _requirement_at(
        contract.acceptance_items,
        0,
        "Answer the primary workspace Git-repository question directly from a structured Git probe.",
    )
    observation = next(
        (candidate for result in tool_results if (candidate := _git_repository_observation(result)) is not None),
        None,
    )
    if observation is None:
        return [
            _missing(
                "acceptance",
                requirement,
                "no structured primary git_status probe exists; additional roots require /move",
                ("git_status",),
            )
        ]
    expected_repository = observation.repository
    actual_repository = _git_repository_conclusion(final_content)
    if actual_repository is None:
        return [
            _missing(
                "acceptance",
                requirement,
                "final answer does not state an unambiguous primary Git-repository conclusion",
            )
        ]
    if actual_repository != expected_repository:
        expected_text = "is a Git repository" if expected_repository else "is not a Git repository"
        return [
            _missing(
                "acceptance",
                requirement,
                f"final Git conclusion conflicts with structured primary probe: expected {expected_text}",
            )
        ]
    return [
        _passed("acceptance", requirement, "structured primary Git probe matches the final conclusion"),
        _passed(
            "evidence",
            _requirement_at(contract.evidence_requirements, 0, "Cite the primary git_status probe."),
            "Git probe distinguishes repository state from execution errors",
        ),
        _passed(
            "verification",
            _requirement_at(contract.verification_requirements, 0, "Do not modify files for this workspace metadata check."),
            "workspace metadata check is read-only",
        ),
    ]


def _git_repository_observation(result: ToolResultSummary) -> GitRepositoryObservation | None:
    if result.name != "git_status":
        return None
    metadata = result.metadata
    repository = metadata.get("git_repository")
    root_label = str(metadata.get("evidence_root_label") or "")
    if repository not in {True, False} or root_label != "primary":
        return None
    probe_root = str(metadata.get("git_probe_root") or "")
    return GitRepositoryObservation(
        subject="git_repository",
        root_label=root_label,
        repository=bool(repository),
        provenance=f"git_status:{probe_root or 'primary'}",
    )


def _has_direct_semantic_answer(content: str) -> bool:
    normalized = (content or "").strip()
    if len(normalized) < 8 or _looks_incomplete(normalized):
        return False
    lowered = normalized.lower()
    return not any(marker in lowered for marker in ("拒绝回答", "无法回答", "cannot answer", "refuse to answer"))


def _has_unverified_status(content: str) -> bool:
    lowered = (content or "").lower()
    return any(marker in lowered for marker in ("未验证", "无法确认", "不能确认", "unverified", "not verified", "cannot confirm"))


def _git_repository_conclusion(content: str) -> bool | None:
    conclusions: set[bool] = set()
    for clause in _git_conclusion_clauses(content):
        normalized_clause = _normalize_git_conclusion_clause(clause)
        for match in _PRIMARY_GIT_NEGATIVE.finditer(normalized_clause):
            if not _git_match_is_question_or_condition(normalized_clause, match.start()):
                conclusions.add(False)
        for match in _PRIMARY_GIT_POSITIVE.finditer(normalized_clause):
            if not _git_match_is_question_or_condition(normalized_clause, match.start()):
                conclusions.add(True)
    if len(conclusions) != 1:
        return None
    return conclusions.pop()


_GIT_CONCLUSION_CLAUSE_BREAK = re.compile(r"[。！？!?；;\n]+")
_PRIMARY_GIT_SUBJECT = (
    r"(?:当前\s*primary(?:\s*(?:workspace|root|目录|工作区|工作空间))?|"
    r"primary(?:\s*(?:workspace|root|目录|工作区|工作空间))?|主工作区|当前工作区|主工作空间|当前工作空间|"
    r"(?:the\s+)?(?:current|this|primary)\s+(?:workspace|root|directory))"
)
_PRIMARY_GIT_NEGATIVE = re.compile(
    rf"{_PRIMARY_GIT_SUBJECT}\s*(?:不是|并非|非)\s*(?:一个\s*)?git\s*(?:仓库|repo|repository)"
    rf"|{_PRIMARY_GIT_SUBJECT}\s+(?:is\s+not|isn't)\s+(?:a\s+)?git\s+(?:repo|repository)\b",
    re.IGNORECASE,
)
_PRIMARY_GIT_POSITIVE = re.compile(
    rf"{_PRIMARY_GIT_SUBJECT}\s*(?:是|为)\s*(?:一个\s*)?git\s*(?:仓库|repo|repository)"
    rf"|{_PRIMARY_GIT_SUBJECT}\s+(?:is|is\s+an?|is\s+a)\s+(?:a\s+)?git\s+(?:repo|repository)\b",
    re.IGNORECASE,
)
_GIT_QUESTION_OR_CONDITION = re.compile(
    r"(?:是否|是不是|检查\s*是否|需要\s*检查|如果|若|\b(?:whether|if|check|verify)\b)",
    re.IGNORECASE,
)
_GIT_INLINE_CODE = re.compile(r"`[^`\n]{0,512}`")


def _git_conclusion_clauses(content: str) -> tuple[str, ...]:
    return tuple(clause.strip() for clause in _GIT_CONCLUSION_CLAUSE_BREAK.split(content or "") if clause.strip())


def _normalize_git_conclusion_clause(clause: str) -> str:
    """Remove bounded Markdown presentation that cannot change claim ownership."""

    without_inline_path = _GIT_INLINE_CODE.sub(" ", clause)
    return without_inline_path.replace("**", "").replace("__", "")


def _git_match_is_question_or_condition(clause: str, start: int) -> bool:
    """Only a governing prefix can turn this specific declaration into a question."""

    return bool(_GIT_QUESTION_OR_CONDITION.search(clause[max(0, start - 32) : start]))


def _implementation_items(
    contract: RequirementContract,
    *,
    final_content: str,
    tool_results: list[ToolResultSummary],
    source_paths: list[str],
    open_todos: list[str],
) -> list[CompletionAuditItem]:
    content = final_content or ""
    write_happened = workspace_write_happened(tool_results)
    blocked_no_edit_claim = _looks_like_blocked_no_edit(content)
    blocked_no_edit_evidence = _has_blocked_no_edit_evidence(tool_results)
    unsupported_existence_claims = unsupported_negative_existence_claims(content, tool_results)
    blocked_no_edit = blocked_no_edit_claim and blocked_no_edit_evidence and not unsupported_existence_claims
    code_evidence = _has_successful_code_evidence(tool_results)
    tests_run = successful_tool_after_last_write(tool_results, "run_tests")
    diff_run = successful_tool_after_last_write(tool_results, "git_diff")
    cannot_test = _mentions_cannot_test(content) or _tool_results_mention_cannot_test_after_last_write(tool_results)
    source_ref = _mentions_source_reference(content, source_paths)
    items: list[CompletionAuditItem] = []

    implement_requirement = contract.acceptance_items[0]
    if write_happened:
        items.append(_passed("acceptance", implement_requirement, "workspace write tool changed files"))
    elif blocked_no_edit:
        items.append(
            _passed(
                "acceptance",
                implement_requirement,
                "answer explicitly stops and a tool result records a concrete blocking condition",
            )
        )
    else:
        reason = "no workspace change happened and the final answer does not clearly mark the task blocked/no-edit"
        allowed_tools = ("read_file", "search_code", "apply_patch")
        if unsupported_existence_claims:
            reason = "blocked/no-edit conclusion includes unsupported negative existence claims: " + "; ".join(
                render_negative_existence_issues(unsupported_existence_claims)
            )
            allowed_tools = allowed_tools_for_negative_claims(unsupported_existence_claims) or allowed_tools
        elif blocked_no_edit_claim:
            reason = "final answer claims blocked/no-edit, but no tool result records a concrete blocking condition"
        items.append(
            _missing(
                "acceptance",
                implement_requirement,
                reason,
                allowed_tools,
            )
        )

    pattern_requirement = contract.acceptance_items[1]
    if blocked_no_edit or code_evidence:
        items.append(_passed("acceptance", pattern_requirement, "existing code evidence was inspected or task is explicitly blocked"))
    else:
        items.append(
            _missing(
                "acceptance",
                pattern_requirement,
                "no successful local code inspection is recorded",
                IMPLEMENTATION_EVIDENCE_TOOLS,
            )
        )

    test_requirement = contract.acceptance_items[2]
    if not write_happened or tests_run or cannot_test:
        items.append(_passed("acceptance", test_requirement, "test status is acceptable for current write state"))
    else:
        items.append(_missing("acceptance", test_requirement, "files changed but tests were neither run nor explained", ("run_tests",)))

    modified_files_requirement = contract.evidence_requirements[0]
    if not write_happened and blocked_no_edit:
        items.append(_passed("evidence", modified_files_requirement, "answer reports no files changed"))
    elif write_happened and (_has_file_path(content) or diff_run):
        items.append(_passed("evidence", modified_files_requirement, "modified files are traceable from final answer or git_diff"))
    else:
        items.append(_missing("evidence", modified_files_requirement, "modified/no-modified file status is not traceable"))

    existing_paths_requirement = contract.evidence_requirements[1]
    if blocked_no_edit or source_ref or code_evidence:
        items.append(_passed("evidence", existing_paths_requirement, "existing code evidence is recorded"))
    else:
        items.append(
            _missing(
                "evidence",
                existing_paths_requirement,
                "no existing code path or pattern is recorded",
                IMPLEMENTATION_EVIDENCE_TOOLS,
            )
        )

    run_tests_requirement = contract.verification_requirements[0]
    if not write_happened or tests_run or cannot_test:
        items.append(_passed("verification", run_tests_requirement, "test run or no-write state satisfies requirement"))
    else:
        items.append(_missing("verification", run_tests_requirement, "files changed but no successful run_tests result exists", ("run_tests",)))

    report_verification_requirement = contract.verification_requirements[1]
    if tests_run or cannot_test or blocked_no_edit:
        items.append(_passed("verification", report_verification_requirement, "verification status is explicitly supported"))
    else:
        items.append(
            _missing(
                "verification",
                report_verification_requirement,
                "final answer does not report test result or why tests were not run",
                ("run_tests",) if write_happened else (),
            )
        )

    if write_happened:
        diff_requirement = "Inspect git_diff after workspace changes and summarize the diff."
        if diff_run:
            items.append(_passed("verification", diff_requirement, "git_diff ran after a write"))
        else:
            items.append(_missing("verification", diff_requirement, "files changed but git_diff has not run", ("git_diff",)))

    if open_todos and not _mentions_todo_status(content):
        items.append(
            _missing(
                "verification",
                "Resolve or report open todo items before finalizing.",
                "open todo items exist but the final answer does not mention their state",
                TODO_TOOLS,
            )
        )

    return items


def _effective_task_kind(contract: RequirementContract, request: str | None) -> str:
    if contract.task_kind == "code-implementation":
        return "code-implementation"
    if contract.task_kind == "read-only":
        return "read-only"
    request_text = (request or "").lower()
    if any(marker in request_text for marker in CODE_EVIDENCE_REQUEST_MARKERS):
        return "read-only"
    return "unclear"


def _passed(category: AuditCategory, requirement: str, reason: str) -> CompletionAuditItem:
    return CompletionAuditItem(category=category, requirement=requirement, status="passed", reason=reason)


def _missing(
    category: AuditCategory,
    requirement: str,
    reason: str,
    allowed_tools: tuple[str, ...] | frozenset[str] = (),
) -> CompletionAuditItem:
    return CompletionAuditItem(
        category=category,
        requirement=requirement,
        status="missing",
        reason=reason,
        allowed_tools=tuple(sorted(allowed_tools)),
    )


def _requirement_at(items: list[str], index: int, default: str) -> str:
    if 0 <= index < len(items) and items[index]:
        return items[index]
    return default


def _has_successful_code_evidence(results: list[ToolResultSummary]) -> bool:
    return any(_successful(result) and result.name in CODE_EVIDENCE_TOOL_NAMES for result in results)


def _has_complete_path_discovery_evidence(results: list[ToolResultSummary]) -> bool:
    return any(
        (
            _successful(result)
            and result.name == "glob_files"
            and bool(result.metadata.get("complete"))
            and not bool(result.metadata.get("truncated"))
        )
        or (
            str(result.metadata.get("negative_evidence_type") or "") == "exact_path_missing"
            and bool(result.metadata.get("complete"))
        )
        for result in results
    )


def _successful(result: ToolResultSummary) -> bool:
    return bool(result.name) and not result.is_error


def _has_negative_evidence_result(results: list[ToolResultSummary]) -> bool:
    for result in results:
        if not _successful(result):
            continue
        if result.name not in CODE_EVIDENCE_TOOL_NAMES:
            continue
        if result.useless or any(marker in result.content.lower() for marker in NEGATIVE_EVIDENCE_MARKERS):
            return True
    return False


def _has_blocked_no_edit_evidence(results: list[ToolResultSummary]) -> bool:
    """Require a tool-observed reason before accepting a no-edit implementation stop."""

    for result in results:
        lowered = (result.content or "").lower()
        if result.name in CODE_EVIDENCE_TOOL_NAMES and (result.useless or any(marker in lowered for marker in NEGATIVE_EVIDENCE_MARKERS)):
            return True
        if result.is_error and any(marker in lowered for marker in NO_EDIT_BLOCK_EVIDENCE_MARKERS):
            return True
    return False


def _mentions_source_reference(content: str, source_paths: list[str]) -> bool:
    if _has_file_path(content):
        return True
    lowered = content.lower()
    for path in source_paths:
        if path and (path.lower() in lowered or path.rsplit("/", 1)[-1].lower() in lowered):
            return True
    return any(marker in lowered for marker in {"glob_files", "search_code", "lsp_", "搜索词", "search term"})


def _has_file_path(content: str) -> bool:
    return bool(
        re.search(
            r"(?<![\w./-])[\w./-]+\.(?:java|vue|ts|tsx|js|jsx|py|xml|md|yml|yaml|properties|json)\b",
            content or "",
            flags=re.IGNORECASE,
        )
    )


def _has_evidence_status(content: str) -> bool:
    lowered = (content or "").lower()
    return any(marker in lowered for marker in EVIDENCE_STATUS_MARKERS)


def _mentions_negative_evidence(content: str) -> bool:
    lowered = (content or "").lower()
    return any(marker in lowered for marker in NEGATIVE_EVIDENCE_MARKERS)


def _mentions_no_edit(content: str) -> bool:
    lowered = (content or "").lower()
    return any(marker in lowered for marker in NO_EDIT_MARKERS)


def _looks_like_blocked_no_edit(content: str) -> bool:
    lowered = (content or "").lower()
    return _mentions_no_edit(content) or any(marker in lowered for marker in BLOCKED_NO_EDIT_MARKERS)


def _mentions_cannot_test(content: str) -> bool:
    lowered = (content or "").lower()
    return any(marker in lowered for marker in CANNOT_TEST_MARKERS)


def _tool_results_mention_cannot_test_after_last_write(results: list[ToolResultSummary]) -> bool:
    lowered = "\n".join(result.content for result in results_after_last_write(results)).lower()
    return any(marker in lowered for marker in CANNOT_TEST_MARKERS)


def _mentions_todo_status(content: str) -> bool:
    lowered = (content or "").lower()
    return any(marker in lowered for marker in {"todo", "待办", "任务清单", "open item", "remaining"})


def _looks_incomplete(content: str) -> bool:
    lowered = (content or "").lower()
    return any(marker in lowered for marker in INCOMPLETE_FINAL_MARKERS)


def _request_summary(request: str | None) -> str:
    if not request:
        return ""
    return "\n\nOriginal user request to satisfy now:\n" f"- {_one_line(request, max_chars=1200)}"


def _one_line(content: str, *, max_chars: int = 240) -> str:
    normalized = " ".join((content or "").split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 14] + "...<truncated>"


__all__ = [
    "CompletionAuditItem",
    "CompletionAuditResult",
    "audit_completion",
    "render_completion_audit_message",
]

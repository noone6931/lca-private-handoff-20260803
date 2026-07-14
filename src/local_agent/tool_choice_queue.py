from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .design_evidence import missing_design_evidence_roots
from .document_artifacts import DocumentArtifactRequirement
from .document_artifacts import document_artifact_coverage
from .document_artifacts import missing_document_artifacts
from .negative_evidence import allowed_tools_for_negative_claims, parse_negative_evidence_claims, unsupported_negative_existence_claims
from .read_only_explore import PRECISE_EVIDENCE_TOOLS, evaluate_read_only_explore
from .runtime_prompt import _one_line
from .task_contract import is_inspection_forbidden
from .tool_observation import ToolResultSummary
from .verification_timeline import last_workspace_write_index
from .verification_timeline import result_changed_workspace
from .verification_timeline import successful_tool_after_last_write
from .verification_timeline import workspace_write_happened
from .verification_timeline import WRITE_TOOL_NAMES


DEFAULT_TOOL_NAMES = frozenset(
    {
        "apply_patch",
        "ask_user",
        "git_diff",
        "git_status",
        "glob_files",
        "inspect_image",
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
CODE_EVIDENCE_ALLOWED_TOOL_NAMES = frozenset({"glob_files", "list_files", *CODE_EVIDENCE_TOOL_NAMES})
# A document-only contract is narrower than a requirement document used as an
# input to a code investigation. The former must not quietly widen into source
# discovery after the first Markdown read.
DOCUMENT_ONLY_TOOL_NAMES = frozenset({"ask_user", "list_files", "read_file", "inspect_image"})
REQUIREMENT_DOC_TOOL_NAMES = frozenset({"ask_user", "inspect_image", "list_files", "read_file", "search_code"})
WORKSPACE_INVENTORY_TOOL_NAMES = frozenset({"glob_files", "list_files", "read_file"})
WORKSPACE_INVENTORY_DISCOVERY_TOOL_NAMES = frozenset({"glob_files"})
MAX_WORKSPACE_INVENTORY_DISCOVERY_CALLS_PER_ROOT = 2
MAX_WORKSPACE_INVENTORY_DISCOVERY_CALLS = 8
PLANNER_EXPLORE_TOOL_NAMES = frozenset(
    {
        "ask_user",
        "git_diff",
        "git_status",
        "glob_files",
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
WORKSPACE_INVENTORY_MARKERS = frozenset(
    {
        "workspace inventory",
        "repository inventory",
        "repo inventory",
        "repository layout",
        "project layout",
        "目录主要是在干什么",
        "目录主要做什么",
        "当前目录主要",
        "当前项目主要",
        "项目结构",
        "项目代码",
        "代码结构",
        "有哪些代码",
        "代码都有哪些",
        "有哪些项目",
        "当前有哪些项目",
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
@dataclass(frozen=True)
class ToolChoiceDecision:
    steering_required: bool
    allowed_tool_names: frozenset[str]
    reason: str
    rule_id: str | None = None
    missing_requirements: tuple[str, ...] = ()
    preferred_tool_names: tuple[str, ...] = ()
    tool_call_hints: tuple[str, ...] = ()
    required_glob_roots: tuple[str, ...] = ()
    scoped_read_paths: tuple[str, ...] = ()
    scoped_read_budget: int | None = None
    stop_message: str | None = None
    force_final_answer_without_tools: bool = False

    @property
    def needs_steering(self) -> bool:
        return self.steering_required

    @property
    def allowed_tools(self) -> frozenset[str]:
        return self.allowed_tool_names

    @property
    def should_stop(self) -> bool:
        return self.stop_message is not None


def tool_choice_steering_signature(decision: ToolChoiceDecision, result_count: int) -> str:
    payload = {
        "rule_id": decision.rule_id,
        "missing": decision.missing_requirements,
        "results": result_count,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def tool_choice_signature_count(signatures: set[str], rule_id: str | None) -> int:
    prefix = f'"rule_id": "{rule_id}"' if rule_id else '"rule_id": null'
    return sum(1 for signature in signatures if prefix in signature)


def tool_choice_steering_message(decision: ToolChoiceDecision, current_user_request: str | None) -> str:
    allowed = ", ".join(sorted(decision.allowed_tool_names)) or "(no tools currently allowed)"
    preferred = ", ".join(decision.preferred_tool_names) or "(none)"
    missing = ", ".join(decision.missing_requirements) or "(none)"
    hints = "\n".join(f"- call hint: {hint}" for hint in decision.tool_call_hints)
    request = _one_line(current_user_request or "", max_chars=800)
    if decision.force_final_answer_without_tools:
        return (
            "[Runtime tool choice queue]\n"
            "The bounded exploration budget is exhausted. Your next response must be the final answer without tool calls. "
            "Use only collected evidence, include searched scope and incomplete/truncated limits, and do not infer absence "
            "from omitted results.\n"
            f"- rule: {decision.rule_id or 'unknown'}\n"
            f"- reason: {decision.reason}\n"
            f"- original request: {request}"
        )
    return (
        "[Runtime tool choice queue]\n"
        "A required workflow gate is not satisfied yet. Use the allowed tool set for the next step; "
        "do not answer as final until the missing requirement is satisfied or you can explicitly explain why it cannot be satisfied.\n"
        f"- rule: {decision.rule_id or 'unknown'}\n"
        f"- missing: {missing}\n"
        f"- preferred next tools: {preferred}\n"
        f"- allowed tools now: {allowed}\n"
        f"- reason: {decision.reason}\n"
        f"{hints + chr(10) if hints else ''}"
        f"- original request: {request}"
    )


@dataclass(frozen=True)
class SoftToolDirective:
    """A bounded turn reminder which never changes the active tool schema."""

    kind: str
    message: str
    paths: tuple[str, ...] = ()


def session_evidence_reuse_directive(
    tool_results: Iterable[ToolResultSummary],
) -> SoftToolDirective | None:
    """Remind a follow-up turn that fresh cached evidence is already available.

    This follows the OMP soft-tool-choice shape: it is an advisory turn
    directive, not a schema restriction or a synthetic tool result. The model
    remains free to re-read a file when it needs a fresher observation.
    """

    cached = [
        result
        for result in tool_results
        if result.metadata.get("evidence_origin") == "session_cached"
    ]
    if not cached:
        return None
    paths: list[str] = []
    descriptions: list[str] = []
    for result in cached:
        path = result.path or str(result.metadata.get("display_path") or "")
        if path and path not in paths:
            paths.append(path)
        description = f"- {result.name}: {path or 'previous verified result'}"
        if description not in descriptions:
            descriptions.append(description)
    return SoftToolDirective(
        kind="session_evidence_reuse",
        paths=tuple(paths),
        message=(
            "[Runtime session evidence reminder]\n"
            "Fresh evidence from the immediately relevant earlier turn was revalidated for this request. "
            "Reuse it before repeating the same read/search; call a tool again only when the current question needs "
            "different scope or a new freshness check. This is advisory and does not restrict available tools.\n"
            + "\n".join(descriptions[:6])
        ),
    )


class RequiredToolGate:
    def evaluate(
        self,
        *,
        task_kind: str,
        prompt: str,
        tool_names: Iterable[str] | None = None,
        tool_results: Iterable[ToolResultSummary | Mapping[str, Any] | str] | None = None,
        available_tool_names: Iterable[str] | None = None,
        design_evidence_roots: Iterable[str] | None = None,
        workspace_roots: Iterable[str] | None = None,
        evidence_domain: str | None = None,
        read_only_review_profile: str | None = None,
        document_artifacts: Iterable[DocumentArtifactRequirement] = (),
    ) -> ToolChoiceDecision:
        return evaluate_tool_choice_state(
            task_kind=task_kind,
            prompt=prompt,
            tool_names=tool_names,
            tool_results=tool_results,
            available_tool_names=available_tool_names,
            design_evidence_roots=design_evidence_roots,
            workspace_roots=workspace_roots,
            evidence_domain=evidence_domain,
            read_only_review_profile=read_only_review_profile,
            document_artifacts=document_artifacts,
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
    design_evidence_roots: Iterable[str] | None = None,
    workspace_roots: Iterable[str] | None = None,
    evidence_domain: str | None = None,
    read_only_review_profile: str | None = None,
    document_artifacts: Iterable[DocumentArtifactRequirement] = (),
) -> ToolChoiceDecision:
    if is_inspection_forbidden(prompt):
        return ToolChoiceDecision(
            steering_required=False,
            allowed_tool_names=frozenset(),
            reason="inspection_forbidden: semantic-only task must not inspect the workspace.",
            rule_id="inspection_forbidden",
        )
    results = tuple(_normalize_tool_result(result) for result in (tool_results or ()))
    seen_tool_names = _tool_name_set(tool_names, results)
    all_tools = _available_tool_names(available_tool_names)
    read_only = _is_read_only_task(task_kind, prompt)
    allowed_tools = all_tools - READ_ONLY_FORBIDDEN_TOOL_NAMES if read_only else all_tools

    if evidence_domain == "requirement_documents":
        artifacts = tuple(document_artifacts)
        coverage = document_artifact_coverage(artifacts, results)
        missing = missing_document_artifacts(coverage)
        has_document_read = _has_requirement_doc_read(prompt, results)
        complete = not missing and (bool(artifacts) or has_document_read)
        return ToolChoiceDecision(
            steering_required=not complete,
            allowed_tool_names=_allowed_subset(DOCUMENT_ONLY_TOOL_NAMES, allowed_tools),
            reason=(
                "document_only contract: expose only document browsing/reading and clarification tools; "
                "repository code discovery remains out of scope."
            ),
            rule_id="document_only_requirement_analysis",
            missing_requirements=(
                tuple(f"document_artifact:{item.label}" for item in missing)
                if missing
                else () if complete else ("requirement_document_read",)
            ),
            preferred_tool_names=tuple(
                dict.fromkeys("inspect_image" if item.kind == "image" else "read_file" for item in missing)
            ) or ("read_file",),
            tool_call_hints=_document_read_tool_hints(missing),
        )

    negative_discovery = _negative_discovery_decision(prompt, results, allowed_tools)
    if negative_discovery is not None:
        return negative_discovery

    inventory_decision = _workspace_inventory_decision(
        task_kind=task_kind,
        prompt=prompt,
        results=results,
        allowed_tools=allowed_tools,
        workspace_roots=workspace_roots,
    )
    if inventory_decision is not None:
        return inventory_decision

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
            tool_call_hints=_document_read_tool_hints(),
        )

    explore_decision = evaluate_read_only_explore(
        profile=read_only_review_profile,
        tool_results=results,
        code_roots=tuple(design_evidence_roots or workspace_roots or ()),
    )
    if explore_decision.is_applicable:
        missing = explore_decision.missing_roots
        if explore_decision.action == "finalize":
            coverage = "all required code roots have one bounded read" if not missing else "some required code roots remain unread"
            return ToolChoiceDecision(
                steering_required=True,
                allowed_tool_names=frozenset(),
                reason=(
                    "read_only_profile_explore budget reached: stop broad exploration and produce a scoped candidate "
                    f"from the evidence collected ({coverage}; observations={explore_decision.observation_calls}/"
                    f"{explore_decision.hard_budget})."
                ),
                rule_id="read_only_profile_explore_final",
                missing_requirements=tuple(f"code_read:{root}" for root in missing),
                force_final_answer_without_tools=True,
            )
        return ToolChoiceDecision(
            steering_required=True,
            allowed_tool_names=_allowed_subset(
                {"read_file"}
                if explore_decision.read_candidates
                else {"glob_files"}
                if explore_decision.discovery_roots
                else PRECISE_EVIDENCE_TOOLS,
                allowed_tools,
            ),
            reason=(
                "read_only_profile_explore active: "
                + (
                    "read the typed search/LSP candidates before continuing discovery; "
                    if explore_decision.read_candidates
                    else "run one root-scoped fallback discovery for missing roots before finalizing; "
                    if explore_decision.discovery_roots
                    else "use only precise source evidence to cover each remaining code root; "
                )
                + "do not continue broad directory inventory. "
                f"observations={explore_decision.observation_calls}/{explore_decision.hard_budget}."
            ),
            rule_id=(
                "read_only_profile_explore_soft"
                if explore_decision.observation_calls >= explore_decision.soft_budget
                else "read_only_profile_explore"
            ),
            missing_requirements=tuple(f"code_read:{root}" for root in missing),
            preferred_tool_names=(
                ("read_file",)
                if explore_decision.read_candidates
                else ("glob_files",)
                if explore_decision.discovery_roots
                else ("search_code", "read_file")
            ),
            tool_call_hints=(
                ("read_file candidates: " + ", ".join(explore_decision.read_candidates),)
                if explore_decision.read_candidates
                else (
                    "Run one bounded glob_files discovery rooted at the missing root(s): "
                    + ", ".join(explore_decision.discovery_roots),
                )
                if explore_decision.discovery_roots
                else (
                    (
                        "Cover the least-observed required root(s) before repeating discovery elsewhere: "
                        + ", ".join(explore_decision.preferred_roots),
                    )
                    if explore_decision.preferred_roots
                    else ()
                )
            ),
            required_glob_roots=explore_decision.discovery_roots,
            scoped_read_paths=explore_decision.read_candidates,
            scoped_read_budget=(len(explore_decision.read_candidates) if explore_decision.read_candidates else None),
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

    missing_design_roots = missing_design_evidence_roots(
        tuple(design_evidence_roots or ()),
        (result.path for result in results if _successful_tool_result(result) and result.name == "read_file"),
    )
    if missing_design_roots:
        target_root = missing_design_roots[0]
        return ToolChoiceDecision(
            steering_required=True,
            allowed_tool_names=_allowed_subset(CODE_EVIDENCE_ALLOWED_TOOL_NAMES, allowed_tools),
            reason=(
                "cross_root_design_evidence missing: this read-only design task needs at least one successful "
                "source-file read from each declared code root before it can finalize."
            ),
            rule_id=f"cross_root_design_evidence:{target_root}",
            missing_requirements=(f"code_read:{target_root}",),
            preferred_tool_names=("search_code", "read_file"),
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


def _negative_discovery_decision(
    prompt: str,
    results: tuple[ToolResultSummary, ...],
    allowed_tools: frozenset[str],
) -> ToolChoiceDecision | None:
    """Give typed negative discovery claims a bounded owner before model turn.

    Importing here preserves the existing `task_contract -> tool_choice_queue`
    direction while letting the queue consume the taxonomy only after module
    initialization. This is a soft scheduling decision, not a textual final
    answer audit.
    """

    claims = parse_negative_evidence_claims(prompt)
    actionable = tuple(claim for claim in claims if claim.stance in {"asserted_absence", "observed_no_match"})
    if not actionable:
        return None
    unsupported = unsupported_negative_existence_claims(prompt, results)
    if not unsupported:
        return None
    required = frozenset(allowed_tools_for_negative_claims(unsupported))
    available = _allowed_subset(required, allowed_tools)
    if not available:
        return ToolChoiceDecision(
            steering_required=False,
            allowed_tool_names=frozenset(),
            reason="negative_discovery_unavailable",
            rule_id="negative_discovery_unavailable",
            missing_requirements=tuple(f"negative_discovery:{claim.subject}" for claim in unsupported),
            stop_message=(
                "Unable to verify the requested file/source absence because the required discovery tools are denied. "
                "No repository inspection was performed; the requested 'checked/not found' statement remains unverified."
            ),
        )
    return ToolChoiceDecision(
        steering_required=True,
        allowed_tool_names=available,
        reason="negative_discovery missing: an observed or asserted file/source absence needs matching discovery evidence.",
        rule_id="negative_discovery",
        missing_requirements=tuple(f"negative_discovery:{claim.subject}" for claim in unsupported),
        preferred_tool_names=tuple(sorted(available)),
    )


def _workspace_inventory_decision(
    *,
    task_kind: str,
    prompt: str,
    results: tuple[ToolResultSummary, ...],
    allowed_tools: frozenset[str],
    workspace_roots: Iterable[str] | None,
) -> ToolChoiceDecision | None:
    if _is_implementation_task(task_kind, prompt) or not _is_workspace_inventory_request(task_kind, prompt):
        return None
    discovery_results = [result for result in results if result.name in {"glob_files", "list_files"}]
    successful_globs = [
        result
        for result in results
        if result.name == "glob_files" and _successful_tool_result(result)
    ]
    roots = tuple(sorted({str(root) for root in (workspace_roots or ()) if str(root).strip()}))
    root_count = len(roots)
    budget = min(
        MAX_WORKSPACE_INVENTORY_DISCOVERY_CALLS,
        max(4, max(root_count, 1) * MAX_WORKSPACE_INVENTORY_DISCOVERY_CALLS_PER_ROOT),
    )
    if len(discovery_results) >= budget:
        return ToolChoiceDecision(
            steering_required=True,
            allowed_tool_names=frozenset(),
            reason=(
                "workspace_inventory discovery budget reached: stop broad directory discovery and summarize the "
                "structured scope, matches, and any incomplete results already collected."
            ),
            rule_id="workspace_inventory_budget",
            missing_requirements=(),
            force_final_answer_without_tools=True,
        )
    if not successful_globs:
        return ToolChoiceDecision(
            steering_required=True,
            allowed_tool_names=_allowed_subset(WORKSPACE_INVENTORY_DISCOVERY_TOOL_NAMES, allowed_tools),
            reason=(
                "workspace_inventory discovery missing: filename/path inventory must use glob_files before "
                "drawing repository, language, source-tree, or build-layout conclusions."
            ),
            rule_id="workspace_inventory_discovery",
            missing_requirements=("path_discovery_evidence",),
            preferred_tool_names=("glob_files",),
            tool_call_hints=(_inventory_glob_call_hint(roots),),
            required_glob_roots=roots,
        )
    covered_roots = _inventory_covered_roots(successful_globs)
    missing_roots = tuple(root for root in roots if root not in covered_roots)
    if missing_roots:
        return ToolChoiceDecision(
            steering_required=True,
            allowed_tool_names=_allowed_subset(WORKSPACE_INVENTORY_DISCOVERY_TOOL_NAMES, allowed_tools),
            reason=(
                "workspace_inventory root coverage missing: run a bounded glob_files discovery for each uncovered "
                f"workspace root before finalizing. Uncovered roots: {', '.join(missing_roots)}."
            ),
            rule_id="workspace_inventory_root_coverage",
            missing_requirements=tuple(f"path_discovery:{root}" for root in missing_roots),
            preferred_tool_names=("glob_files",),
            tool_call_hints=(_inventory_glob_call_hint(missing_roots),),
            required_glob_roots=missing_roots,
        )
    return ToolChoiceDecision(
        steering_required=False,
        allowed_tool_names=_allowed_subset(WORKSPACE_INVENTORY_TOOL_NAMES, allowed_tools),
        reason=(
            "workspace_inventory bounded discovery active: keep file discovery limited to glob_files/list_files/read_file "
            "until the user-facing inventory is ready to summarize."
        ),
        preferred_tool_names=("glob_files",),
        tool_call_hints=(
            "Do not repeat a completed identical glob_files call. Use an uncovered root or a narrower pattern if more "
            "evidence is needed."
        ),
    )


def _allowed_subset(candidates: Iterable[str], allowed_tools: frozenset[str]) -> frozenset[str]:
    return frozenset(name for name in candidates if name in allowed_tools)


def _document_read_tool_hints(missing: Iterable[DocumentArtifactRequirement] = ()) -> tuple[str, ...]:
    labels = tuple(item.label for item in missing)
    coverage_hint = (
        "Complete every requested artifact before finalizing: " + ", ".join(labels) + "."
        if labels
        else "Complete each explicitly requested document artifact before finalizing."
    )
    return (
        coverage_hint,
        'Use read_file with {"path":"<authorized document path>"}.',
        'For a listed image, use inspect_image with {"path":"<authorized image path>","question":"<focused question>"}; do not pass a directory or image bytes.',
    )


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
    metadata = result.get("metadata")
    return ToolResultSummary(
        name=name,
        content=content,
        is_error=bool(error_value),
        useless=bool(result.get("useless", False)),
        path=str(path) if path is not None else None,
        changed=changed if isinstance(changed, bool) else None,
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
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
    # TaskContract has already distinguished a global read-only request from a
    # local exclusion such as "do not modify README or docs". Do not reopen
    # that classification here, or a scoped exclusion can wrongly disable the
    # implementation workflow.
    if kind in {"bugfix", "code_edit", "code_implementation", "edit", "feature", "fix", "implementation", "implement"}:
        return False
    text = _lower_text(prompt)
    return any(keyword in text for keyword in READ_ONLY_KEYWORDS)


def _is_workspace_inventory_request(task_kind: str, prompt: str) -> bool:
    if _is_implementation_task(task_kind, prompt):
        return False
    text = _lower_text(prompt)
    return any(marker in text for marker in WORKSPACE_INVENTORY_MARKERS) or _has_structured_inventory_phrase(text)


def _has_structured_inventory_phrase(text: str) -> bool:
    """Recognize inventory wording without treating every ``盘点`` as discovery.

    Analysis prompts such as "盘点当前代码中的安全问题" need search/LSP,
    not a repository-wide file inventory. The standalone verb is therefore not
    a marker; it must name a structural inventory target.
    """

    if "盘点" not in text:
        return False
    targets = (
        "项目代码",
        "项目结构",
        "代码结构",
        "目录结构",
        "仓库结构",
        "项目目录",
        "代码目录",
        "源码目录",
        "代码清单",
        "项目清单",
        "目录清单",
        "仓库清单",
        "workspace root",
        "workspace roots",
        "工作区根",
        "授权 root",
    )
    return any(target in text for target in targets)


def _inventory_covered_roots(results: Iterable[ToolResultSummary]) -> set[str]:
    covered: set[str] = set()
    for result in results:
        searched_roots = result.metadata.get("searched_roots")
        if not isinstance(searched_roots, (list, tuple)):
            continue
        covered.update(str(root) for root in searched_roots if str(root).strip())
    return covered


def _inventory_glob_call_hint(roots: Iterable[str]) -> str:
    """Render a bounded, executable discovery shape for every uncovered root.

    A directory-wide ``**/*`` scan is both expensive and likely truncated on a
    multi-project checkout.  Project manifests and a small source-language sample
    give the model enough evidence to identify candidate code projects without
    widening shell or Git permissions.
    """

    patterns: list[str] = []
    markers = (
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "package.json",
        "pyproject.toml",
        "go.mod",
        "Cargo.toml",
        "src/main/java/**/*.java",
        "src/**/*.py",
        "src/**/*.js",
        "src/**/*.ts",
        "src/**/*.vue",
    )
    for root in roots:
        cleaned = str(root).rstrip("/")
        if not cleaned:
            continue
        patterns.extend(f"{cleaned}/**/{marker}" for marker in markers)
    arguments = {"paths": patterns, "limit": 200, "hidden": False, "gitignore": True}
    return (
        "Use this bounded multi-root discovery call exactly (do not send an empty paths entry): "
        f"glob_files({json.dumps(arguments, ensure_ascii=False)})"
    )


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
    for result in read_results:
        result_text = _lower_text(" ".join(part for part in (result.path, result.content) if part))
        if any(marker in result_text for marker in DOC_READ_MARKERS):
            return True
    return False


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


def autonomous_small_change_candidate_paths(
    task_kind: str,
    prompt: str,
    results: tuple[ToolResultSummary, ...] | list[ToolResultSummary],
) -> tuple[str, ...]:
    """Return a narrow candidate only for an explicitly autonomous tiny-change task."""

    if not _is_implementation_task(task_kind, prompt):
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
    if not _is_implementation_task(task_kind, prompt):
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


def _successful_tool_result(result: ToolResultSummary) -> bool:
    return bool(result.name) and not result.is_error


def _compact(value: str) -> str:
    return re.sub(r"[\s-]+", "_", (value or "").strip().lower())


def _lower_text(value: str) -> str:
    return (value or "").lower()


__all__ = [
    "CODE_EVIDENCE_ALLOWED_TOOL_NAMES",
    "CODE_EVIDENCE_TOOL_NAMES",
    "DOCUMENT_ONLY_TOOL_NAMES",
    "CANDIDATE_DELIVERY_TOOL_NAMES",
    "CANDIDATE_DIFF_TOOL_NAMES",
    "CANDIDATE_REMEDIATION_TOOL_NAMES",
    "CANDIDATE_TEST_TOOL_NAMES",
    "DEFAULT_TOOL_NAMES",
    "MAX_CANDIDATE_READ_REVISITS",
    "MAX_CANDIDATE_PATCH_PREVIEW_FAILURES",
    "PLANNER_EXPLORE_TOOL_NAMES",
    "POST_DIFF_REMEDIATION_TOOL_NAMES",
    "READ_ONLY_FORBIDDEN_TOOL_NAMES",
    "REQUIREMENT_DOC_TOOL_NAMES",
    "WORKSPACE_INVENTORY_TOOL_NAMES",
    "MAX_WORKSPACE_INVENTORY_DISCOVERY_CALLS_PER_ROOT",
    "MAX_WORKSPACE_INVENTORY_DISCOVERY_CALLS",
    "RequiredToolGate",
    "ToolChoiceDecision",
    "ToolChoiceQueue",
    "ToolResultSummary",
    "WRITE_TOOL_NAMES",
    "autonomous_small_change_candidate_paths",
    "evaluate_tool_choice_state",
]

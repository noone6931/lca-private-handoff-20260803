from __future__ import annotations

from ..completion_audit import audit_completion
from ..completion_audit import render_completion_audit_message
from ..patch_reviewer import render_patch_review_message
from ..patch_reviewer import review_patch
from .models import *  # noqa: F403
from .evidence import request_mentions_todo
from .evidence import tool_names_since

class NoEditFinalHygieneSteerer:
    kind = "no_edit_final_hygiene"

    def __init__(self, *, max_steers: int) -> None:
        self._max_steers = max_steers

    def decide(self, context: FinalAnswerContext) -> SteeringDecision | None:
        if context.steer_counts.get(self.kind, 0) >= self._max_steers:
            return None
        if not context.is_code_implementation_request:
            return None
        if not looks_like_no_edit_final(context.content):
            return None
        tool_names = tool_names_since(context.messages, context.run_start_index)
        if tool_names.intersection({"apply_patch", "write_file", "rollback_patch"}):
            return None
        missing = no_edit_final_hygiene_missing(context.request, tool_names, context.open_todos)
        if not missing:
            return None
        content_summary = one_line(context.content, max_chars=800)
        steering = (
            "Runtime steering: you are about to finish an implementation task without changing files. "
            "That is acceptable when evidence shows the requested implementation is unsafe, out of scope, or belongs "
            "to another service, but the no-edit stop must still be auditable before the final answer.\n"
            f"- Missing hygiene: {', '.join(missing)}.\n"
            "- Use only todo_read/todo_add/todo_update and git_status/git_diff now.\n"
            "- If the user requested todo tracking or open todos exist, update/read todo state and mark the task "
            "blocked/skipped with the evidence-backed reason.\n"
            "- Run git_status or git_diff to prove whether the workspace changed; if no tests were run because no files "
            "changed or the target service is missing, say that explicitly in the final answer.\n"
            f"- Draft final answer that triggered this check: {content_summary}"
            f"{final_answer_request_summary(context.request)}"
        )
        return SteeringDecision(
            kind=self.kind,
            message=steering,
            payload={"missing": missing},
            force_final_answer_without_tools=False,
            temporary_tool_allowlist=set(NO_EDIT_FINAL_HYGIENE_TOOLS),
        )


class FinalStructureSteerer:
    kind = "final_structure"

    def __init__(self, *, max_steers: int) -> None:
        self._max_steers = max_steers

    def decide(self, context: FinalAnswerContext) -> SteeringDecision | None:
        if context.steer_counts.get(self.kind, 0) >= self._max_steers:
            return None
        issues = final_structure_issues(context.request, context.content)
        if not issues:
            return None
        content_summary = one_line(context.content, max_chars=800)
        steering = (
            "Runtime steering: the previous final answer did not satisfy the user's requested output structure. "
            "Do not call tools. Produce the final answer now using the evidence already collected.\n"
            f"- Missing/invalid structure: {', '.join(issues)}.\n"
            "- If the user requested tables or named sections, include the actual tables/sections in this response.\n"
            "- Do not say you are ready to output it; output it directly.\n"
            f"- Draft final answer that triggered this check: {content_summary}"
            f"{final_answer_request_summary(context.request)}"
        )
        return SteeringDecision(
            kind=self.kind,
            message=steering,
            payload={"issues": issues},
            severity=FinalAnswerSteeringSeverity.PRESENTATION,
        )


class CompletionAuditSteerer:
    kind = "completion_audit"

    def __init__(self, *, max_steers: int) -> None:
        self._max_steers = max_steers

    def decide(self, context: FinalAnswerContext) -> SteeringDecision | None:
        if context.steer_counts.get(self.kind, 0) >= self._max_steers:
            return None
        result = audit_completion(
            context.requirement_contract,
            request=context.request,
            final_content=context.content,
            tool_results=context.tool_results,
            source_paths=context.read_file_evidence_paths,
            open_todos=context.open_todos,
            verification_plan=context.verification_plan,
        )
        if result.passed:
            return None
        allowed_tools = set(result.allowed_tool_names())
        return SteeringDecision(
            kind=self.kind,
            message=render_completion_audit_message(
                result,
                request=context.request,
                final_content=context.content,
            ),
            payload=result.payload(),
            force_final_answer_without_tools=not allowed_tools,
            temporary_tool_allowlist=allowed_tools or None,
        )


class PatchReviewSteerer:
    """Enforce deterministic post-diff review before the completion audit closes a write task."""

    kind = "patch_reviewer"

    def __init__(self, *, max_steers: int) -> None:
        self._max_steers = max_steers

    def decide(self, context: FinalAnswerContext) -> SteeringDecision | None:
        if context.steer_counts.get(self.kind, 0) >= self._max_steers:
            return None
        result = review_patch(
            context.requirement_contract,
            request=context.request,
            tool_results=context.tool_results,
        )
        if result.passed:
            return None
        allowed_tools = set(result.allowed_tool_names())
        return SteeringDecision(
            kind=self.kind,
            message=render_patch_review_message(result, request=context.request),
            payload=result.payload(),
            force_final_answer_without_tools=not allowed_tools,
            temporary_tool_allowlist=allowed_tools or None,
        )


_HARD_GATE_LABELS = {
    "requirement_evidence": "需求事实与行号证据",
    "source_grounded_numeric": "源码数值/状态事实",
    "source_evidence_false_negative": "源码证据一致性",
    "negative_existence": "文件/源码存在性",
    "patch_reviewer": "变更审查",
    "completion_audit": "完成验收",
    "design_evidence_final": "跨项目设计证据覆盖",
}



def render_unverified_final_answer(kind: str, reason: str) -> str:
    """Return a truthful terminal response when a hard rewrite cannot run."""

    label = _HARD_GATE_LABELS.get(kind, "事实/证据")
    reason_text = {
        "deadline_reserve": "剩余时间预算不足",
        "continuation_limit": "最终重写次数已达到安全上限",
        "rewrite_timeout": "最终重写请求超时",
    }.get(reason, "最终重写未能完成")
    return (
        f"未完成/未验证：上一版答复未通过{label}校验，但{reason_text}。\n\n"
        "为避免将未经验证的内容表述为已完成，本次不会复用该草稿。"
        "请在保留当前 session 的前提下重试，或补充可验证的源码/需求证据后再继续。"
    )


def looks_like_no_edit_final(content: str) -> bool:
    lowered = content.lower()
    compact = " ".join(content.split())
    return any(keyword in lowered or keyword in compact for keyword in NO_EDIT_FINAL_KEYWORDS)


def no_edit_final_hygiene_missing(
    request: str | None,
    tool_names: set[str],
    open_todos: list[str],
) -> list[str]:
    missing: list[str] = []
    if not tool_names.intersection({"git_status", "git_diff"}):
        missing.append("git_status_or_git_diff")
    if request_mentions_todo(request) or open_todos:
        if not any(name.startswith("todo_") for name in tool_names):
            missing.append("todo_state")
    return missing


def final_structure_issues(request: str | None, content: str) -> list[str]:
    request_text = request or ""
    content_text = content or ""
    lowered_content = content_text.lower()
    issues: list[str] = []
    if any(marker in lowered_content for marker in INCOMPLETE_FINAL_MARKERS):
        issues.append("incomplete_final")
    if request_asks_for_table(request_text) and not has_markdown_table(content_text):
        issues.append("missing_table")
    missing_labels = [
        label
        for label in FINAL_LABEL_CANDIDATES
        if label in request_text and label not in content_text
    ]
    if missing_labels:
        issues.append("missing_labels:" + ",".join(missing_labels))
    if request_asks_for_project_scope_table(request_text) and not markdown_table_has_any_column(
        content_text,
        PROJECT_SCOPE_TABLE_COLUMN_KEYWORDS,
    ):
        issues.append("missing_project_or_service_table_column")
    if request_needs_evidence_status_labels(request_text, content_text) and not content_has_evidence_status_label(
        content_text
    ):
        issues.append("missing_evidence_status_labels")
    return issues


def request_asks_for_table(request: str) -> bool:
    lowered = request.lower()
    return any(keyword.lower() in lowered for keyword in FINAL_TABLE_REQUEST_KEYWORDS)


def has_markdown_table(content: str) -> bool:
    lines = [line.strip() for line in content.splitlines()]
    has_row = any(line.startswith("|") and line.endswith("|") and line.count("|") >= 2 for line in lines)
    has_separator = any(
        line.startswith("|") and "---" in line and line.endswith("|")
        for line in lines
    )
    return has_row and has_separator


def request_asks_for_project_scope_table(request: str) -> bool:
    lowered = request.lower()
    asks_for_scope = any(keyword.lower() in lowered for keyword in PROJECT_SCOPE_TABLE_REQUEST_KEYWORDS)
    asks_for_table_shape = request_asks_for_table(request) or "表" in request or "清单" in request
    return asks_for_scope and asks_for_table_shape


def markdown_table_has_any_column(content: str, keywords: set[str]) -> bool:
    lowered_keywords = {keyword.lower() for keyword in keywords}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        if "---" in stripped:
            continue
        cells = [cell.strip().lower() for cell in stripped.strip("|").split("|")]
        if any(any(keyword in cell for keyword in lowered_keywords) for cell in cells):
            return True
    return False


def request_needs_evidence_status_labels(request: str, content: str) -> bool:
    lowered_request = request.lower()
    lowered_content = content.lower()
    if any(keyword.lower() in lowered_request for keyword in EVIDENCE_STATUS_REQUEST_KEYWORDS):
        return True
    asks_for_evidence = any(keyword.lower() in lowered_request for keyword in EVIDENCE_REQUEST_KEYWORDS)
    has_inference = any(marker.lower() in lowered_content for marker in INFERENCE_MARKERS)
    return asks_for_evidence and has_inference


def content_has_evidence_status_label(content: str) -> bool:
    lowered = content.lower()
    return any(label.lower() in lowered for label in EVIDENCE_STATUS_LABELS)


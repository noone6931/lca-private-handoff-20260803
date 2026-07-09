from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from ..completion_audit import audit_completion
from ..completion_audit import render_completion_audit_message
from ..task_contract import RequirementContract
from ..tool_choice_queue import ToolResultSummary


NO_EDIT_FINAL_HYGIENE_TOOLS = {"todo_read", "todo_add", "todo_update", "git_status", "git_diff"}
READ_ONLY_EVIDENCE_TOOLS = {
    "search_code",
    "read_file",
    "lsp_symbols",
    "lsp_workspace_symbols",
    "lsp_document_symbols",
    "lsp_definition",
    "lsp_references",
    "lsp_diagnostics",
}

NO_EDIT_FINAL_KEYWORDS = {
    "belongs to another service",
    "cannot safely",
    "can't safely",
    "did not change",
    "did not modify",
    "insufficient evidence",
    "no changes",
    "no edits",
    "no files changed",
    "not safe",
    "out of scope",
    "target service",
    "unable to safely",
    "不在当前仓库",
    "不做修改",
    "不能安全",
    "不应包含",
    "不应强行",
    "不安全",
    "不属于当前仓库",
    "依赖不足",
    "停止实施",
    "当前仓库不包含",
    "当前授权",
    "没有修改",
    "未修改",
    "无改动",
    "无法安全",
    "目标服务",
    "证据不足",
}
INCOMPLETE_FINAL_MARKERS = {
    "ready to output",
    "ready to provide",
    "ready to generate",
    "final table follows",
    "final answer follows",
    "准备输出",
    "可以输出",
    "马上输出",
}
FINAL_TABLE_REQUEST_KEYWORDS = {
    "markdown table",
    "table",
    "表格",
    "分析表",
}
FINAL_LABEL_CANDIDATES = (
    "必须关注",
    "可能关注",
    "暂不关注",
    "需要用户确认",
    "必须关心",
    "可能关心",
    "暂不关心",
)
PROJECT_SCOPE_TABLE_REQUEST_KEYWORDS = {
    "项目表",
    "项目范围",
    "项目清单",
    "服务范围",
    "关注项目",
    "关心项目",
    "需要关注哪些项目",
    "project scope",
    "service scope",
}
PROJECT_SCOPE_TABLE_COLUMN_KEYWORDS = {
    "项目",
    "服务",
    "project",
    "service",
}
EVIDENCE_REQUEST_KEYWORDS = {
    "代码证据",
    "代码依据",
    "源码",
    "代码里",
    "代码中",
    "源码中",
    "source evidence",
    "source-code evidence",
    "code evidence",
}
NO_SPECULATION_REQUEST_KEYWORDS = {
    "不需要推测",
    "不要推测",
    "别推测",
    "不要猜",
    "别猜",
    "不靠猜",
    "不要行业惯例",
    "no speculation",
    "don't guess",
    "do not guess",
}
IMPLEMENTATION_EVIDENCE_REQUEST_KEYWORDS = {
    "怎么解决",
    "怎么处理",
    "如何处理",
    "如何实现",
    "怎么实现",
    "实现逻辑",
    "处理逻辑",
    "解决方案",
    "在哪里实现",
    "哪里实现",
}
SOURCE_NOT_FOUND_MARKERS = {
    "未找到",
    "没有找到",
    "未发现",
    "没有发现",
    "找不到",
    "无代码证据",
    "缺少代码证据",
    "没有代码证据",
    "not found",
    "no matches",
    "no evidence",
    "not located",
}
SOURCE_INCOMPLETE_READ_MARKERS = {
    "仅读取",
    "仅部分源码",
    "读取不完整",
    "未包含",
    "未在当前截获内容",
    "需补充读取",
    "需要补充读取",
    "only read",
    "partial source",
    "incomplete read",
}
SOURCE_FALSE_NEGATIVE_STOPWORDS = {
    "agent",
    "api",
    "budget",
    "code",
    "context",
    "final",
    "guard",
    "mini",
    "msp",
    "pay",
    "source",
    "test",
    "token",
}
EVIDENCE_STATUS_REQUEST_KEYWORDS = {
    "已验证",
    "推断",
    "证据状态",
    "verified",
    "inferred",
}
INFERENCE_MARKERS = {
    "可能",
    "推测",
    "猜测",
    "大概",
    "应该",
    "未验证",
    "likely",
    "possibly",
    "probably",
    "inferred",
    "guess",
}
EVIDENCE_STATUS_LABELS = {
    "已验证",
    "推断",
    "verified",
    "inferred",
    "证据支持",
    "未验证",
}
TODO_REQUEST_KEYWORDS = {
    "todo",
    "task list",
    "checklist",
    "待办",
    "任务清单",
    "维护 todo",
    "维护todo",
}


@dataclass(frozen=True)
class SourceEvidence:
    path: str
    content: str


@dataclass(frozen=True)
class FinalAnswerContext:
    request: str | None
    content: str
    messages: list[dict[str, Any]]
    run_start_index: int
    requirement_contract: RequirementContract | None
    tool_results: list[ToolResultSummary]
    read_file_evidence_paths: list[str]
    source_evidence: list[SourceEvidence]
    open_todos: list[str]
    is_code_implementation_request: bool
    steer_counts: dict[str, int]


@dataclass(frozen=True)
class SteeringDecision:
    kind: str
    message: str
    payload: dict[str, Any]
    force_final_answer_without_tools: bool = True
    temporary_tool_allowlist: set[str] | None = None


class FinalAnswerSteerer(Protocol):
    kind: str

    def decide(self, context: FinalAnswerContext) -> SteeringDecision | None:
        """Return a steering decision when a final answer needs correction."""


class ReadOnlyEvidenceSteerer:
    kind = "read_only_evidence"

    def __init__(self, *, max_steers: int) -> None:
        self._max_steers = max_steers

    def decide(self, context: FinalAnswerContext) -> SteeringDecision | None:
        if context.steer_counts.get(self.kind, 0) >= self._max_steers:
            return None
        if not request_needs_read_only_code_evidence(context.request):
            return None
        if has_successful_read_file_since(context.messages, context.run_start_index):
            return None
        if content_reports_no_source_evidence(context.content) and has_negative_source_evidence_since(
            context.messages,
            context.run_start_index,
        ):
            return None
        content_summary = one_line(context.content, max_chars=800)
        steering = (
            "Runtime steering: the user asked for code/source evidence, but the previous answer was not grounded "
            "in a successful read_file result from this run. Do not give an industry-practice or filename-based "
            "guess.\n"
            "- Use search_code or lsp_* to locate candidate files, then read_file the relevant implementation file.\n"
            "- If searches return no matches, state that as a verified negative result and include the search terms.\n"
            "- After collecting code evidence, answer the original question directly and separate verified facts from inference.\n"
            f"- Draft final answer that triggered this check: {content_summary}"
            f"{final_answer_request_summary(context.request)}"
        )
        return SteeringDecision(
            kind=self.kind,
            message=steering,
            payload={},
            force_final_answer_without_tools=False,
            temporary_tool_allowlist=set(READ_ONLY_EVIDENCE_TOOLS),
        )


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
        )


class SourceGroundedNumericSteerer:
    kind = "source_grounded_numeric"

    def __init__(self, *, max_steers: int) -> None:
        self._max_steers = max_steers

    def decide(self, context: FinalAnswerContext) -> SteeringDecision | None:
        if context.steer_counts.get(self.kind, 0) >= self._max_steers:
            return None
        if not context.source_evidence:
            return None
        if not request_needs_source_grounded_numeric_facts(context.request, context.content):
            return None
        issues = source_numeric_issues(context.content, context.source_evidence)
        if not issues:
            return None
        issue_lines: list[str] = []
        for issue in issues[:5]:
            issue_lines.append(f"- Unsupported numeric/source claim: {issue['claim']}")
            issue_lines.append(f"  Evidence file: {issue['path']}")
            for snippet in issue["snippets"][:8]:
                issue_lines.append(f"  {snippet}")
        steering = (
            "Runtime steering: the previous final answer contains numeric/status/interface facts that do not match "
            "the source snippets read in this run. Do not call tools. Rewrite the final answer using only exact "
            "values from the evidence below; if a value is not present, mark it as 未找到/未确认 instead of inventing it.\n"
            + "\n".join(issue_lines)
            + f"{final_answer_request_summary(context.request)}"
        )
        return SteeringDecision(
            kind=self.kind,
            message=steering,
            payload={"issues": [issue["summary"] for issue in issues[:5]]},
        )


class SourceEvidenceFalseNegativeSteerer:
    kind = "source_evidence_false_negative"

    def __init__(self, *, max_steers: int) -> None:
        self._max_steers = max_steers

    def decide(self, context: FinalAnswerContext) -> SteeringDecision | None:
        if context.steer_counts.get(self.kind, 0) >= self._max_steers:
            return None
        if not context.source_evidence:
            return None
        if not request_needs_read_only_code_evidence(context.request):
            return None
        if not content_claims_source_missing_or_incomplete(context.content):
            return None
        issues = source_false_negative_issues(context.request or "", context.content, context.source_evidence)
        if not issues:
            return None
        issue_lines: list[str] = []
        for issue in issues[:5]:
            issue_lines.append(f"- Claimed missing/incomplete, but evidence contains: {', '.join(issue['terms'])}")
            issue_lines.append(f"  Evidence file: {issue['path']}")
            for snippet in issue["snippets"][:8]:
                issue_lines.append(f"  {snippet}")
        steering = (
            "Runtime steering: the previous final answer said source evidence was missing or incomplete, but "
            "the already-read source snippets below contain requested symbols/facts. Do not call tools. Rewrite "
            "the final answer from these snippets; only mark a specific item 未找到/未确认 when it is absent from "
            "the evidence below.\n"
            + "\n".join(issue_lines)
            + final_answer_request_summary(context.request)
        )
        return SteeringDecision(
            kind=self.kind,
            message=steering,
            payload={"issues": [issue["summary"] for issue in issues[:5]]},
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


def final_answer_request_summary(request: str | None) -> str:
    if not request:
        return ""
    return "\n\nOriginal user request to satisfy now:\n" f"- {one_line(request, max_chars=1200)}"


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


def request_needs_read_only_code_evidence(request: str | None) -> bool:
    lowered = (request or "").lower()
    if not lowered.strip():
        return False
    if any(keyword.lower() in lowered for keyword in NO_SPECULATION_REQUEST_KEYWORDS):
        return True
    if any(keyword.lower() in lowered for keyword in EVIDENCE_REQUEST_KEYWORDS):
        return True
    return any(keyword.lower() in lowered for keyword in IMPLEMENTATION_EVIDENCE_REQUEST_KEYWORDS)


def has_successful_read_file_since(messages: list[dict[str, Any]], start_index: int) -> bool:
    for message in messages[start_index:]:
        if message.get("role") != "tool":
            continue
        if message.get("_lca_tool_name") == "read_file" and not message.get("_lca_is_error"):
            return True
    return False


def has_negative_source_evidence_since(messages: list[dict[str, Any]], start_index: int) -> bool:
    for message in messages[start_index:]:
        if message.get("role") != "tool" or message.get("_lca_is_error"):
            continue
        name = str(message.get("_lca_tool_name") or "")
        if (name == "search_code" or name.startswith("lsp_")) and message.get("_lca_useless"):
            return True
    return False


def content_reports_no_source_evidence(content: str) -> bool:
    lowered = content.lower()
    return any(marker.lower() in lowered for marker in SOURCE_NOT_FOUND_MARKERS)


def content_claims_source_missing_or_incomplete(content: str) -> bool:
    lowered = content.lower()
    return any(marker.lower() in lowered for marker in SOURCE_NOT_FOUND_MARKERS | SOURCE_INCOMPLETE_READ_MARKERS)


def source_false_negative_issues(
    request: str,
    content: str,
    evidence: list[SourceEvidence],
) -> list[dict[str, Any]]:
    terms = request_source_terms(request)
    if not terms:
        return []
    lowered_content = content.lower()
    issues: list[dict[str, Any]] = []
    for item in evidence:
        lowered_source = item.content.lower()
        matched_terms = [
            term
            for term in terms
            if term.lower() in lowered_source
            and (term.lower() in lowered_content or content_claims_source_missing_or_incomplete(content))
        ]
        if not matched_terms:
            continue
        snippets = _source_snippets_for_terms(item.content, matched_terms)
        if not snippets:
            continue
        issues.append(
            {
                "path": item.path,
                "terms": matched_terms[:8],
                "snippets": snippets,
                "summary": f"{item.path}: evidence contains {', '.join(matched_terms[:5])}",
            }
        )
    return issues


def request_source_terms(request: str) -> list[str]:
    raw_terms = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", request or "")
    seen: set[str] = set()
    terms: list[str] = []
    for term in raw_terms:
        lowered = term.lower()
        if lowered in seen or lowered in SOURCE_FALSE_NEGATIVE_STOPWORDS:
            continue
        seen.add(lowered)
        terms.append(term)
    return terms[:24]


def request_mentions_todo(content: str | None) -> bool:
    lowered = (content or "").lower()
    return any(keyword in lowered for keyword in TODO_REQUEST_KEYWORDS)


def tool_names_since(messages: list[dict[str, Any]], start_index: int) -> set[str]:
    names: set[str] = set()
    for message in messages[start_index:]:
        if message.get("role") == "tool":
            name = message.get("_lca_tool_name")
            if isinstance(name, str) and name:
                names.add(name)
        elif message.get("role") == "assistant":
            names.update(assistant_tool_call_names(message))
    return names


def assistant_tool_call_names(message: dict[str, Any]) -> list[str]:
    names: list[str] = []
    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        return names
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function") or {}
        if isinstance(function, dict) and function.get("name"):
            names.append(str(function["name"]))
    return names


def request_needs_source_grounded_numeric_facts(request: str | None, content: str) -> bool:
    lowered_request = (request or "").lower()
    if not any(char.isdigit() for char in content):
        return False
    if any(keyword.lower() in lowered_request for keyword in EVIDENCE_REQUEST_KEYWORDS):
        return True
    if any(keyword.lower() in lowered_request for keyword in IMPLEMENTATION_EVIDENCE_REQUEST_KEYWORDS):
        return True
    return any(term in content for term in {"Enum", "Status", "状态", "code", "接口", "字段"})


def source_numeric_issues(content: str, evidence: list[SourceEvidence]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    evidence_by_key = _evidence_by_key(evidence)
    for claim in _numeric_claim_lines(content):
        claim_numbers = _number_tokens(claim)
        if not claim_numbers:
            continue
        matched = _matching_evidence(claim, evidence_by_key)
        for item in matched:
            missing = [number for number in claim_numbers if number not in _number_tokens(item.content)]
            if not missing:
                continue
            snippets = _source_snippets_for_claim(item.content, claim)
            issues.append(
                {
                    "claim": one_line(claim, max_chars=220),
                    "path": item.path,
                    "snippets": snippets,
                    "summary": f"{item.path}: missing numbers {', '.join(missing)} for claim {one_line(claim, max_chars=120)}",
                }
            )
            break
    return issues


def _evidence_by_key(evidence: list[SourceEvidence]) -> dict[str, SourceEvidence]:
    by_key: dict[str, SourceEvidence] = {}
    for item in evidence:
        path_parts = re.split(r"[/\\]", item.path)
        filename = path_parts[-1] if path_parts else item.path
        stem = filename.rsplit(".", 1)[0]
        for key in {filename, stem}:
            if key:
                by_key[key.lower()] = item
    return by_key


def _matching_evidence(claim: str, evidence_by_key: dict[str, SourceEvidence]) -> list[SourceEvidence]:
    lowered_claim = claim.lower()
    matched: list[SourceEvidence] = []
    for key, item in evidence_by_key.items():
        if key and key in lowered_claim and item not in matched:
            matched.append(item)
    claim_identifiers = _claim_identifiers(claim)
    for item in evidence_by_key.values():
        if item in matched:
            continue
        lowered_source = item.content.lower()
        if any(identifier in lowered_source for identifier in claim_identifiers):
            matched.append(item)
    return matched


def _numeric_claim_lines(content: str) -> list[str]:
    claims: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or not any(char.isdigit() for char in stripped):
            continue
        if (
            any(token in stripped for token in {"Enum", "Status", "状态", "枚举", "code", "接口", "字段", "Controller"})
            or _claim_identifiers(stripped)
            or _looks_like_numeric_table_row(stripped)
        ):
            claims.append(stripped)
    return claims


def _claim_identifiers(claim: str) -> set[str]:
    identifiers: set[str] = set()
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", claim):
        if len(token) < 3:
            continue
        if token.lower() in SOURCE_FALSE_NEGATIVE_STOPWORDS:
            continue
        if token.isupper() or "_" in token or any(char.isupper() for char in token[1:]):
            identifiers.add(token.lower())
    return identifiers


def _looks_like_numeric_table_row(line: str) -> bool:
    if not (line.startswith("|") and line.endswith("|")):
        return False
    cells = [cell.strip() for cell in line.strip("|").split("|")]
    return any(re.fullmatch(r"-?\d+", cell) for cell in cells)


def _number_tokens(content: str) -> set[str]:
    without_path_lines = re.sub(
        r"(?i)\b[\w./\\-]+\.(?:java|vue|ts|tsx|js|jsx|py|xml|md|yml|yaml|properties):\d+\b",
        "",
        content,
    )
    without_read_file_line_numbers = re.sub(r"(?m)^\s*\d+:", "", without_path_lines)
    return set(re.findall(r"(?<![\w.])-?\d+(?![\w.])", without_read_file_line_numbers))


def _source_snippets_for_claim(source_content: str, claim: str) -> list[str]:
    claim_terms = {
        token.lower()
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]{2,}", claim)
        if len(token) >= 2
    }
    snippets: list[str] = []
    for line in source_content.splitlines():
        lowered = line.lower()
        if any(term in lowered for term in claim_terms) or any(char.isdigit() for char in line):
            snippets.append(line)
        if len(snippets) >= 12:
            break
    return snippets or source_content.splitlines()[:8]


def _source_snippets_for_terms(source_content: str, terms: list[str]) -> list[str]:
    lowered_terms = [term.lower() for term in terms if term]
    snippets: list[str] = []
    lines = source_content.splitlines()
    for index, line in enumerate(lines):
        lowered = line.lower()
        if not any(term in lowered for term in lowered_terms):
            continue
        start = max(0, index - 2)
        end = min(len(lines), index + 3)
        for snippet in lines[start:end]:
            if snippet not in snippets:
                snippets.append(snippet)
    return snippets[:12] or source_content.splitlines()[:8]


def one_line(content: str, *, max_chars: int = 240) -> str:
    normalized = " ".join(content.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 14] + "...<truncated>"

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Any, Protocol

from ..requirement_evidence import RequirementEvidence
from ..task_contract import RequirementContract
from ..tool_choice_queue import ToolResultSummary
from ..verification_plan import VerificationPlan

NO_EDIT_FINAL_HYGIENE_TOOLS = {"todo_read", "todo_add", "todo_update", "git_status", "git_diff"}
READ_ONLY_EVIDENCE_TOOLS = {
    "glob_files",
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
# A scoped negative such as "未找到结算单实体" is not a claim that source
# evidence is absent. It is an allowed conclusion when paired with the
# matching discovery evidence and is checked by NegativeExistenceSteerer.
# This guard is reserved for the stronger failure mode: the answer says the
# code/source evidence itself was not read or is unavailable despite relevant
# snippets having already been read.
SOURCE_EVIDENCE_ABSENCE_MARKERS = {
    "未找到代码证据",
    "未找到源码证据",
    "没有代码证据",
    "没有源码证据",
    "无代码证据",
    "无源码证据",
    "代码证据缺失",
    "源码证据缺失",
    "代码不完整",
    "源码不完整",
    "未读取源码",
    "未读源码",
    "no code evidence",
    "no source evidence",
    "source evidence missing",
    "source incomplete",
    "incomplete source",
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
    "line",
    "path",
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
KNOWN_TOOL_EVIDENCE_NAMES = frozenset(
    {
        "apply_patch",
        "ask_user",
        "git_diff",
        "git_status",
        "glob_files",
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
TOOL_EVIDENCE_CLAIM_MARKERS = (
    "based on",
    "called",
    "call ",
    "no result",
    "no results",
    "not provide",
    "not return",
    "result",
    "returned",
    "tool output",
    "根据",
    "调用",
    "结果",
    "返回",
    "通过",
    "未提供",
    "未返回",
    "无结果",
    "没有结果",
    "均未",
    "使用",
    "执行",
    "运行",
    "检查",
    "查找",
    "检索",
    "used",
    "executed",
    "ran",
    "checked",
    "searched",
    "found",
)

_EXPLICIT_TOOL_NON_EXECUTION = re.compile(
    r"(?:未|没有|并未|未曾)\s*(?:调用|使用|执行|运行|检查|查找|检索)"
    r"|(?:did\s+not|didn't|not)\s+(?:call|use|run|execute|check|search)",
    re.IGNORECASE,
)



class FinalAnswerSteeringSeverity(str, Enum):
    """Whether a failed final rewrite may safely reuse the previous draft."""

    PRESENTATION = "presentation"
    HARD = "hard"


@dataclass(frozen=True)
class SourceEvidence:
    path: str
    content: str
    root: str | None = field(default=None, compare=False)
    scope: str = field(default="root_local", compare=False)
    origin: str = field(default="current_run", compare=False)


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
    requirement_evidence: list[RequirementEvidence] = field(default_factory=list)
    required_design_evidence_roots: tuple[str, ...] = ()
    design_evidence_read_paths: list[str] = field(default_factory=list)
    verification_plan: VerificationPlan | None = None


@dataclass(frozen=True)
class SteeringDecision:
    kind: str
    message: str
    payload: dict[str, Any]
    force_final_answer_without_tools: bool = True
    temporary_tool_allowlist: set[str] | None = None
    severity: FinalAnswerSteeringSeverity = FinalAnswerSteeringSeverity.HARD


class FinalAnswerSteerer(Protocol):
    kind: str

    def decide(self, context: FinalAnswerContext) -> SteeringDecision | None:
        """Return a steering decision when a final answer needs correction."""



def final_answer_request_summary(request: str | None) -> str:
    if not request:
        return ""
    return "\n\nOriginal user request to satisfy now:\n" f"- {one_line(request, max_chars=1200)}"



def one_line(content: str, *, max_chars: int = 240) -> str:
    normalized = " ".join(content.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 14] + "...<truncated>"

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from dataclasses import dataclass, field, replace
from pathlib import Path
import time
from typing import Any

from .compaction import SUMMARY_INPUT_CHAR_LIMIT
from .compaction import SUMMARY_OUTPUT_CHAR_LIMIT
from .compaction import SUMMARY_REQUEST_TIMEOUT
from .compaction import assistant_snippets as _assistant_snippets
from .compaction import estimate_message_chars as _estimate_message_chars
from .compaction import estimate_message_tokens as _estimate_message_tokens
from .compaction import format_llm_compaction_summary as _format_llm_compaction_summary
from .compaction import messages_to_summary_transcript as _messages_to_summary_transcript
from .compaction import provider_safe_messages as _provider_safe_messages
from .compaction import prune_context_tool_outputs as _prune_context_tool_outputs
from .compaction import resolve_compaction_threshold_chars as _resolve_compaction_threshold_chars
from .compaction import resolve_compaction_threshold_tokens as _resolve_compaction_threshold_tokens
from .compaction import snippets_for_role as _snippets_for_role
from .compaction import summary_cache_key as _summary_cache_key
from .compaction import summary_request_content as _summary_request_content
from .compaction import tool_snippets as _tool_snippets
from .compaction import truncate_recent_tool_outputs as _truncate_recent_tool_outputs
from .compaction import valid_recent_messages as _valid_recent_messages
from .config import AgentConfig
from .config import normalize_approval_mode
from .llm import LlmError
from .llm import OpenAICompatibleClient
from .patch.anchored import display_workspace_path
from .patch.anchored import PatchError
from .patch.anchored import resolve_workspace_path
from .planner import render_planner_explore_context
from .protocol.events import AgentEvent
from .protocol.events import EventEmitter
from .protocol.events import EventSink
from .protocol.events import NullEventSink
from .protocol.events import StderrEventSink
from .session.jsonl_store import JsonlSessionStore
from .state import default_config_root
from .steering.final_answer import FinalAnswerContext
from .steering.final_answer import FinalAnswerSteerer
from .steering.final_answer import FinalStructureSteerer
from .steering.final_answer import CompletionAuditSteerer
from .steering.final_answer import NoEditFinalHygieneSteerer
from .steering.final_answer import READ_ONLY_EVIDENCE_TOOLS
from .steering.final_answer import ReadOnlyEvidenceSteerer
from .steering.final_answer import SourceEvidenceFalseNegativeSteerer
from .steering.final_answer import request_mentions_todo
from .steering.final_answer import SourceEvidence
from .steering.final_answer import SourceGroundedNumericSteerer
from .steering.final_answer import SteeringDecision
from .task_contract import generate_requirement_contract
from .task_contract import render_contract_context
from .task_contract import RequirementContract
from .tools import create_default_registry
from .tools.base import ToolContext
from .tools.base import ToolResult
from .tools.base import tool_state_dir
from .tools.git import capture_git_baseline
from .tools.relevance import is_analysis_only_request
from .tools.relevance import is_code_implementation_request
from .tools.relevance import is_low_relevance_patch_path
from .tools.relevance import path_matches_any
from .tools.relevance import request_mentions_config_or_path
from .tool_choice_queue import ToolChoiceDecision
from .tool_choice_queue import ToolChoiceQueue
from .tool_choice_queue import ToolResultSummary


SYSTEM_PROMPT = """You are a local coding agent running inside a user's workspace.

Default working style:
- Work from local evidence, not guesses. Choose the tools yourself; the user should not need to spell out tool order.
- For repo understanding, start with list_files/search_code/read_file as needed. For code navigation in Python, Java, JavaScript, TypeScript, or Vue, prefer lsp_symbols/lsp_definition/lsp_references/lsp_diagnostics before broad text search when helpful. lsp_workspace_symbols and lsp_document_symbols are compatibility aliases for lsp_symbols. Read the exact file or range before editing it.
- The primary --cwd is the main workspace. If additional directories are configured, file/search/LSP/patch tools may access those explicit paths; shell, git, session, todo, and memory remain anchored to --cwd.
- For multi-step coding or implementation work, maintain a concise todo list with todo_add/todo_update/todo_read. For pure read-only analysis, skip todo unless the user asks for it.
- If a requirement is ambiguous and guessing would affect the result, use ask_user. If local evidence is enough, continue without asking.
- For read-only tasks, do not modify files, run commands, or write memory unless the user asks.
- For edits to existing files, use read_file first, then apply_patch with the hash tag returned by read_file. Preview meaningful edits with dry_run=true before writing unless the user explicitly says to skip preview.
- For insertions, use apply_patch with mode=insert_before or mode=insert_after instead of empty replacements.
- After changes, run the most relevant tests or checks available in the workspace. If you cannot run them, say why.
- Inspect git_diff after writing so the final answer can summarize exactly what changed.
- Do not claim a command, test, or diff passed unless you actually ran the relevant tool.
- Memory is advisory. Current user instructions and direct repository evidence override memory. Do not write memory or use learn unless the user asks you to remember something or a durable convention is clearly established.
- User/project AGENTS.md context and RULES.md sticky rules are advisory operating guidance. Current user instructions and direct repository evidence still take precedence when they conflict.
- Keep final answers concise and include changed files, verification, and any remaining risk.
"""

MEMORY_CONSOLIDATION_INPUT_CHAR_LIMIT = 14000
MEMORY_CONSOLIDATION_OUTPUT_CHAR_LIMIT = 8000
MEMORY_CONSOLIDATION_REQUEST_TIMEOUT = 30.0
MEMORY_CONSOLIDATION_MIN_AUTO_CHARS = 500
MEMORY_CONSOLIDATION_MAX_ITEMS_PER_BUCKET = 5
MEMORY_CONSOLIDATION_MAX_ITEM_CHARS = 700
MEMORY_CONSOLIDATION_BUCKETS = ("project", "decisions", "conventions", "learned")
MEMORY_CONSOLIDATION_WRITE_TOOLS = {"memory_write", "learn"}
STARTUP_MEMORY_NAMES = ("project", "decisions", "conventions", "learned")
STARTUP_MEMORY_CHAR_LIMIT = 8000
STARTUP_CONTEXT_CHAR_LIMIT = 8000
STICKY_RULES_CHAR_LIMIT = 4000
CURRENT_TASK_CONTRACT_CHAR_LIMIT = 2000
STARTUP_SKILLS_CHAR_LIMIT = 4000
MAX_AUTHORED_SKILLS = 40
MAX_SKILL_DESCRIPTION_CHARS = 320
EVIDENCE_LEDGER_MAX_RECORDS = 30
EVIDENCE_LEDGER_CONTEXT_RECORDS = 18
EVIDENCE_LEDGER_CONTEXT_CHAR_LIMIT = 6000
MAX_IDENTICAL_TOOL_CALLS_IN_RECENT_WINDOW = 3
REPEAT_TOOL_CALL_WINDOW = 12
MAX_DUPLICATE_TOOL_GUARD_HITS = 8
MAX_DUPLICATE_TOOL_FINAL_ANSWER_STEERS = 2
MAX_USELESS_SEARCHES_PER_PATTERN_IN_RECENT_WINDOW = 8
USELESS_SEARCH_PATTERN_WINDOW = 20
MAX_USELESS_SEARCH_PATTERN_GUARD_HITS = 4
MAX_USELESS_SEARCH_PATTERN_FINAL_ANSWER_STEERS = 2
MAX_USELESS_LSP_SYMBOL_QUERIES_IN_RECENT_WINDOW = 12
USELESS_LSP_SYMBOL_QUERY_WINDOW = 24
MAX_USELESS_LSP_SYMBOL_GUARD_HITS = 4
MAX_USELESS_LSP_SYMBOL_FINAL_ANSWER_STEERS = 2
MAX_READ_FILE_CALLS_PER_FILE_IN_RECENT_WINDOW = 8
READ_FILE_PATH_WINDOW = 14
MAX_READ_FILE_SUCCESSES_PER_RANGE_IN_RUN = 3
MAX_REPEATED_READ_FILE_GUARD_HITS = 4
MAX_REPEATED_READ_FILE_FINAL_ANSWER_STEERS = 2
MAX_SEMANTIC_EXPLORATIONS_PER_KEY_IN_RECENT_WINDOW = 4
SEMANTIC_EXPLORATION_WINDOW = 20
MAX_SEMANTIC_EXPLORATION_GUARD_HITS = 4
MAX_SEMANTIC_EXPLORATION_STEERS = 2
MAX_SOFT_TOOL_REQUIREMENT_STEERS = 3
MAX_NO_EDIT_FINAL_HYGIENE_STEERS = 2
MAX_FINAL_STRUCTURE_STEERS = 2
MAX_READ_ONLY_EVIDENCE_STEERS = 2
MAX_SOURCE_EVIDENCE_FALSE_NEGATIVE_STEERS = 2
MAX_SOURCE_GROUNDED_NUMERIC_STEERS = 2
MAX_COMPLETION_AUDIT_STEERS = 2
MAX_TOOL_CHOICE_QUEUE_STEERS_PER_SIGNATURE = 1
INVALID_TOOL_CALL_NAME = "__invalid_tool_call"
WORKFLOW_NUDGE = (
    "For this coding task, infer the tool sequence yourself. "
    "Use local inspection and lsp_* code navigation before editing; use todo for multi-step work; use ask_user only when ambiguity affects the outcome; "
    "preview meaningful existing-file edits with apply_patch dry_run=true; verify changes with tests/checks and git_diff."
)

WORKFLOW_NUDGE_KEYWORDS = {
    "agent",
    "bug",
    "change",
    "code",
    "diff",
    "fix",
    "implement",
    "patch",
    "readme",
    "refactor",
    "review",
    "test",
    "update",
    "代码",
    "修改",
    "实现",
    "修复",
    "测试",
    "需求",
    "项目",
    "文档",
}

ALLOWED_DIR_REQUIREMENT_KEYWORDS = {
    "requirement",
    "requirements",
    "spec",
    "specs",
    "prd",
    "需求",
    "需求目录",
    "需求文档",
    "读取需求",
    "外部需求",
}
ALLOWED_DIR_DOC_SUFFIXES = {".md", ".txt", ".rst", ".html", ".htm"}
ALLOWED_DIR_DOC_NAME_KEYWORDS = {
    "requirement",
    "requirements",
    "spec",
    "prd",
    "handoff",
    "需求",
    "文档",
    "说明",
    "方案",
}
MAX_ALLOWED_DIR_DOC_CANDIDATES = 8
READ_FILE_DRIFT_GUARD_KEYWORDS = {
    "analysis",
    "analyze",
    "describe",
    "inspect",
    "review",
    "readonly",
    "read-only",
    "只读",
    "分析",
    "总结",
    "阅读",
    "定位",
    "压测",
}
READ_FILE_DRIFT_GUARD_STRONG_READONLY_KEYWORDS = {
    "do not edit files",
    "do not modify files",
    "don't edit files",
    "don't modify files",
    "no edits",
    "read-only",
    "readonly",
    "不要改文件",
    "不要修改文件",
    "不要写文件",
    "不修改文件",
    "不写文件",
    "禁止修改文件",
    "只读",
}
READ_FILE_DRIFT_GUARD_EDIT_KEYWORDS = {
    "apply_patch",
    "change",
    "edit",
    "fix",
    "implement",
    "modify",
    "patch",
    "write",
    "修改",
    "修复",
    "实现",
    "写入",
}
@dataclass
class SoftToolRequirement:
    kind: str
    allowed_dirs: tuple[Path, ...]
    candidate_files: tuple[Path, ...] = ()
    steers: int = 0
    satisfied: bool = False


@dataclass(frozen=True)
class EvidenceRecord:
    tool: str
    subject: str
    summary: str
    status: str = "ok"

    def render(self) -> str:
        return f"- [{self.status}] {self.tool} {self.subject}: {self.summary}"


@dataclass
class RunStats:
    run_id: str
    prompt_chars: int
    started_monotonic: float
    llm_requests: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    useless_tool_results: int = 0
    synthetic_tool_results: int = 0
    compactions: int = 0
    llm_context_summaries: int = 0
    local_context_summaries: int = 0
    tool_counts: dict[str, int] = field(default_factory=dict)
    guard_start: dict[str, int] = field(default_factory=dict)
    steer_start: dict[str, int] = field(default_factory=dict)


class AgentRuntime:
    def __init__(
        self,
        config: AgentConfig,
        *,
        show_tool_logs: bool = True,
        session_id: str | None = None,
        continue_session: bool = False,
        event_sink: EventSink | None = None,
    ):
        self._config = config
        self._client = OpenAICompatibleClient(config)
        self._registry = create_default_registry()
        self._session_tool_approval: dict[str, str] = {}
        self._summary_cache: dict[str, str] = {}
        self._recent_tool_call_signatures: list[str] = []
        self._recent_useless_search_pattern_keys: list[str] = []
        self._recent_useless_lsp_symbol_query_keys: list[str] = []
        self._recent_read_file_path_keys: list[str] = []
        self._recent_semantic_exploration_keys: list[str] = []
        self._read_file_range_counts: dict[tuple[str, int, str], int] = {}
        self._duplicate_tool_guard_hits = 0
        self._duplicate_tool_final_answer_steers = 0
        self._useless_search_pattern_guard_hits = 0
        self._useless_search_pattern_final_answer_steers = 0
        self._useless_lsp_symbol_guard_hits = 0
        self._useless_lsp_symbol_final_answer_steers = 0
        self._repeated_read_file_guard_hits = 0
        self._repeated_read_file_final_answer_steers = 0
        self._semantic_exploration_guard_hits = 0
        self._semantic_exploration_steers = 0
        self._no_edit_final_hygiene_steers = 0
        self._final_structure_steers = 0
        self._read_only_evidence_steers = 0
        self._source_evidence_false_negative_steers = 0
        self._source_grounded_numeric_steers = 0
        self._completion_audit_steers = 0
        self._read_file_evidence_paths: list[str] = []
        self._source_evidence: list[SourceEvidence] = []
        self._strong_relevance_paths: list[str] = []
        self._evidence_records: list[EvidenceRecord] = []
        self._workspace_root_evidence_recorded = False
        self._current_user_request: str | None = None
        self._read_file_drift_guard_enabled = False
        self._force_final_answer_without_tools = False
        self._temporary_tool_allowlist: set[str] | None = None
        self._tool_choice_queue = ToolChoiceQueue()
        self._tool_choice_allowed_tool_names: set[str] | None = None
        self._tool_choice_steering_signatures: set[str] = set()
        self._tool_choice_results: list[ToolResultSummary] = []
        self._tool_choice_tool_names: list[str] = []
        self._requirement_contract: RequirementContract | None = None
        self._requirement_contract_context = ""
        self._soft_tool_requirement: SoftToolRequirement | None = None
        self._run_stats: RunStats | None = None
        self._last_run_summary: dict[str, Any] | None = None
        self._pending_compaction_summary_mode: str | None = None
        self._final_answer_steerers: tuple[FinalAnswerSteerer, ...] = (
            ReadOnlyEvidenceSteerer(max_steers=MAX_READ_ONLY_EVIDENCE_STEERS),
            NoEditFinalHygieneSteerer(max_steers=MAX_NO_EDIT_FINAL_HYGIENE_STEERS),
            FinalStructureSteerer(max_steers=MAX_FINAL_STRUCTURE_STEERS),
            SourceEvidenceFalseNegativeSteerer(max_steers=MAX_SOURCE_EVIDENCE_FALSE_NEGATIVE_STEERS),
            SourceGroundedNumericSteerer(max_steers=MAX_SOURCE_GROUNDED_NUMERIC_STEERS),
            CompletionAuditSteerer(max_steers=MAX_COMPLETION_AUDIT_STEERS),
        )
        self._state_dir = config.state_dir or config.workspace / ".local-agent"
        self._session = JsonlSessionStore(
            config.workspace,
            state_dir=self._state_dir,
            session_id=session_id,
            continue_recent=continue_session,
        )
        sink = event_sink if event_sink is not None else StderrEventSink() if show_tool_logs else NullEventSink()
        self._events = EventEmitter(
            session_id=self._session.session_id,
            sink=sink,
            recorder=self._record_event_v1,
        )
        self._user_config_dir = default_config_root()
        system_prompt = _system_prompt_with_startup_context(
            config.workspace,
            self._user_config_dir,
            state_dir=self._state_dir,
            allowed_dirs=config.allowed_dirs,
        )
        self._messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *self._session.load_messages(),
        ]
        self._tool_context = ToolContext(
            workspace=config.workspace,
            approval_mode=config.approval_mode,
            state_dir=self._state_dir,
            allowed_dirs=config.allowed_dirs,
            session_id=self._session.session_id,
            auto_approve_tools=config.auto_approve_tools,
            tool_approval=config.tool_approval,
            session_tool_approval=self._session_tool_approval,
            event_callback=self._emit_event,
        )
        self._events.emit(
            "SessionStarted",
            {
                "workspace": str(config.workspace),
                "state_dir": str(self._state_dir),
                "provider": config.provider,
                "continued": bool(continue_session or session_id),
            },
        )

    def run(self, prompt: str) -> str:
        run_id = self._events.start_run()
        started_monotonic = time.monotonic()
        self._run_stats = self._new_run_stats(run_id, prompt, started_monotonic)
        deadline = (
            started_monotonic + self._config.budget_seconds
            if self._config.budget_seconds is not None
            else None
        )
        git_baseline = capture_git_baseline(self._config.workspace)
        self._session.append("git_baseline", git_baseline)
        run_start_index = len(self._messages)
        model_prompt = _with_workflow_nudge(prompt)
        self._current_user_request = prompt
        self._no_edit_final_hygiene_steers = 0
        self._final_structure_steers = 0
        self._read_only_evidence_steers = 0
        self._source_evidence_false_negative_steers = 0
        self._source_grounded_numeric_steers = 0
        self._source_evidence = []
        self._temporary_tool_allowlist = None
        self._tool_choice_allowed_tool_names = None
        self._tool_choice_steering_signatures = set()
        self._tool_choice_results = []
        self._tool_choice_tool_names = []
        self._requirement_contract = generate_requirement_contract(prompt)
        self._requirement_contract_context = render_contract_context(self._requirement_contract)
        self._messages.append({"role": "user", "content": model_prompt})
        self._session.append("user", {"content": prompt})
        self._events.emit("UserMessage", {"content": prompt})
        self._session.append(
            "runtime_steering",
            {
                "kind": "requirement_contract",
                "task_kind": self._requirement_contract.task_kind,
                "objective": self._requirement_contract.objective,
            },
        )
        if model_prompt != prompt:
            self._session.append("workflow_nudge", {"content": WORKFLOW_NUDGE})
        self._read_file_drift_guard_enabled = _should_guard_repeated_read_file(prompt)
        self._soft_tool_requirement = _initial_soft_tool_requirement(
            prompt,
            self._config.workspace,
            self._config.allowed_dirs,
        )
        if self._soft_tool_requirement is not None:
            self._append_soft_tool_requirement_message(self._soft_tool_requirement)
        self._record_workspace_root_evidence()
        tool_context = replace(
            self._tool_context,
            deadline_monotonic=deadline,
            git_baseline=git_baseline,
            current_user_request=prompt,
            patch_relevance_checker=self._patch_relevance_denial_reason,
        )

        step = 1
        while self._config.max_steps == 0 or step <= self._config.max_steps:
            if self._deadline_exceeded(deadline):
                return self._stop_for_budget(deadline, run_start_index)

            self._record_llm_request()
            self._session.append("llm_request", {"step": step})
            self._apply_tool_choice_queue_if_needed()
            messages_for_model = self._messages_for_model(deadline)
            tools_for_model = self._tools_for_model()
            self._events.emit(
                "LlmRequest",
                {
                    "step": step,
                    "message_count": len(messages_for_model),
                    "tool_schema_count": len(tools_for_model),
                    "force_final_answer": self._force_final_answer_without_tools,
                },
            )
            force_final_answer = self._force_final_answer_without_tools
            self._force_final_answer_without_tools = False
            response = self._client.chat(
                messages_for_model,
                tools_for_model,
                timeout=self._remaining_timeout(deadline),
            )
            if force_final_answer:
                self._session.append("runtime_steering", {"kind": "forced_final_answer", "step": step})
            message = _provider_safe_assistant_message({**response.message, "role": "assistant"})
            self._messages.append(message)
            self._session.append("assistant", message)
            self._events.emit("AssistantMessage", _assistant_event_payload(message))

            tool_calls = message.get("tool_calls") or []
            if getattr(response, "finish_reason", None) == "length":
                self._append_synthetic_tool_results(tool_calls, self._length_stop_tool_message())
                return self._stop_for_length(deadline, run_start_index)
            if not tool_calls:
                if self._needs_soft_tool_requirement_steer():
                    if self._steer_for_soft_tool_requirement():
                        step += 1
                        continue
                    return self._finish_run(
                        self._soft_tool_requirement_stop_message(),
                        deadline,
                        run_start_index,
                        reason="soft_tool_requirement",
                    )
                content = message.get("content") or ""
                steering = self._decide_final_answer_steering(content, run_start_index)
                if steering is not None:
                    self._apply_final_answer_steering(steering)
                    step += 1
                    continue
                return self._finish_run(content, deadline, run_start_index)

            for index, tool_call in enumerate(tool_calls):
                if self._deadline_exceeded(deadline):
                    self._append_synthetic_tool_results(tool_calls[index:], self._budget_stop_message())
                    return self._stop_for_budget(deadline, run_start_index)
                function = tool_call.get("function") or {}
                name = function.get("name") or ""
                arguments = function.get("arguments") or "{}"
                self._log_tool_start(name, arguments)
                duplicate_hits_before = self._duplicate_tool_guard_hits
                useless_search_hits_before = self._useless_search_pattern_guard_hits
                useless_lsp_hits_before = self._useless_lsp_symbol_guard_hits
                repeated_read_hits_before = self._repeated_read_file_guard_hits
                semantic_exploration_hits_before = self._semantic_exploration_guard_hits
                try:
                    result = self._execute_tool_with_repeat_guard(name, arguments, tool_context)
                except KeyboardInterrupt:
                    self._append_synthetic_tool_results(
                        tool_calls[index:],
                        "the user interrupted execution before the tool call completed.",
                    )
                    self._stop_for_interrupt()
                    raise
                self._log_tool_end(name, result.is_error, len(result.content))
                self._append_tool_result(
                    tool_call,
                    name,
                    result.content,
                    is_error=result.is_error,
                    useless=result.useless,
                )
                self._record_tool_choice_result(name, arguments, result)
                self._record_read_file_evidence(name, arguments, result)
                self._record_tool_evidence(name, arguments, result)
                self._observe_soft_tool_requirement(name, arguments, result)
                duplicate_skipped = self._duplicate_tool_guard_hits > duplicate_hits_before
                useless_search_skipped = self._useless_search_pattern_guard_hits > useless_search_hits_before
                useless_lsp_skipped = self._useless_lsp_symbol_guard_hits > useless_lsp_hits_before
                repeated_read_skipped = self._repeated_read_file_guard_hits > repeated_read_hits_before
                semantic_exploration_skipped = (
                    self._semantic_exploration_guard_hits > semantic_exploration_hits_before
                )
                if repeated_read_skipped and self._steer_after_repeated_read_file():
                    self._append_synthetic_tool_results(
                        tool_calls[index + 1 :],
                        self._repeated_read_file_stop_message(),
                    )
                    break
                if self._repeated_read_file_guard_hits >= MAX_REPEATED_READ_FILE_GUARD_HITS:
                    self._append_synthetic_tool_results(
                        tool_calls[index + 1 :],
                        self._repeated_read_file_stop_message(),
                    )
                    return self._stop_for_repeated_read_file(deadline, run_start_index)
                if semantic_exploration_skipped and self._steer_after_semantic_exploration():
                    self._append_synthetic_tool_results(
                        tool_calls[index + 1 :],
                        self._semantic_exploration_stop_message(),
                    )
                    break
                if self._semantic_exploration_guard_hits >= MAX_SEMANTIC_EXPLORATION_GUARD_HITS:
                    self._append_synthetic_tool_results(
                        tool_calls[index + 1 :],
                        self._semantic_exploration_stop_message(),
                    )
                    return self._stop_for_semantic_exploration(deadline, run_start_index)
                if useless_search_skipped and self._steer_after_useless_search_pattern():
                    self._append_synthetic_tool_results(
                        tool_calls[index + 1 :],
                        self._useless_search_pattern_stop_message(),
                    )
                    break
                if self._useless_search_pattern_guard_hits >= MAX_USELESS_SEARCH_PATTERN_GUARD_HITS:
                    self._append_synthetic_tool_results(
                        tool_calls[index + 1 :],
                        self._useless_search_pattern_stop_message(),
                    )
                    return self._stop_for_useless_search_pattern(deadline, run_start_index)
                if useless_lsp_skipped and self._steer_after_useless_lsp_symbol_queries():
                    self._append_synthetic_tool_results(
                        tool_calls[index + 1 :],
                        self._useless_lsp_symbol_stop_message(),
                    )
                    break
                if self._useless_lsp_symbol_guard_hits >= MAX_USELESS_LSP_SYMBOL_GUARD_HITS:
                    self._append_synthetic_tool_results(
                        tool_calls[index + 1 :],
                        self._useless_lsp_symbol_stop_message(),
                    )
                    return self._stop_for_useless_lsp_symbol_queries(deadline, run_start_index)
                if duplicate_skipped and self._steer_after_duplicate_tool_call(name):
                    self._append_synthetic_tool_results(
                        tool_calls[index + 1 :],
                        self._duplicate_tool_stop_message(),
                    )
                    break
                if self._duplicate_tool_guard_hits >= MAX_DUPLICATE_TOOL_GUARD_HITS:
                    self._append_synthetic_tool_results(
                        tool_calls[index + 1 :],
                        self._duplicate_tool_stop_message(),
                    )
                    return self._stop_for_duplicate_tools(deadline, run_start_index)
                if self._deadline_exceeded(deadline):
                    self._append_synthetic_tool_results(tool_calls[index + 1 :], self._budget_stop_message())
                    return self._stop_for_budget(deadline, run_start_index)
            step += 1

        return self._finish_run(
            f"Stopped after reaching max_steps={self._config.max_steps}.",
            deadline,
            run_start_index,
            reason="max_steps",
        )

    def _tools_for_model(self) -> list[dict[str, Any]]:
        if self._force_final_answer_without_tools:
            return []
        allowed_names: set[str] | None = None
        if self._temporary_tool_allowlist is not None:
            allowed_names = set(self._temporary_tool_allowlist)
        requirement = self._soft_tool_requirement
        if requirement is not None and not requirement.satisfied:
            allowed_names = _intersect_optional_tool_allowlist(allowed_names, {"list_files", "read_file"})
        if self._tool_choice_allowed_tool_names is not None:
            allowed_names = _intersect_optional_tool_allowlist(allowed_names, self._tool_choice_allowed_tool_names)
        if allowed_names is None:
            return self._registry.schemas()
        return [
            schema
            for schema in self._registry.schemas()
            if schema.get("function", {}).get("name") in allowed_names
        ]

    def _apply_tool_choice_queue_if_needed(self) -> None:
        contract = self._requirement_contract
        if contract is None:
            self._tool_choice_allowed_tool_names = None
            return
        decision = self._tool_choice_queue.evaluate(
            task_kind=contract.task_kind,
            prompt=self._current_user_request or "",
            tool_names=self._tool_choice_tool_names,
            tool_results=self._tool_choice_results,
            available_tool_names=self._available_registry_tool_names(),
        )
        self._tool_choice_allowed_tool_names = set(decision.allowed_tool_names)
        if not decision.steering_required:
            return
        signature = _tool_choice_steering_signature(decision, len(self._tool_choice_results))
        if signature in self._tool_choice_steering_signatures:
            return
        if _tool_choice_signature_count(self._tool_choice_steering_signatures, decision.rule_id) >= (
            MAX_TOOL_CHOICE_QUEUE_STEERS_PER_SIGNATURE
        ):
            return
        self._tool_choice_steering_signatures.add(signature)
        content = _tool_choice_steering_message(decision, self._current_user_request)
        self._messages.append({"role": "user", "content": content})
        self._session.append(
            "runtime_steering",
            {
                "kind": "tool_choice_queue",
                "rule_id": decision.rule_id,
                "missing_requirements": list(decision.missing_requirements),
                "allowed_tool_names": sorted(decision.allowed_tool_names),
                "reason": decision.reason,
            },
        )

    def _available_registry_tool_names(self) -> tuple[str, ...]:
        if hasattr(self._registry, "tool_names"):
            return tuple(self._registry.tool_names())
        names: list[str] = []
        for schema in self._registry.schemas():
            name = schema.get("function", {}).get("name")
            if isinstance(name, str) and name:
                names.append(name)
        return tuple(names)

    def approval_summary(self) -> str:
        lines = [
            "Approval settings:",
            f"- mode: {self._tool_context.approval_mode}",
        ]
        config_policies = self._tool_context.tool_approval or {}
        if config_policies:
            lines.append("- configured tool policies:")
            for tool, policy in sorted(config_policies.items()):
                lines.append(f"  - {tool}: {policy}")
        else:
            lines.append("- configured tool policies: none")
        if self._session_tool_approval:
            lines.append("- session tool policies:")
            for tool, policy in sorted(self._session_tool_approval.items()):
                lines.append(f"  - {tool}: {policy}")
        else:
            lines.append("- session tool policies: none")
        return "\n".join(lines)

    def status_summary(self) -> str:
        lines = [
            "Runtime status:",
            f"- session: {self._session.session_id}",
            f"- workspace: {self._config.workspace}",
            f"- state_dir: {self._state_dir}",
            f"- provider: {self._config.provider}",
            f"- model: {self._config.model}",
            f"- approval_mode: {self._tool_context.approval_mode}",
            f"- budget_seconds: {_display_optional_int(self._config.budget_seconds)}",
            f"- max_steps: {self._config.max_steps}",
            f"- summary_mode: {self._config.summary_mode}",
            f"- memory_consolidation: {self._config.memory_consolidation}",
        ]
        if self._config.allowed_dirs:
            lines.append("- allowed_dirs:")
            lines.extend(f"  - {path}" for path in self._config.allowed_dirs)
        else:
            lines.append("- allowed_dirs: none")
        if self._tool_context.tool_approval:
            lines.append("- configured tool policies:")
            lines.extend(
                f"  - {tool}: {policy}"
                for tool, policy in sorted(self._tool_context.tool_approval.items())
            )
        else:
            lines.append("- configured tool policies: none")
        if self._last_run_summary is not None:
            lines.extend(_format_last_run_status(self._last_run_summary))
        else:
            lines.append("- last_run: none")
        return "\n".join(lines)

    def tool_summary(self) -> str:
        lines = ["Available tools:"]
        lines.extend(f"- {name}" for name in self._registry.tool_names())
        return "\n".join(lines)

    def set_session_approval_mode(self, mode: str) -> None:
        self._tool_context = replace(self._tool_context, approval_mode=normalize_approval_mode(mode))

    def set_session_tool_policy(self, tool: str, policy: str) -> None:
        tool = self._validate_known_tool_name(tool)
        normalized = policy.strip().lower()
        if normalized == "allow":
            self._session_tool_approval[tool] = "allow_always"
        elif normalized == "prompt":
            self._session_tool_approval[tool] = "prompt"
        elif normalized == "deny":
            self._session_tool_approval[tool] = "reject_always"
        else:
            raise ValueError("approval policy must be one of: allow, prompt, deny.")

    def reset_session_tool_policy(self, tool: str) -> None:
        self._session_tool_approval.pop(self._validate_known_tool_name(tool), None)

    def _validate_known_tool_name(self, tool: str) -> str:
        normalized = _validate_runtime_tool_name(tool)
        if not self._registry.has_tool(normalized):
            known = ", ".join(self._registry.tool_names())
            raise ValueError(f"unknown tool: {normalized}. Known tools: {known}")
        return normalized

    def _new_run_stats(self, run_id: str, prompt: str, started_monotonic: float) -> RunStats:
        return RunStats(
            run_id=run_id,
            prompt_chars=len(prompt),
            started_monotonic=started_monotonic,
            guard_start={
                "duplicate_tool": self._duplicate_tool_guard_hits,
                "useless_search_pattern": self._useless_search_pattern_guard_hits,
                "useless_lsp_symbol": self._useless_lsp_symbol_guard_hits,
                "repeated_read_file": self._repeated_read_file_guard_hits,
                "semantic_exploration": self._semantic_exploration_guard_hits,
            },
            steer_start={
                "duplicate_tool_final_answer": self._duplicate_tool_final_answer_steers,
                "useless_search_pattern_final_answer": self._useless_search_pattern_final_answer_steers,
                "useless_lsp_symbol_final_answer": self._useless_lsp_symbol_final_answer_steers,
                "repeated_read_file_final_answer": self._repeated_read_file_final_answer_steers,
                "semantic_exploration": self._semantic_exploration_steers,
            },
        )

    def _record_llm_request(self) -> None:
        if self._run_stats is not None:
            self._run_stats.llm_requests += 1

    def _record_context_compaction(self) -> None:
        if self._run_stats is not None:
            self._run_stats.compactions += 1
            if self._pending_compaction_summary_mode == "llm":
                self._run_stats.llm_context_summaries += 1
            elif self._pending_compaction_summary_mode == "local":
                self._run_stats.local_context_summaries += 1
        self._pending_compaction_summary_mode = None

    def _record_llm_context_summary(self) -> None:
        self._pending_compaction_summary_mode = "llm"

    def _record_local_context_summary(self) -> None:
        self._pending_compaction_summary_mode = "local"

    def _record_tool_started_for_run(self, name: str) -> None:
        if self._run_stats is None:
            return
        self._run_stats.tool_calls += 1
        self._run_stats.tool_counts[name] = self._run_stats.tool_counts.get(name, 0) + 1

    def _record_tool_finished_for_run(self, *, is_error: bool) -> None:
        if self._run_stats is not None and is_error:
            self._run_stats.tool_errors += 1

    def _record_tool_result_for_run(self, *, is_error: bool, useless: bool) -> None:
        if self._run_stats is not None and useless and not is_error:
            self._run_stats.useless_tool_results += 1

    def _record_synthetic_tool_result_for_run(self) -> None:
        if self._run_stats is None:
            return
        self._run_stats.synthetic_tool_results += 1
        self._run_stats.tool_errors += 1

    def _finish_run_summary(self, reason: str) -> dict[str, Any]:
        stats = self._run_stats
        if stats is None:
            return {"termination_reason": reason}
        guard_hits = {
            "duplicate_tool": self._duplicate_tool_guard_hits - stats.guard_start.get("duplicate_tool", 0),
            "useless_search_pattern": (
                self._useless_search_pattern_guard_hits - stats.guard_start.get("useless_search_pattern", 0)
            ),
            "useless_lsp_symbol": (
                self._useless_lsp_symbol_guard_hits - stats.guard_start.get("useless_lsp_symbol", 0)
            ),
            "repeated_read_file": (
                self._repeated_read_file_guard_hits - stats.guard_start.get("repeated_read_file", 0)
            ),
            "semantic_exploration": (
                self._semantic_exploration_guard_hits - stats.guard_start.get("semantic_exploration", 0)
            ),
        }
        steering_counts = {
            "duplicate_tool_final_answer": (
                self._duplicate_tool_final_answer_steers
                - stats.steer_start.get("duplicate_tool_final_answer", 0)
            ),
            "useless_search_pattern_final_answer": (
                self._useless_search_pattern_final_answer_steers
                - stats.steer_start.get("useless_search_pattern_final_answer", 0)
            ),
            "useless_lsp_symbol_final_answer": (
                self._useless_lsp_symbol_final_answer_steers
                - stats.steer_start.get("useless_lsp_symbol_final_answer", 0)
            ),
            "repeated_read_file_final_answer": (
                self._repeated_read_file_final_answer_steers
                - stats.steer_start.get("repeated_read_file_final_answer", 0)
            ),
            "semantic_exploration": (
                self._semantic_exploration_steers - stats.steer_start.get("semantic_exploration", 0)
            ),
            "no_edit_final_hygiene": self._no_edit_final_hygiene_steers,
            "final_structure": self._final_structure_steers,
            "read_only_evidence": self._read_only_evidence_steers,
            "source_evidence_false_negative": self._source_evidence_false_negative_steers,
            "source_grounded_numeric": self._source_grounded_numeric_steers,
            "completion_audit": self._completion_audit_steers,
            "soft_tool_requirement": self._soft_tool_requirement.steers if self._soft_tool_requirement else 0,
        }
        payload: dict[str, Any] = {
            "run_id": stats.run_id,
            "termination_reason": reason,
            "elapsed_ms": _elapsed_ms_since(stats.started_monotonic),
            "prompt_chars": stats.prompt_chars,
            "llm_requests": stats.llm_requests,
            "tool_calls": stats.tool_calls,
            "tool_errors": stats.tool_errors,
            "useless_tool_results": stats.useless_tool_results,
            "synthetic_tool_results": stats.synthetic_tool_results,
            "compactions": stats.compactions,
            "llm_context_summaries": stats.llm_context_summaries,
            "local_context_summaries": stats.local_context_summaries,
            "tool_counts": dict(sorted(stats.tool_counts.items())),
            "guard_hits": {key: value for key, value in guard_hits.items() if value},
            "steering_counts": {key: value for key, value in steering_counts.items() if value},
        }
        self._last_run_summary = payload
        self._session.append("run_summary", payload)
        self._events.emit("RunSummary", payload)
        return payload

    def _messages_for_model(self, deadline: float | None = None) -> list[dict[str, Any]]:
        todo_summary = self._open_todo_summary()
        provider_context = _prune_context_tool_outputs(self._messages)
        if not self._context_budget_enabled():
            return self._provider_safe_runtime_messages(provider_context, todo_summary)
        thresholds = self._context_budget_thresholds()
        if not self._context_budget_exceeded(provider_context):
            return self._provider_safe_runtime_messages(provider_context, todo_summary)

        system_messages = [message for message in provider_context if message.get("role") == "system"]
        non_system = [message for message in provider_context if message.get("role") != "system"]
        recent_count = min(self._config.context_recent_messages, len(non_system))
        current_user_request = _latest_user_content(non_system)

        while recent_count > 0:
            recent = _truncate_recent_tool_outputs(_valid_recent_messages(non_system[-recent_count:]))
            dropped_count = len(non_system) - recent_count
            dropped = non_system[: max(dropped_count, 0)]
            compaction_summary = self._build_compaction_summary(dropped, current_user_request, deadline)
            compacted = [
                _system_message_with_compaction_summary(system_messages, compaction_summary),
                *recent,
            ]
            if not self._context_budget_exceeded(compacted) or recent_count <= 6:
                payload: dict[str, Any] = {
                    "original_messages": len(self._messages),
                    "sent_messages": len(compacted),
                    "dropped_messages": len(dropped),
                    "estimated_chars": _estimate_message_chars(compacted),
                    "estimated_tokens": _estimate_message_tokens(compacted),
                }
                payload.update(thresholds)
                self._session.append("context_compaction", payload)
                self._record_context_compaction()
                return self._provider_safe_runtime_messages(compacted, todo_summary)
            recent_count = max(6, recent_count // 2)
        return self._provider_safe_runtime_messages(self._messages, todo_summary)

    def _context_budget_enabled(self) -> bool:
        return self._config.context_char_budget > 0 or self._config.context_token_budget > 0

    def _context_budget_thresholds(self) -> dict[str, int]:
        thresholds: dict[str, int] = {}
        if self._config.context_char_budget > 0:
            thresholds["threshold_chars"] = _resolve_compaction_threshold_chars(self._config.context_char_budget)
        if self._config.context_token_budget > 0:
            thresholds["threshold_tokens"] = _resolve_compaction_threshold_tokens(self._config.context_token_budget)
        return thresholds

    def _context_budget_exceeded(self, messages: list[dict[str, Any]]) -> bool:
        if self._config.context_token_budget > 0:
            threshold = _resolve_compaction_threshold_tokens(self._config.context_token_budget)
            if _estimate_message_tokens(messages) > threshold:
                return True
        if self._config.context_char_budget > 0:
            threshold = _resolve_compaction_threshold_chars(self._config.context_char_budget)
            if _estimate_message_chars(messages) > threshold:
                return True
        return False

    def _provider_safe_runtime_messages(
        self,
        messages: list[dict[str, Any]],
        todo_summary: list[str],
    ) -> list[dict[str, Any]]:
        evidence_ledger = self._evidence_ledger_summary()
        planner_explore_context = render_planner_explore_context(
            self._requirement_contract,
            prompt=self._current_user_request,
            tool_results=list(self._tool_choice_results),
        )
        return _provider_safe_messages(
            _messages_with_runtime_context(
                messages,
                todo_summary,
                evidence_ledger,
                planner_explore_context,
                self._config.workspace,
                self._user_config_dir,
                self._config.allowed_dirs,
                self._current_user_request,
                self._requirement_contract_context,
            )
        )

    def _build_compaction_summary(
        self,
        dropped: list[dict[str, Any]],
        current_user_request: str | None,
        deadline: float | None,
    ) -> str:
        todo_summary = self._open_todo_summary()
        if self._config.summary_mode in {"auto", "llm"}:
            llm_summary = self._llm_compaction_summary(dropped, current_user_request, todo_summary, deadline)
            if llm_summary:
                return llm_summary
        return self._local_compaction_summary(dropped, current_user_request, todo_summary)

    def _local_compaction_summary(
        self,
        dropped: list[dict[str, Any]],
        current_user_request: str | None,
        todo_summary: list[str],
    ) -> str:
        self._record_local_context_summary()
        lines = [
            "Earlier conversation was compacted locally to stay within the context budget.",
            "Preserve these facts while continuing the current task.",
            "",
            f"- Compacted messages: {len(dropped)}",
        ]
        if current_user_request:
            lines.extend(
                [
                    "",
                    "Current user request:",
                    f"- {_one_line(current_user_request, max_chars=1200)}",
                    "- After completing explicitly requested tool calls, answer the requested final response instead of exploring further unless more information is truly necessary.",
                ]
            )
        if todo_summary:
            lines.extend(["", "Open todos:", *todo_summary])
        user_items = _snippets_for_role(dropped, "user", limit=6)
        if user_items:
            lines.extend(["", "Earlier user requests:", *user_items])
        assistant_items = _assistant_snippets(dropped, limit=6)
        if assistant_items:
            lines.extend(["", "Earlier assistant outputs:", *assistant_items])
        tool_items = _tool_snippets(dropped, limit=6)
        if tool_items:
            lines.extend(["", "Earlier tool results:", *tool_items])
        return "\n".join(lines)

    def _llm_compaction_summary(
        self,
        dropped: list[dict[str, Any]],
        current_user_request: str | None,
        todo_summary: list[str],
        deadline: float | None,
    ) -> str | None:
        if not dropped or self._deadline_exceeded(deadline):
            return None
        transcript = _messages_to_summary_transcript(dropped, max_chars=SUMMARY_INPUT_CHAR_LIMIT)
        if not transcript.strip():
            return None
        cache_key = _summary_cache_key(transcript, current_user_request, todo_summary)
        cached = self._summary_cache.get(cache_key)
        if cached:
            return cached

        remaining_timeout = self._remaining_timeout(deadline)
        timeout = min(remaining_timeout, SUMMARY_REQUEST_TIMEOUT)
        if timeout < 1:
            return None
        messages = [
            {
                "role": "system",
                "content": (
                    "Summarize earlier messages for a local coding agent. "
                    "Keep durable facts, completed work, failed attempts, user constraints, file paths, decisions, and unresolved todos. "
                    "Do not invent facts. Keep it concise."
                ),
            },
            {
                "role": "user",
                "content": _summary_request_content(transcript, current_user_request, todo_summary),
            },
        ]
        try:
            response = self._client.chat(messages, [], timeout=timeout)
        except LlmError as exc:
            self._session.append("context_summary_error", {"mode": "llm", "error": str(exc)})
            return None
        content = response.message.get("content")
        if not isinstance(content, str) or not content.strip():
            self._session.append("context_summary_error", {"mode": "llm", "error": "empty summary"})
            return None
        summary = _format_llm_compaction_summary(
            content.strip()[:SUMMARY_OUTPUT_CHAR_LIMIT],
            current_user_request,
            todo_summary,
        )
        self._summary_cache[cache_key] = summary
        self._record_llm_context_summary()
        self._session.append(
            "context_summary",
            {
                "mode": "llm",
                "input_chars": len(transcript),
                "summary_chars": len(summary),
            },
        )
        return summary

    def _open_todo_summary(self) -> list[str]:
        path = tool_state_dir(self._tool_context) / "todos" / f"{self._session.session_id}.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        lines: list[str] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "")
            if status in {"done", "skipped"}:
                continue
            todo_id = str(item.get("id") or "").strip()
            task = str(item.get("task") or "").strip()
            note = str(item.get("note") or "").strip()
            if not todo_id or not task:
                continue
            suffix = f" — {note}" if note else ""
            lines.append(f"- [{status or 'todo'}] {todo_id}: {task}{suffix}")
        return lines[:20]

    def _remaining_timeout(self, deadline: float | None) -> float:
        if deadline is None:
            return float(self._config.request_timeout)
        remaining = deadline - time.monotonic()
        return min(float(self._config.request_timeout), max(1.0, remaining))

    def _execute_tool_with_repeat_guard(
        self,
        name: str,
        arguments: str | dict[str, Any],
        tool_context: ToolContext,
    ) -> ToolResult:
        read_file_key = (
            _read_file_path_key(name, arguments, self._config.workspace, self._config.allowed_dirs)
            if self._read_file_drift_guard_enabled
            else None
        )
        read_file_range_key = (
            _read_file_range_key(name, arguments, self._config.workspace, self._config.allowed_dirs)
            if self._read_file_drift_guard_enabled
            else None
        )
        if read_file_range_key is not None:
            range_count = self._read_file_range_counts.get(read_file_range_key, 0)
            if range_count >= MAX_READ_FILE_SUCCESSES_PER_RANGE_IN_RUN:
                self._repeated_read_file_guard_hits += 1
                return self._repeated_read_file_result(
                    _display_read_file_range_key(
                        read_file_range_key,
                        self._config.workspace,
                        self._config.allowed_dirs,
                    ),
                    range_count,
                    evidence=self._evidence_for_read_file_range(read_file_range_key),
                )
        if read_file_key is not None:
            recent_path_count = self._recent_read_file_path_keys.count(read_file_key)
            self._recent_read_file_path_keys.append(read_file_key)
            self._recent_read_file_path_keys = self._recent_read_file_path_keys[-READ_FILE_PATH_WINDOW:]
            if recent_path_count >= MAX_READ_FILE_CALLS_PER_FILE_IN_RECENT_WINDOW:
                self._repeated_read_file_guard_hits += 1
                return self._repeated_read_file_result(read_file_key, recent_path_count)
        signature = _tool_call_signature(name, arguments)
        recent_count = self._recent_tool_call_signatures.count(signature)
        self._recent_tool_call_signatures.append(signature)
        self._recent_tool_call_signatures = self._recent_tool_call_signatures[-REPEAT_TOOL_CALL_WINDOW:]
        if recent_count >= MAX_IDENTICAL_TOOL_CALLS_IN_RECENT_WINDOW:
            self._duplicate_tool_guard_hits += 1
            return self._duplicate_tool_result(name, recent_count)
        search_pattern_key = _search_pattern_key(name, arguments)
        if search_pattern_key is not None:
            recent_useless_count = self._recent_useless_search_pattern_keys.count(search_pattern_key)
            if recent_useless_count >= MAX_USELESS_SEARCHES_PER_PATTERN_IN_RECENT_WINDOW:
                self._useless_search_pattern_guard_hits += 1
                return self._useless_search_pattern_result(search_pattern_key, recent_useless_count)
        lsp_symbol_query_key = _lsp_symbol_query_key(name, arguments)
        if (
            lsp_symbol_query_key is not None
            and len(self._recent_useless_lsp_symbol_query_keys) >= MAX_USELESS_LSP_SYMBOL_QUERIES_IN_RECENT_WINDOW
        ):
            self._useless_lsp_symbol_guard_hits += 1
            return self._useless_lsp_symbol_result(
                lsp_symbol_query_key,
                len(self._recent_useless_lsp_symbol_query_keys),
            )
        semantic_exploration_key = _semantic_exploration_key(
            name,
            arguments,
            self._config.workspace,
            self._config.allowed_dirs,
        )
        if semantic_exploration_key is not None:
            recent_semantic_count = self._recent_semantic_exploration_keys.count(semantic_exploration_key)
            self._recent_semantic_exploration_keys.append(semantic_exploration_key)
            self._recent_semantic_exploration_keys = self._recent_semantic_exploration_keys[
                -SEMANTIC_EXPLORATION_WINDOW:
            ]
            if recent_semantic_count >= MAX_SEMANTIC_EXPLORATIONS_PER_KEY_IN_RECENT_WINDOW:
                self._semantic_exploration_guard_hits += 1
                return self._semantic_exploration_result(semantic_exploration_key, recent_semantic_count)
        result = self._registry.execute(name, arguments, tool_context)
        if search_pattern_key is not None and result.useless and not result.is_error:
            self._recent_useless_search_pattern_keys.append(search_pattern_key)
            self._recent_useless_search_pattern_keys = self._recent_useless_search_pattern_keys[
                -USELESS_SEARCH_PATTERN_WINDOW:
            ]
        if lsp_symbol_query_key is not None and not result.is_error:
            if result.useless:
                self._recent_useless_lsp_symbol_query_keys.append(lsp_symbol_query_key)
                self._recent_useless_lsp_symbol_query_keys = self._recent_useless_lsp_symbol_query_keys[
                    -USELESS_LSP_SYMBOL_QUERY_WINDOW:
                ]
            else:
                self._recent_useless_lsp_symbol_query_keys = []
        if read_file_range_key is not None and not result.is_error:
            self._read_file_range_counts[read_file_range_key] = (
                self._read_file_range_counts.get(read_file_range_key, 0) + 1
            )
        return result

    def _repeated_read_file_result(self, path_key: str, prior_count: int, *, evidence: str = "") -> ToolResult:
        evidence_note = f"\nExisting evidence:\n{evidence}" if evidence else ""
        return ToolResult(
            (
                f"Tool call skipped: read_file has already read '{path_key}' {prior_count} times in this run. "
                "Use the collected evidence and provide the requested final answer, "
                "or switch to a different, more targeted file only if new evidence is truly necessary."
                f"{evidence_note}"
            ),
            is_error=True,
        )

    def _duplicate_tool_result(self, name: str, prior_count: int) -> ToolResult:
        return ToolResult(
            (
                f"Tool call skipped: identical call to '{name}' with the same arguments "
                f"has already run {prior_count} times in this session. "
                "Use the earlier tool results and provide the requested final answer, "
                "or call a different tool/arguments only if new evidence is truly necessary."
            ),
            is_error=True,
        )

    def _useless_search_pattern_result(self, pattern_key: str, prior_count: int) -> ToolResult:
        return ToolResult(
            (
                f"Tool call skipped: search_code has already returned no matches for pattern "
                f"'{pattern_key}' {prior_count} times recently across paths. "
                "Use the collected evidence and provide the requested final answer, "
                "or switch to a meaningfully different business term only if new evidence is truly necessary."
            ),
            is_error=True,
        )

    def _useless_lsp_symbol_result(self, query_key: str, prior_count: int) -> ToolResult:
        return ToolResult(
            (
                f"Tool call skipped: lsp symbol queries have returned no matches {prior_count} times recently; "
                f"latest query was '{query_key}'. Use the collected evidence and provide the requested final answer, "
                "or switch to search_code with a genuinely different business term only if new evidence is necessary."
            ),
            is_error=True,
        )

    def _semantic_exploration_result(self, path_key: str, prior_count: int) -> ToolResult:
        return ToolResult(
            (
                f"Tool call skipped: directory exploration under '{path_key}' has already happened "
                f"{prior_count} times recently. Stop guessing parent/child paths in the same module. "
                "Use search_code, lsp_* navigation, or read_file on exact matched files; if evidence is sufficient, "
                "answer the user's original question and mark any uncertainty explicitly."
            ),
            is_error=True,
        )

    def _steer_after_duplicate_tool_call(self, name: str) -> bool:
        if self._duplicate_tool_final_answer_steers >= MAX_DUPLICATE_TOOL_FINAL_ANSWER_STEERS:
            return False
        self._duplicate_tool_final_answer_steers += 1
        evidence = self._read_file_evidence_summary()
        request_summary = self._final_answer_request_summary()
        content = (
            "Runtime steering: repeated identical tool calls are no longer useful. "
            "Your next response must be a final answer without tool calls. "
            "Use the evidence already collected, state uncertainty explicitly, and list exact next files or queries "
            "instead of repeating prior searches."
            f"{request_summary}"
            f"{evidence}"
        )
        self._messages.append({"role": "user", "content": content})
        self._session.append(
            "runtime_steering",
            {
                "kind": "duplicate_tool_final_answer",
                "tool": name,
                "duplicate_hits": self._duplicate_tool_guard_hits,
                "steer_count": self._duplicate_tool_final_answer_steers,
            },
        )
        self._force_final_answer_without_tools = True
        return True

    def _steer_after_useless_search_pattern(self) -> bool:
        if self._useless_search_pattern_final_answer_steers >= MAX_USELESS_SEARCH_PATTERN_FINAL_ANSWER_STEERS:
            return False
        self._useless_search_pattern_final_answer_steers += 1
        evidence = self._read_file_evidence_summary()
        request_summary = self._final_answer_request_summary()
        content = (
            "Runtime steering: repeated search_code calls with the same no-match pattern are no longer useful. "
            "Your next response must be a final answer without tool calls. "
            "Return to the user's original requested output structure, use the evidence already collected, "
            "state uncertainty explicitly, and list exact next files or different business terms instead of "
            "continuing to search the same empty keyword across directories."
            f"{request_summary}"
            f"{evidence}"
        )
        self._messages.append({"role": "user", "content": content})
        self._session.append(
            "runtime_steering",
            {
                "kind": "useless_search_pattern_final_answer",
                "guard_hits": self._useless_search_pattern_guard_hits,
                "steer_count": self._useless_search_pattern_final_answer_steers,
            },
        )
        self._force_final_answer_without_tools = True
        return True

    def _steer_after_useless_lsp_symbol_queries(self) -> bool:
        if self._useless_lsp_symbol_final_answer_steers >= MAX_USELESS_LSP_SYMBOL_FINAL_ANSWER_STEERS:
            return False
        self._useless_lsp_symbol_final_answer_steers += 1
        evidence = self._read_file_evidence_summary()
        request_summary = self._final_answer_request_summary()
        content = (
            "Runtime steering: repeated lsp symbol queries with no matches are no longer useful. "
            "Your next response must be a final answer without tool calls. "
            "Return to the user's original requested output structure, use the evidence already collected, "
            "state uncertainty explicitly, and list exact next files or truly different search terms instead of "
            "continuing to guess symbol names."
            f"{request_summary}"
            f"{evidence}"
        )
        self._messages.append({"role": "user", "content": content})
        self._session.append(
            "runtime_steering",
            {
                "kind": "useless_lsp_symbol_final_answer",
                "guard_hits": self._useless_lsp_symbol_guard_hits,
                "steer_count": self._useless_lsp_symbol_final_answer_steers,
            },
        )
        self._force_final_answer_without_tools = True
        return True

    def _steer_after_repeated_read_file(self) -> bool:
        if self._repeated_read_file_final_answer_steers >= MAX_REPEATED_READ_FILE_FINAL_ANSWER_STEERS:
            return False
        self._repeated_read_file_final_answer_steers += 1
        evidence = self._read_file_evidence_summary()
        request_summary = self._final_answer_request_summary()
        content = (
            "Runtime steering: repeated read_file slices from the same file are no longer useful. "
            "Your next response must be a final answer without tool calls. "
            "Return to the user's original requested output structure, use the evidence already collected, "
            "state uncertainty explicitly, and list exact next files instead of continuing to read adjacent ranges."
            f"{request_summary}"
            f"{evidence}"
        )
        self._messages.append({"role": "user", "content": content})
        self._session.append(
            "runtime_steering",
            {
                "kind": "repeated_read_file_final_answer",
                "duplicate_hits": self._repeated_read_file_guard_hits,
                "steer_count": self._repeated_read_file_final_answer_steers,
            },
        )
        self._force_final_answer_without_tools = True
        return True

    def _steer_after_semantic_exploration(self) -> bool:
        if self._semantic_exploration_steers >= MAX_SEMANTIC_EXPLORATION_STEERS:
            return False
        self._semantic_exploration_steers += 1
        evidence = self._read_file_evidence_summary()
        request_summary = self._final_answer_request_summary()
        content = (
            "Runtime steering: directory/path exploration is repeating under the same module or parent path. "
            "Do not keep calling list_files on sibling, parent, or child guesses. "
            "Use targeted evidence tools only: search_code with business terms, lsp_* navigation, or read_file on "
            "exact files already discovered. If enough evidence has been collected, answer the user's original "
            "question directly and label uncertainty explicitly."
            f"{request_summary}"
            f"{evidence}"
        )
        self._messages.append({"role": "user", "content": content})
        self._session.append(
            "runtime_steering",
            {
                "kind": "semantic_exploration",
                "guard_hits": self._semantic_exploration_guard_hits,
                "steer_count": self._semantic_exploration_steers,
            },
        )
        self._temporary_tool_allowlist = set(READ_ONLY_EVIDENCE_TOOLS)
        return True

    def _record_read_file_evidence(self, name: str, arguments: str | dict[str, Any], result: ToolResult) -> None:
        if name != "read_file" or result.is_error:
            return
        try:
            parsed = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return
        if not isinstance(parsed, dict):
            return
        raw_path = parsed.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return
        display_path = _display_read_file_evidence_path(
            self._config.workspace,
            raw_path.strip(),
            self._config.allowed_dirs,
        )
        if display_path in self._read_file_evidence_paths:
            self._source_evidence.append(SourceEvidence(display_path, result.content))
            self._source_evidence = self._source_evidence[-40:]
            return
        self._read_file_evidence_paths.append(display_path)
        self._read_file_evidence_paths = self._read_file_evidence_paths[-20:]
        self._source_evidence.append(SourceEvidence(display_path, result.content))
        self._source_evidence = self._source_evidence[-40:]

    def _record_tool_choice_result(self, name: str, arguments: str | dict[str, Any], result: ToolResult) -> None:
        self._tool_choice_tool_names.append(name)
        self._tool_choice_tool_names = self._tool_choice_tool_names[-80:]
        self._tool_choice_results.append(
            ToolResultSummary(
                name=name,
                content=_one_line_block(result.content, max_chars=2000),
                is_error=result.is_error,
                useless=result.useless,
                path=_tool_choice_argument_path(arguments),
            )
        )
        self._tool_choice_results = self._tool_choice_results[-80:]

    def _record_tool_evidence(self, name: str, arguments: str | dict[str, Any], result: ToolResult) -> None:
        self._record_strong_relevance_paths(name, arguments, result)
        record = _build_tool_evidence_record(
            name,
            arguments,
            result,
            self._config.workspace,
            self._config.allowed_dirs,
        )
        if record is None:
            return
        self._append_evidence_record(record)

    def _append_evidence_record(self, record: EvidenceRecord) -> None:
        rendered = record.render()
        if self._evidence_records and self._evidence_records[-1].render() == rendered:
            return
        self._evidence_records.append(record)
        self._evidence_records = self._evidence_records[-EVIDENCE_LEDGER_MAX_RECORDS:]
        self._session.append(
            "evidence",
            {
                "tool": record.tool,
                "subject": record.subject,
                "status": record.status,
                "summary": record.summary,
            },
        )

    def _record_workspace_root_evidence(self) -> None:
        if self._workspace_root_evidence_recorded:
            return
        self._workspace_root_evidence_recorded = True
        markers = _workspace_root_markers(self._config.workspace)
        if not markers:
            return
        self._append_evidence_record(
            EvidenceRecord(
                tool="workspace",
                subject="root",
                summary="Primary workspace contains: " + ", ".join(markers) + ".",
            )
        )

    def _record_strong_relevance_paths(
        self,
        name: str,
        arguments: str | dict[str, Any],
        result: ToolResult,
    ) -> None:
        if result.is_error:
            return
        paths: list[str] = []
        if name == "search_code":
            paths = [
                path
                for path in _first_search_result_paths(result.content, limit=8)
                if not is_low_relevance_patch_path(path)
            ]
        elif name.startswith("lsp_"):
            paths = _first_result_line_paths(result.content, limit=8)
        for path in paths:
            if path and path not in self._strong_relevance_paths:
                self._strong_relevance_paths.append(path)
        self._strong_relevance_paths = self._strong_relevance_paths[-30:]

    def _patch_relevance_denial_reason(self, raw_path: str, resolved_path: Path) -> str | None:
        display_path = display_workspace_path(
            self._config.workspace,
            resolved_path,
            self._config.allowed_dirs,
        )
        if not path_matches_any(display_path, tuple(self._read_file_evidence_paths)):
            return (
                f"Patch relevance gate: refusing real apply_patch for {display_path!r} because that file "
                "has not been read with read_file in this run. Call read_file on the exact target first; "
                "apply_patch dry_run=true previews are still allowed."
            )
        if (
            is_code_implementation_request(self._current_user_request)
            and is_low_relevance_patch_path(display_path)
            and not request_mentions_config_or_path(self._current_user_request, display_path)
            and not path_matches_any(display_path, tuple(self._strong_relevance_paths))
        ):
            return (
                f"Patch relevance gate: refusing real apply_patch for {display_path!r} because the current "
                "request looks like a code implementation task, while the target looks like deployment/config "
                "material. Before editing this path, establish direct relevance from source-code evidence or "
                "ask the user to confirm a configuration/deployment edit. apply_patch dry_run=true previews are "
                "still allowed."
            )
        return None

    def _read_file_evidence_summary(self) -> str:
        if not self._read_file_evidence_paths:
            return ""
        recent_paths = self._read_file_evidence_paths[-12:]
        lines = [
            "",
            "",
            "Already read these files in this run; do not claim they were unread:",
            *[f"- {path}" for path in recent_paths],
            "If one of these files still needs deeper implementation review, say it was already read and specify the missing detail.",
        ]
        return "\n".join(lines)

    def _evidence_for_read_file_range(self, range_key: tuple[str, int, str]) -> str:
        subject = _display_read_file_range_subject(range_key, self._config.workspace, self._config.allowed_dirs)
        matches = [
            record.render()
            for record in reversed(self._evidence_records)
            if record.tool == "read_file" and record.subject == subject
        ]
        return "\n".join(reversed(matches[:3]))

    def _evidence_ledger_summary(self) -> str:
        if not self._evidence_records:
            return ""
        lines = [
            "[Evidence ledger]",
            "Runtime-collected tool evidence for this run. Use it to distinguish evidence-backed facts from inference.",
            "In final answers, cite exact paths only when they appear here or in tool results; label guessed files/classes as unverified.",
            "Do not claim workspace root files are missing when workspace evidence lists them.",
        ]
        lines.extend(record.render() for record in self._evidence_records[-EVIDENCE_LEDGER_CONTEXT_RECORDS:])
        return _one_line_block("\n".join(lines), max_chars=EVIDENCE_LEDGER_CONTEXT_CHAR_LIMIT)

    def _final_answer_request_summary(self) -> str:
        if not self._current_user_request:
            return ""
        return (
            "\n\nOriginal user request to satisfy now:\n"
            f"- {_one_line(self._current_user_request, max_chars=1200)}"
        )

    def _decide_final_answer_steering(
        self,
        content: str,
        run_start_index: int,
    ) -> SteeringDecision | None:
        context = FinalAnswerContext(
            request=self._current_user_request,
            content=content,
            messages=self._messages,
            run_start_index=run_start_index,
            requirement_contract=self._requirement_contract,
            tool_results=list(self._tool_choice_results),
            read_file_evidence_paths=list(self._read_file_evidence_paths),
            source_evidence=list(self._source_evidence),
            open_todos=self._open_todo_summary(),
            is_code_implementation_request=is_code_implementation_request(self._current_user_request),
            steer_counts=self._final_answer_steer_counts(),
        )
        for steerer in self._final_answer_steerers:
            decision = steerer.decide(context)
            if decision is not None:
                return decision
        return None

    def _apply_final_answer_steering(self, decision: SteeringDecision) -> None:
        steer_count = self._increment_final_answer_steer_count(decision.kind)
        self._messages.append({"role": "user", "content": decision.message})
        payload = {
            "kind": decision.kind,
            **decision.payload,
            "steer_count": steer_count,
        }
        self._session.append("runtime_steering", payload)
        self._force_final_answer_without_tools = decision.force_final_answer_without_tools
        self._temporary_tool_allowlist = decision.temporary_tool_allowlist

    def _final_answer_steer_counts(self) -> dict[str, int]:
        return {
            "read_only_evidence": self._read_only_evidence_steers,
            "no_edit_final_hygiene": self._no_edit_final_hygiene_steers,
            "final_structure": self._final_structure_steers,
            "source_evidence_false_negative": self._source_evidence_false_negative_steers,
            "source_grounded_numeric": self._source_grounded_numeric_steers,
            "completion_audit": self._completion_audit_steers,
        }

    def _increment_final_answer_steer_count(self, kind: str) -> int:
        if kind == "read_only_evidence":
            self._read_only_evidence_steers += 1
            return self._read_only_evidence_steers
        if kind == "no_edit_final_hygiene":
            self._no_edit_final_hygiene_steers += 1
            return self._no_edit_final_hygiene_steers
        if kind == "final_structure":
            self._final_structure_steers += 1
            return self._final_structure_steers
        if kind == "source_evidence_false_negative":
            self._source_evidence_false_negative_steers += 1
            return self._source_evidence_false_negative_steers
        if kind == "source_grounded_numeric":
            self._source_grounded_numeric_steers += 1
            return self._source_grounded_numeric_steers
        if kind == "completion_audit":
            self._completion_audit_steers += 1
            return self._completion_audit_steers
        return 0

    def _append_soft_tool_requirement_message(self, requirement: SoftToolRequirement) -> None:
        content = _soft_tool_requirement_message(requirement)
        self._messages.append({"role": "user", "content": content})
        self._session.append(
            "runtime_steering",
            {
                "kind": requirement.kind,
                "allowed_dirs": [str(path) for path in requirement.allowed_dirs],
                "candidate_files": [str(path) for path in requirement.candidate_files],
            },
        )

    def _needs_soft_tool_requirement_steer(self) -> bool:
        requirement = self._soft_tool_requirement
        return requirement is not None and not requirement.satisfied

    def _steer_for_soft_tool_requirement(self) -> bool:
        requirement = self._soft_tool_requirement
        if requirement is None or requirement.satisfied:
            return False
        if requirement.steers >= MAX_SOFT_TOOL_REQUIREMENT_STEERS:
            return False
        requirement.steers += 1
        content = _soft_tool_requirement_message(requirement)
        self._messages.append({"role": "user", "content": content})
        self._session.append(
            "runtime_steering",
            {
                "kind": f"{requirement.kind}_reminder",
                "steers": requirement.steers,
            },
        )
        return True

    def _soft_tool_requirement_stop_message(self) -> str:
        requirement = self._soft_tool_requirement
        if requirement is None:
            return "Stopped because a required tool step was not completed."
        if requirement.kind == "authored_skill":
            return (
                "Stopped because the task explicitly referenced a project skill, but the assistant did not "
                "read that skill's SKILL.md after repeated reminders. Retry or explicitly ask it to read the "
                "skill file first."
            )
        return (
            "Stopped because the task required reading requirement/spec documents from an allowed directory, "
            "but the assistant did not call read_file on any allowed-directory document after repeated reminders. "
            "Retry with the same --allow-dir, or explicitly name the requirement file path."
        )

    def _repeated_read_file_stop_message(self) -> str:
        return (
            "Tool call was not executed because repeated read_file slices from the same file were no longer useful. "
            "Use the already collected evidence and answer the user's original request."
        )

    def _observe_soft_tool_requirement(self, name: str, arguments: str | dict[str, Any], result: ToolResult) -> None:
        requirement = self._soft_tool_requirement
        if requirement is None or requirement.satisfied or result.is_error:
            return
        if name != "read_file":
            return
        try:
            parsed = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return
        if not isinstance(parsed, dict):
            return
        raw_path = parsed.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return
        try:
            path = resolve_workspace_path(self._config.workspace, raw_path, self._config.allowed_dirs)
        except PatchError:
            return
        if _soft_tool_requirement_path_satisfies(requirement, path):
            requirement.satisfied = True
            self._session.append(
                "runtime_steering",
                {
                    "kind": f"{requirement.kind}_satisfied",
                    "path": str(path),
                },
            )

    def _deadline_exceeded(self, deadline: float | None) -> bool:
        return deadline is not None and time.monotonic() >= deadline

    def _budget_stop_message(self) -> str:
        return f"Stopped after reaching budget_seconds={self._config.budget_seconds}."

    def _finish_run(
        self,
        content: str,
        deadline: float | None,
        run_start_index: int,
        *,
        reason: str = "final",
    ) -> str:
        self._session.append("final", {"content": content})
        run_messages = self._messages[run_start_index:]
        self._maybe_consolidate_session_memory(run_messages, content, deadline)
        run_summary = self._finish_run_summary(reason)
        self._events.emit(
            "SessionFinished",
            {
                "content": content,
                "reason": reason,
                "run_summary": run_summary,
            },
        )
        return content

    def _maybe_consolidate_session_memory(
        self,
        run_messages: list[dict[str, Any]],
        final_content: str,
        deadline: float | None,
    ) -> None:
        mode = self._config.memory_consolidation
        if mode == "off":
            return
        if self._deadline_exceeded(deadline):
            self._session.append("memory_consolidation", {"mode": mode, "status": "skipped", "reason": "deadline"})
            return
        if _run_used_memory_write_tool(run_messages):
            self._session.append(
                "memory_consolidation",
                {"mode": mode, "status": "skipped", "reason": "memory tool already wrote"},
            )
            return
        transcript = _messages_to_memory_transcript(
            run_messages,
            final_content,
            max_chars=MEMORY_CONSOLIDATION_INPUT_CHAR_LIMIT,
        )
        if not transcript.strip():
            return
        if mode == "auto" and not _should_auto_consolidate_memory(transcript, run_messages, final_content):
            self._session.append(
                "memory_consolidation",
                {"mode": mode, "status": "skipped", "reason": "no durable signal"},
            )
            return
        extracted = self._llm_memory_consolidation(transcript, deadline)
        if not extracted:
            return
        memory_root = _memory_consolidation_root(
            self._config.workspace,
            self._state_dir,
            self._config.memory_scope,
        )
        written = _append_consolidated_memory(memory_root, self._session.session_id, extracted)
        self._session.append(
            "memory_consolidation",
            {
                "mode": mode,
                "scope": self._config.memory_scope,
                "memory_root": str(memory_root),
                "status": "written" if written else "empty",
                "written": written,
            },
        )

    def _llm_memory_consolidation(self, transcript: str, deadline: float | None) -> dict[str, list[str]] | None:
        if deadline is None:
            remaining_timeout = float(self._config.request_timeout)
        else:
            remaining_timeout = deadline - time.monotonic()
        timeout = min(float(self._config.request_timeout), remaining_timeout, MEMORY_CONSOLIDATION_REQUEST_TIMEOUT)
        if timeout < 1:
            return None
        messages = [
            {
                "role": "system",
                "content": (
                    "Extract durable project memory for a local coding agent. "
                    "Return only strict JSON with keys project, decisions, conventions, learned, each an array of strings. "
                    "Include only reusable facts, accepted decisions, coding conventions, commands, debugging insights, or workflow lessons that will help future sessions. "
                    "Do not include secrets, credentials, raw source code, one-off todos, temporary user requests, or guesses. "
                    "If there is no durable memory, return empty arrays."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Session transcript:\n"
                    f"{transcript}\n\n"
                    "Return JSON shaped exactly like:\n"
                    '{"project":[],"decisions":[],"conventions":[],"learned":[]}'
                )[:MEMORY_CONSOLIDATION_INPUT_CHAR_LIMIT],
            },
        ]
        try:
            response = self._client.chat(messages, [], timeout=timeout)
        except LlmError as exc:
            self._session.append("memory_consolidation_error", {"mode": "llm", "error": str(exc)})
            return None
        content = response.message.get("content")
        if not isinstance(content, str) or not content.strip():
            self._session.append("memory_consolidation_error", {"mode": "llm", "error": "empty response"})
            return None
        parsed = _parse_memory_consolidation_response(content[:MEMORY_CONSOLIDATION_OUTPUT_CHAR_LIMIT])
        if parsed is None:
            self._session.append("memory_consolidation_error", {"mode": "llm", "error": "invalid JSON response"})
            return None
        return parsed

    def _stop_for_budget(self, deadline: float | None, run_start_index: int) -> str:
        content = self._budget_stop_message()
        return self._finish_run(content, deadline, run_start_index, reason="budget")

    def _duplicate_tool_stop_message(self) -> str:
        return "the assistant repeated identical tool calls too many times."

    def _stop_for_duplicate_tools(self, deadline: float | None, run_start_index: int) -> str:
        content = (
            "Stopped because the assistant repeated identical tool calls too many times. "
            "Retry with a narrower request or ask it to answer from the evidence already collected."
        )
        return self._finish_run(content, deadline, run_start_index, reason="duplicate_tool_guard")

    def _stop_for_repeated_read_file(self, deadline: float | None, run_start_index: int) -> str:
        content = (
            "Stopped because the assistant kept reading adjacent ranges from the same file. "
            "Retry with a narrower request or ask it to answer from the evidence already collected."
        )
        return self._finish_run(content, deadline, run_start_index, reason="repeated_read_file_guard")

    def _useless_search_pattern_stop_message(self) -> str:
        return (
            "Tool call was not executed because repeated search_code calls with the same no-match pattern "
            "were no longer useful. Use the already collected evidence and answer the user's original request."
        )

    def _stop_for_useless_search_pattern(self, deadline: float | None, run_start_index: int) -> str:
        content = (
            "Stopped because the assistant kept searching the same no-match pattern across paths. "
            "Retry with a narrower request or ask it to answer from the evidence already collected."
        )
        return self._finish_run(content, deadline, run_start_index, reason="useless_search_pattern_guard")

    def _useless_lsp_symbol_stop_message(self) -> str:
        return (
            "Tool call was not executed because repeated lsp symbol queries with no matches "
            "were no longer useful. Use the already collected evidence and answer the user's original request."
        )

    def _stop_for_useless_lsp_symbol_queries(self, deadline: float | None, run_start_index: int) -> str:
        content = (
            "Stopped because the assistant kept guessing lsp symbol queries with no matches. "
            "Retry with a narrower request or ask it to answer from the evidence already collected."
        )
        return self._finish_run(content, deadline, run_start_index, reason="useless_lsp_symbol_guard")

    def _semantic_exploration_stop_message(self) -> str:
        return (
            "Tool call was not executed because repeated list_files exploration under the same module or parent path "
            "was no longer useful. Use targeted search_code/lsp/read_file evidence or answer from collected evidence."
        )

    def _stop_for_semantic_exploration(self, deadline: float | None, run_start_index: int) -> str:
        content = (
            "Stopped because the assistant kept exploring sibling, parent, or child directories in the same module. "
            "Retry with a narrower request or ask it to use search_code/LSP evidence instead of path guessing."
        )
        return self._finish_run(content, deadline, run_start_index, reason="semantic_exploration_guard")

    def _stop_for_interrupt(self) -> str:
        content = "Stopped after user interrupt."
        self._session.append("final", {"content": content})
        run_summary = self._finish_run_summary("interrupt")
        self._events.emit(
            "SessionFinished",
            {
                "content": content,
                "reason": "interrupt",
                "run_summary": run_summary,
            },
        )
        return content

    def _length_stop_tool_message(self) -> str:
        return (
            "the assistant hit its output token limit before the tool call could be trusted. "
            "Retry with a smaller request or ask to continue in smaller steps."
        )

    def _stop_for_length(self, deadline: float | None, run_start_index: int) -> str:
        content = (
            "Stopped because the LLM response hit finish_reason=length. "
            "Retry with a smaller request or continue in smaller steps."
        )
        return self._finish_run(content, deadline, run_start_index, reason="length")

    def _append_tool_result(
        self,
        tool_call: dict[str, Any],
        name: str,
        content: str,
        *,
        is_error: bool,
        useless: bool = False,
    ) -> None:
        self._record_tool_result_for_run(is_error=is_error, useless=useless)
        self._session.append(
            "tool_result",
            {
                "tool_call_id": tool_call.get("id"),
                "name": name,
                "is_error": is_error,
                "content": content,
                "useless": bool(useless and not is_error),
            },
        )
        self._events.emit(
            "ToolOutput",
            {
                "tool_call_id": tool_call.get("id"),
                "name": name,
                "is_error": is_error,
                "useless": bool(useless and not is_error),
                "content_length": len(content),
                "content_preview": _event_preview(content),
            },
        )
        self._messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.get("id"),
                "content": content,
                "_lca_tool_name": name,
                "_lca_is_error": is_error,
                "_lca_useless": bool(useless and not is_error),
            }
        )

    def _append_synthetic_tool_results(self, tool_calls: list[dict[str, Any]], content: str) -> None:
        for tool_call in tool_calls:
            function = tool_call.get("function") or {}
            name = function.get("name") or ""
            result = f"Tool call was not executed because {content}"
            self._record_synthetic_tool_result_for_run()
            self._append_tool_result(tool_call, name, result, is_error=True)

    def _log_tool_start(self, name: str, arguments: Any) -> None:
        self._record_tool_started_for_run(name)
        self._events.emit("ToolStarted", {"name": name, "arguments": arguments})

    def _log_tool_end(self, name: str, is_error: bool, content_length: int) -> None:
        self._record_tool_finished_for_run(is_error=is_error)
        self._events.emit(
            "ToolFailed" if is_error else "ToolFinished",
            {"name": name, "content_length": content_length},
        )

    def _record_event_v1(self, event: AgentEvent) -> None:
        self._session.append("event_v1", event.to_dict())

    def _emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self._events.emit(event_type, payload)


def _assistant_event_payload(message: dict[str, Any]) -> dict[str, Any]:
    tool_calls = message.get("tool_calls") or []
    return {
        "content": message.get("content") or "",
        "tool_calls": [_tool_call_event_payload(tool_call) for tool_call in tool_calls if isinstance(tool_call, dict)],
    }


def _provider_safe_assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return message
    safe_tool_calls = [_provider_safe_tool_call(tool_call, index) for index, tool_call in enumerate(tool_calls)]
    return {**message, "tool_calls": safe_tool_calls}


def _provider_safe_tool_call(tool_call: Any, index: int) -> dict[str, Any]:
    if not isinstance(tool_call, dict):
        return {
            "id": f"invalid_tool_call_{index}",
            "type": "function",
            "function": {"name": INVALID_TOOL_CALL_NAME, "arguments": "{}"},
        }
    function = tool_call.get("function")
    function = function if isinstance(function, dict) else {}
    name = function.get("name")
    name = _provider_safe_tool_name(name)
    arguments = _provider_safe_tool_arguments(function.get("arguments"))
    tool_call_id = tool_call.get("id")
    if not isinstance(tool_call_id, str) or not tool_call_id.strip():
        tool_call_id = f"invalid_tool_call_{index}"
    return {
        **tool_call,
        "id": tool_call_id,
        "type": tool_call.get("type") or "function",
        "function": {**function, "name": name, "arguments": arguments},
    }


def _provider_safe_tool_name(name: Any) -> str:
    if not isinstance(name, str):
        return INVALID_TOOL_CALL_NAME
    try:
        return _validate_runtime_tool_name(name)
    except ValueError:
        return INVALID_TOOL_CALL_NAME


def _provider_safe_tool_arguments(arguments: Any) -> str:
    if isinstance(arguments, dict):
        return json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    if not isinstance(arguments, str):
        return "{}"
    stripped = arguments.strip()
    if not stripped:
        return "{}"
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return json.dumps({"_invalid_arguments": stripped[:500]}, ensure_ascii=False, sort_keys=True)
    if not isinstance(parsed, dict):
        return json.dumps({"_invalid_arguments": parsed}, ensure_ascii=False, sort_keys=True, default=str)
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True)


def _tool_call_event_payload(tool_call: dict[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function") or {}
    return {
        "id": tool_call.get("id"),
        "name": function.get("name") or "",
        "arguments_preview": _event_preview(function.get("arguments") or ""),
    }


def _event_preview(value: Any, limit: int = 1200) -> str:
    rendered = str(value)
    if len(rendered) <= limit:
        return rendered
    return rendered[:limit] + "...<truncated>"


def _system_prompt_with_startup_context(
    workspace: Path,
    user_config_dir: Path | None = None,
    *,
    state_dir: Path | None = None,
    allowed_dirs: tuple[Path, ...] = (),
) -> str:
    blocks = [SYSTEM_PROMPT.rstrip()]
    workspace_roots = _workspace_roots_context(workspace, allowed_dirs)
    if workspace_roots:
        blocks.append(workspace_roots)
    startup_context = _load_startup_context_files(
        workspace,
        user_config_dir or default_config_root(),
        max_chars=STARTUP_CONTEXT_CHAR_LIMIT,
    )
    if startup_context:
        blocks.append(
            "[User/project context]\n"
            "The following AGENTS.md context is advisory guidance loaded at session start. "
            "Prefer current user instructions and freshly inspected files when they conflict.\n\n"
            f"{startup_context}"
        )
    memory = _load_startup_memory(workspace, state_dir=state_dir, max_chars=STARTUP_MEMORY_CHAR_LIMIT)
    if memory:
        blocks.append(
            "[Memory]\n"
            "The following Markdown memory is advisory context loaded from project and state memory. "
            "Prefer current user instructions and freshly inspected files when they conflict.\n\n"
            f"{memory}"
        )
    skills = _load_authored_skills(workspace, max_chars=STARTUP_SKILLS_CHAR_LIMIT)
    if skills:
        blocks.append(
            "[Available project skills]\n"
            "The following project-authored skills are advisory workflow documents. "
            "If a skill is relevant, read its SKILL.md with read_file before using it; "
            "do not assume the full procedure from this metadata alone.\n\n"
            f"{skills}"
        )
    return "\n\n".join(blocks)


def _workspace_roots_context(workspace: Path, allowed_dirs: tuple[Path, ...]) -> str:
    lines = [
        "[Workspace roots]",
        f"Primary workspace (--cwd): {workspace}",
    ]
    if allowed_dirs:
        lines.extend(
            [
                "Additional allowed directories for file/search/LSP/patch tools:",
                *[f"- {path}" for path in allowed_dirs],
                (
                    "For multi-root tasks, first list/read the relevant allowed directory by its exact absolute "
                    "path. Do not invent a requirements directory under --cwd unless it actually appears in "
                    "list_files output."
                ),
                "Shell, git, session, todo, and memory remain anchored to --cwd.",
            ]
        )
    return "\n".join(lines)


def _workspace_root_markers(workspace: Path) -> list[str]:
    candidates = [
        ("pom.xml", workspace / "pom.xml"),
        ("build.gradle", workspace / "build.gradle"),
        ("settings.gradle", workspace / "settings.gradle"),
        ("package.json", workspace / "package.json"),
        ("pyproject.toml", workspace / "pyproject.toml"),
        ("src/main/java", workspace / "src" / "main" / "java"),
        ("src/main/resources", workspace / "src" / "main" / "resources"),
        ("src", workspace / "src"),
    ]
    markers: list[str] = []
    for label, path in candidates:
        if path.exists():
            markers.append(label)
    return markers


def _load_startup_context_files(workspace: Path, user_config_dir: Path, *, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    candidates = [
        user_config_dir / "AGENTS.md",
        workspace / ".local-agent" / "AGENTS.md",
    ]
    return _load_markdown_blocks(workspace, candidates, max_chars=max_chars, truncation_marker="...<earlier context truncated>\n")


def _load_startup_memory(workspace: Path, *, state_dir: Path | None = None, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    paths = _startup_memory_paths(_startup_memory_dirs(workspace, state_dir))
    return _load_markdown_blocks(workspace, paths, max_chars=max_chars, truncation_marker="...<earlier memory truncated>\n")


def _startup_memory_paths(memory_dirs: list[Path]) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for memory_dir in memory_dirs:
        priority_paths = [memory_dir / f"{name}.md" for name in STARTUP_MEMORY_NAMES]
        extra_paths = sorted(
            (
                path
                for path in memory_dir.glob("*.md")
                if path.name not in {f"{name}.md" for name in STARTUP_MEMORY_NAMES}
            ),
            key=lambda path: path.name.lower(),
        )
        for path in [*priority_paths, *extra_paths]:
            resolved = path.expanduser().resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            paths.append(path)
    return paths


def _startup_memory_dirs(workspace: Path, state_dir: Path | None) -> list[Path]:
    candidates = [workspace / ".local-agent" / "memory"]
    if state_dir is not None:
        candidates.append(state_dir / "memory")
    memory_dirs: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists() and resolved.is_dir():
            memory_dirs.append(resolved)
    return memory_dirs


def _load_sticky_rules(workspace: Path, user_config_dir: Path, *, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    candidates = [
        user_config_dir / "RULES.md",
        workspace / ".local-agent" / "RULES.md",
    ]
    return _load_markdown_blocks(workspace, candidates, max_chars=max_chars, truncation_marker="...<earlier rules truncated>\n")


def _load_markdown_blocks(
    workspace: Path,
    paths: list[Path],
    *,
    max_chars: int,
    truncation_marker: str,
) -> str:
    blocks: list[str] = []
    remaining = max_chars
    for path in paths:
        if remaining <= 0:
            break
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        text = text.replace("\x00", "").strip()
        if not text:
            continue
        header = f"### {_display_context_path(workspace, path)}\n"
        available = remaining - len(header)
        if available <= 0:
            break
        clipped = _clip_context_text(text, max_chars=available, marker=truncation_marker)
        block = f"{header}{clipped}"
        blocks.append(block)
        remaining -= len(block) + 2
    return "\n\n".join(blocks)


def _display_context_path(workspace: Path, path: Path) -> str:
    try:
        return str(path.relative_to(workspace))
    except ValueError:
        pass
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _display_read_file_evidence_path(workspace: Path, raw_path: str, allowed_dirs: tuple[Path, ...]) -> str:
    try:
        path = resolve_workspace_path(workspace, raw_path, allowed_dirs)
    except PatchError:
        return raw_path
    try:
        return str(path.relative_to(workspace))
    except ValueError:
        return str(path)


def _build_tool_evidence_record(
    name: str,
    arguments: str | dict[str, Any],
    result: ToolResult,
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
) -> EvidenceRecord | None:
    parsed = _parse_tool_arguments(arguments)
    if result.is_error and name not in {
        "apply_patch",
        "git_diff",
        "git_status",
        "rollback_patch",
        "run_tests",
        "shell",
        "write_file",
    }:
        return None
    if name == "read_file" and not result.is_error:
        return _read_file_evidence_record(parsed, result, workspace, allowed_dirs)
    if name == "search_code" and not result.is_error:
        return _search_code_evidence_record(parsed, result)
    if name.startswith("lsp_") and not result.is_error:
        return _lsp_evidence_record(name, parsed, result)
    if name in {"apply_patch", "rollback_patch", "write_file"}:
        return _file_change_evidence_record(name, parsed, result)
    if name in {"git_diff", "git_status"}:
        return _git_evidence_record(name, result)
    if name in {"run_tests", "shell"}:
        return _command_evidence_record(name, parsed, result)
    return None


def _read_file_evidence_record(
    parsed: dict[str, Any],
    result: ToolResult,
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
) -> EvidenceRecord | None:
    raw_path = parsed.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    subject = _display_read_file_evidence_path(workspace, raw_path.strip(), allowed_dirs)
    header = _first_nonempty_line(result.content)
    start_line = parsed.get("start_line") or 1
    end_line = parsed.get("end_line")
    if end_line:
        line_range = f"lines {start_line}-{end_line}"
    else:
        line_range = f"from line {start_line}"
    summary = f"{_one_line(header, max_chars=180)}; read {line_range}."
    return EvidenceRecord(tool="read_file", subject=subject, summary=summary)


def _search_code_evidence_record(parsed: dict[str, Any], result: ToolResult) -> EvidenceRecord | None:
    pattern = str(parsed.get("pattern") or "").strip()
    if not pattern:
        return None
    raw_path = str(parsed.get("path") or ".").strip() or "."
    subject = f"pattern={pattern!r} path={raw_path!r}"
    if result.useless or result.content.strip().startswith("No matches."):
        return EvidenceRecord(
            tool="search_code",
            subject=subject,
            summary="No matches returned.",
            status="no_match",
        )
    paths = _first_search_result_paths(result.content, limit=5)
    if paths:
        summary = "Matched files: " + ", ".join(paths)
    else:
        summary = "Returned matches; inspect the search_code tool result for exact lines."
    return EvidenceRecord(tool="search_code", subject=subject, summary=summary)


def _lsp_evidence_record(name: str, parsed: dict[str, Any], result: ToolResult) -> EvidenceRecord | None:
    subject = _lsp_subject(parsed)
    if not subject:
        subject = "query"
    if result.useless or result.content.strip().startswith("No "):
        return EvidenceRecord(
            tool=name,
            subject=subject,
            summary=_one_line(result.content, max_chars=220),
            status="no_match",
        )
    lines = _first_content_lines(result.content, limit=4)
    summary = " | ".join(_one_line(line, max_chars=160) for line in lines)
    if not summary:
        summary = "Returned lightweight code navigation results."
    return EvidenceRecord(tool=name, subject=subject, summary=summary)


def _file_change_evidence_record(name: str, parsed: dict[str, Any], result: ToolResult) -> EvidenceRecord | None:
    raw_path = parsed.get("path") if isinstance(parsed.get("path"), str) else ""
    subject = raw_path or name
    status = "error" if result.is_error else "ok"
    if name == "apply_patch" and parsed.get("dry_run") and not result.is_error:
        status = "preview"
    summary = _one_line(_first_nonempty_line(result.content) or result.content, max_chars=260)
    changed_files = _diff_changed_files(result.content, limit=4)
    if changed_files:
        summary = f"{summary}; diff files: {', '.join(changed_files)}"
    return EvidenceRecord(tool=name, subject=str(subject), summary=summary, status=status)


def _git_evidence_record(name: str, result: ToolResult) -> EvidenceRecord:
    status = "error" if result.is_error else "ok"
    if result.content.strip() in {"(empty)", "(empty diff)"}:
        status = "empty"
        summary = "No output."
    else:
        changed_files = _diff_changed_files(result.content, limit=6)
        if changed_files:
            summary = "Changed files: " + ", ".join(changed_files)
        else:
            summary = _one_line(result.content, max_chars=260)
    return EvidenceRecord(tool=name, subject="workspace", summary=summary, status=status)


def _command_evidence_record(name: str, parsed: dict[str, Any], result: ToolResult) -> EvidenceRecord:
    command = str(parsed.get("command") or ("default test command" if name == "run_tests" else "command"))
    status = "error" if result.is_error else "ok"
    exit_code = _last_exit_code_line(result.content)
    first_line = _first_nonempty_line(result.content)
    summary_parts = []
    if first_line:
        summary_parts.append(_one_line(first_line, max_chars=180))
    if exit_code:
        summary_parts.append(exit_code)
    summary = "; ".join(summary_parts) or "Command executed."
    return EvidenceRecord(tool=name, subject=_one_line(command, max_chars=140), summary=summary, status=status)


def _parse_tool_arguments(arguments: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _lsp_subject(parsed: dict[str, Any]) -> str:
    query = parsed.get("query")
    symbol = parsed.get("symbol")
    path = parsed.get("path")
    parts = []
    if isinstance(query, str) and query.strip():
        parts.append(f"query={query.strip()!r}")
    if isinstance(symbol, str) and symbol.strip():
        parts.append(f"symbol={symbol.strip()!r}")
    if isinstance(path, str) and path.strip():
        parts.append(f"path={path.strip()!r}")
    return " ".join(parts)


def _first_search_result_paths(content: str, *, limit: int) -> list[str]:
    paths: list[str] = []
    for line in content.splitlines():
        if not line or line.startswith("...") or line.startswith("Workspace roots:") or line.startswith("- "):
            continue
        path = line.split(":", 1)[0].strip()
        if not path or path in paths:
            continue
        paths.append(path)
        if len(paths) >= limit:
            break
    return paths


def _first_result_line_paths(content: str, *, limit: int) -> list[str]:
    paths: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("No ") or stripped.startswith("["):
            continue
        path = stripped.split(":", 1)[0].strip()
        if not path or path in paths:
            continue
        paths.append(path)
        if len(paths) >= limit:
            break
    return paths


def _diff_changed_files(content: str, *, limit: int) -> list[str]:
    files: list[str] = []
    for line in content.splitlines():
        path = ""
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3]
        elif line.startswith("+++ b/"):
            path = line[4:]
        elif line.startswith("--- a/"):
            path = line[4:]
        if path.startswith("b/") or path.startswith("a/"):
            path = path[2:]
        if path and path != "/dev/null" and path not in files:
            files.append(path)
            if len(files) >= limit:
                break
    return files


def _first_content_lines(content: str, *, limit: int) -> list[str]:
    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lines.append(stripped)
        if len(lines) >= limit:
            break
    return lines


def _first_nonempty_line(content: str) -> str:
    lines = _first_content_lines(content, limit=1)
    return lines[0] if lines else ""


def _last_exit_code_line(content: str) -> str:
    for line in reversed(content.splitlines()):
        stripped = line.strip()
        if stripped.startswith("[exit_code]"):
            return stripped
    return ""


def _one_line_block(content: str, *, max_chars: int) -> str:
    if len(content) <= max_chars:
        return content
    marker = "\n...<evidence ledger truncated>"
    keep = max(0, max_chars - len(marker))
    return content[:keep].rstrip() + marker


def _clip_memory_text(text: str, *, max_chars: int) -> str:
    return _clip_context_text(text, max_chars=max_chars, marker="...<earlier memory truncated>\n")


def _clip_context_text(text: str, *, max_chars: int, marker: str) -> str:
    if len(text) <= max_chars:
        return text
    keep = max(0, max_chars - len(marker))
    if keep == 0:
        return marker[:max_chars]
    return marker + text[-keep:].lstrip()


def _load_authored_skills(workspace: Path, *, max_chars: int) -> str:
    skills_dir = workspace / ".local-agent" / "skills"
    if max_chars <= 0 or not skills_dir.exists() or not skills_dir.is_dir():
        return ""
    lines: list[str] = []
    remaining = max_chars
    for skill_file in _iter_authored_skill_files(skills_dir):
        metadata = _read_skill_metadata(workspace, skill_file)
        if metadata is None or metadata.get("hide"):
            continue
        name = str(metadata["name"])
        description = str(metadata["description"])
        source = str(skill_file.relative_to(workspace))
        rendered = f"- {name}: {description} Source: {source}"
        if len(rendered) + 1 > remaining:
            break
        lines.append(rendered)
        remaining -= len(rendered) + 1
        if len(lines) >= MAX_AUTHORED_SKILLS:
            break
    return "\n".join(lines)


def _iter_authored_skill_files(skills_dir: Path) -> list[Path]:
    skill_files: list[Path] = []
    for child in sorted(skills_dir.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir():
            continue
        skill_file = child / "SKILL.md"
        if skill_file.is_file():
            skill_files.append(skill_file)
    return skill_files


def _read_skill_metadata(workspace: Path, skill_file: Path) -> dict[str, str | bool] | None:
    try:
        skill_file.resolve().relative_to(workspace.resolve())
    except (OSError, ValueError):
        return None
    try:
        raw = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    frontmatter = _parse_frontmatter(raw)
    fallback_name = skill_file.parent.name
    name = _clean_skill_name(str(frontmatter.get("name") or fallback_name))
    if not name:
        return None
    description = _clean_skill_description(str(frontmatter.get("description") or _fallback_skill_description(raw)))
    if not description:
        return None
    hide = _parse_bool(frontmatter.get("hide"))
    return {"name": name, "description": description, "hide": hide}


def _parse_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    data: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = _strip_wrapping_quotes(value.strip())
        if key in {"name", "description", "hide"}:
            data[key] = value
    return data


def _fallback_skill_description(text: str) -> str:
    in_frontmatter = False
    frontmatter_done = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "---" and not frontmatter_done:
            in_frontmatter = not in_frontmatter
            if not in_frontmatter:
                frontmatter_done = True
            continue
        if in_frontmatter or not line:
            continue
        if line.startswith("#"):
            continue
        return line
    return ""


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _clean_skill_name(name: str) -> str:
    cleaned = name.strip()[:64]
    if not cleaned or not all(char.isalnum() or char in {"-", "_"} for char in cleaned):
        return ""
    return cleaned


def _clean_skill_description(description: str) -> str:
    cleaned = " ".join(description.replace("\x00", "").split())
    if len(cleaned) > MAX_SKILL_DESCRIPTION_CHARS:
        cleaned = cleaned[: MAX_SKILL_DESCRIPTION_CHARS - 14].rstrip() + "...<truncated>"
    return cleaned


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _messages_with_runtime_todo_reminder(
    messages: list[dict[str, Any]],
    todo_summary: list[str],
) -> list[dict[str, Any]]:
    if not todo_summary:
        return list(messages)
    reminder = "\n".join(
        [
            "[Runtime todo reminder]",
            "Open todos are active for this session. Use them to stay oriented and update their status before finalizing when the task changes.",
            *todo_summary,
        ]
    )
    system_messages = [message for message in messages if message.get("role") == "system"]
    non_system = [message for message in messages if message.get("role") != "system"]
    return [_system_message_with_appended_context(system_messages, reminder), *non_system]


def _messages_with_runtime_context(
    messages: list[dict[str, Any]],
    todo_summary: list[str],
    evidence_ledger: str,
    planner_explore_context: str,
    workspace: Path,
    user_config_dir: Path,
    allowed_dirs: tuple[Path, ...] = (),
    current_user_request: str | None = None,
    requirement_contract_context: str = "",
) -> list[dict[str, Any]]:
    updated = list(messages)
    workspace_roots = _workspace_roots_context(workspace, allowed_dirs)
    if workspace_roots:
        updated = _messages_with_workspace_roots(updated, workspace_roots)
    if current_user_request:
        updated = _messages_with_current_task_contract(updated, current_user_request)
        updated = _messages_with_no_edit_final_hygiene(updated, current_user_request, todo_summary)
    if requirement_contract_context:
        updated = _messages_with_requirement_contract(updated, requirement_contract_context)
    if planner_explore_context:
        updated = _messages_with_planner_explore_context(updated, planner_explore_context)
    if evidence_ledger:
        updated = _messages_with_evidence_ledger(updated, evidence_ledger)
    sticky_rules = _load_sticky_rules(workspace, user_config_dir, max_chars=STICKY_RULES_CHAR_LIMIT)
    if sticky_rules:
        updated = _messages_with_sticky_rules(updated, sticky_rules)
    return _messages_with_runtime_todo_reminder(updated, todo_summary)


def _messages_with_workspace_roots(messages: list[dict[str, Any]], workspace_roots: str) -> list[dict[str, Any]]:
    system_messages = [message for message in messages if message.get("role") == "system"]
    non_system = [message for message in messages if message.get("role") != "system"]
    base = _system_message_with_appended_context(system_messages, workspace_roots)
    content = str(base.get("content") or "")
    first_marker = content.find("[Workspace roots]")
    last_marker = content.rfind("[Workspace roots]")
    if first_marker != -1 and first_marker != last_marker:
        base["content"] = content[:last_marker].rstrip()
    return [base, *non_system]


def _messages_with_current_task_contract(messages: list[dict[str, Any]], current_user_request: str) -> list[dict[str, Any]]:
    request = _one_line(current_user_request, max_chars=CURRENT_TASK_CONTRACT_CHAR_LIMIT)
    block = (
        "[Current task contract]\n"
        "This is the original user request for the current run. Preserve its hard constraints and final output "
        "structure when answering, even after many tool calls or compaction. Do not replace the requested final "
        "analysis with a summary of the last file you read; if evidence is incomplete, answer in the requested "
        "structure and state the uncertainty explicitly. File paths in final answers must be evidence-backed by "
        "tool results; label guessed class/file names as unverified candidates instead of presenting them as "
        "existing evidence paths. For evidence-heavy answers, separate directly verified facts from inference "
        "instead of stating inferred class/file roles as proven facts.\n"
        f"- {request}"
    )
    system_messages = [message for message in messages if message.get("role") == "system"]
    non_system = [message for message in messages if message.get("role") != "system"]
    base = _system_message_with_appended_context(system_messages, block)
    content = str(base.get("content") or "")
    first_marker = content.find("[Current task contract]")
    last_marker = content.rfind("[Current task contract]")
    if first_marker != -1 and first_marker != last_marker:
        base["content"] = content[:last_marker].rstrip()
    return [base, *non_system]


def _messages_with_requirement_contract(messages: list[dict[str, Any]], requirement_contract_context: str) -> list[dict[str, Any]]:
    block = (
        "[Requirement contract]\n"
        "Deterministic local checklist for this run. Use it as the acceptance/evidence boundary; "
        "do not treat it as repository evidence.\n"
        f"{requirement_contract_context}"
    )
    system_messages = [message for message in messages if message.get("role") == "system"]
    non_system = [message for message in messages if message.get("role") != "system"]
    base = _system_message_with_appended_context(system_messages, block)
    content = str(base.get("content") or "")
    first_marker = content.find("[Requirement contract]")
    last_marker = content.rfind("[Requirement contract]")
    if first_marker != -1 and first_marker != last_marker:
        base["content"] = content[:last_marker].rstrip()
    return [base, *non_system]


def _messages_with_planner_explore_context(messages: list[dict[str, Any]], planner_explore_context: str) -> list[dict[str, Any]]:
    block = (
        "[Planner / Explore]\n"
        "Local deterministic phase guidance for implementation tasks. Use it to decide whether to inspect, write, "
        "or verify; do not treat it as repository evidence.\n"
        f"{planner_explore_context}"
    )
    system_messages = [message for message in messages if message.get("role") == "system"]
    non_system = [message for message in messages if message.get("role") != "system"]
    base = _system_message_with_appended_context(system_messages, block)
    content = str(base.get("content") or "")
    first_marker = content.find("[Planner / Explore]")
    last_marker = content.rfind("[Planner / Explore]")
    if first_marker != -1 and first_marker != last_marker:
        base["content"] = content[:last_marker].rstrip()
    return [base, *non_system]


def _messages_with_no_edit_final_hygiene(
    messages: list[dict[str, Any]],
    current_user_request: str,
    todo_summary: list[str],
) -> list[dict[str, Any]]:
    if not is_code_implementation_request(current_user_request):
        return list(messages)
    todo_clause = (
        "If the user requested todo tracking or open todos exist, update/read todo state before finalizing."
        if request_mentions_todo(current_user_request) or todo_summary
        else "Todo tracking is optional unless the task becomes multi-step or ambiguous."
    )
    block = (
        "[No-edit final hygiene]\n"
        "For implementation/change requests, an evidence-backed decision to make no file changes is valid. "
        "Before finalizing that kind of no-edit stop, make it auditable: use git_status or git_diff to show whether "
        "the workspace changed, and explain why tests were not run if no files changed or the target implementation "
        "belongs to another service. "
        f"{todo_clause}"
    )
    system_messages = [message for message in messages if message.get("role") == "system"]
    non_system = [message for message in messages if message.get("role") != "system"]
    base = _system_message_with_appended_context(system_messages, block)
    content = str(base.get("content") or "")
    first_marker = content.find("[No-edit final hygiene]")
    last_marker = content.rfind("[No-edit final hygiene]")
    if first_marker != -1 and first_marker != last_marker:
        base["content"] = content[:last_marker].rstrip()
    return [base, *non_system]


def _messages_with_evidence_ledger(messages: list[dict[str, Any]], evidence_ledger: str) -> list[dict[str, Any]]:
    system_messages = [message for message in messages if message.get("role") == "system"]
    non_system = [message for message in messages if message.get("role") != "system"]
    base = _system_message_with_appended_context(system_messages, evidence_ledger)
    content = str(base.get("content") or "")
    first_marker = content.find("[Evidence ledger]")
    last_marker = content.rfind("[Evidence ledger]")
    if first_marker != -1 and first_marker != last_marker:
        base["content"] = content[:last_marker].rstrip()
    return [base, *non_system]


def _messages_with_sticky_rules(messages: list[dict[str, Any]], sticky_rules: str) -> list[dict[str, Any]]:
    block = (
        "[Sticky rules]\n"
        "The following RULES.md guidance is repeated for this model request. "
        "Follow it unless the current user explicitly overrides it.\n\n"
        f"{sticky_rules}"
    )
    system_messages = [message for message in messages if message.get("role") == "system"]
    non_system = [message for message in messages if message.get("role") != "system"]
    return [_system_message_with_appended_context(system_messages, block), *non_system]


def _system_message_with_appended_context(
    system_messages: list[dict[str, Any]],
    context: str,
) -> dict[str, Any]:
    base = dict(system_messages[0]) if system_messages else {"role": "system", "content": SYSTEM_PROMPT}
    base["role"] = "system"
    content = str(base.get("content") or "")
    base["content"] = f"{content.rstrip()}\n\n{context}"
    return base


def _system_message_with_compaction_summary(
    system_messages: list[dict[str, Any]],
    compaction_summary: str,
) -> dict[str, Any]:
    return _system_message_with_appended_context(
        system_messages,
        f"[Local context compaction]\n{compaction_summary}",
    )


def _latest_user_content(messages: list[dict[str, Any]]) -> str | None:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return _strip_workflow_nudge(content)
    return None


def _one_line(content: str, *, max_chars: int = 240) -> str:
    normalized = " ".join(content.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 14] + "...<truncated>"


def _display_optional_int(value: int | None) -> str:
    return "disabled" if value is None else str(value)


def _elapsed_ms_since(started_monotonic: float) -> int:
    try:
        return max(0, int((time.monotonic() - started_monotonic) * 1000))
    except Exception:  # noqa: BLE001 - run summary must never break task completion.
        return 0


def _format_last_run_status(summary: dict[str, Any]) -> list[str]:
    lines = [
        "- last_run:",
        f"  - reason: {summary.get('termination_reason', 'unknown')}",
        f"  - elapsed_ms: {summary.get('elapsed_ms', 0)}",
        f"  - llm_requests: {summary.get('llm_requests', 0)}",
        f"  - tool_calls: {summary.get('tool_calls', 0)}",
        f"  - tool_errors: {summary.get('tool_errors', 0)}",
        f"  - compactions: {summary.get('compactions', 0)}",
    ]
    tool_counts = summary.get("tool_counts")
    if isinstance(tool_counts, dict) and tool_counts:
        rendered_tools = ", ".join(f"{name}={count}" for name, count in sorted(tool_counts.items()))
        lines.append(f"  - tools: {rendered_tools}")
    guard_hits = summary.get("guard_hits")
    if isinstance(guard_hits, dict) and guard_hits:
        rendered_guards = ", ".join(f"{name}={count}" for name, count in sorted(guard_hits.items()))
        lines.append(f"  - guards: {rendered_guards}")
    steering_counts = summary.get("steering_counts")
    if isinstance(steering_counts, dict) and steering_counts:
        rendered_steers = ", ".join(f"{name}={count}" for name, count in sorted(steering_counts.items()))
        lines.append(f"  - steering: {rendered_steers}")
    return lines


def _with_workflow_nudge(prompt: str) -> str:
    if not _should_add_workflow_nudge(prompt):
        return prompt
    return f"{prompt.rstrip()}\n\n[Runtime workflow reminder]\n{WORKFLOW_NUDGE}"


def _strip_workflow_nudge(content: str) -> str:
    marker = "\n\n[Runtime workflow reminder]\n"
    if marker not in content:
        return content
    return content.split(marker, 1)[0]


def _should_add_workflow_nudge(prompt: str) -> bool:
    if is_analysis_only_request(prompt):
        return False
    lowered = prompt.lower()
    if len(prompt.strip()) <= 24 and not any(keyword in lowered for keyword in WORKFLOW_NUDGE_KEYWORDS):
        return False
    return any(keyword in lowered for keyword in WORKFLOW_NUDGE_KEYWORDS)


def _should_guard_repeated_read_file(prompt: str) -> bool:
    lowered = prompt.lower()
    if any(keyword in lowered for keyword in READ_FILE_DRIFT_GUARD_STRONG_READONLY_KEYWORDS):
        return True
    if any(keyword in lowered for keyword in READ_FILE_DRIFT_GUARD_EDIT_KEYWORDS):
        return False
    return any(keyword in lowered for keyword in READ_FILE_DRIFT_GUARD_KEYWORDS)


def _initial_soft_tool_requirement(
    prompt: str,
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
) -> SoftToolRequirement | None:
    skill_file = _mentioned_authored_skill_file(prompt, workspace)
    if skill_file is not None:
        return SoftToolRequirement(
            kind="authored_skill",
            allowed_dirs=(),
            candidate_files=(skill_file,),
        )
    if not allowed_dirs:
        return None
    lowered = prompt.lower()
    if not any(keyword in lowered for keyword in ALLOWED_DIR_REQUIREMENT_KEYWORDS):
        return None
    return SoftToolRequirement(
        kind="allowed_dir_requirements",
        allowed_dirs=allowed_dirs,
        candidate_files=_allowed_dir_doc_candidates(allowed_dirs),
    )


def _mentioned_authored_skill_file(prompt: str, workspace: Path) -> Path | None:
    lowered = prompt.lower()
    skills_dir = workspace / ".local-agent" / "skills"
    if not lowered.strip() or not skills_dir.exists() or not skills_dir.is_dir():
        return None
    for skill_file in _iter_authored_skill_files(skills_dir):
        metadata = _read_skill_metadata(workspace, skill_file)
        if metadata is None or metadata.get("hide"):
            continue
        names = {
            str(metadata["name"]).lower(),
            skill_file.parent.name.lower(),
        }
        if any(name and name in lowered for name in names):
            return skill_file
    return None


def _allowed_dir_doc_candidates(allowed_dirs: tuple[Path, ...]) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for root in allowed_dirs:
        if not root.exists() or not root.is_dir():
            continue
        for path in _iter_allowed_dir_files(root):
            if path.suffix.lower() not in ALLOWED_DIR_DOC_SUFFIXES:
                continue
            candidates.append(path)
    candidates.sort(key=_allowed_dir_doc_sort_key)
    return tuple(candidates[:MAX_ALLOWED_DIR_DOC_CANDIDATES])


def _iter_allowed_dir_files(root: Path):
    skipped = {".git", ".local-agent", ".venv", "__pycache__", "node_modules", "target", "dist", "build"}
    for child in sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if child.is_dir():
            if child.name in skipped or child.name.startswith("."):
                continue
            yield from _iter_allowed_dir_files(child)
        elif child.is_file():
            yield child


def _allowed_dir_doc_sort_key(path: Path) -> tuple[int, str]:
    lowered = path.name.lower()
    score = 0 if any(keyword in lowered for keyword in ALLOWED_DIR_DOC_NAME_KEYWORDS) else 1
    return (score, str(path).lower())


def _soft_tool_requirement_message(requirement: SoftToolRequirement) -> str:
    if requirement.kind == "authored_skill":
        lines = [
            "[Runtime tool requirement]",
            "This task explicitly references a project-authored skill. Read the skill instructions before applying it.",
            "Use only read_file until this requirement is satisfied.",
            "",
            "Required skill file:",
            *[f"- {path}" for path in requirement.candidate_files],
            "",
            "Required next evidence: call read_file on the relevant SKILL.md file above. "
            "Do not answer from skill metadata alone.",
        ]
        return "\n".join(lines)
    lines = [
        "[Runtime tool requirement]",
        (
            "This task explicitly references external requirements/spec documents. "
            "Before searching or concluding from the primary code workspace, read evidence from an allowed directory."
        ),
        "Use only list_files/read_file until this requirement is satisfied.",
        "",
        "Allowed directories:",
        *[f"- {path}" for path in requirement.allowed_dirs],
    ]
    if requirement.candidate_files:
        lines.extend(
            [
                "",
                "Candidate requirement/spec files; prefer read_file on the most relevant ones first:",
                *[f"- {path}" for path in requirement.candidate_files],
            ]
        )
    lines.extend(
        [
            "",
            "Required next evidence: call read_file with a path under one of the allowed directories. "
            "Do not answer or search the primary code workspace until at least one allowed-directory document has been read.",
        ]
    )
    return "\n".join(lines)


def _soft_tool_requirement_path_satisfies(requirement: SoftToolRequirement, path: Path) -> bool:
    if requirement.kind == "authored_skill":
        return _path_matches_any_candidate(path, requirement.candidate_files)
    if requirement.kind == "allowed_dir_requirements":
        return _path_is_under_any(path, requirement.allowed_dirs)
    return False


def _path_matches_any_candidate(path: Path, candidates: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    for candidate in candidates:
        try:
            if resolved == candidate.resolve():
                return True
        except OSError:
            continue
    return False


def _path_is_under_any(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _messages_to_memory_transcript(
    messages: list[dict[str, Any]],
    final_content: str,
    *,
    max_chars: int,
) -> str:
    lines: list[str] = []
    total = 0
    for message in messages:
        rendered = _render_memory_transcript_message(message)
        if not rendered:
            continue
        remaining = max_chars - total
        if remaining <= 0:
            break
        if len(rendered) > remaining:
            rendered = rendered[: max(0, remaining - 14)] + "...<truncated>"
        lines.append(rendered)
        total += len(rendered) + 1
    if final_content.strip() and not _last_assistant_content_is(messages, final_content):
        rendered = f"final: {_one_line(final_content, max_chars=1200)}"
        remaining = max_chars - total
        if remaining > 0:
            if len(rendered) > remaining:
                rendered = rendered[: max(0, remaining - 14)] + "...<truncated>"
            lines.append(rendered)
            total += len(rendered) + 1
    if total >= max_chars:
        lines.append("...<transcript truncated for memory consolidation>")
    return "\n".join(lines)


def _render_memory_transcript_message(message: dict[str, Any]) -> str:
    role = str(message.get("role") or "unknown")
    if role == "system":
        return ""
    if role == "user":
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            return ""
        return f"user: {_one_line(_strip_workflow_nudge(content), max_chars=1200)}"
    if role == "assistant":
        tool_names = _assistant_tool_call_names(message)
        content = message.get("content")
        if tool_names:
            prefix = f"assistant tool_calls: {', '.join(tool_names)}"
            if isinstance(content, str) and content.strip():
                return f"{prefix}; note: {_one_line(content, max_chars=600)}"
            return prefix
        if isinstance(content, str) and content.strip():
            return f"assistant: {_one_line(content, max_chars=1200)}"
        return ""
    if role == "tool":
        name = str(message.get("_lca_tool_name") or "tool")
        error = " error" if message.get("_lca_is_error") is True else ""
        content = message.get("content")
        return f"{name}{error}: {_one_line(str(content or ''), max_chars=1200)}"
    content = message.get("content")
    if content is None:
        return ""
    return f"{role}: {_one_line(str(content), max_chars=1200)}"


def _assistant_tool_call_names(message: dict[str, Any]) -> list[str]:
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


def _last_assistant_content_is(messages: list[dict[str, Any]], final_content: str) -> bool:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        return isinstance(content, str) and content == final_content
    return False


def _run_used_memory_write_tool(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        if message.get("role") != "assistant":
            continue
        if any(name in MEMORY_CONSOLIDATION_WRITE_TOOLS for name in _assistant_tool_call_names(message)):
            return True
    return False


def _should_auto_consolidate_memory(
    transcript: str,
    messages: list[dict[str, Any]],
    final_content: str,
) -> bool:
    lowered = f"{transcript}\n{final_content}".lower()
    durable_keywords = {
        "always",
        "convention",
        "decision",
        "learn",
        "lesson",
        "memory",
        "prefer",
        "remember",
        "以后",
        "偏好",
        "决策",
        "学到",
        "惯例",
        "经验",
        "记住",
        "约定",
    }
    if any(keyword in lowered for keyword in durable_keywords):
        return True
    if len(transcript) < MEMORY_CONSOLIDATION_MIN_AUTO_CHARS:
        return False
    has_tool_result = any(message.get("role") == "tool" for message in messages)
    if has_tool_result:
        return True
    return len(final_content.strip()) >= MEMORY_CONSOLIDATION_MIN_AUTO_CHARS


def _parse_memory_consolidation_response(content: str) -> dict[str, list[str]] | None:
    raw = _extract_json_object_text(content)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    parsed: dict[str, list[str]] = {}
    for bucket in MEMORY_CONSOLIDATION_BUCKETS:
        value = data.get(bucket, [])
        if value is None:
            value = []
        if not isinstance(value, list):
            return None
        items: list[str] = []
        seen: set[str] = set()
        for raw_item in value:
            if not isinstance(raw_item, str):
                continue
            item = _clean_consolidated_memory_item(raw_item)
            if not item:
                continue
            key = _normalized_memory_item_key(item)
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            if len(items) >= MEMORY_CONSOLIDATION_MAX_ITEMS_PER_BUCKET:
                break
        parsed[bucket] = items
    return parsed


def _extract_json_object_text(content: str) -> str | None:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        return None
    return stripped[start : end + 1]


def _clean_consolidated_memory_item(item: str) -> str:
    cleaned = " ".join(item.replace("\x00", "").split())
    if len(cleaned) > MEMORY_CONSOLIDATION_MAX_ITEM_CHARS:
        cleaned = cleaned[: MEMORY_CONSOLIDATION_MAX_ITEM_CHARS - 14].rstrip() + "...<truncated>"
    return cleaned


def _memory_consolidation_root(workspace: Path, state_dir: Path, scope: str) -> Path:
    if scope == "project":
        return workspace / ".local-agent" / "memory"
    return state_dir / "memory"


def _append_consolidated_memory(
    memory_dir: Path,
    session_id: str,
    items_by_bucket: dict[str, list[str]],
) -> dict[str, int]:
    written: dict[str, int] = {}
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    for bucket in MEMORY_CONSOLIDATION_BUCKETS:
        items = items_by_bucket.get(bucket) or []
        if not items:
            continue
        path = memory_dir / f"{bucket}.md"
        existing = ""
        if path.exists():
            try:
                existing = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                existing = ""
        pending: list[tuple[str, str]] = []
        for item in items:
            digest = _memory_item_digest(bucket, item)
            if f"lca-memory:{digest}" in existing:
                continue
            pending.append((digest, item))
        if not pending:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## {stamp} - consolidated from session {session_id}\n\n")
            for digest, item in pending:
                handle.write(f"<!-- lca-memory:{digest} -->\n- {item}\n")
        written[bucket] = len(pending)
    return written


def _memory_item_digest(bucket: str, item: str) -> str:
    payload = f"{bucket}\0{_normalized_memory_item_key(item)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _normalized_memory_item_key(item: str) -> str:
    return " ".join(item.casefold().split())


def _tool_call_signature(name: str, arguments: str | dict[str, Any]) -> str:
    if isinstance(arguments, dict):
        normalized_arguments: Any = arguments
    else:
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            parsed = arguments
        normalized_arguments = parsed
    payload = {
        "name": name,
        "arguments": normalized_arguments,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _intersect_optional_tool_allowlist(
    current: set[str] | None,
    next_allowed: set[str] | frozenset[str],
) -> set[str]:
    allowed = set(next_allowed)
    if current is None:
        return allowed
    return current.intersection(allowed)


def _tool_choice_steering_signature(decision: ToolChoiceDecision, result_count: int) -> str:
    payload = {
        "rule_id": decision.rule_id,
        "missing": decision.missing_requirements,
        "results": result_count,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _tool_choice_signature_count(signatures: set[str], rule_id: str | None) -> int:
    prefix = f'"rule_id": "{rule_id}"' if rule_id else '"rule_id": null'
    return sum(1 for signature in signatures if prefix in signature)


def _tool_choice_steering_message(decision: ToolChoiceDecision, current_user_request: str | None) -> str:
    allowed = ", ".join(sorted(decision.allowed_tool_names)) or "(no tools currently allowed)"
    preferred = ", ".join(decision.preferred_tool_names) or "(none)"
    missing = ", ".join(decision.missing_requirements) or "(none)"
    request = _one_line(current_user_request or "", max_chars=800)
    return (
        "[Runtime tool choice queue]\n"
        "A required workflow gate is not satisfied yet. Use the allowed tool set for the next step; "
        "do not answer as final until the missing requirement is satisfied or you can explicitly explain why it cannot be satisfied.\n"
        f"- rule: {decision.rule_id or 'unknown'}\n"
        f"- missing: {missing}\n"
        f"- preferred next tools: {preferred}\n"
        f"- allowed tools now: {allowed}\n"
        f"- reason: {decision.reason}\n"
        f"- original request: {request}"
    )


def _tool_choice_argument_path(arguments: str | dict[str, Any]) -> str | None:
    parsed: Any = arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    path = parsed.get("path")
    return str(path) if path is not None else None


def _search_pattern_key(name: str, arguments: str | dict[str, Any]) -> str | None:
    if name != "search_code":
        return None
    if isinstance(arguments, dict):
        parsed: Any = arguments
    else:
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    pattern = parsed.get("pattern")
    if not isinstance(pattern, str):
        return None
    normalized = " ".join(pattern.strip().lower().split())
    return normalized or None


def _lsp_symbol_query_key(name: str, arguments: str | dict[str, Any]) -> str | None:
    if name not in {"lsp_symbols", "lsp_workspace_symbols", "lsp_document_symbols"}:
        return None
    if isinstance(arguments, dict):
        parsed: Any = arguments
    else:
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    query = parsed.get("query")
    if not isinstance(query, str):
        return None
    normalized = " ".join(query.strip().lower().split())
    return normalized or None


def _semantic_exploration_key(
    name: str,
    arguments: str | dict[str, Any],
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
) -> str | None:
    if name != "list_files":
        return None
    parsed = _parse_tool_arguments(arguments)
    raw_path = str(parsed.get("path") or ".").strip() or "."
    if raw_path in {"", "."}:
        return None
    try:
        path = resolve_workspace_path(workspace, raw_path, allowed_dirs)
    except PatchError:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = workspace / path
    return _semantic_directory_key(path, workspace, allowed_dirs)


def _semantic_directory_key(path: Path, workspace: Path, allowed_dirs: tuple[Path, ...]) -> str | None:
    parts = _path_parts_relative_to_known_root(path, (workspace, *allowed_dirs))
    if not parts:
        return None
    if "src" in parts:
        src_index = parts.index("src")
        if src_index > 0:
            parts = parts[:src_index]
    elif len(parts) > 2:
        parts = parts[:2]
    key_parts = [part for part in parts[:3] if part not in {"", ".", "/"}]
    if not key_parts:
        return None
    return "/".join(key_parts)


def _path_parts_relative_to_known_root(path: Path, roots: tuple[Path, ...]) -> list[str]:
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path.absolute()
    best_relative: Path | None = None
    best_depth = -1
    for root in roots:
        try:
            resolved_root = root.resolve(strict=False)
            relative = resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        depth = len(resolved_root.parts)
        if depth > best_depth:
            best_relative = relative
            best_depth = depth
    candidate = best_relative if best_relative is not None else resolved
    return [part for part in candidate.parts if part not in {"", ".", candidate.anchor}]


def _read_file_path_key(
    name: str,
    arguments: str | dict[str, Any],
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
) -> str | None:
    if name != "read_file":
        return None
    if isinstance(arguments, dict):
        parsed: Any = arguments
    else:
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    raw_path = parsed.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    try:
        path = resolve_workspace_path(workspace, raw_path, allowed_dirs)
    except PatchError:
        return raw_path
    return str(path)


def _read_file_range_key(
    name: str,
    arguments: str | dict[str, Any],
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
) -> tuple[str, int, str] | None:
    if name != "read_file":
        return None
    parsed = _parse_tool_arguments(arguments)
    raw_path = parsed.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    start_line = _read_file_line_number(parsed.get("start_line"), default=1)
    if start_line is None:
        return None
    end_value = parsed.get("end_line")
    if end_value is None:
        end_key = "default"
    else:
        end_line = _read_file_line_number(end_value, default=1)
        if end_line is None or end_line < start_line:
            return None
        end_key = str(end_line)
    try:
        path = resolve_workspace_path(workspace, raw_path, allowed_dirs)
    except PatchError:
        path_key = raw_path
    else:
        path_key = str(path)
    return (path_key, start_line, end_key)


def _read_file_line_number(value: object, *, default: int) -> int | None:
    if value is None:
        return default
    try:
        line_number = int(value)
    except (TypeError, ValueError):
        return None
    return line_number if line_number >= 1 else None


def _display_read_file_range_key(
    range_key: tuple[str, int, str],
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
) -> str:
    subject = _display_read_file_range_subject(range_key, workspace, allowed_dirs)
    _, start_line, end_key = range_key
    if end_key == "default":
        return f"{subject} from line {start_line}"
    return f"{subject} lines {start_line}-{end_key}"


def _display_read_file_range_subject(
    range_key: tuple[str, int, str],
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
) -> str:
    path_key = range_key[0]
    try:
        return display_workspace_path(workspace, Path(path_key), allowed_dirs)
    except (OSError, RuntimeError, ValueError):
        return path_key


def _validate_runtime_tool_name(tool: str) -> str:
    normalized = tool.strip()
    if not normalized or not all(char.isalnum() or char == "_" for char in normalized):
        raise ValueError(f"invalid tool name: {tool}")
    return normalized

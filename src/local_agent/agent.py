from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from pathlib import Path
import sys
import time
from typing import Any

from .config import AgentConfig
from .config import normalize_approval_mode
from .llm import LlmError
from .llm import OpenAICompatibleClient
from .patch.anchored import PatchError
from .patch.anchored import resolve_workspace_path
from .session.jsonl_store import JsonlSessionStore
from .state import default_config_root
from .tools import create_default_registry
from .tools.base import ToolContext
from .tools.base import ToolResult
from .tools.base import tool_state_dir


SYSTEM_PROMPT = """You are a local coding agent running inside a user's workspace.

Default working style:
- Work from local evidence, not guesses. Choose the tools yourself; the user should not need to spell out tool order.
- For repo understanding, start with list_files/search_code/read_file as needed. For code navigation in Python, Java, JavaScript, TypeScript, or Vue, prefer lsp_symbols/lsp_definition/lsp_references/lsp_diagnostics before broad text search when helpful. lsp_workspace_symbols and lsp_document_symbols are compatibility aliases for lsp_symbols. Read the exact file or range before editing it.
- The primary --cwd is the main workspace. If additional directories are configured, file/search/LSP/patch tools may access those explicit paths; shell, git, session, todo, and memory remain anchored to --cwd.
- For multi-step or ambiguous work, maintain a concise todo list with todo_add/todo_update/todo_read.
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

COMPACTION_TOOL_CONTENT_CHAR_LIMIT = 6000
SUMMARY_INPUT_CHAR_LIMIT = 12000
SUMMARY_OUTPUT_CHAR_LIMIT = 4000
SUMMARY_REQUEST_TIMEOUT = 30.0
MEMORY_CONSOLIDATION_INPUT_CHAR_LIMIT = 14000
MEMORY_CONSOLIDATION_OUTPUT_CHAR_LIMIT = 8000
MEMORY_CONSOLIDATION_REQUEST_TIMEOUT = 30.0
MEMORY_CONSOLIDATION_MIN_AUTO_CHARS = 500
MEMORY_CONSOLIDATION_MAX_ITEMS_PER_BUCKET = 5
MEMORY_CONSOLIDATION_MAX_ITEM_CHARS = 700
MEMORY_CONSOLIDATION_BUCKETS = ("project", "decisions", "conventions", "learned")
MEMORY_CONSOLIDATION_WRITE_TOOLS = {"memory_write", "learn"}
DEFAULT_RESERVE_CHARS = 16384 * 4
MIN_RESERVE_RATIO = 0.15
STARTUP_MEMORY_NAMES = ("project", "decisions", "conventions", "learned")
STARTUP_MEMORY_CHAR_LIMIT = 8000
STARTUP_CONTEXT_CHAR_LIMIT = 8000
STICKY_RULES_CHAR_LIMIT = 4000
CURRENT_TASK_CONTRACT_CHAR_LIMIT = 2000
STARTUP_SKILLS_CHAR_LIMIT = 4000
MAX_AUTHORED_SKILLS = 40
MAX_SKILL_DESCRIPTION_CHARS = 320
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
MAX_REPEATED_READ_FILE_GUARD_HITS = 4
MAX_REPEATED_READ_FILE_FINAL_ANSWER_STEERS = 2
MAX_SOFT_TOOL_REQUIREMENT_STEERS = 3
USELESS_TOOL_RESULT_NOTICE = "[Uneventful tool result elided during local context pruning]"
SUPERSEDED_TOOL_RESULT_NOTICE = "[Superseded by a newer equivalent tool result during local context pruning]"

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


class AgentRuntime:
    def __init__(
        self,
        config: AgentConfig,
        *,
        show_tool_logs: bool = True,
        session_id: str | None = None,
        continue_session: bool = False,
    ):
        self._config = config
        self._show_tool_logs = show_tool_logs
        self._client = OpenAICompatibleClient(config)
        self._registry = create_default_registry()
        self._session_tool_approval: dict[str, str] = {}
        self._summary_cache: dict[str, str] = {}
        self._recent_tool_call_signatures: list[str] = []
        self._recent_useless_search_pattern_keys: list[str] = []
        self._recent_useless_lsp_symbol_query_keys: list[str] = []
        self._recent_read_file_path_keys: list[str] = []
        self._duplicate_tool_guard_hits = 0
        self._duplicate_tool_final_answer_steers = 0
        self._useless_search_pattern_guard_hits = 0
        self._useless_search_pattern_final_answer_steers = 0
        self._useless_lsp_symbol_guard_hits = 0
        self._useless_lsp_symbol_final_answer_steers = 0
        self._repeated_read_file_guard_hits = 0
        self._repeated_read_file_final_answer_steers = 0
        self._read_file_evidence_paths: list[str] = []
        self._current_user_request: str | None = None
        self._read_file_drift_guard_enabled = False
        self._force_final_answer_without_tools = False
        self._soft_tool_requirement: SoftToolRequirement | None = None
        self._state_dir = config.state_dir or config.workspace / ".local-agent"
        self._session = JsonlSessionStore(
            config.workspace,
            state_dir=self._state_dir,
            session_id=session_id,
            continue_recent=continue_session,
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
        )
        if self._show_tool_logs:
            print(f"[session] {self._session.session_id}", file=sys.stderr)

    def run(self, prompt: str) -> str:
        deadline = (
            time.monotonic() + self._config.budget_seconds
            if self._config.budget_seconds is not None
            else None
        )
        run_start_index = len(self._messages)
        model_prompt = _with_workflow_nudge(prompt)
        self._current_user_request = prompt
        self._messages.append({"role": "user", "content": model_prompt})
        self._session.append("user", {"content": prompt})
        if model_prompt != prompt:
            self._session.append("workflow_nudge", {"content": WORKFLOW_NUDGE})
        self._read_file_drift_guard_enabled = _should_guard_repeated_read_file(prompt)
        self._soft_tool_requirement = _initial_soft_tool_requirement(prompt, self._config.allowed_dirs)
        if self._soft_tool_requirement is not None:
            self._append_soft_tool_requirement_message(self._soft_tool_requirement)
        tool_context = replace(self._tool_context, deadline_monotonic=deadline)

        step = 1
        while self._config.max_steps == 0 or step <= self._config.max_steps:
            if self._deadline_exceeded(deadline):
                return self._stop_for_budget(deadline, run_start_index)

            self._session.append("llm_request", {"step": step})
            messages_for_model = self._messages_for_model(deadline)
            tools_for_model = self._tools_for_model()
            force_final_answer = self._force_final_answer_without_tools
            self._force_final_answer_without_tools = False
            response = self._client.chat(
                messages_for_model,
                tools_for_model,
                timeout=self._remaining_timeout(deadline),
            )
            if force_final_answer:
                self._session.append("runtime_steering", {"kind": "forced_final_answer", "step": step})
            message = {**response.message, "role": "assistant"}
            self._messages.append(message)
            self._session.append("assistant", message)

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
                    )
                content = message.get("content") or ""
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
                self._record_read_file_evidence(name, arguments, result)
                self._observe_soft_tool_requirement(name, arguments, result)
                duplicate_skipped = self._duplicate_tool_guard_hits > duplicate_hits_before
                useless_search_skipped = self._useless_search_pattern_guard_hits > useless_search_hits_before
                useless_lsp_skipped = self._useless_lsp_symbol_guard_hits > useless_lsp_hits_before
                repeated_read_skipped = self._repeated_read_file_guard_hits > repeated_read_hits_before
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

        return self._finish_run(f"Stopped after reaching max_steps={self._config.max_steps}.", deadline, run_start_index)

    def _tools_for_model(self) -> list[dict[str, Any]]:
        if self._force_final_answer_without_tools:
            return []
        requirement = self._soft_tool_requirement
        if requirement is not None and not requirement.satisfied:
            allowed_names = {"list_files", "read_file"}
            return [
                schema
                for schema in self._registry.schemas()
                if schema.get("function", {}).get("name") in allowed_names
            ]
        return self._registry.schemas()

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

    def _messages_for_model(self, deadline: float | None = None) -> list[dict[str, Any]]:
        todo_summary = self._open_todo_summary()
        provider_context = _prune_context_tool_outputs(self._messages)
        if self._config.context_char_budget <= 0:
            return self._provider_safe_runtime_messages(provider_context, todo_summary)
        threshold = _resolve_compaction_threshold_chars(self._config.context_char_budget)
        if _estimate_message_chars(provider_context) <= threshold:
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
            if _estimate_message_chars(compacted) <= self._config.context_char_budget or recent_count <= 6:
                self._session.append(
                    "context_compaction",
                    {
                        "original_messages": len(self._messages),
                        "sent_messages": len(compacted),
                        "dropped_messages": len(dropped),
                        "threshold_chars": threshold,
                    },
                )
                return self._provider_safe_runtime_messages(compacted, todo_summary)
            recent_count = max(6, recent_count // 2)
        return self._provider_safe_runtime_messages(self._messages, todo_summary)

    def _provider_safe_runtime_messages(
        self,
        messages: list[dict[str, Any]],
        todo_summary: list[str],
    ) -> list[dict[str, Any]]:
        return _provider_safe_messages(
            _messages_with_runtime_context(
                messages,
                todo_summary,
                self._config.workspace,
                self._user_config_dir,
                self._config.allowed_dirs,
                self._current_user_request,
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
        return result

    def _repeated_read_file_result(self, path_key: str, prior_count: int) -> ToolResult:
        return ToolResult(
            (
                f"Tool call skipped: read_file has already read '{path_key}' {prior_count} times recently. "
                "Use the collected evidence and provide the requested final answer, "
                "or switch to a different, more targeted file only if new evidence is truly necessary."
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
            return
        self._read_file_evidence_paths.append(display_path)
        self._read_file_evidence_paths = self._read_file_evidence_paths[-20:]

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

    def _final_answer_request_summary(self) -> str:
        if not self._current_user_request:
            return ""
        return (
            "\n\nOriginal user request to satisfy now:\n"
            f"- {_one_line(self._current_user_request, max_chars=1200)}"
        )

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
        if requirement.kind != "allowed_dir_requirements":
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
        if _path_is_under_any(path, requirement.allowed_dirs):
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

    def _finish_run(self, content: str, deadline: float | None, run_start_index: int) -> str:
        self._session.append("final", {"content": content})
        run_messages = self._messages[run_start_index:]
        self._maybe_consolidate_session_memory(run_messages, content, deadline)
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
        return self._finish_run(content, deadline, run_start_index)

    def _duplicate_tool_stop_message(self) -> str:
        return "the assistant repeated identical tool calls too many times."

    def _stop_for_duplicate_tools(self, deadline: float | None, run_start_index: int) -> str:
        content = (
            "Stopped because the assistant repeated identical tool calls too many times. "
            "Retry with a narrower request or ask it to answer from the evidence already collected."
        )
        return self._finish_run(content, deadline, run_start_index)

    def _stop_for_repeated_read_file(self, deadline: float | None, run_start_index: int) -> str:
        content = (
            "Stopped because the assistant kept reading adjacent ranges from the same file. "
            "Retry with a narrower request or ask it to answer from the evidence already collected."
        )
        return self._finish_run(content, deadline, run_start_index)

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
        return self._finish_run(content, deadline, run_start_index)

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
        return self._finish_run(content, deadline, run_start_index)

    def _stop_for_interrupt(self) -> str:
        content = "Stopped after user interrupt."
        self._session.append("final", {"content": content})
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
        return self._finish_run(content, deadline, run_start_index)

    def _append_tool_result(
        self,
        tool_call: dict[str, Any],
        name: str,
        content: str,
        *,
        is_error: bool,
        useless: bool = False,
    ) -> None:
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
            self._append_tool_result(tool_call, name, result, is_error=True)

    def _log_tool_start(self, name: str, arguments: Any) -> None:
        if not self._show_tool_logs:
            return
        rendered = str(arguments)
        if len(rendered) > 1000:
            rendered = rendered[:1000] + "...<truncated>"
        print(f"[tool:start] {name} {rendered}", file=sys.stderr)

    def _log_tool_end(self, name: str, is_error: bool, content_length: int) -> None:
        if not self._show_tool_logs:
            return
        status = "error" if is_error else "ok"
        print(f"[tool:end] {name} {status} ({content_length} chars)", file=sys.stderr)


def _estimate_message_chars(messages: list[dict[str, Any]]) -> int:
    return sum(len(json.dumps(message, ensure_ascii=False, default=str)) for message in messages)


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
    memory_dirs = _startup_memory_dirs(workspace, state_dir)
    paths = [memory_dir / f"{name}.md" for memory_dir in memory_dirs for name in STARTUP_MEMORY_NAMES]
    return _load_markdown_blocks(workspace, paths, max_chars=max_chars, truncation_marker="...<earlier memory truncated>\n")


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


def _resolve_compaction_threshold_chars(context_window_chars: int) -> int:
    if context_window_chars <= 1:
        return 0
    reserve = _resolve_budget_reserve_chars(context_window_chars)
    return max(0, min(context_window_chars - 1, context_window_chars - reserve))


def _resolve_budget_reserve_chars(context_window_chars: int) -> int:
    proportional_reserve = max(1, int(context_window_chars * MIN_RESERVE_RATIO))
    default_reserve = max(proportional_reserve, DEFAULT_RESERVE_CHARS)
    default_reserve_is_impossible = default_reserve >= context_window_chars - proportional_reserve
    reserve_exceeds_window = default_reserve >= context_window_chars
    return proportional_reserve if default_reserve_is_impossible or reserve_exceeds_window else default_reserve


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
    workspace: Path,
    user_config_dir: Path,
    allowed_dirs: tuple[Path, ...] = (),
    current_user_request: str | None = None,
) -> list[dict[str, Any]]:
    updated = list(messages)
    workspace_roots = _workspace_roots_context(workspace, allowed_dirs)
    if workspace_roots:
        updated = _messages_with_workspace_roots(updated, workspace_roots)
    if current_user_request:
        updated = _messages_with_current_task_contract(updated, current_user_request)
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
        "existing evidence paths.\n"
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


def _valid_recent_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    recent = list(messages)
    while recent and recent[0].get("role") == "tool":
        recent = recent[1:]
    return _drop_trailing_unpaired_tool_calls(recent)


def _prune_context_tool_outputs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    superseded_tool_call_ids = _superseded_tool_call_ids(messages)
    pruned: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "tool":
            pruned.append(message)
            continue
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id in superseded_tool_call_ids:
            pruned.append(_tool_message_with_notice(message, SUPERSEDED_TOOL_RESULT_NOTICE))
        elif message.get("_lca_useless") is True and message.get("_lca_is_error") is not True:
            pruned.append(_tool_message_with_notice(message, USELESS_TOOL_RESULT_NOTICE))
        else:
            pruned.append(message)
    return pruned


def _tool_message_with_notice(message: dict[str, Any], notice: str) -> dict[str, Any]:
    copied = dict(message)
    copied["content"] = notice
    return copied


def _superseded_tool_call_ids(messages: list[dict[str, Any]]) -> set[str]:
    tool_calls_by_id = _tool_calls_by_id(messages)
    latest_by_key: dict[str, str] = {}
    superseded: set[str] = set()
    for message in messages:
        if message.get("role") != "tool":
            continue
        tool_call_id = message.get("tool_call_id")
        if not isinstance(tool_call_id, str):
            continue
        if message.get("_lca_is_error") is True:
            continue
        tool_call = tool_calls_by_id.get(tool_call_id)
        if tool_call is None:
            continue
        key = _tool_supersede_key(tool_call)
        if key is None:
            continue
        previous = latest_by_key.get(key)
        if previous is not None:
            superseded.add(previous)
        latest_by_key[key] = tool_call_id
    return superseded


def _tool_calls_by_id(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    tool_calls: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        raw_tool_calls = message.get("tool_calls") or []
        if not isinstance(raw_tool_calls, list):
            continue
        for tool_call in raw_tool_calls:
            if isinstance(tool_call, dict) and isinstance(tool_call.get("id"), str):
                tool_calls[tool_call["id"]] = tool_call
    return tool_calls


def _tool_supersede_key(tool_call: dict[str, Any]) -> str | None:
    function = tool_call.get("function") or {}
    if not isinstance(function, dict):
        return None
    name = str(function.get("name") or "")
    if name not in {
        "read_file",
        "search_code",
        "lsp_workspace_symbols",
        "lsp_document_symbols",
        "lsp_symbols",
        "lsp_definition",
        "lsp_references",
        "lsp_diagnostics",
    }:
        return None
    return _tool_call_signature(name, function.get("arguments") or "{}")


def _truncate_recent_tool_outputs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    truncated: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "tool" and isinstance(content, str):
            truncated.append(_truncate_tool_message(message, content))
        else:
            truncated.append(message)
    return truncated


def _system_message_with_compaction_summary(
    system_messages: list[dict[str, Any]],
    compaction_summary: str,
) -> dict[str, Any]:
    return _system_message_with_appended_context(
        system_messages,
        f"[Local context compaction]\n{compaction_summary}",
    )


def _provider_safe_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    for message in messages:
        safe.append({key: value for key, value in message.items() if not key.startswith("_lca_")})
    return safe


def _truncate_tool_message(message: dict[str, Any], content: str) -> dict[str, Any]:
    if len(content) <= COMPACTION_TOOL_CONTENT_CHAR_LIMIT:
        return message
    omitted = len(content) - COMPACTION_TOOL_CONTENT_CHAR_LIMIT
    copied = dict(message)
    copied["content"] = (
        content[:COMPACTION_TOOL_CONTENT_CHAR_LIMIT]
        + f"\n...<truncated {omitted} chars from tool output during local context compaction>"
    )
    return copied


def _drop_trailing_unpaired_tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trimmed = list(messages)
    while trimmed:
        index = _last_assistant_with_tool_calls_index(trimmed)
        if index is None:
            return trimmed
        expected = _assistant_tool_call_ids(trimmed[index])
        following = {
            message.get("tool_call_id")
            for message in trimmed[index + 1 :]
            if message.get("role") == "tool"
        }
        if expected.issubset(following):
            return trimmed
        trimmed = trimmed[:index]
    return trimmed


def _last_assistant_with_tool_calls_index(messages: list[dict[str, Any]]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "assistant" and _assistant_tool_call_ids(message):
            return index
    return None


def _assistant_tool_call_ids(message: dict[str, Any]) -> set[str]:
    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        return set()
    ids: set[str] = set()
    for tool_call in tool_calls:
        if isinstance(tool_call, dict) and isinstance(tool_call.get("id"), str):
            ids.add(tool_call["id"])
    return ids


def _snippets_for_role(messages: list[dict[str, Any]], role: str, *, limit: int) -> list[str]:
    snippets: list[str] = []
    for message in messages:
        if message.get("role") != role:
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        snippets.append(f"- {_one_line(content)}")
    return snippets[-limit:]


def _assistant_snippets(messages: list[dict[str, Any]], *, limit: int) -> list[str]:
    snippets: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        tool_calls = message.get("tool_calls") or []
        if isinstance(content, str) and content.strip():
            snippets.append(f"- {_one_line(content)}")
        elif tool_calls:
            names = []
            for tool_call in tool_calls:
                if isinstance(tool_call, dict):
                    function = tool_call.get("function") or {}
                    if isinstance(function, dict) and function.get("name"):
                        names.append(str(function["name"]))
            if names:
                snippets.append(f"- Requested tools: {', '.join(names)}")
    return snippets[-limit:]


def _tool_snippets(messages: list[dict[str, Any]], *, limit: int) -> list[str]:
    snippets: list[str] = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            snippets.append(f"- {_one_line(content)}")
    return snippets[-limit:]


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


def _initial_soft_tool_requirement(prompt: str, allowed_dirs: tuple[Path, ...]) -> SoftToolRequirement | None:
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


def _path_is_under_any(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _messages_to_summary_transcript(messages: list[dict[str, Any]], *, max_chars: int) -> str:
    lines: list[str] = []
    total = 0
    for message in messages:
        rendered = _render_summary_transcript_message(message)
        if not rendered:
            continue
        remaining = max_chars - total
        if remaining <= 0:
            break
        if len(rendered) > remaining:
            rendered = rendered[: max(0, remaining - 14)] + "...<truncated>"
        lines.append(rendered)
        total += len(rendered) + 1
    if total >= max_chars:
        lines.append("...<transcript truncated for summary request>")
    return "\n".join(lines)


def _render_summary_transcript_message(message: dict[str, Any]) -> str:
    role = str(message.get("role") or "unknown")
    if role == "assistant":
        tool_calls = message.get("tool_calls") or []
        if isinstance(tool_calls, list) and tool_calls:
            names: list[str] = []
            for tool_call in tool_calls:
                if isinstance(tool_call, dict):
                    function = tool_call.get("function") or {}
                    if isinstance(function, dict) and function.get("name"):
                        names.append(str(function["name"]))
            if names:
                return f"assistant tool_calls: {', '.join(names)}"
    if role == "tool":
        tool_call_id = message.get("tool_call_id") or "unknown"
        content = message.get("content")
        return f"tool[{tool_call_id}]: {_one_line(str(content or ''), max_chars=1200)}"
    content = message.get("content")
    if content is None:
        return ""
    return f"{role}: {_one_line(str(content), max_chars=1200)}"


def _summary_request_content(
    transcript: str,
    current_user_request: str | None,
    todo_summary: list[str],
) -> str:
    parts = ["Earlier transcript:", transcript]
    if current_user_request:
        parts.extend(["", "Current user request:", current_user_request])
    if todo_summary:
        parts.extend(["", "Open todos:", "\n".join(todo_summary)])
    parts.append(
        "\nReturn a compact summary for the next model call. "
        "Preserve constraints and completed actions; omit noise."
    )
    return "\n".join(parts)


def _format_llm_compaction_summary(
    summary: str,
    current_user_request: str | None,
    todo_summary: list[str],
) -> str:
    lines = [
        "Earlier conversation was summarized by the configured LLM to stay within the context budget.",
        "Preserve these facts while continuing the current task.",
        "",
        "Summary:",
        summary,
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
    return "\n".join(lines)


def _summary_cache_key(transcript: str, current_user_request: str | None, todo_summary: list[str]) -> str:
    payload = json.dumps(
        {
            "transcript": transcript,
            "current_user_request": current_user_request,
            "todo_summary": todo_summary,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


def _validate_runtime_tool_name(tool: str) -> str:
    normalized = tool.strip()
    if not normalized or not all(char.isalnum() or char == "_" for char in normalized):
        raise ValueError(f"invalid tool name: {tool}")
    return normalized

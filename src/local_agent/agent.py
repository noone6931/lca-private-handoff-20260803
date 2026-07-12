from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
import time
from typing import Any, Mapping

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
from .chat_runtime import call_chat_with_timeout
from .config import AgentConfig
from .config import normalize_approval_mode
from .design_evidence import FINAL_RESPONSE_RESERVE_SECONDS
from .design_evidence import cross_root_design_evidence_roots
from .delivery_report import render_delivery_report
from .evidence import EvidenceRecord
from .evidence import first_result_line_paths
from .evidence import first_search_result_paths
from .evidence import evidence_root_for_path
from .evidence import evidence_root_label
from .finalization import FINAL_ANSWER_STEERING_HARD
from .finalization import MAX_FINALIZATION_ATTEMPTS
from .llm import LlmError
from .llm import LlmTimeoutError
from .llm import OpenAICompatibleClient
from .lsp.client import close_all_clients
from .patch.anchored import display_workspace_path
from .patch.anchored import PatchError
from .patch.anchored import resolve_workspace_path
from .planner import render_planner_explore_context
from .patch_reviewer import review_input_summary
from .patch_reviewer import review_input_metadata
from .path_rules import candidate_paths_for_path_rules
from .path_rules import discover_path_scoped_rules
from .path_rules import matching_path_rule_context
from .path_rules import render_path_rule_metadata
from .requirement_evidence import render_pinned_requirement_evidence
from .run_context import RunContext
from .session_evidence import SessionEvidenceCache
from .soft_tool_requirement import advance_soft_tool_requirement
from .soft_tool_requirement import initial_soft_tool_requirement
from .soft_tool_requirement import observe_soft_tool_requirement
from .soft_tool_requirement import SoftToolRequirement
from .soft_tool_requirement import soft_tool_requirement_message
from .soft_tool_requirement import soft_tool_requirement_stop_message
from .startup_context import build_system_prompt
from .startup_context import load_sticky_rules
from .startup_context import workspace_roots_context
from .protocol.events import AgentEvent
from .protocol.events import EventEmitter
from .protocol.events import EventSink
from .protocol.events import NullEventSink
from .protocol.events import StderrEventSink
from .protocol.interactions import InteractionHandler
from .session.jsonl_store import JsonlSessionStore
from .session_guard_state import SessionGuardState
from .state import default_config_root
from .state import workspace_state_dir
from .workspace_context import WorkspaceContext
from .workspace_migration import WorkspaceMigrationError
from .workspace_migration import migrate_session_artifacts
from .workspace_migration import rollback_session_artifacts
from .steering.final_answer import FinalAnswerContext
from .steering.final_answer import FinalAnswerSteeringSeverity
from .steering.final_answer import FinalAnswerSteerer
from .steering.final_answer import FinalStructureSteerer
from .steering.final_answer import CompletionAuditSteerer
from .steering.final_answer import DesignEvidenceSteerer
from .steering.final_answer import NegativeExistenceSteerer
from .steering.final_answer import NoEditFinalHygieneSteerer
from .steering.final_answer import PatchReviewSteerer
from .steering.final_answer import ReadOnlyEvidenceSteerer
from .steering.final_answer import RequirementEvidenceSteerer
from .steering.final_answer import SourceEvidenceFalseNegativeSteerer
from .steering.final_answer import ToolUsageEvidenceSteerer
from .steering.final_answer import request_mentions_todo
from .steering.final_answer import render_unverified_final_answer
from .steering.final_answer import SourceGroundedNumericSteerer
from .steering.final_answer import SteeringDecision
from .steering.tool_loop import ToolLoopSignals
from .steering.tool_loop import ToolLoopSteeringDecision
from .steering.tool_loop import ToolLoopSteeringRegistry
from .steering.tool_loop import is_filename_search_misuse
from .steering.termination import synthetic_tool_stop_message
from .steering.termination import termination_message
from .task_contract import generate_requirement_contract
from .task_contract import render_contract_context
from .test_planner import plan_narrow_test
from .tools import create_default_registry
from .tools.base import ToolContext
from .tools.base import ToolResult
from .tools.base import tool_state_dir
from .tools.git import capture_git_baseline
from .tools.relevance import is_analysis_only_request
from .tools.relevance import is_code_implementation_request
from .tools.relevance import path_matches_any
from .tools.relevance import request_mentions_config_or_path
from .tool_choice_queue import ToolChoiceDecision
from .tool_choice_queue import ToolResultSummary
from .verification_timeline import workspace_write_happened
from .user_facts import UserFactsLayer


SYSTEM_PROMPT = """You are a local coding agent running inside a user's workspace.

Default working style:
- Work from local evidence, not guesses. Choose the tools yourself; the user should not need to spell out tool order.
- For repo understanding, use glob_files for filename, extension, and directory discovery; use list_files only to browse a nearby directory and search_code only for text inside file contents. For code navigation in Python, Java, JavaScript, TypeScript, or Vue, prefer lsp_symbols/lsp_definition/lsp_references/lsp_diagnostics before broad text search when helpful. lsp_workspace_symbols and lsp_document_symbols are compatibility aliases for lsp_symbols. Read the exact file or range before editing it.
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
- Path-scoped rules are advisory project guidance. Their metadata may be visible for every request, but their bodies apply only after a relevant path is mentioned or inspected. They never grant tool permissions.
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
STARTUP_MEMORY_CHAR_LIMIT = 8000
STARTUP_CONTEXT_CHAR_LIMIT = 8000
STICKY_RULES_CHAR_LIMIT = 4000
CURRENT_TASK_CONTRACT_CHAR_LIMIT = 2000
STARTUP_SKILLS_CHAR_LIMIT = 4000
MAX_AUTHORED_SKILLS = 40
MAX_SKILL_DESCRIPTION_CHARS = 320
MAX_READ_FILE_SUCCESSES_PER_RANGE_IN_RUN = 3
MAX_NO_EDIT_FINAL_HYGIENE_STEERS = 2
MAX_FINAL_STRUCTURE_STEERS = 2
MAX_READ_ONLY_EVIDENCE_STEERS = 2
MAX_REQUIREMENT_EVIDENCE_STEERS = 2
MAX_DESIGN_EVIDENCE_STEERS = 2
MAX_SOURCE_EVIDENCE_FALSE_NEGATIVE_STEERS = 2
MAX_TOOL_USAGE_EVIDENCE_STEERS = 2
MAX_NEGATIVE_EXISTENCE_STEERS = 2
MAX_SOURCE_GROUNDED_NUMERIC_STEERS = 2
MAX_COMPLETION_AUDIT_STEERS = 2
MAX_PATCH_REVIEW_STEERS = 2
MAX_TOOL_CHOICE_QUEUE_STEERS_PER_SIGNATURE = 1
MAX_SESSION_EVIDENCE_TAGGED_PATHS = 32
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
class AgentRuntime:
    def __init__(
        self,
        config: AgentConfig,
        *,
        show_tool_logs: bool = True,
        session_id: str | None = None,
        continue_session: bool = False,
        event_sink: EventSink | None = None,
        interaction_handler: InteractionHandler | None = None,
    ):
        self._config = config
        self._workspace_context = WorkspaceContext(config.workspace, config.allowed_dirs)
        self._is_running = False
        self._client = OpenAICompatibleClient(config)
        self._registry = create_default_registry()
        self._session_tool_approval: dict[str, str] = {}
        self._summary_cache: dict[str, str] = {}
        self._session_guards = SessionGuardState()
        self._session_evidence = SessionEvidenceCache()
        self._user_facts = UserFactsLayer()
        self._run = RunContext()
        self._last_run_summary: dict[str, Any] | None = None
        self._final_answer_steerers: tuple[FinalAnswerSteerer, ...] = (
            ReadOnlyEvidenceSteerer(max_steers=MAX_READ_ONLY_EVIDENCE_STEERS),
            RequirementEvidenceSteerer(max_steers=MAX_REQUIREMENT_EVIDENCE_STEERS),
            DesignEvidenceSteerer(max_steers=MAX_DESIGN_EVIDENCE_STEERS),
            NoEditFinalHygieneSteerer(max_steers=MAX_NO_EDIT_FINAL_HYGIENE_STEERS),
            FinalStructureSteerer(max_steers=MAX_FINAL_STRUCTURE_STEERS),
            SourceEvidenceFalseNegativeSteerer(max_steers=MAX_SOURCE_EVIDENCE_FALSE_NEGATIVE_STEERS),
            ToolUsageEvidenceSteerer(max_steers=MAX_TOOL_USAGE_EVIDENCE_STEERS),
            NegativeExistenceSteerer(max_steers=MAX_NEGATIVE_EXISTENCE_STEERS),
            SourceGroundedNumericSteerer(max_steers=MAX_SOURCE_GROUNDED_NUMERIC_STEERS),
            PatchReviewSteerer(max_steers=MAX_PATCH_REVIEW_STEERS),
            CompletionAuditSteerer(max_steers=MAX_COMPLETION_AUDIT_STEERS),
        )
        self._state_dir = config.state_dir or config.workspace / ".local-agent"
        self._state_root = config.state_root
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
        missing_roots = self._restore_session_workspace_roots()
        self._path_rule_index = discover_path_scoped_rules(self._workspace_context.all_roots)
        system_prompt = self._build_system_prompt()
        self._messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *self._session.load_messages(),
        ]
        self._tool_context = ToolContext(
            workspace=config.workspace,
            approval_mode=config.approval_mode,
            state_dir=self._state_dir,
            allowed_dirs=self._workspace_context.additional_roots,
            session_id=self._session.session_id,
            auto_approve_tools=config.auto_approve_tools,
            tool_approval=config.tool_approval,
            session_tool_approval=self._session_tool_approval,
            event_callback=self._emit_event,
            interaction_handler=interaction_handler,
        )
        self._events.emit(
            "SessionStarted",
            {
                "workspace": str(config.workspace),
                "workspace_roots_revision": self._workspace_context.revision,
                "additional_roots": [str(path) for path in self._workspace_context.additional_roots],
                "state_dir": str(self._state_dir),
                "provider": config.provider,
                "continued": bool(continue_session or session_id),
            },
        )
        for path in missing_roots:
            self._events.emit(
                "ErrorEvent",
                {
                    "kind": "workspace_root_restore",
                    "message": f"Skipped missing session workspace root: {path}",
                },
            )

    def run(self, prompt: str) -> str:
        if self._is_running:
            raise RuntimeError("Cannot start a new run while the current run is still active.")
        self._is_running = True
        try:
            return self._run_prompt(prompt)
        finally:
            self._is_running = False

    def set_interaction_handler(self, handler: InteractionHandler | None) -> None:
        """Attach a frontend-owned interaction channel while the Runtime is idle."""

        if self._is_running:
            raise RuntimeError("Cannot replace the interaction handler while the current run is active.")
        self._tool_context = replace(self._tool_context, interaction_handler=handler)

    def _run_prompt(self, prompt: str) -> str:
        run_id = self._events.start_run()
        started_monotonic = time.monotonic()
        deadline = (
            started_monotonic + self._config.budget_seconds
            if self._config.budget_seconds is not None
            else None
        )
        git_baseline = capture_git_baseline(self._workspace_context.primary)
        self._session.append("git_baseline", git_baseline)
        run_start_index = len(self._messages)
        model_prompt = _with_workflow_nudge(prompt)
        requirement_contract = generate_requirement_contract(prompt)
        requirement_contract_context = render_contract_context(requirement_contract)
        design_evidence_roots = (
            cross_root_design_evidence_roots(self._workspace_context.primary, self._workspace_context.additional_roots, prompt)
            if requirement_contract.task_kind == "read-only"
            else ()
        )
        self._run.begin(
            run_id=run_id,
            started_monotonic=started_monotonic,
            deadline_monotonic=deadline,
            run_start_index=run_start_index,
            git_baseline=git_baseline,
            prompt=prompt,
            requirement_contract=requirement_contract,
            requirement_contract_context=requirement_contract_context,
            design_evidence_roots=design_evidence_roots,
        )
        self._start_run_collector(run_id, prompt, started_monotonic)
        self._user_facts.begin_run(prompt, run_id)
        self._run.user_facts_context = self._user_facts.render_for(prompt)
        self._hydrate_session_evidence(prompt)
        self._record_verification_plan_snapshot("snapshot")
        self._messages.append({"role": "user", "content": model_prompt})
        self._session.append("user", {"content": prompt})
        self._events.emit("UserMessage", {"content": prompt})
        self._session.append(
            "runtime_steering",
            {
                "kind": "requirement_contract",
                "task_kind": self._run.requirement_contract.task_kind,
                "objective": self._run.requirement_contract.objective,
            },
        )
        if self._run.design_evidence_coverage.roots:
            self._session.append(
                "runtime_steering",
                {"kind": "design_evidence_roots", "roots": list(self._run.design_evidence_coverage.roots)},
            )
        if model_prompt != prompt:
            self._session.append("workflow_nudge", {"content": WORKFLOW_NUDGE})
        self._run.read_file_drift_guard_enabled = _should_guard_repeated_read_file(prompt)
        self._run.soft_tool_requirement = initial_soft_tool_requirement(
            prompt,
            self._workspace_context.primary,
            self._workspace_context.additional_roots,
            max_skill_description_chars=MAX_SKILL_DESCRIPTION_CHARS,
        )
        if self._run.soft_tool_requirement is not None:
            self._append_soft_tool_requirement_message(self._run.soft_tool_requirement)
        self._record_workspace_root_evidence()
        tool_context = replace(
            self._tool_context,
            deadline_monotonic=deadline,
            git_baseline=git_baseline,
            current_user_request=prompt,
            patch_relevance_checker=self._patch_relevance_denial_reason,
            patch_preview_checker=self._patch_preview_denial_reason,
        )

        step = 1
        while self._config.max_steps == 0 or step <= self._config.max_steps:
            if self._deadline_exceeded(deadline):
                return self._stop_for_budget(deadline, run_start_index)

            tool_choice_stop_message = self._apply_tool_choice_queue_if_needed(deadline)
            if tool_choice_stop_message is not None:
                return self._finish_run(
                    tool_choice_stop_message,
                    deadline,
                    run_start_index,
                    reason="tool_choice_queue",
                )
            messages_for_model = self._messages_for_model(deadline)
            tools_for_model = self._tools_for_model()
            tool_schema_names = [
                str(schema.get("function", {}).get("name") or "")
                for schema in tools_for_model
                if isinstance(schema, Mapping)
            ]
            self._record_llm_request()
            self._session.append(
                "llm_request",
                {"step": step, "tool_schema_names": tool_schema_names},
            )
            self._events.emit(
                "LlmRequest",
                {
                    "step": step,
                    "message_count": len(messages_for_model),
                    "tool_schema_count": len(tools_for_model),
                    "tool_schema_names": tool_schema_names,
                    "force_final_answer": self._run.force_final_answer_without_tools,
                },
            )
            force_final_answer = self._run.finalization.begin_forced_final_turn()
            try:
                response = call_chat_with_timeout(
                    self._client,
                    messages_for_model,
                    tools_for_model,
                    timeout=self._remaining_timeout(deadline),
                )
            except LlmError as exc:
                fallback = self._forced_final_timeout_fallback(
                    force_final_answer,
                    exc,
                    deadline,
                    run_start_index,
                )
                if fallback is not None:
                    return fallback
                return self._stop_for_provider_failure(exc, deadline, run_start_index)
            if force_final_answer:
                self._session.append("runtime_steering", {"kind": "forced_final_answer", "step": step})
                self._run.clear_forced_final_answer_request()
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
                    if self._apply_final_answer_steering(steering):
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
                guard_hits_before = self._session_guards.counts()
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
                    metadata={
                        **dict(result.metadata),
                        "filename_search_misuse": is_filename_search_misuse(name, arguments),
                    },
                )
                self._run.reset_forced_final_answer_continuations()
                self._record_tool_choice_result(name, arguments, result)
                self._record_successful_patch_preview(name, arguments, result)
                self._record_read_file_evidence(name, arguments, result)
                self._record_tool_evidence(name, arguments, result)
                self._invalidate_stale_source_evidence_after_write(name, arguments, result)
                self._observe_soft_tool_requirement(name, arguments, result)
                if name == "git_diff" and not result.is_error:
                    patch_review = self._decide_post_diff_patch_review(run_start_index)
                    self._record_verification_patch_review(patch_review)
                    if patch_review is not None:
                        self._apply_final_answer_steering(patch_review)
                        self._append_synthetic_tool_results(
                            tool_calls[index + 1 :],
                            "Skipped because runtime patch review requires correction or verification before further work.",
                        )
                        break
                guard_hits = self._session_guards.counts()
                tool_loop_steering_signals = ToolLoopSignals(
                    duplicate_skipped=guard_hits["duplicate_tool"] > guard_hits_before["duplicate_tool"],
                    duplicate_tool_name=name,
                    duplicate_guard_hits=guard_hits["duplicate_tool"],
                    useless_search_skipped=guard_hits["useless_search_pattern"] > guard_hits_before["useless_search_pattern"],
                    useless_search_guard_hits=guard_hits["useless_search_pattern"],
                    useless_lsp_skipped=guard_hits["useless_lsp_symbol"] > guard_hits_before["useless_lsp_symbol"],
                    useless_lsp_guard_hits=guard_hits["useless_lsp_symbol"],
                    repeated_read_skipped=guard_hits["repeated_read_file"] > guard_hits_before["repeated_read_file"],
                    repeated_read_guard_hits=guard_hits["repeated_read_file"],
                    semantic_exploration_skipped=guard_hits["semantic_exploration"] > guard_hits_before["semantic_exploration"],
                    semantic_exploration_guard_hits=guard_hits["semantic_exploration"],
                    read_file_evidence=self._read_file_evidence_summary(),
                    request_summary=self._final_answer_request_summary(),
                )
                termination_reason = self._run.tool_loop_steering.termination_reason(tool_loop_steering_signals)
                if termination_reason is not None:
                    self._append_synthetic_tool_results(
                        tool_calls[index + 1 :],
                        synthetic_tool_stop_message(termination_reason),
                    )
                    return self._finish_run(
                        termination_message(termination_reason),
                        deadline,
                        run_start_index,
                        reason=termination_reason,
                    )
                tool_loop_steering = self._run.tool_loop_steering.decide(tool_loop_steering_signals)
                if tool_loop_steering is not None:
                    self._apply_tool_loop_steering(tool_loop_steering)
                    self._append_synthetic_tool_results(
                        tool_calls[index + 1 :],
                        self._tool_loop_stop_message(tool_loop_steering.kind),
                    )
                    break
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
        allowed_names = self._effective_runtime_tool_allowlist()
        if allowed_names == set():
            return []
        denied_names = self._denied_model_tool_names()
        if allowed_names is None:
            return [
                schema
                for schema in self._registry.schemas()
                if schema.get("function", {}).get("name") not in denied_names
            ]
        return [
            schema
            for schema in self._registry.schemas()
            if schema.get("function", {}).get("name") in allowed_names
            and schema.get("function", {}).get("name") not in denied_names
        ]

    def _denied_model_tool_names(self) -> set[str]:
        denied = {
            name
            for name, policy in (self._tool_context.tool_approval or {}).items()
            if policy == "deny"
        }
        denied.update(
            name
            for name, policy in self._session_tool_approval.items()
            if policy == "reject_always"
        )
        return denied

    def _effective_runtime_tool_allowlist(self) -> set[str] | None:
        if self._run.force_final_answer_without_tools:
            return set()
        allowed_names: set[str] | None = None
        if self._run.temporary_tool_allowlist is not None:
            allowed_names = set(self._run.temporary_tool_allowlist)
        requirement = self._run.soft_tool_requirement
        if requirement is not None and not requirement.satisfied:
            allowed_names = _intersect_optional_tool_allowlist(allowed_names, {"list_files", "read_file"})
        if self._run.tool_choice_allowed_tool_names is not None:
            allowed_names = _intersect_optional_tool_allowlist(allowed_names, self._run.tool_choice_allowed_tool_names)
        return allowed_names

    def _apply_tool_choice_queue_if_needed(self, deadline: float | None = None) -> str | None:
        contract = self._run.requirement_contract
        if contract is None:
            self._run.tool_choice_allowed_tool_names = None
            return None
        decision = self._run.tool_choice_queue.evaluate(
            task_kind=contract.task_kind,
            prompt=self._run.current_user_request or "",
            tool_names=self._run.tool_choice_tool_names,
            tool_results=self._run.tool_choice_results,
            available_tool_names=self._available_registry_tool_names(),
            design_evidence_roots=self._run.design_evidence_coverage.roots,
            workspace_roots=tuple(str(root) for root in self._workspace_context.all_roots),
        )
        self._run.tool_choice_allowed_tool_names = set(decision.allowed_tool_names)
        self._run.update_tool_choice_read_scope(decision.scoped_read_paths, decision.scoped_read_budget)
        self._run.tool_choice_required_glob_roots = set(decision.required_glob_roots) or None
        if decision.force_final_answer_without_tools:
            content = _tool_choice_steering_message(decision, self._run.current_user_request)
            self._messages.append({"role": "user", "content": content})
            self._session.append(
                "runtime_steering",
                {
                    "kind": "tool_choice_queue",
                    "rule_id": decision.rule_id,
                    "missing_requirements": list(decision.missing_requirements),
                    "allowed_tool_names": [],
                    "reason": decision.reason,
                    "force_final_answer_without_tools": True,
                },
            )
            if not self._queue_forced_final_answer(kind=decision.rule_id or "tool_choice_queue"):
                self._run.block_unverified_final_answer(
                    kind=decision.rule_id or "tool_choice_queue",
                    reason=self._final_answer_rewrite_skip_reason() or "continuation_limit",
                )
            return None
        if decision.should_stop:
            self._session.append(
                "runtime_steering",
                {
                    "kind": "tool_choice_queue",
                    "rule_id": decision.rule_id,
                    "reason": decision.reason,
                    "stop_message": decision.stop_message,
                },
            )
            return decision.stop_message
        coverage = self._run.design_evidence_coverage.observe(
            queue_requires_steering=decision.steering_required,
            read_paths=(
                result.path
                for result in self._run.tool_choice_results
                if result.name == "read_file" and not result.is_error
            ),
            tool_count=len(self._run.tool_choice_results),
            deadline=deadline,
            request_summary=self._final_answer_request_summary(),
        )
        if coverage is not None:
            for kind, payload in coverage.preceding_events:
                self._session.append("runtime_steering", {"kind": kind, **payload})
            self._session.append(
                "runtime_steering",
                {"kind": coverage.kind, **coverage.payload},
            )
            if coverage.message is not None:
                self._messages.append({"role": "user", "content": coverage.message})
                if coverage.force_final_answer_without_tools:
                    if self._queue_forced_final_answer(kind=coverage.kind):
                        self._run.force_final_answer_without_tools = True
                    else:
                        self._session.append(
                            "runtime_steering",
                            {
                                "kind": "forced_final_answer_skipped",
                                "source": coverage.kind,
                                "reason": "continuation_limit",
                            },
                        )
                        self._run.force_final_answer_without_tools = False
                else:
                    self._run.force_final_answer_without_tools = False
                self._run.temporary_tool_allowlist = None
                return None
        if self._run.force_final_answer_without_tools:
            return None
        if not decision.steering_required:
            return None
        signature = _tool_choice_steering_signature(decision, len(self._run.tool_choice_results))
        if signature in self._run.tool_choice_steering_signatures:
            return None
        if _tool_choice_signature_count(self._run.tool_choice_steering_signatures, decision.rule_id) >= (
            MAX_TOOL_CHOICE_QUEUE_STEERS_PER_SIGNATURE
        ):
            return None
        self._run.tool_choice_steering_signatures.add(signature)
        content = _tool_choice_steering_message(decision, self._run.current_user_request)
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
        return None

    def _available_registry_tool_names(self) -> tuple[str, ...]:
        denied_names = self._denied_model_tool_names()
        if hasattr(self._registry, "tool_names"):
            return tuple(name for name in self._registry.tool_names() if name not in denied_names)
        names: list[str] = []
        for schema in self._registry.schemas():
            name = schema.get("function", {}).get("name")
            if isinstance(name, str) and name and name not in denied_names:
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
            f"- workspace: {self._workspace_context.primary}",
            f"- state_dir: {self._state_dir}",
            f"- provider: {self._config.provider}",
            f"- model: {self._config.model}",
            f"- approval_mode: {self._tool_context.approval_mode}",
            f"- budget_seconds: {_display_optional_int(self._config.budget_seconds)}",
            f"- max_steps: {self._config.max_steps}",
            f"- summary_mode: {self._config.summary_mode}",
            f"- memory_consolidation: {self._config.memory_consolidation}",
        ]
        if self._workspace_context.additional_roots:
            lines.append("- allowed_dirs:")
            lines.extend(f"  - {path}" for path in self._workspace_context.additional_roots)
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

    def workspace_summary(self) -> str:
        return self._workspace_context.summary()

    def add_workspace_root(self, raw_path: str) -> Path:
        self._ensure_workspace_idle()
        next_context = self._workspace_context.copy()
        path, changed = next_context.add_session_root(raw_path)
        self._record_workspace_roots_change(next_context, "add", path, changed)
        return path

    def remove_workspace_root(self, raw_path: str) -> Path:
        self._ensure_workspace_idle()
        next_context = self._workspace_context.copy()
        path, changed = next_context.remove_session_root(raw_path)
        self._record_workspace_roots_change(next_context, "remove", path, changed)
        return path

    def reset_workspace_roots(self) -> None:
        self._ensure_workspace_idle()
        next_context = self._workspace_context.copy()
        changed = next_context.reset_session_roots()
        self._record_workspace_roots_change(next_context, "reset", None, changed)

    def move_workspace(self, raw_path: str) -> Path:
        """Move this session's primary workspace without losing its session identity."""

        self._ensure_workspace_idle()
        next_context, changed = self._workspace_context.moved_primary(raw_path)
        if not changed:
            return self._workspace_context.primary
        if self._state_root is None:
            raise RuntimeError(
                "Cannot move a runtime without a workspace-partitioned state root. Restart with --state-dir first."
            )

        previous_primary = self._workspace_context.primary
        next_state_dir = workspace_state_dir(self._state_root, next_context.primary)
        # Loading startup sources is intentionally done before moving any artifact.
        # A read failure must not leave the session split across two state dirs.
        try:
            next_system_prompt = self._build_system_prompt_for(next_context, next_state_dir)
            next_path_rule_index = discover_path_scoped_rules(next_context.all_roots)
            previous_session_bytes = self._session.path.read_bytes()
        except OSError as exc:
            raise WorkspaceMigrationError(f"Cannot prepare workspace move: {exc}") from exc

        previous_workspace_context = self._workspace_context
        previous_state_dir = self._state_dir
        previous_tool_context = self._tool_context
        previous_messages = self._messages
        previous_run = self._run
        previous_session_guards = self._session_guards
        previous_summary_cache = self._summary_cache
        previous_last_run_summary = self._last_run_summary
        previous_path_rule_index = self._path_rule_index
        previous_session_location = (
            self._session.state_dir,
            self._session.session_dir,
            self._session.path,
        )
        next_tool_context = replace(
            self._tool_context,
            workspace=next_context.primary,
            state_dir=next_state_dir,
            allowed_dirs=next_context.additional_roots,
        )
        next_messages = [
            {"role": "system", "content": next_system_prompt},
            *(message for message in self._messages if message.get("role") != "system"),
        ]
        payload = next_context.snapshot(operation="move", path=next_context.primary, changed=True)
        payload.update(
            {
                "previous_primary": str(previous_primary),
                "state_dir": str(next_state_dir),
            }
        )
        moves = migrate_session_artifacts(
            source_state_dir=self._state_dir,
            target_state_dir=next_state_dir,
            session_id=self._session.session_id,
        )

        try:
            self._session.relocate(next_state_dir)
            self._workspace_context = next_context
            self._state_dir = next_state_dir
            self._tool_context = next_tool_context
            self._messages = next_messages
            self._run = RunContext()
            self._session_guards = SessionGuardState()
            self._summary_cache = {}
            self._last_run_summary = None
            self._path_rule_index = next_path_rule_index
            close_all_clients()
            self._session.append("workspace_moved", payload)
        except Exception as exc:  # noqa: BLE001 - every post-migration commit must compensate.
            self._workspace_context = previous_workspace_context
            self._state_dir = previous_state_dir
            self._tool_context = previous_tool_context
            self._messages = previous_messages
            self._run = previous_run
            self._session_guards = previous_session_guards
            self._summary_cache = previous_summary_cache
            self._last_run_summary = previous_last_run_summary
            self._path_rule_index = previous_path_rule_index
            rollback_error: Exception | None = None
            try:
                rollback_session_artifacts(moves)
                previous_session_location[2].write_bytes(previous_session_bytes)
            except Exception as rollback_exc:  # noqa: BLE001 - include compensation failure in the raised error.
                rollback_error = rollback_exc
            finally:
                # JsonlSessionStore.relocate() only assigns these three fields. Restore
                # them directly so a failing relocate mock cannot strand Runtime state.
                self._session.state_dir = previous_session_location[0]
                self._session.session_dir = previous_session_location[1]
                self._session.path = previous_session_location[2]
            detail = f"; rollback failed: {rollback_error}" if rollback_error is not None else ""
            raise WorkspaceMigrationError(f"Workspace move failed and was rolled back: {exc}{detail}") from exc
        self._invalidate_session_evidence_for_workspace_change("workspace_moved")
        self._emit_post_commit_event("WorkspaceMoved", payload)
        return next_context.primary

    def _ensure_workspace_idle(self) -> None:
        if self._is_running:
            raise RuntimeError("Workspace roots can only be changed while the runtime is idle.")

    def _record_workspace_roots_change(
        self,
        next_context: WorkspaceContext,
        operation: str,
        path: Path | None,
        changed: bool,
    ) -> None:
        if not changed:
            return
        payload = next_context.snapshot(operation=operation, path=path, changed=True)
        previous_session_bytes = self._session.path.read_bytes()
        try:
            self._session.append("workspace_roots_changed", payload)
        except Exception as exc:  # noqa: BLE001 - append can fail after partially writing JSONL.
            rollback_error: Exception | None = None
            try:
                self._session.path.write_bytes(previous_session_bytes)
            except Exception as rollback_exc:  # noqa: BLE001 - expose failed compensation to the caller.
                rollback_error = rollback_exc
            detail = f"; session rollback failed: {rollback_error}" if rollback_error is not None else ""
            raise RuntimeError(f"Workspace root change failed and was rolled back: {exc}{detail}") from exc

        self._workspace_context = next_context
        self._tool_context = replace(
            self._tool_context,
            allowed_dirs=next_context.additional_roots,
        )
        self._session_guards = SessionGuardState()
        self._summary_cache = {}
        self._invalidate_session_evidence_for_workspace_change("workspace_roots_changed")
        self._refresh_path_rules()
        self._emit_post_commit_event("WorkspaceRootsChanged", payload)

    def _refresh_path_rules(self) -> None:
        self._path_rule_index = discover_path_scoped_rules(self._workspace_context.all_roots)

    def _invalidate_session_evidence_for_workspace_change(self, reason: str) -> None:
        removed = self._session_evidence.invalidate_workspace_revision()
        if removed:
            self._record_session_evidence_event("invalidated", {"reason": reason, "count": removed})

    def _emit_post_commit_event(self, event_type: str, payload: dict[str, object]) -> None:
        """Notify an external sink without turning a committed workspace change into a rollback."""

        try:
            self._events.emit(event_type, payload)
        except Exception as exc:  # noqa: BLE001 - sinks are observer-only after commit.
            try:
                self._session.append(
                    "event_delivery_error",
                    {
                        "event_type": event_type,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            except Exception:
                pass

    def _restore_session_workspace_roots(self) -> tuple[Path, ...]:
        snapshot = self._session.load_latest_workspace_roots()
        if snapshot is None:
            return ()
        primary = snapshot.get("primary")
        if primary is not None and str(primary) != str(self._workspace_context.primary):
            return ()
        raw_paths = snapshot.get("session_roots")
        if not isinstance(raw_paths, list) or not all(isinstance(path, str) for path in raw_paths):
            return ()
        revision = snapshot.get("revision")
        try:
            normalized_revision = int(revision) if revision is not None else 0
        except (TypeError, ValueError):
            normalized_revision = 0
        return self._workspace_context.restore_session_roots(tuple(raw_paths), normalized_revision)

    def _build_system_prompt(self) -> str:
        return self._build_system_prompt_for(self._workspace_context, self._state_dir)

    def _build_system_prompt_for(self, workspace_context: WorkspaceContext, state_dir: Path) -> str:
        return build_system_prompt(
            SYSTEM_PROMPT,
            workspace_context.primary,
            self._user_config_dir,
            state_dir=state_dir,
            allowed_dirs=workspace_context.additional_roots,
            startup_context_char_limit=STARTUP_CONTEXT_CHAR_LIMIT,
            startup_memory_char_limit=STARTUP_MEMORY_CHAR_LIMIT,
            startup_skills_char_limit=STARTUP_SKILLS_CHAR_LIMIT,
            max_authored_skills=MAX_AUTHORED_SKILLS,
            max_skill_description_chars=MAX_SKILL_DESCRIPTION_CHARS,
        )

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

    def _start_run_collector(self, run_id: str, prompt: str, started_monotonic: float) -> None:
        self._run.collector.start(
            run_id,
            prompt,
            started_monotonic,
            guard_start=self._session_guards.counts(),
            steer_start={
                "duplicate_tool_final_answer": self._run.tool_loop_steering.count("duplicate_tool_final_answer"),
                "useless_search_pattern_final_answer": self._run.tool_loop_steering.count("useless_search_pattern_final_answer"),
                "useless_lsp_symbol_final_answer": self._run.tool_loop_steering.count("useless_lsp_symbol_final_answer"),
                "repeated_read_file_final_answer": self._run.tool_loop_steering.count("repeated_read_file_final_answer"),
                "semantic_exploration": self._run.tool_loop_steering.count("semantic_exploration"),
            },
        )

    def _record_llm_request(self) -> None:
        self._run.collector.record_llm_request()

    def _record_context_compaction(
        self,
        *,
        estimated_tokens_before: int,
        estimated_tokens_after: int,
    ) -> None:
        self._run.collector.record_context_compaction(
            estimated_tokens_before=estimated_tokens_before,
            estimated_tokens_after=estimated_tokens_after,
        )

    def _record_llm_context_summary(self) -> None:
        self._run.collector.mark_llm_context_summary()

    def _record_local_context_summary(self) -> None:
        self._run.collector.mark_local_context_summary()

    def _record_tool_started_for_run(self, name: str) -> None:
        self._run.collector.record_tool_started(name)

    def _record_tool_finished_for_run(self, *, is_error: bool) -> None:
        self._run.collector.record_tool_finished(is_error=is_error)

    def _record_tool_result_for_run(
        self,
        *,
        name: str,
        is_error: bool,
        useless: bool,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._run.collector.record_tool_result(
            name=name,
            is_error=is_error,
            useless=useless,
            metadata=metadata or {},
        )

    def _record_synthetic_tool_result_for_run(self) -> None:
        self._run.collector.record_synthetic_tool_result()

    def _finish_run_summary(self, reason: str) -> dict[str, Any]:
        payload = self._run.collector.finish(
            reason,
            guard_values=self._session_guards.counts(),
            steering_values={
                "duplicate_tool_final_answer": self._run.tool_loop_steering.count("duplicate_tool_final_answer"),
                "useless_search_pattern_final_answer": self._run.tool_loop_steering.count("useless_search_pattern_final_answer"),
                "useless_lsp_symbol_final_answer": self._run.tool_loop_steering.count("useless_lsp_symbol_final_answer"),
                "repeated_read_file_final_answer": self._run.tool_loop_steering.count("repeated_read_file_final_answer"),
                "semantic_exploration": self._run.tool_loop_steering.count("semantic_exploration"),
                "no_edit_final_hygiene": self._run.final_answer_steers.get("no_edit_final_hygiene", 0),
                "final_structure": self._run.final_answer_steers.get("final_structure", 0),
                "read_only_evidence": self._run.final_answer_steers.get("read_only_evidence", 0),
                "source_evidence_false_negative": self._run.final_answer_steers.get("source_evidence_false_negative", 0),
                "tool_usage_evidence": self._run.final_answer_steers.get("tool_usage_evidence", 0),
                "source_grounded_numeric": self._run.final_answer_steers.get("source_grounded_numeric", 0),
                "patch_reviewer": self._run.final_answer_steers.get("patch_reviewer", 0),
                "completion_audit": self._run.final_answer_steers.get("completion_audit", 0),
                "soft_tool_requirement": self._run.soft_tool_requirement.steers if self._run.soft_tool_requirement else 0,
            },
        )
        payload["finalization_attempts"] = self._run.finalization.aggregate_attempts
        if self._run.verification_plan.active:
            payload["verification_plan"] = self._run.verification_plan.coverage(delivery_only=True)
            payload["business_acceptance"] = self._run.verification_plan.business_acceptance_summary()
            if self._run.verification_test_plan is not None:
                payload["test_plan"] = self._run.verification_test_plan.snapshot()
        payload["session_evidence"] = {
            **dict(payload.get("session_evidence") or {}),
            "cache_entries": self._session_evidence.snapshot().get("entries", 0),
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

        estimated_tokens_before = _estimate_message_tokens(provider_context)

        system_messages = [message for message in provider_context if message.get("role") == "system"]
        non_system = [message for message in provider_context if message.get("role") != "system"]
        recent_count = min(self._config.context_recent_messages, len(non_system))
        current_user_request = _latest_user_content(non_system)

        while recent_count > 0:
            recent = _truncate_recent_tool_outputs(_valid_recent_messages(non_system[-recent_count:]))
            dropped_count = len(non_system) - recent_count
            dropped = non_system[: max(dropped_count, 0)]
            compaction_summary = self._build_compaction_summary(
                dropped,
                current_user_request,
                deadline,
                prefer_local=self._run.force_final_answer_without_tools,
            )
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
                    "estimated_tokens_before": estimated_tokens_before,
                    "estimated_tokens_after": _estimate_message_tokens(compacted),
                }
                payload["estimated_tokens"] = payload["estimated_tokens_after"]
                payload.update(thresholds)
                self._session.append("context_compaction", payload)
                self._record_context_compaction(
                    estimated_tokens_before=estimated_tokens_before,
                    estimated_tokens_after=int(payload["estimated_tokens_after"]),
                )
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
            self._run.requirement_contract,
            prompt=self._run.current_user_request,
            tool_results=list(self._run.tool_choice_results),
        )
        verification_plan_context = self._run.verification_plan.render_context()
        if self._run.verification_test_plan is not None:
            test_plan = self._run.verification_test_plan
            verification_plan_context = (
                verification_plan_context
                + f"\n- Test candidate ({test_plan.breadth}): {test_plan.command or '(none)'} ({test_plan.reason})"
            )
        path_rule_candidates = candidate_paths_for_path_rules(
            self._run.current_user_request or "",
            (result.path for result in self._run.tool_choice_results if result.path),
            primary_workspace=self._workspace_context.primary,
        )
        path_rule_metadata = render_path_rule_metadata(self._path_rule_index)
        matched_path_rules = matching_path_rule_context(self._path_rule_index, path_rule_candidates)
        return _provider_safe_messages(
            _messages_with_runtime_context(
                messages,
                todo_summary,
                evidence_ledger,
                planner_explore_context,
                self._workspace_context.primary,
                self._user_config_dir,
                self._workspace_context.additional_roots,
                self._run.current_user_request,
                self._run.requirement_contract_context,
                render_pinned_requirement_evidence(self._run.evidence.pinned_requirement_evidence),
                self._run.user_facts_context,
                path_rule_metadata,
                matched_path_rules,
                verification_plan_context,
            )
        )

    def _build_compaction_summary(
        self,
        dropped: list[dict[str, Any]],
        current_user_request: str | None,
        deadline: float | None,
        *,
        prefer_local: bool = False,
    ) -> str:
        todo_summary = self._open_todo_summary()
        if not prefer_local and self._config.summary_mode in {"auto", "llm"}:
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
                    "Current user request remains in the user-role conversation history.",
                    "- After completing explicitly requested tool calls, answer the requested final response instead of exploring further unless more information is truly necessary.",
                ]
            )
        if todo_summary:
            lines.extend(["", "Open todos:", *todo_summary])
        user_items = _snippets_for_role(dropped, "user", limit=6)
        if user_items:
            lines.extend(["", f"Earlier user messages compacted: {len(user_items)} (kept as user-role context, not copied here)."])
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
            response = call_chat_with_timeout(self._client, messages, [], timeout=timeout)
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

    def _forced_final_timeout_fallback(
        self,
        forced_final_answer: bool,
        error: LlmError,
        deadline: float | None,
        run_start_index: int,
    ) -> str | None:
        if not forced_final_answer:
            return None
        kind = self._run.forced_final_answer_kind
        if not self._run.allows_forced_final_draft_fallback():
            self._session.append(
                "runtime_steering",
                {
                    "kind": "forced_final_timeout_unverified",
                    "steering_kind": kind,
                    "error": str(error),
                },
            )
            return self._finish_run(
                render_unverified_final_answer(kind, "rewrite_timeout"),
                deadline,
                run_start_index,
                reason="forced_final_timeout_unverified",
            )
        draft = _most_recent_terminal_assistant_content(self._messages[run_start_index:])
        if not draft:
            return None
        self._session.append(
            "runtime_steering",
            {"kind": "forced_final_timeout_fallback", "steering_kind": kind, "error": str(error)},
        )
        content = (
            f"{draft}\n\n"
            "注：最终的格式/证据校验重写请求超时，已返回上一版基于已读取证据的答复；未继续调用工具。"
        )
        return self._finish_run(content, deadline, run_start_index, reason="forced_final_timeout_fallback")

    def _stop_for_provider_failure(
        self,
        error: LlmError,
        deadline: float | None,
        run_start_index: int,
    ) -> str:
        reason = _llm_failure_reason(error)
        self._session.append(
            "runtime_error",
            {"kind": reason, "error": str(error)},
        )
        self._events.emit("ErrorEvent", {"kind": reason, "message": str(error)})
        if reason == "llm_timeout":
            content = (
                "未完成：本次模型请求超时且未返回响应，任务已停止。"
                "失败后未继续请求模型或执行后续步骤；此前动作以本轮 tool timeline 和 diff 为准。"
                "请检查 provider 连通性后重试。"
            )
        else:
            content = (
                "未完成：本次模型 provider 请求失败且未返回响应，任务已停止。"
                "失败后未继续请求模型或执行后续步骤；此前动作以本轮 tool timeline 和 diff 为准。"
                "请检查 provider 配置或稍后重试。"
            )
        return self._finish_run(
            content,
            deadline,
            run_start_index,
            reason=reason,
            skip_memory_consolidation=True,
        )

    def _execute_tool_with_repeat_guard(
        self,
        name: str,
        arguments: str | dict[str, Any],
        tool_context: ToolContext,
    ) -> ToolResult:
        allowed_tools = self._effective_runtime_tool_allowlist()
        scoped_read_paths = self._effective_runtime_read_file_paths()
        required_glob_roots = self._run.tool_choice_required_glob_roots
        if allowed_tools is not None or scoped_read_paths is not None or required_glob_roots is not None:
            tool_context = replace(
                tool_context,
                runtime_tool_allowlist=frozenset(allowed_tools) if allowed_tools is not None else None,
                runtime_read_file_paths=scoped_read_paths,
                runtime_read_file_remaining=self._run.tool_choice_read_file_remaining,
                runtime_glob_required_roots=(
                    frozenset(required_glob_roots) if required_glob_roots is not None else None
                ),
            )
        read_file_key = (
            _read_file_path_key(name, arguments, self._workspace_context.primary, self._workspace_context.additional_roots)
            if self._run.read_file_drift_guard_enabled
            else None
        )
        read_file_range_key = (
            _read_file_range_key(name, arguments, self._workspace_context.primary, self._workspace_context.additional_roots)
            if self._run.read_file_drift_guard_enabled
            else None
        )
        if read_file_range_key is not None:
            range_count = self._run.read_file_range_counts.get(read_file_range_key, 0)
            if range_count >= MAX_READ_FILE_SUCCESSES_PER_RANGE_IN_RUN:
                self._session_guards.record_hit("repeated_read_file")
                return self._repeated_read_file_result(
                    _display_read_file_range_key(
                        read_file_range_key,
                        self._workspace_context.primary,
                        self._workspace_context.additional_roots,
                    ),
                    range_count,
                    evidence=self._evidence_for_read_file_range(read_file_range_key),
                )
        signature = _tool_call_signature(name, arguments)
        search_pattern_key = _search_pattern_key(name, arguments)
        lsp_symbol_query_key = _lsp_symbol_query_key(name, arguments)
        # SessionGuardState records this name only after the registry has marked a prior
        # result as unknown, so known tools never consume the unknown-tool budget.
        unknown_tool_name = name
        semantic_exploration_key = _semantic_exploration_key(
            name,
            arguments,
            self._workspace_context.primary,
            self._workspace_context.additional_roots,
        )
        decision = self._session_guards.before_tool(
            read_file_key=read_file_key,
            signature=signature,
            search_pattern_key=search_pattern_key,
            lsp_symbol_query_key=lsp_symbol_query_key,
            semantic_exploration_key=semantic_exploration_key,
            unknown_tool_name=unknown_tool_name,
            complete_glob_signature=signature if name == "glob_files" else None,
        )
        if decision is not None:
            if decision.kind == "repeated_read_file":
                return self._repeated_read_file_result(decision.subject, decision.prior_count)
            if decision.kind == "duplicate_tool":
                return self._duplicate_tool_result(name, decision.prior_count)
            if decision.kind == "useless_search_pattern":
                return self._useless_search_pattern_result(decision.subject, decision.prior_count)
            if decision.kind == "useless_lsp_symbol":
                return self._useless_lsp_symbol_result(decision.subject, decision.prior_count)
            if decision.kind == "unknown_tool":
                return self._unknown_tool_result(decision.subject, decision.prior_count)
            if decision.kind == "repeated_complete_glob":
                return self._repeated_complete_glob_result()
            return self._semantic_exploration_result(decision.subject, decision.prior_count)
        result = self._registry.execute(name, arguments, tool_context)
        self._session_guards.record_result(
            search_pattern_key=search_pattern_key,
            lsp_symbol_query_key=lsp_symbol_query_key,
            unknown_tool_name=unknown_tool_name,
            complete_glob_signature=signature if name == "glob_files" else None,
            result=result,
        )
        if read_file_range_key is not None and not result.is_error:
            self._run.read_file_range_counts[read_file_range_key] = (
                self._run.read_file_range_counts.get(read_file_range_key, 0) + 1
            )
        return result

    def _effective_runtime_read_file_paths(self) -> frozenset[str] | None:
        raw_paths = self._run.tool_choice_read_file_paths
        if raw_paths is None:
            return None
        resolved_paths: set[str] = set()
        for raw_path in raw_paths:
            try:
                resolved = resolve_workspace_path(self._workspace_context.primary, raw_path, self._workspace_context.additional_roots)
            except PatchError:
                continue
            resolved_paths.add(str(resolved))
        return frozenset(resolved_paths)

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

    def _repeated_complete_glob_result(self) -> ToolResult:
        return ToolResult(
            (
                "Tool call skipped: identical glob_files arguments already returned a complete result in this session. "
                "Use the collected scope, or query a different uncovered workspace root or narrower pattern instead."
            ),
            is_error=True,
            metadata={"repeated_complete_glob": True, "guarded": True},
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

    def _unknown_tool_result(self, name: str, prior_count: int) -> ToolResult:
        return ToolResult(
            (
                f"Tool call skipped: unknown tool '{name}' has already been rejected {prior_count} times recently. "
                "Use a tool name from the current exposed tool list; do not keep retrying the same unknown name."
            ),
            is_error=True,
            metadata={"unknown_tool": True, "requested_tool": name, "guarded": True},
        )

    def _apply_tool_loop_steering(self, decision: ToolLoopSteeringDecision) -> None:
        self._messages.append({"role": "user", "content": decision.message})
        payload = {
            "kind": decision.kind,
            **decision.payload,
            "steer_count": self._run.tool_loop_steering.count(decision.kind),
        }
        self._session.append("runtime_steering", payload)
        if decision.force_final_answer_without_tools:
            if not self._queue_forced_final_answer(kind=decision.kind):
                self._run.block_unverified_final_answer(
                    kind=decision.kind,
                    reason=self._final_answer_rewrite_skip_reason() or "continuation_limit",
                )
        else:
            self._run.clear_forced_final_answer_request()
        self._run.temporary_tool_allowlist = decision.temporary_tool_allowlist

    def _tool_loop_stop_message(self, kind: str) -> str:
        return (
            f"Remaining tool calls were not executed because runtime steering '{kind}' requires the assistant "
            "to use the collected evidence and answer the user's original request."
        )

    def _record_read_file_evidence(self, name: str, arguments: str | dict[str, Any], result: ToolResult) -> None:
        if name == "read_file":
            requirement = self._run.soft_tool_requirement
            self._run.evidence.record_read_file(
                arguments=arguments,
                result=result,
                workspace=self._workspace_context.primary,
                allowed_dirs=self._workspace_context.additional_roots,
                requirement_candidates=requirement.candidate_files if requirement is not None else (),
            )

    def _record_tool_choice_result(self, name: str, arguments: str | dict[str, Any], result: ToolResult) -> None:
        metadata = self._tool_choice_result_metadata(name, arguments, result)
        self._run.tool_choice_tool_names.append(name)
        self._run.tool_choice_tool_names = self._run.tool_choice_tool_names[-80:]
        self._run.tool_choice_results.append(
            ToolResultSummary(
                name=name,
                content=review_input_summary(name, result.content, max_chars=6000),
                is_error=result.is_error,
                useless=result.useless,
                path=_tool_choice_result_path(arguments, result),
                metadata={
                    **review_input_metadata(name, result.content),
                    **metadata,
                },
            )
        )
        self._run.tool_choice_results = self._run.tool_choice_results[-80:]
        self._run.consume_tool_choice_read(name)
        self._refresh_verification_plan()

    def _refresh_verification_plan(self) -> None:
        plan = self._run.verification_plan
        if not plan.active:
            return
        test_plan = plan_narrow_test(self._workspace_context.primary, self._run.tool_choice_results)
        test_plan_changed = test_plan != self._run.verification_test_plan
        self._run.verification_test_plan = test_plan
        if plan.observe(self._run.tool_choice_results, test_plan=test_plan) or test_plan_changed:
            self._record_verification_plan_snapshot("update")

    def _record_verification_plan_snapshot(self, event: str) -> None:
        plan = self._run.verification_plan
        if not plan.active:
            return
        payload: dict[str, Any] = {"event": event, **plan.snapshot()}
        if self._run.verification_test_plan is not None:
            payload["test_plan"] = self._run.verification_test_plan.snapshot()
        self._session.append(f"verification_plan_{event}", payload)
        self._events.emit("ContextUpdated", {"kind": f"verification_plan_{event}", **payload})

    def _record_verification_patch_review(self, decision: SteeringDecision | None) -> None:
        plan = self._run.verification_plan
        if not plan.active:
            return
        review_capped = self._run.final_answer_steers.get("patch_reviewer", 0) >= MAX_PATCH_REVIEW_STEERS
        if decision is None and review_capped:
            changed = plan.record_patch_review(
                passed=None,
                reason="deterministic post-diff reviewer was skipped because its continuation cap was reached",
                refs=["git_diff:post-write"],
            )
        elif decision is None:
            changed = plan.record_patch_review(
                passed=True,
                reason="deterministic post-diff reviewer completed without blocking findings",
                refs=["git_diff:post-write"],
            )
        else:
            changed = plan.record_patch_review(
                passed=False,
                reason=decision.message,
                refs=["git_diff:post-write", f"steerer:{decision.kind}"],
            )
        if changed:
            self._record_verification_plan_snapshot("update")

    def _record_tool_evidence(self, name: str, arguments: str | dict[str, Any], result: ToolResult) -> None:
        record = self._run.evidence.record_tool(
            name=name,
            arguments=arguments,
            result=result,
            workspace=self._workspace_context.primary,
            allowed_dirs=self._workspace_context.additional_roots,
        )
        if record is not None:
            self._append_evidence_record(record)
        self._capture_session_evidence(record)

    def _invalidate_stale_source_evidence_after_write(
        self,
        name: str,
        arguments: str | dict[str, Any],
        result: ToolResult,
    ) -> None:
        self._run.evidence.invalidate_source_after_write(
            name=name,
            arguments=arguments,
            result=result,
            workspace=self._workspace_context.primary,
            allowed_dirs=self._workspace_context.additional_roots,
        )
        if result.is_error or name not in {"apply_patch", "rollback_patch", "write_file"}:
            return
        if name == "apply_patch" and _tool_call_uses_dry_run(arguments):
            return
        raw_path = _tool_choice_result_path(arguments, result)
        if not raw_path:
            return
        try:
            changed_path = resolve_workspace_path(
                self._workspace_context.primary,
                raw_path,
                self._workspace_context.additional_roots,
            )
        except PatchError:
            return
        removed = self._session_evidence.invalidate_paths((changed_path,))
        if removed:
            self._run.collector.record_session_evidence_invalidation(removed)
            self._record_session_evidence_event(
                "invalidated",
                {"reason": "workspace_write", "paths": [str(changed_path)], "count": removed},
            )

    def _capture_session_evidence(self, record: EvidenceRecord | None) -> None:
        if record is None or not self._run.tool_choice_results:
            return
        tool_result = self._run.tool_choice_results[-1]
        source = None
        requirement = None
        if tool_result.name == "read_file":
            resolved_path = record.details.get("resolved_path")
            source = next(
                (
                    item
                    for item in reversed(self._run.evidence.source_evidence)
                    if _source_evidence_matches_path(
                        item.path,
                        resolved_path,
                        self._workspace_context.primary,
                        self._workspace_context.additional_roots,
                    )
                ),
                None,
            )
            requirement = next(
                (
                    item
                    for item in reversed(self._run.evidence.pinned_requirement_evidence)
                    if _source_evidence_matches_path(
                        item.path,
                        resolved_path,
                        self._workspace_context.primary,
                        self._workspace_context.additional_roots,
                    )
                ),
                None,
            )
        if self._session_evidence.capture(
            tool_result=tool_result,
            record=record,
            source_evidence=source,
            requirement_evidence=requirement,
            workspace_revision=self._workspace_context.revision,
            request=self._run.current_user_request or "",
            run_id=self._run.run_id or "",
        ):
            self._record_session_evidence_event("captured", {"tool": tool_result.name, "path": tool_result.path})

    def _hydrate_session_evidence(self, prompt: str) -> None:
        reuse = self._session_evidence.reuse_for_request(
            prompt=prompt,
            workspace_revision=self._workspace_context.revision,
            authorized_roots=self._workspace_context.all_roots,
        )
        self._run.session_evidence_reuse = reuse
        self._run.collector.record_session_evidence(
            hits=reuse.hit_count,
            misses=reuse.miss_count,
            stale=reuse.stale_count,
            invalidations=reuse.invalidation_count,
            reused_paths=[self._display_session_evidence_path(path) for path in reuse.reused_paths],
        )
        for entry in reuse.entries:
            self._run.tool_choice_results.append(entry.tool_result)
            self._run.tool_choice_tool_names.append(entry.tool_result.name)
            if self._run.evidence.hydrate_session_cached(
                record=entry.record,
                source_evidence=entry.source_evidence,
                requirement_evidence=entry.requirement_evidence,
                canonical_paths=tuple(entry.content_tags),
            ):
                self._session.append(
                    "session_evidence_reused",
                    {
                        "entry_id": entry.entry_id,
                        "tool": entry.tool_result.name,
                        "path": entry.tool_result.path,
                        "root": entry.root,
                        "origin_run_id": entry.origin_run_id,
                    },
                )
        if reuse.hit_count or reuse.stale_count:
            self._record_session_evidence_event(
                "reused",
                {
                    "hits": reuse.hit_count,
                    "misses": reuse.miss_count,
                    "stale": reuse.stale_count,
                    "reused_paths": [self._display_session_evidence_path(path) for path in reuse.reused_paths],
                },
            )

    def _record_session_evidence_event(self, event: str, payload: Mapping[str, Any]) -> None:
        data = {"event": event, **dict(payload)}
        self._session.append(f"session_evidence_{event}", data)
        self._events.emit("ContextUpdated", {"kind": f"session_evidence_{event}", **data})

    def _display_session_evidence_path(self, raw_path: str) -> str:
        try:
            resolved = Path(raw_path).resolve()
            root = evidence_root_for_path(
                resolved,
                self._workspace_context.primary,
                self._workspace_context.additional_roots,
            )
            label = evidence_root_label(root, self._workspace_context.primary, self._workspace_context.additional_roots)
            return f"{label}:{resolved.relative_to(root)}"
        except (OSError, ValueError):
            return raw_path

    def _append_evidence_record(self, record: EvidenceRecord) -> None:
        if not self._run.evidence.append(record):
            return
        self._session.append(
            "evidence",
            {
                "tool": record.tool,
                "subject": record.subject,
                "status": record.status,
                "summary": record.summary,
                "details": dict(record.details),
            },
        )

    def _record_workspace_root_evidence(self) -> None:
        record = self._run.evidence.record_workspace_root(self._workspace_context.primary)
        if record is not None:
            self._append_evidence_record(record)

    def _patch_relevance_denial_reason(self, raw_path: str, resolved_path: Path) -> str | None:
        display_path = display_workspace_path(self._workspace_context.primary, resolved_path, self._workspace_context.additional_roots)
        return self._run.evidence.patch_relevance_denial_reason(
            raw_path,
            resolved_path,
            workspace=self._workspace_context.primary,
            allowed_dirs=self._workspace_context.additional_roots,
            is_code_implementation_request=is_code_implementation_request(self._run.current_user_request),
            request_mentions_config_or_path=request_mentions_config_or_path(self._run.current_user_request, display_path),
        )

    def _patch_preview_denial_reason(self, args: dict[str, Any], resolved_path: Path) -> str | None:
        return self._run.evidence.patch_preview_denial_reason(
            args,
            resolved_path,
            preview_required=_request_requires_patch_preview(self._run.current_user_request),
        )

    def _record_successful_patch_preview(
        self,
        name: str,
        arguments: str | dict[str, Any],
        result: ToolResult,
    ) -> None:
        self._run.evidence.record_successful_patch_preview(
            name=name,
            arguments=arguments,
            result=result,
            workspace=self._workspace_context.primary,
            allowed_dirs=self._workspace_context.additional_roots,
        )

    def _read_file_evidence_summary(self) -> str:
        return self._run.evidence.read_file_summary()

    def _evidence_for_read_file_range(self, range_key: tuple[str, int, str]) -> str:
        subject = _display_read_file_range_subject(range_key, self._workspace_context.primary, self._workspace_context.additional_roots)
        return self._run.evidence.evidence_for_read_file_range(subject)

    def _evidence_ledger_summary(self) -> str:
        return self._run.evidence.summary()

    def _final_answer_request_summary(self) -> str:
        if not self._run.current_user_request:
            return ""
        return (
            "\n\nOriginal user request to satisfy now:\n"
            f"- {_one_line(self._run.current_user_request, max_chars=1200)}"
        )

    def _decide_final_answer_steering(
        self,
        content: str,
        run_start_index: int,
    ) -> SteeringDecision | None:
        context = self._final_answer_context(content, run_start_index)
        for steerer in self._final_answer_steerers:
            decision = steerer.decide(context)
            if decision is not None:
                return decision
        return None

    def _decide_post_diff_patch_review(self, run_start_index: int) -> SteeringDecision | None:
        context = self._final_answer_context("", run_start_index)
        for steerer in self._final_answer_steerers:
            if steerer.kind != "patch_reviewer":
                continue
            return steerer.decide(context)
        return None

    def _final_answer_context(self, content: str, run_start_index: int) -> FinalAnswerContext:
        return FinalAnswerContext(
            request=self._run.current_user_request,
            content=content,
            messages=self._messages,
            run_start_index=run_start_index,
            requirement_contract=self._run.requirement_contract,
            tool_results=list(self._run.tool_choice_results),
            read_file_evidence_paths=list(self._run.evidence.read_file_paths),
            source_evidence=list(self._run.evidence.source_evidence),
            requirement_evidence=list(self._run.evidence.pinned_requirement_evidence),
            required_design_evidence_roots=self._run.design_evidence_coverage.roots,
            design_evidence_read_paths=list(self._run.evidence.design_read_paths),
            open_todos=self._open_todo_summary(),
            is_code_implementation_request=is_code_implementation_request(self._run.current_user_request),
            steer_counts=self._final_answer_steer_counts(),
            verification_plan=self._run.verification_plan,
        )

    def _apply_final_answer_steering(self, decision: SteeringDecision) -> bool:
        if decision.force_final_answer_without_tools:
            skip_reason = self._final_answer_rewrite_skip_reason()
            if skip_reason is not None:
                self._session.append(
                    "runtime_steering",
                    {
                        "kind": "final_answer_steering_skipped",
                        "skipped_kind": decision.kind,
                        "reason": skip_reason,
                    },
                )
                if decision.severity == FinalAnswerSteeringSeverity.HARD:
                    self._run.block_unverified_final_answer(kind=decision.kind, reason=skip_reason)
                    self._session.append(
                        "runtime_steering",
                        {
                            "kind": "final_answer_hard_gate_unresolved",
                            "steering_kind": decision.kind,
                            "reason": skip_reason,
                        },
                    )
                return False
            if not self._queue_forced_final_answer(kind=decision.kind, severity=decision.severity.value):
                skip_reason = self._final_answer_rewrite_skip_reason() or "continuation_limit"
                self._session.append(
                    "runtime_steering",
                    {
                        "kind": "final_answer_steering_skipped",
                        "skipped_kind": decision.kind,
                        "reason": skip_reason,
                    },
                )
                if decision.severity == FinalAnswerSteeringSeverity.HARD:
                    self._run.block_unverified_final_answer(kind=decision.kind, reason=skip_reason)
                    self._session.append(
                        "runtime_steering",
                        {
                            "kind": "final_answer_hard_gate_unresolved",
                            "steering_kind": decision.kind,
                            "reason": skip_reason,
                        },
                    )
                return False
        steer_count = self._increment_final_answer_steer_count(decision.kind)
        self._messages.append({"role": "user", "content": decision.message})
        payload = {
            "kind": decision.kind,
            **decision.payload,
            "steer_count": steer_count,
        }
        self._session.append("runtime_steering", payload)
        if decision.force_final_answer_without_tools:
            self._run.force_final_answer_without_tools = True
        else:
            self._run.clear_forced_final_answer_request()
        self._run.temporary_tool_allowlist = decision.temporary_tool_allowlist
        return True

    def _final_answer_rewrite_skip_reason(self) -> str | None:
        deadline = self._run.deadline_monotonic
        if deadline is not None and deadline - time.monotonic() <= FINAL_RESPONSE_RESERVE_SECONDS:
            return "deadline_reserve"
        if self._run.finalization.aggregate_attempts >= MAX_FINALIZATION_ATTEMPTS:
            return "aggregate_limit"
        if not self._run.can_queue_forced_final_answer():
            return "continuation_limit"
        return None

    def _final_answer_steer_counts(self) -> dict[str, int]:
        return {
            "read_only_evidence": self._run.final_answer_steers.get("read_only_evidence", 0),
            "requirement_evidence": self._run.final_answer_steers.get("requirement_evidence", 0),
            "design_evidence": self._run.final_answer_steers.get("design_evidence", 0),
            "design_evidence_final": self._run.design_evidence_coverage.final_steers,
            "no_edit_final_hygiene": self._run.final_answer_steers.get("no_edit_final_hygiene", 0),
            "final_structure": self._run.final_answer_steers.get("final_structure", 0),
            "source_evidence_false_negative": self._run.final_answer_steers.get("source_evidence_false_negative", 0),
            "tool_usage_evidence": self._run.final_answer_steers.get("tool_usage_evidence", 0),
            "negative_existence": self._run.final_answer_steers.get("negative_existence", 0),
            "source_grounded_numeric": self._run.final_answer_steers.get("source_grounded_numeric", 0),
            "patch_reviewer": self._run.final_answer_steers.get("patch_reviewer", 0),
            "completion_audit": self._run.final_answer_steers.get("completion_audit", 0),
        }

    def _increment_final_answer_steer_count(self, kind: str) -> int:
        if kind not in self._final_answer_steer_counts():
            return 0
        self._run.final_answer_steers[kind] = self._run.final_answer_steers.get(kind, 0) + 1
        return self._run.final_answer_steers[kind]

    def _append_soft_tool_requirement_message(self, requirement: SoftToolRequirement) -> None:
        content = soft_tool_requirement_message(requirement)
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
        requirement = self._run.soft_tool_requirement
        return requirement is not None and not requirement.satisfied

    def _steer_for_soft_tool_requirement(self) -> bool:
        requirement = self._run.soft_tool_requirement
        if requirement is None or requirement.satisfied:
            return False
        if not advance_soft_tool_requirement(requirement):
            return False
        content = soft_tool_requirement_message(requirement)
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
        return soft_tool_requirement_stop_message(self._run.soft_tool_requirement)

    def _observe_soft_tool_requirement(self, name: str, arguments: str | dict[str, Any], result: ToolResult) -> None:
        requirement = self._run.soft_tool_requirement
        path = observe_soft_tool_requirement(
            requirement,
            name=name,
            arguments=arguments,
            result=result,
            workspace=self._workspace_context.primary,
            allowed_dirs=self._workspace_context.additional_roots,
        )
        if path is not None and requirement is not None:
            self._session.append(
                "runtime_steering",
                {"kind": f"{requirement.kind}_satisfied", "path": str(path)},
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
        skip_memory_consolidation: bool = False,
    ) -> str:
        incomplete_delivery: str | None = None
        hard_gate = self._run.unresolved_final_answer_gate
        if hard_gate is not None:
            content = render_unverified_final_answer(hard_gate.kind, hard_gate.reason)
            if reason == "final":
                reason = "unverified_final_gate"
        elif reason == "final" and workspace_write_happened(self._run.tool_choice_results):
            incomplete_delivery = self._run.verification_plan.render_incomplete_terminal()
        self._session_evidence.remember_request(self._run.current_user_request or "", self._run.run_id)
        if incomplete_delivery:
            content = incomplete_delivery
            reason = "incomplete_delivery"
        delivery_report = render_delivery_report(self._run.verification_plan, self._run.tool_choice_results)
        if delivery_report:
            content = f"{content.rstrip()}\n\n{delivery_report}"
        self._session.append("final", {"content": content})
        run_messages = self._messages[run_start_index:]
        if skip_memory_consolidation:
            self._session.append(
                "memory_consolidation",
                {"mode": self._config.memory_consolidation, "status": "skipped", "reason": reason},
            )
        else:
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
            self._workspace_context.primary,
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
            response = call_chat_with_timeout(self._client, messages, [], timeout=timeout)
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
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._record_tool_result_for_run(
            name=name,
            is_error=is_error,
            useless=useless,
            metadata=metadata,
        )
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
        if metadata and metadata.get("provider_schema_violation"):
            self._events.emit(
                "ErrorEvent",
                {
                    "kind": "provider_schema_violation",
                    "tool": name,
                    "allowed_tools": list(metadata.get("allowed_tools") or ()),
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

    def _tool_choice_result_metadata(
        self,
        name: str,
        arguments: str | dict[str, Any],
        result: ToolResult,
    ) -> dict[str, Any]:
        metadata = dict(result.metadata)
        raw_path = _tool_choice_result_path(arguments, result)
        if raw_path:
            try:
                resolved = resolve_workspace_path(
                    self._workspace_context.primary,
                    raw_path,
                    self._workspace_context.additional_roots,
                )
            except PatchError:
                resolved = None
            if resolved is not None:
                root = evidence_root_for_path(
                    resolved,
                    self._workspace_context.primary,
                    self._workspace_context.additional_roots,
                )
                metadata.setdefault("evidence_root", str(root))
                metadata.setdefault(
                    "evidence_root_label",
                    evidence_root_label(
                        root,
                        self._workspace_context.primary,
                        self._workspace_context.additional_roots,
                    ),
                )
                metadata.setdefault("evidence_scope", "root_local")
        if name == "glob_files":
            metadata.setdefault("evidence_scope", "root_discovery")
        if name == "search_code":
            paths = first_search_result_paths(result.content, limit=MAX_SESSION_EVIDENCE_TAGGED_PATHS + 1)
            metadata.setdefault("evidence_paths", paths[:MAX_SESSION_EVIDENCE_TAGGED_PATHS])
            if len(paths) > MAX_SESSION_EVIDENCE_TAGGED_PATHS:
                metadata.setdefault("evidence_paths_overflow", True)
        elif name.startswith("lsp_"):
            paths = first_result_line_paths(result.content, limit=MAX_SESSION_EVIDENCE_TAGGED_PATHS + 1)
            metadata.setdefault("evidence_paths", paths[:MAX_SESSION_EVIDENCE_TAGGED_PATHS])
            if len(paths) > MAX_SESSION_EVIDENCE_TAGGED_PATHS:
                metadata.setdefault("evidence_paths_overflow", True)
        return metadata

    def _queue_forced_final_answer(
        self,
        *,
        kind: str,
        severity: str = FINAL_ANSWER_STEERING_HARD,
    ) -> bool:
        outcome = self._run.finalization.request(
            kind=kind,
            severity=severity,
            deadline_monotonic=self._run.deadline_monotonic,
            reserve_seconds=FINAL_RESPONSE_RESERVE_SECONDS,
        )
        return outcome.accepted

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


def _parse_tool_arguments(arguments: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _clip_memory_text(text: str, *, max_chars: int) -> str:
    return _clip_context_text(text, max_chars=max_chars, marker="...<earlier memory truncated>\n")


def _clip_context_text(text: str, *, max_chars: int, marker: str) -> str:
    if len(text) <= max_chars:
        return text
    keep = max(0, max_chars - len(marker))
    if keep == 0:
        return marker[:max_chars]
    return marker + text[-keep:].lstrip()


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
    pinned_requirement_evidence: str = "",
    user_facts_context: str = "",
    path_rule_metadata: str = "",
    matched_path_rules: str = "",
    verification_plan_context: str = "",
) -> list[dict[str, Any]]:
    updated = list(messages)
    workspace_roots = workspace_roots_context(workspace, allowed_dirs)
    if workspace_roots:
        updated = _messages_with_workspace_roots(updated, workspace_roots)
    if current_user_request:
        updated = _messages_with_current_task_contract(updated, current_user_request)
        updated = _messages_with_no_edit_final_hygiene(updated, current_user_request, todo_summary)
    if requirement_contract_context:
        updated = _messages_with_requirement_contract(updated, requirement_contract_context)
    if pinned_requirement_evidence:
        updated = _messages_with_pinned_requirement_evidence(updated, pinned_requirement_evidence)
    if user_facts_context:
        updated = _messages_with_user_facts_context(updated, user_facts_context)
    if planner_explore_context:
        updated = _messages_with_planner_explore_context(updated, planner_explore_context)
    if evidence_ledger:
        updated = _messages_with_evidence_ledger(updated, evidence_ledger)
    if path_rule_metadata:
        updated = _messages_with_path_rule_context(updated, path_rule_metadata, "[Path-scoped rules]")
    if matched_path_rules:
        updated = _messages_with_path_rule_context(updated, matched_path_rules, "[Matched path-scoped rules]")
    if verification_plan_context:
        updated = _messages_with_path_rule_context(updated, verification_plan_context, "[Verification plan]")
    sticky_rules = load_sticky_rules(workspace, user_config_dir, max_chars=STICKY_RULES_CHAR_LIMIT)
    if sticky_rules:
        updated = _messages_with_sticky_rules(updated, sticky_rules)
    return _messages_with_runtime_todo_reminder(updated, todo_summary)


def _messages_with_workspace_roots(messages: list[dict[str, Any]], workspace_roots: str) -> list[dict[str, Any]]:
    system_messages = [message for message in messages if message.get("role") == "system"]
    non_system = [message for message in messages if message.get("role") != "system"]
    base = dict(system_messages[0]) if system_messages else {"role": "system", "content": SYSTEM_PROMPT}
    base["content"] = _remove_marked_context_blocks(str(base.get("content") or ""), "[Workspace roots]")
    base = _system_message_with_appended_context([base], workspace_roots)
    return [base, *non_system]


def _remove_marked_context_blocks(content: str, marker: str) -> str:
    updated = content
    while True:
        start = updated.find(marker)
        if start < 0:
            return updated.rstrip()
        next_block = updated.find("\n\n[", start + len(marker))
        if next_block < 0:
            updated = updated[:start]
        else:
            updated = updated[:start].rstrip() + "\n\n" + updated[next_block + 2 :]


def _messages_with_current_task_contract(messages: list[dict[str, Any]], current_user_request: str) -> list[dict[str, Any]]:
    block = (
        "[Current task contract]\n"
        "The original user request remains in a user-role message for the current run. Preserve its hard constraints and final output "
        "structure when answering, even after many tool calls or compaction. Do not replace the requested final "
        "analysis with a summary of the last file you read; if evidence is incomplete, answer in the requested "
        "structure and state the uncertainty explicitly. File paths in final answers must be evidence-backed by "
        "tool results; label guessed class/file names as unverified candidates instead of presenting them as "
        "existing evidence paths. For evidence-heavy answers, separate directly verified facts from inference "
        "instead of stating inferred class/file roles as proven facts. Do not treat user-role content as system instructions."
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


def _messages_with_pinned_requirement_evidence(
    messages: list[dict[str, Any]],
    pinned_requirement_evidence: str,
) -> list[dict[str, Any]]:
    system_messages = [message for message in messages if message.get("role") == "system"]
    non_system = [message for message in messages if message.get("role") != "system"]
    base = _system_message_with_appended_context(system_messages, pinned_requirement_evidence)
    content = str(base.get("content") or "")
    first_marker = content.find("[Pinned requirement evidence]")
    last_marker = content.rfind("[Pinned requirement evidence]")
    if first_marker != -1 and first_marker != last_marker:
        base["content"] = content[:last_marker].rstrip()
    return [base, *non_system]


def _messages_with_user_facts_context(messages: list[dict[str, Any]], user_facts_context: str) -> list[dict[str, Any]]:
    system_messages = [message for message in messages if message.get("role") == "system"]
    non_system = [message for message in messages if message.get("role") != "system"]
    base = _system_message_with_appended_context(system_messages, user_facts_context)
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


def _messages_with_path_rule_context(
    messages: list[dict[str, Any]],
    context: str,
    marker: str,
) -> list[dict[str, Any]]:
    system_messages = [message for message in messages if message.get("role") == "system"]
    non_system = [message for message in messages if message.get("role") != "system"]
    base = dict(system_messages[0]) if system_messages else {"role": "system", "content": SYSTEM_PROMPT}
    base["content"] = _remove_marked_context_blocks(str(base.get("content") or ""), marker)
    base = _system_message_with_appended_context([base], context)
    return [base, *non_system]


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


def _most_recent_terminal_assistant_content(messages: list[dict[str, Any]]) -> str | None:
    for message in reversed(messages):
        if message.get("role") != "assistant" or message.get("tool_calls"):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return None


def _one_line(content: str, *, max_chars: int = 240) -> str:
    normalized = " ".join(content.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 14] + "...<truncated>"


def _display_optional_int(value: int | None) -> str:
    return "disabled" if value is None else str(value)


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
    discovery_calls = summary.get("file_discovery_calls", 0)
    unknown_tool_calls = summary.get("unknown_tool_calls", 0)
    if discovery_calls or unknown_tool_calls:
        lines.append(
            "  - discovery: "
            f"glob_calls={discovery_calls}, "
            f"incomplete={summary.get('file_discovery_incomplete_results', 0)}, "
            f"no_match={summary.get('file_discovery_no_match_results', 0)}, "
            f"unknown_tools={unknown_tool_calls}, "
            f"suggested={summary.get('unknown_tool_suggestions', 0)}, "
            f"filename_search_misuse={summary.get('filename_search_misuse_calls', 0)}"
        )
    guard_hits = summary.get("guard_hits")
    if isinstance(guard_hits, dict) and guard_hits:
        rendered_guards = ", ".join(f"{name}={count}" for name, count in sorted(guard_hits.items()))
        lines.append(f"  - guards: {rendered_guards}")
    steering_counts = summary.get("steering_counts")
    if isinstance(steering_counts, dict) and steering_counts:
        rendered_steers = ", ".join(f"{name}={count}" for name, count in sorted(steering_counts.items()))
        lines.append(f"  - steering: {rendered_steers}")
    verification_plan = summary.get("verification_plan")
    if isinstance(verification_plan, dict):
        rendered_plan = ", ".join(f"{name}={count}" for name, count in sorted(verification_plan.items()))
        lines.append(f"  - delivery_checks: {rendered_plan}")
    business_acceptance = summary.get("business_acceptance")
    if isinstance(business_acceptance, dict):
        rendered_business = ", ".join(f"{name}={count}" for name, count in sorted(business_acceptance.items()))
        lines.append(f"  - business_acceptance_unverified: {rendered_business}")
    session_evidence = summary.get("session_evidence")
    if isinstance(session_evidence, dict):
        paths = session_evidence.get("reused_paths", ())
        rendered_paths = ", ".join(str(path) for path in paths) if isinstance(paths, (list, tuple)) and paths else "none"
        lines.append(
            "  - session_evidence: "
            f"hits={session_evidence.get('hits', 0)}, "
            f"misses={session_evidence.get('misses', 0)}, "
            f"stale={session_evidence.get('stale', 0)}, "
            f"invalidations={session_evidence.get('invalidations', 0)}, "
            f"paths={rendered_paths}"
        )
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


def _tool_choice_result_path(arguments: str | dict[str, Any], result: ToolResult) -> str | None:
    parsed: Any = arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    path = parsed.get("path")
    if path is not None:
        return str(path)
    changed_path = result.metadata.get("changed_path") if isinstance(result.metadata, Mapping) else None
    return str(changed_path) if isinstance(changed_path, str) and changed_path.strip() else None


def _tool_call_uses_dry_run(arguments: str | dict[str, Any]) -> bool:
    if isinstance(arguments, dict):
        return bool(arguments.get("dry_run"))
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return False
    return bool(parsed.get("dry_run")) if isinstance(parsed, dict) else False


def _source_evidence_matches_path(
    display_path: str,
    resolved_path: object,
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
) -> bool:
    if not isinstance(resolved_path, str) or not resolved_path:
        return False
    try:
        return resolve_workspace_path(workspace, display_path, allowed_dirs) == Path(resolved_path).resolve()
    except (PatchError, OSError):
        return False


def _request_requires_patch_preview(request: str | None) -> bool:
    lowered = (request or "").lower()
    if any(marker in lowered for marker in {"skip preview", "skip dry_run", "跳过预览", "无需预览"}):
        return False
    if "dry_run" in lowered or "dry run" in lowered:
        return True
    preview_markers = {"必须预览", "先预览", "预览后", "预览 diff", "预览补丁", "patch preview"}
    return any(marker in lowered for marker in preview_markers)


def _patch_preview_signature(args: dict[str, Any], resolved_path: Path) -> str:
    payload = {
        "path": str(resolved_path),
        "tag": args.get("tag"),
        "start_line": args.get("start_line"),
        "end_line": args.get("end_line"),
        "old_text": args.get("old_text"),
        "new_text": args.get("new_text"),
        "mode": args.get("mode") or "replace",
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


def _llm_failure_reason(error: LlmError) -> str:
    if isinstance(error, LlmTimeoutError):
        return "llm_timeout"
    return "provider_error"


def _validate_runtime_tool_name(tool: str) -> str:
    normalized = tool.strip()
    if not normalized or not all(char.isalnum() or char == "_" for char in normalized):
        raise ValueError(f"invalid tool name: {tool}")
    return normalized

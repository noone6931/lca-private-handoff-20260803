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
from .compaction import compaction_recent_messages as _compaction_recent_messages
from .compaction import estimate_message_chars as _estimate_message_chars
from .compaction import estimate_message_tokens as _estimate_message_tokens
from .compaction import format_llm_compaction_summary as _format_llm_compaction_summary
from .compaction import last_user_message_index as _last_user_message_index
from .compaction import messages_with_compaction_context as _messages_with_compaction_context
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
from .chat_runtime import call_chat_with_timeout
from .config import AgentConfig
from .config import normalize_approval_mode
from .design_evidence import project_workspace_evidence_roots
from .delivery_report import render_delivery_report
from .evidence import EvidenceRecord
from .evidence import first_result_line_paths
from .evidence import first_search_result_paths
from .evidence import evidence_root_for_path
from .evidence import evidence_root_label
from .finalization import FINAL_ANSWER_STEERING_HARD
from .llm import LlmError
from .llm import LlmTimeoutError
from .llm import OpenAICompatibleClient
from .provider_protocol import ProviderProtocolArtifact
from .provider_protocol import provider_safe_assistant_message as _provider_safe_assistant_message
from .provider_protocol import classify_provider_content_artifact
from .provider_protocol import normalize_provider_dialect_message
from .lsp.client import close_all_clients
from .negative_evidence import negative_claim_metrics as _negative_claim_metrics
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
from .session_evidence import query_identity as _session_evidence_query_identity
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
from .steering.pre_review import PRE_REVIEW_AUDIT_KINDS
from .steering.pre_review import should_aggregate_pre_review_audits
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
from .task_contract import requires_no_edit_final_hygiene
from .tools.relevance import path_matches_any
from .tools.relevance import request_mentions_config_or_path
from .tool_observation import ToolResultSummary
from .verification_timeline import workspace_write_happened
from .user_facts import UserFactsLayer
from .provider_context import ProviderContextPhase
from .runtime_evidence import EvidenceVerificationLifecycle
from .runtime_memory import MemoryConsolidationLifecycle
from .runtime_tool_directive import RuntimeToolDirectivePhase
from .runtime_tool_choice_directive import RuntimeToolChoiceDirectivePhase
from .runtime_tool_choice_queue import RuntimeToolChoiceQueuePhase
from .runtime_workflow_profile import WorkflowReadOnlyReviewPhase
from .runtime_read_only_explore import RuntimeReadOnlyExplorePhase
from .runtime_provider_terminal import ProviderTerminalPhase
from .runtime_workspace import WorkspaceLifecycle
from .runtime_prompt import _assistant_event_payload
from .runtime_prompt import _tool_call_event_payload
from .runtime_prompt import _event_preview
from .runtime_prompt import _parse_tool_arguments
from .runtime_prompt import _clip_memory_text
from .runtime_prompt import _clip_context_text
from .runtime_prompt import _messages_with_runtime_todo_reminder
from .runtime_prompt import _messages_with_runtime_context
from .runtime_prompt import _latest_user_content
from .runtime_prompt import _most_recent_terminal_assistant_content
from .runtime_prompt import _one_line
from .runtime_prompt import _display_optional_int
from .runtime_prompt import _format_last_run_status
from .runtime_prompt import _with_workflow_nudge
from .runtime_prompt import _strip_workflow_nudge
from .runtime_prompt import _should_add_workflow_nudge
from .runtime_prompt import _should_guard_repeated_read_file
from .memory_consolidation import _messages_to_memory_transcript
from .memory_consolidation import _render_memory_transcript_message
from .memory_consolidation import _assistant_tool_call_names
from .memory_consolidation import _last_assistant_content_is
from .memory_consolidation import _run_used_memory_write_tool
from .memory_consolidation import _should_auto_consolidate_memory
from .memory_consolidation import _parse_memory_consolidation_response
from .memory_consolidation import _extract_json_object_text
from .memory_consolidation import _clean_consolidated_memory_item
from .memory_consolidation import _memory_consolidation_root
from .memory_consolidation import _append_consolidated_memory
from .memory_consolidation import _memory_item_digest
from .memory_consolidation import _normalized_memory_item_key
from .tool_gateway import _tool_call_signature
from .tool_gateway import _intersect_optional_tool_allowlist
from .tool_gateway import _tool_choice_result_path
from .tool_gateway import _tool_call_uses_dry_run
from .tool_gateway import _source_evidence_matches_path
from .tool_gateway import _request_requires_patch_preview
from .tool_gateway import _patch_preview_signature
from .tool_gateway import _search_pattern_key
from .tool_gateway import _lsp_symbol_query_key
from .tool_gateway import _semantic_exploration_key
from .tool_gateway import _semantic_directory_key
from .tool_gateway import _path_parts_relative_to_known_root
from .tool_gateway import _read_file_path_key
from .tool_gateway import _read_file_range_key
from .tool_gateway import _read_file_line_number
from .tool_gateway import _display_read_file_range_key
from .tool_gateway import _display_read_file_range_subject
from .tool_gateway import _llm_failure_reason
from .tool_gateway import _validate_runtime_tool_name
from .tool_gateway import is_session_evidence_reread

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
WORKFLOW_NUDGE = (
    "For this coding task, infer the tool sequence yourself. "
    "Use local inspection and lsp_* code navigation before editing; use todo for multi-step work; use ask_user only when ambiguity affects the outcome; "
    "preview meaningful existing-file edits with apply_patch dry_run=true; verify changes with tests/checks and git_diff."
)

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
        self._base_system_prompt = SYSTEM_PROMPT
        self._session_guards = SessionGuardState()
        self._session_evidence = SessionEvidenceCache()
        self._user_facts = UserFactsLayer()
        self._run = RunContext(workflow_profile_selector=config.workflow_profile)
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
        self._provider_context_phase = ProviderContextPhase(self)
        self._workspace_phase = WorkspaceLifecycle(self)
        self._evidence_phase = EvidenceVerificationLifecycle(self)
        self._memory_phase = MemoryConsolidationLifecycle(self)
        self._tool_directive_phase = RuntimeToolDirectivePhase(self)
        self._tool_choice_directive_phase = RuntimeToolChoiceDirectivePhase(self)
        self._tool_choice_queue_phase = RuntimeToolChoiceQueuePhase(self)
        self._read_only_explore_phase = RuntimeReadOnlyExplorePhase(self)
        self._read_only_review_phase = WorkflowReadOnlyReviewPhase(self)
        self._provider_terminal_phase = ProviderTerminalPhase(self)
        missing_roots = self._workspace_phase.restore_session_workspace_roots()
        self._path_rule_index = discover_path_scoped_rules(self._workspace_context.all_roots)
        system_prompt = self._workspace_phase.build_system_prompt()
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
            vision_inspector=self._provider_context_phase.inspect_image,
        )
        self._evidence_phase.restore_session_evidence_cache()
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
        workspace_evidence_roots = project_workspace_evidence_roots(
            self._workspace_context.primary,
            self._workspace_context.additional_roots,
            read_only_review_profile=requirement_contract.read_only_review_profile,
            inspection_forbidden=requirement_contract.inspection_forbidden,
        )
        design_evidence_roots = workspace_evidence_roots.cross_root_coverage_roots
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
        self._tool_directive_phase.begin_run()
        self._read_only_review_phase.begin_run()
        self._start_run_collector(run_id, prompt, started_monotonic)
        self._user_facts.begin_run(prompt, run_id)
        self._run.user_facts_context = self._user_facts.render_for(prompt)
        self._evidence_phase.hydrate_session_evidence(prompt)
        self._evidence_phase.record_verification_plan_snapshot("snapshot")
        self._messages.append({"role": "user", "content": model_prompt})
        self._evidence_phase.append_session_evidence_reuse_directive()
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
        self._session.append("workflow_profile", self._run.workflow_profile_payload())
        self._events.emit("ContextUpdated", {"kind": "workflow_profile", **self._run.workflow_profile_payload()})
        if self._run.active_design_evidence_roots():
            self._session.append(
                "runtime_steering",
                {"kind": "design_evidence_roots", "roots": list(self._run.active_design_evidence_roots())},
            )
        if model_prompt != prompt:
            self._session.append("workflow_nudge", {"content": WORKFLOW_NUDGE})
        self._run.read_file_drift_guard_enabled = _should_guard_repeated_read_file(prompt)
        self._run.soft_tool_requirement = (
            None
            if requirement_contract.inspection_forbidden
            else initial_soft_tool_requirement(
                prompt,
                self._workspace_context.primary,
                self._workspace_context.additional_roots,
                max_skill_description_chars=MAX_SKILL_DESCRIPTION_CHARS,
            )
        )
        if self._run.soft_tool_requirement is not None:
            self._append_soft_tool_requirement_message(self._run.soft_tool_requirement)
        self._evidence_phase.record_workspace_root_evidence()
        tool_context = replace(
            self._tool_context,
            deadline_monotonic=deadline,
            git_baseline=git_baseline,
            current_user_request=prompt,
            patch_relevance_checker=self._evidence_phase.patch_relevance_denial_reason,
            patch_preview_checker=self._evidence_phase.patch_preview_denial_reason,
        )

        step = 1
        while self._config.max_steps == 0 or step <= self._config.max_steps:
            if self._deadline_exceeded(deadline):
                return self._stop_for_budget(deadline, run_start_index)

            tool_choice_stop_message = self._tool_choice_queue_phase.apply_if_needed(deadline)
            if tool_choice_stop_message is not None:
                return self._finish_run(
                    tool_choice_stop_message,
                    deadline,
                    run_start_index,
                    reason=(
                        "unverified_final_gate"
                        if self._run.unresolved_final_answer_gate is not None
                        else self._run.tool_choice_stop_reason or "tool_choice_queue"
                    ),
                )
            if self._tool_directive_phase.before_model_turn():
                step += 1
                continue
            messages_for_model = self._provider_context_phase.messages_for_model(deadline)
            tools_for_model = self._tools_for_model()
            tool_choice_turn = self._tool_choice_directive_phase.before_model_turn()
            tool_schema_names = [
                str(schema.get("function", {}).get("name") or "")
                for schema in tools_for_model
                if isinstance(schema, Mapping)
            ]
            self._record_llm_request()
            self._session.append(
                "llm_request",
                {"step": step, "tool_schema_names": tool_schema_names, "forced_tool_choice": tool_choice_turn.forced_tool_name},
            )
            self._events.emit(
                "LlmRequest",
                {
                    "step": step,
                    "message_count": len(messages_for_model),
                    "tool_schema_count": len(tools_for_model),
                    "tool_schema_names": tool_schema_names,
                    "forced_tool_choice": tool_choice_turn.forced_tool_name,
                    "force_final_answer": self._run.force_final_answer_without_tools,
                },
            )
            force_final_answer = self._run.finalization.begin_forced_final_turn()
            forced_final_kind = self._run.finalization.kind if force_final_answer else None
            try:
                response = call_chat_with_timeout(
                    self._client,
                    messages_for_model,
                    tools_for_model,
                    timeout=self._provider_context_phase.remaining_timeout(deadline),
                    tool_choice=tool_choice_turn.tool_choice,
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
            normalized_response_message, fallback_normalizations = normalize_provider_dialect_message(
                response.message,
                provider=self._config.provider,
            )
            self._provider_terminal_phase.record_argument_normalizations(
                (*getattr(response, "protocol_normalizations", ()), *fallback_normalizations),
            )
            raw_message = {**normalized_response_message, "role": "assistant"}
            raw_tool_calls = raw_message.get("tool_calls") or []
            artifact = getattr(response, "protocol_artifact", None)
            if artifact is None:
                artifact = classify_provider_content_artifact(self._config.provider, raw_message.get("content"))
            if force_final_answer and isinstance(raw_tool_calls, list) and raw_tool_calls:
                protocol_outcome = self._provider_terminal_phase.handle_protocol_violation(
                    phase="forced_final",
                    forced_final_kind=forced_final_kind or "runtime_forced_final",
                    artifact_kind="structured_tool_calls",
                    tool_calls=raw_tool_calls,
                    deadline=deadline,
                )
                if protocol_outcome.action == "retry":
                    step += 1
                    continue
                return self._finish_run(
                    protocol_outcome.terminal_message,
                    deadline,
                    run_start_index,
                    reason=protocol_outcome.terminal_reason or "forced_final_protocol_violation",
                    skip_memory_consolidation=True,
                    preserve_terminal_content=True,
                )
            if isinstance(artifact, ProviderProtocolArtifact):
                protocol_outcome = self._provider_terminal_phase.handle_protocol_violation(
                    phase="forced_final" if force_final_answer else "ordinary",
                    forced_final_kind=(forced_final_kind or "runtime_forced_final") if force_final_answer else None,
                    artifact_kind=artifact.kind,
                    artifact=artifact,
                    deadline=deadline,
                )
                if protocol_outcome.action == "retry":
                    step += 1
                    continue
                return self._finish_run(
                    protocol_outcome.terminal_message,
                    deadline,
                    run_start_index,
                    reason=protocol_outcome.terminal_reason or (
                        "forced_final_protocol_violation" if force_final_answer else "provider_protocol_violation"
                    ),
                    skip_memory_consolidation=True,
                    preserve_terminal_content=True,
                )
            if not raw_tool_calls:
                tool_choice_outcome = self._tool_choice_directive_phase.after_model_turn([])
                if tool_choice_outcome.kind != "none":
                    message = _provider_safe_assistant_message(raw_message)
                    if message.get("content") is None:
                        message = {**message, "content": ""}
                    self._messages.append(message)
                    self._session.append("assistant", message)
                    self._events.emit("AssistantMessage", _assistant_event_payload(message))
                    if tool_choice_outcome.kind == "force":
                        step += 1
                        continue
                    return self._finish_run(
                        tool_choice_outcome.terminal_message,
                        deadline,
                        run_start_index,
                        reason=tool_choice_outcome.terminal_reason,
                        skip_memory_consolidation=True,
                    )
                terminal_outcome = self._provider_terminal_phase.handle_no_tool_response(
                    raw_message.get("content"),
                    forced_final=force_final_answer,
                )
                if terminal_outcome.action == "retry":
                    step += 1
                    continue
                if terminal_outcome.action == "unverified":
                    return self._finish_run(
                        terminal_outcome.terminal_message,
                        deadline,
                        run_start_index,
                        reason=terminal_outcome.terminal_reason or "provider_non_substantive_response",
                        skip_memory_consolidation=True,
                        preserve_terminal_content=True,
                    )
            if force_final_answer:
                self._session.append("runtime_steering", {"kind": "forced_final_answer", "step": step})
                self._run.clear_forced_final_answer_request()
            message = _provider_safe_assistant_message(raw_message)
            self._messages.append(message)
            self._session.append("assistant", message)
            self._events.emit("AssistantMessage", _assistant_event_payload(message))

            tool_calls = message.get("tool_calls") or []
            tool_choice_outcome = self._tool_choice_directive_phase.after_model_turn(tool_calls)
            if tool_choice_outcome.kind != "none":
                if tool_choice_outcome.append_skipped_results:
                    self._append_synthetic_tool_results(
                        tool_calls,
                        tool_choice_outcome.skipped_message,
                        metadata=tool_choice_outcome.skipped_metadata,
                        count_as_error=False,
                    )
                if tool_choice_outcome.kind == "force":
                    step += 1
                    continue
                return self._finish_run(
                    tool_choice_outcome.terminal_message,
                    deadline,
                    run_start_index,
                    reason=tool_choice_outcome.terminal_reason,
                    skip_memory_consolidation=True,
                )
            if getattr(response, "finish_reason", None) == "length":
                self._append_synthetic_tool_results(tool_calls, self._length_stop_tool_message())
                return self._stop_for_length(deadline, run_start_index)
            if not tool_calls:
                if self._tool_directive_phase.after_model_turn():
                    step += 1
                    continue
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
                    if steering.terminal_message:
                        return self._finish_run(
                            steering.terminal_message,
                            deadline,
                            run_start_index,
                            reason="pre_review_audit_unverified",
                            skip_memory_consolidation=True,
                        )
                    if self._apply_final_answer_steering(steering):
                        step += 1
                        continue
                review_outcome = self._read_only_review_phase.review_candidate(content)
                if review_outcome.kind == "unverified":
                    return self._finish_run(
                        review_outcome.terminal_message,
                        deadline,
                        run_start_index,
                        reason="read_only_reviewer_unverified",
                        skip_memory_consolidation=True,
                    )
                if review_outcome.kind == "rewrite":
                    self._messages.append({"role": "user", "content": review_outcome.rewrite_message})
                    self._session.append("runtime_steering", {"kind": "read_only_reviewer", "action": "rewrite"})
                    self._run.force_final_answer_without_tools = True
                    step += 1
                    continue
                return self._finish_run(review_outcome.final_candidate or content, deadline, run_start_index)

            synthetic_paired_tool_ids: set[str] = set()
            for index, tool_call in enumerate(tool_calls):
                if tool_call.get("id") in synthetic_paired_tool_ids:
                    continue
                if self._deadline_exceeded(deadline):
                    self._append_synthetic_tool_results(tool_calls[index:], self._budget_stop_message())
                    return self._stop_for_budget(deadline, run_start_index)
                function = tool_call.get("function") or {}
                name = function.get("name") or ""
                arguments = function.get("arguments") or "{}"
                self._log_tool_start(name, arguments)
                guard_hits_before = self._session_guards.counts()
                directive_transition = self._tool_directive_phase.before_tool_attempt(name)
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
                self._evidence_phase.record_tool_choice_result(name, arguments, result)
                self._evidence_phase.record_successful_patch_preview(name, arguments, result)
                self._evidence_phase.record_read_file_evidence(name, arguments, result)
                self._evidence_phase.record_tool_evidence(name, arguments, result)
                self._evidence_phase.invalidate_stale_source_evidence_after_write(name, arguments, result)
                self._observe_soft_tool_requirement(name, arguments, result)
                if self._tool_directive_phase.after_tool_attempt(
                    directive_transition,
                    tool_name=name,
                    is_error=result.is_error,
                ):
                    self._append_synthetic_tool_results(
                        tool_calls[index + 1 :],
                        "Skipped because the bounded runtime evidence directive is exhausted.",
                    )
                    break
                explore_budget = self._read_only_explore_phase.after_tool_result(name)
                if explore_budget is not None:
                    remaining_calls = tool_calls[index + 1 :]
                    plan = self._read_only_explore_phase.plan_remaining_batch(explore_budget, remaining_calls)
                    if plan.suppress_calls:
                        synthetic_paired_tool_ids.update(
                            str(call.get("id"))
                            for call in plan.suppress_calls
                            if call.get("id") is not None
                        )
                        self._read_only_explore_phase.record_suppressed_calls(
                            len(plan.suppress_calls),
                            explore_budget,
                        )
                        self._append_synthetic_tool_results(
                            list(plan.suppress_calls),
                            self._read_only_explore_phase.suppression_message(explore_budget),
                            count_as_error=False,
                        )
                    if plan.continue_batch:
                        continue
                    break
                if name == "git_diff" and not result.is_error:
                    patch_review = self._decide_post_diff_patch_review(run_start_index)
                    self._evidence_phase.record_verification_patch_review(patch_review)
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
                    read_file_evidence=self._evidence_phase.read_file_evidence_summary(),
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
            self._tool_directive_phase.after_model_turn()
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
            schemas = [
                schema
                for schema in _registry_schemas_for_context(self._registry, self._tool_context)
                if schema.get("function", {}).get("name") not in denied_names
            ]
        else:
            schemas = [
                schema
                for schema in _registry_schemas_for_context(self._registry, self._tool_context)
                if schema.get("function", {}).get("name") in allowed_names
                and schema.get("function", {}).get("name") not in denied_names
            ]
        return self._tool_choice_directive_phase.project_schemas(schemas)

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
        if self._run.requirement_contract is not None and self._run.requirement_contract.inspection_forbidden:
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

    def _available_registry_tool_names(self) -> tuple[str, ...]:
        denied_names = self._denied_model_tool_names()
        if hasattr(self._registry, "exposed_tool_names"):
            return tuple(name for name in self._registry.exposed_tool_names(self._tool_context) if name not in denied_names)
        names: list[str] = []
        for schema in _registry_schemas_for_context(self._registry, self._tool_context):
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
            self._run.workflow_profile_status(),
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
        return self._workspace_phase.add_workspace_root(raw_path)

    def remove_workspace_root(self, raw_path: str) -> Path:
        return self._workspace_phase.remove_workspace_root(raw_path)

    def reset_workspace_roots(self) -> None:
        self._workspace_phase.reset_workspace_roots()

    def move_workspace(self, raw_path: str) -> Path:
        return self._workspace_phase.move_workspace(raw_path)

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
            workflow_profile=self._run.workflow_profile_payload(),
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

    def _record_synthetic_tool_result_for_run(self, *, count_as_error: bool = True) -> None:
        self._run.collector.record_synthetic_tool_result(is_error=count_as_error)

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
                "negative_existence": self._run.final_answer_steers.get("negative_existence", 0),
                "patch_reviewer": self._run.final_answer_steers.get("patch_reviewer", 0),
                "completion_audit": self._run.final_answer_steers.get("completion_audit", 0),
                "soft_tool_requirement": self._run.soft_tool_requirement.steers if self._run.soft_tool_requirement else 0,
            },
        )
        payload["finalization_attempts"] = self._run.finalization.aggregate_attempts
        payload["provider_terminal"] = self._run.finalization.terminal_response_snapshot()
        payload["temporary_tool_directive"] = self._run.temporary_tool_directive.snapshot()
        if self._run.negative_claim_metrics:
            payload["negative_evidence_claims"] = dict(self._run.negative_claim_metrics)
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
        draft = _most_recent_terminal_assistant_content(self._run.current_run_messages(self._messages))
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
        if allowed_tools is not None or scoped_read_paths is not None:
            tool_context = replace(
                tool_context,
                runtime_tool_allowlist=frozenset(allowed_tools) if allowed_tools is not None else None,
                runtime_read_file_paths=scoped_read_paths,
                runtime_read_file_remaining=self._run.tool_choice_read_file_remaining,
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
                    evidence=self._evidence_phase.evidence_for_read_file_range(read_file_range_key),
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
        self._tool_directive_phase.apply_steering(decision.kind, decision.temporary_tool_allowlist)

    def _tool_loop_stop_message(self, kind: str) -> str:
        return (
            f"Remaining tool calls were not executed because runtime steering '{kind}' requires the assistant "
            "to use the collected evidence and answer the user's original request."
        )

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
        pending_preparation = self._read_only_review_phase.owns_pending_candidate_validation()
        preparation_audit = self._read_only_review_phase.refresh_preparation_audit(
            context,
            self._final_answer_steerers,
        )
        if preparation_audit is not None or pending_preparation:
            return None
        claim_metrics = _negative_claim_metrics(content, context.tool_results)
        if any(claim_metrics.values()):
            self._run.record_negative_claim_metrics(claim_metrics)
            self._session.append("negative_evidence_claims", claim_metrics)
        for steerer in self._final_answer_steerers:
            if (
                self._read_only_review_phase.owns_initial_pre_review_audits()
                and steerer.kind in PRE_REVIEW_AUDIT_KINDS
                and should_aggregate_pre_review_audits(context)
            ):
                continue
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
            messages=self._run.current_run_messages(self._messages),
            run_start_index=0,
            requirement_contract=self._run.requirement_contract,
            tool_results=list(self._run.tool_choice_results),
            read_file_evidence_paths=list(self._run.evidence.read_file_paths),
            source_evidence=list(self._run.evidence.source_evidence),
            requirement_evidence=list(self._run.evidence.pinned_requirement_evidence),
            required_design_evidence_roots=self._run.active_design_evidence_roots(),
            design_evidence_read_paths=list(self._run.evidence.design_read_paths),
            open_todos=self._provider_context_phase.open_todo_summary(),
            is_code_implementation_request=requires_no_edit_final_hygiene(self._run.requirement_contract),
            steer_counts=self._final_answer_steer_counts(),
            verification_plan=self._run.verification_plan,
            read_only_explore_finalized=self._run.read_only_explore_finalized,
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
        counted_kinds = decision.counted_kinds or (decision.kind,)
        steer_counts = {
            kind: self._increment_final_answer_steer_count(kind)
            for kind in counted_kinds
        }
        self._messages.append({"role": "user", "content": decision.message})
        payload = {
            "kind": decision.kind,
            **decision.payload,
            "steer_count": max(steer_counts.values(), default=0),
            "steer_counts": steer_counts,
        }
        self._session.append("runtime_steering", payload)
        if decision.force_final_answer_without_tools:
            self._run.force_final_answer_without_tools = True
        else:
            self._run.clear_forced_final_answer_request()
        self._tool_directive_phase.apply_steering(decision.kind, decision.temporary_tool_allowlist)
        return True

    def _final_answer_rewrite_skip_reason(self) -> str | None:
        return self._run.finalization_rewrite_skip_reason()

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
        preserve_terminal_content: bool = False,
    ) -> str:
        self._tool_directive_phase.close_terminal(reason)
        incomplete_delivery: str | None = None
        hard_gate = self._run.unresolved_final_answer_gate
        if hard_gate is not None and not preserve_terminal_content:
            content = render_unverified_final_answer(hard_gate.kind, hard_gate.reason)
            if reason == "final":
                reason = "unverified_final_gate"
        elif reason == "final" and workspace_write_happened(self._run.tool_choice_results):
            incomplete_delivery = self._run.verification_plan.render_incomplete_terminal()
        if reason != "final" and not preserve_terminal_content:
            safe_partial = self._read_only_review_phase.safe_partial_for_terminal(reason)
            if safe_partial:
                content = safe_partial
        self._session_evidence.remember_request(self._run.current_user_request or "", self._run.run_id)
        if incomplete_delivery:
            content = incomplete_delivery
            reason = "incomplete_delivery"
        delivery_report = render_delivery_report(self._run.verification_plan, self._run.tool_choice_results)
        if delivery_report:
            content = f"{content.rstrip()}\n\n{delivery_report}"
        self._session.append("final", {"content": content})
        run_messages = self._run.current_run_messages(self._messages)
        if skip_memory_consolidation:
            self._session.append(
                "memory_consolidation",
                {"mode": self._config.memory_consolidation, "status": "skipped", "reason": reason},
            )
        else:
            self._memory_phase.consolidate_session_memory(run_messages, content, deadline)
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

    def _stop_for_budget(self, deadline: float | None, run_start_index: int) -> str:
        content = self._budget_stop_message()
        return self._finish_run(content, deadline, run_start_index, reason="budget")

    def _stop_for_interrupt(self) -> str:
        content = "Stopped after user interrupt."
        self._tool_directive_phase.close_terminal("interrupt")
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
        canonical_path: str | None = None
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
                canonical_path = str(resolved)
                metadata.setdefault("resolved_path", canonical_path)
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
        metadata.setdefault(
            "session_evidence_query_identity",
            _session_evidence_query_identity(name, arguments, canonical_path=canonical_path),
        )
        if name == "glob_files":
            searched_roots = metadata.get("searched_roots")
            root_values = [str(root).strip() for root in searched_roots if str(root).strip()] if isinstance(searched_roots, list) else []
            if len(root_values) == 1:
                root = Path(root_values[0]).resolve()
                metadata.setdefault("evidence_root", str(root))
                metadata.setdefault(
                    "evidence_root_label",
                    evidence_root_label(root, self._workspace_context.primary, self._workspace_context.additional_roots),
                )
            elif len(root_values) > 1:
                metadata.setdefault("evidence_scope", "multi_root")
            metadata.setdefault("evidence_scope", "root_discovery")
        elif name == "git_status":
            # Git is intentionally anchored to the active primary workspace.
            metadata.setdefault("evidence_root", str(self._workspace_context.primary))
            metadata.setdefault("evidence_root_label", "primary")
            metadata.setdefault("evidence_scope", "root_local")
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
        return self._run.queue_finalization_rewrite(
            kind=kind,
            severity=severity,
        )

    def _append_synthetic_tool_results(
        self,
        tool_calls: list[dict[str, Any]],
        content: str,
        *,
        is_error: bool = True,
        count_as_error: bool | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if count_as_error is None:
            count_as_error = is_error
        for tool_call in tool_calls:
            function = tool_call.get("function") or {}
            name = function.get("name") or ""
            result = f"Tool call was not executed because {content}"
            self._record_synthetic_tool_result_for_run(count_as_error=count_as_error)
            self._append_tool_result(tool_call, name, result, is_error=is_error, metadata=metadata)

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
        self._run.collector.record_event(event_type, payload)
        self._events.emit(event_type, payload)


def _registry_schemas_for_context(registry: Any, context: ToolContext) -> list[dict[str, Any]]:
    """Return context-aware model schemas while tolerating narrow test registries."""

    model_schemas = getattr(registry, "model_schemas", None)
    if callable(model_schemas):
        return model_schemas(context)
    return registry.schemas()

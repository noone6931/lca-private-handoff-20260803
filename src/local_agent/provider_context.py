"""Provider-facing context, compaction, and summary phase owner."""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

from .chat_runtime import call_chat_with_timeout
from .compaction import SUMMARY_INPUT_CHAR_LIMIT, SUMMARY_OUTPUT_CHAR_LIMIT, SUMMARY_REQUEST_TIMEOUT
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
from .llm import LlmError
from .memory_consolidation import _messages_to_memory_transcript
from .path_rules import candidate_paths_for_path_rules, matching_path_rule_context, render_path_rule_metadata
from .planner import render_planner_explore_context
from .requirement_evidence import render_pinned_requirement_evidence
from .runtime_prompt import _latest_user_content, _messages_with_runtime_context
from .tools.base import tool_state_dir


class ProviderRuntimePort(Protocol):
    """Explicit runtime services consumed by provider-context composition."""

    _base_system_prompt: str
    _client: Any
    _config: Any
    _evidence_phase: Any
    _messages: list[dict[str, Any]]
    _path_rule_index: Any
    _run: Any
    _session: Any
    _state_dir: Any
    _summary_cache: dict[str, str]
    _tool_context: Any
    _user_config_dir: Any
    _user_facts: Any
    _workspace_context: Any

    def _deadline_exceeded(self, deadline: float | None) -> bool: ...
    def _record_context_compaction(self, **payload: Any) -> None: ...
    def _record_llm_context_summary(self) -> None: ...
    def _record_local_context_summary(self) -> None: ...


class ProviderContextPhase:
    """Explicit provider-context component used by AgentRuntime turn orchestration."""

    def __init__(self, runtime: ProviderRuntimePort) -> None:
        self._runtime = runtime

    def messages_for_model(self, deadline: float | None = None) -> list[dict[str, Any]]:
        runtime = self._runtime
        todo_summary = self.open_todo_summary()
        provider_context = _prune_context_tool_outputs(runtime._messages)
        if not self.context_budget_enabled():
            return self.provider_safe_runtime_messages(provider_context, todo_summary)
        thresholds = self.context_budget_thresholds()
        if not self.context_budget_exceeded(provider_context):
            return self.provider_safe_runtime_messages(provider_context, todo_summary)

        estimated_tokens_before = _estimate_message_tokens(provider_context)

        system_messages = [message for message in provider_context if message.get("role") == "system"]
        non_system = [message for message in provider_context if message.get("role") != "system"]
        latest_user_index = _last_user_message_index(non_system)
        recent_count = min(runtime._config.context_recent_messages, len(non_system))
        current_user_request = _latest_user_content(non_system)

        while recent_count > 0:
            recent, dropped = _compaction_recent_messages(
                non_system,
                latest_user_index=latest_user_index,
                recent_count=recent_count,
            )
            recent = _truncate_recent_tool_outputs(recent)
            compaction_summary = self.build_compaction_summary(
                dropped,
                current_user_request,
                deadline,
                prefer_local=runtime._run.force_final_answer_without_tools,
            )
            compacted = _messages_with_compaction_context(
                system_messages,
                compaction_summary,
                recent,
                default_system_content=runtime._base_system_prompt,
            )
            still_exceeds_budget = self.context_budget_exceeded(compacted)
            if not still_exceeds_budget or recent_count <= 6:
                payload: dict[str, Any] = {
                    "original_messages": len(runtime._messages),
                    "sent_messages": len(compacted),
                    "dropped_messages": len(dropped),
                    "estimated_chars": _estimate_message_chars(compacted),
                    "estimated_tokens_before": estimated_tokens_before,
                    "estimated_tokens_after": _estimate_message_tokens(compacted),
                    "required_user_message_retained": latest_user_index is not None,
                    "required_recent_messages": 1 if latest_user_index is not None else 0,
                }
                if still_exceeds_budget:
                    payload["budget_exceeded_after_required_retention"] = True
                payload["estimated_tokens"] = payload["estimated_tokens_after"]
                payload.update(thresholds)
                runtime._session.append("context_compaction", payload)
                runtime._record_context_compaction(
                    estimated_tokens_before=estimated_tokens_before,
                    estimated_tokens_after=int(payload["estimated_tokens_after"]),
                )
                return self.provider_safe_runtime_messages(compacted, todo_summary)
            recent_count = max(6, recent_count // 2)
        return self.provider_safe_runtime_messages(runtime._messages, todo_summary)

    def context_budget_enabled(self) -> bool:
        runtime = self._runtime
        return runtime._config.context_char_budget > 0 or runtime._config.context_token_budget > 0

    def context_budget_thresholds(self) -> dict[str, int]:
        runtime = self._runtime
        thresholds: dict[str, int] = {}
        if runtime._config.context_char_budget > 0:
            thresholds["threshold_chars"] = _resolve_compaction_threshold_chars(runtime._config.context_char_budget)
        if runtime._config.context_token_budget > 0:
            thresholds["threshold_tokens"] = _resolve_compaction_threshold_tokens(runtime._config.context_token_budget)
        return thresholds

    def context_budget_exceeded(self, messages: list[dict[str, Any]]) -> bool:
        runtime = self._runtime
        if runtime._config.context_token_budget > 0:
            threshold = _resolve_compaction_threshold_tokens(runtime._config.context_token_budget)
            if _estimate_message_tokens(messages) > threshold:
                return True
        if runtime._config.context_char_budget > 0:
            threshold = _resolve_compaction_threshold_chars(runtime._config.context_char_budget)
            if _estimate_message_chars(messages) > threshold:
                return True
        return False

    def provider_safe_runtime_messages(
        self,
        messages: list[dict[str, Any]],
        todo_summary: list[str],
    ) -> list[dict[str, Any]]:
        runtime = self._runtime
        evidence_ledger = runtime._evidence_phase.evidence_ledger_summary()
        planner_explore_context = render_planner_explore_context(
            runtime._run.requirement_contract,
            prompt=runtime._run.current_user_request,
            tool_results=list(runtime._run.tool_choice_results),
        )
        verification_plan_context = runtime._run.verification_plan.render_context()
        if runtime._run.verification_test_plan is not None:
            test_plan = runtime._run.verification_test_plan
            verification_plan_context = (
                verification_plan_context
                + f"\n- Test candidate ({test_plan.breadth}): {test_plan.command or '(none)'} ({test_plan.reason})"
            )
        path_rule_candidates = candidate_paths_for_path_rules(
            runtime._run.current_user_request or "",
            (result.path for result in runtime._run.tool_choice_results if result.path),
            primary_workspace=runtime._workspace_context.primary,
        )
        path_rule_metadata = render_path_rule_metadata(runtime._path_rule_index)
        matched_path_rules = matching_path_rule_context(runtime._path_rule_index, path_rule_candidates)
        retained_user_contents = [
            str(message.get("content") or "")
            for message in messages
            if message.get("role") == "user"
        ]
        prior_user_context = runtime._user_facts.render_relevant_prior_user_context(
            runtime._run.current_user_request or "",
            retained_user_contents=retained_user_contents,
        )
        return _provider_safe_messages(
            _messages_with_runtime_context(
                messages,
                todo_summary,
                evidence_ledger,
                planner_explore_context,
                runtime._workspace_context.primary,
                runtime._user_config_dir,
                runtime._workspace_context.additional_roots,
                runtime._run.current_user_request,
                runtime._run.requirement_contract,
                runtime._run.requirement_contract_context,
                render_pinned_requirement_evidence(runtime._run.evidence.pinned_requirement_evidence),
                runtime._run.user_facts_context,
                prior_user_context,
                path_rule_metadata,
                matched_path_rules,
                verification_plan_context,
            )
        )

    def build_compaction_summary(
        self,
        dropped: list[dict[str, Any]],
        current_user_request: str | None,
        deadline: float | None,
        *,
        prefer_local: bool = False,
    ) -> str:
        runtime = self._runtime
        todo_summary = self.open_todo_summary()
        if not prefer_local and runtime._config.summary_mode in {"auto", "llm"}:
            llm_summary = self.llm_compaction_summary(dropped, current_user_request, todo_summary, deadline)
            if llm_summary:
                return llm_summary
        return self.local_compaction_summary(dropped, current_user_request, todo_summary)

    def local_compaction_summary(
        self,
        dropped: list[dict[str, Any]],
        current_user_request: str | None,
        todo_summary: list[str],
    ) -> str:
        runtime = self._runtime
        runtime._record_local_context_summary()
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

    def llm_compaction_summary(
        self,
        dropped: list[dict[str, Any]],
        current_user_request: str | None,
        todo_summary: list[str],
        deadline: float | None,
    ) -> str | None:
        runtime = self._runtime
        if not dropped or runtime._deadline_exceeded(deadline):
            return None
        transcript = _messages_to_summary_transcript(dropped, max_chars=SUMMARY_INPUT_CHAR_LIMIT)
        if not transcript.strip():
            return None
        cache_key = _summary_cache_key(transcript, current_user_request, todo_summary)
        cached = runtime._summary_cache.get(cache_key)
        if cached:
            return cached

        remaining_timeout = self.remaining_timeout(deadline)
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
            response = call_chat_with_timeout(runtime._client, messages, [], timeout=timeout)
        except LlmError as exc:
            runtime._session.append("context_summary_error", {"mode": "llm", "error": str(exc)})
            return None
        content = response.message.get("content")
        if not isinstance(content, str) or not content.strip():
            runtime._session.append("context_summary_error", {"mode": "llm", "error": "empty summary"})
            return None
        summary = _format_llm_compaction_summary(
            content.strip()[:SUMMARY_OUTPUT_CHAR_LIMIT],
            current_user_request,
            todo_summary,
        )
        runtime._summary_cache[cache_key] = summary
        runtime._record_llm_context_summary()
        runtime._session.append(
            "context_summary",
            {
                "mode": "llm",
                "input_chars": len(transcript),
                "summary_chars": len(summary),
            },
        )
        return summary

    def open_todo_summary(self) -> list[str]:
        runtime = self._runtime
        path = tool_state_dir(runtime._tool_context) / "todos" / f"{runtime._session.session_id}.json"
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

    def remaining_timeout(self, deadline: float | None) -> float:
        runtime = self._runtime
        if deadline is None:
            return float(runtime._config.request_timeout)
        remaining = deadline - time.monotonic()
        return min(float(runtime._config.request_timeout), max(1.0, remaining))

"""Long-term memory consolidation phase."""
from __future__ import annotations

import time
from typing import Any, Protocol

from ..providers.deadline import call_chat_with_timeout
from ..providers.llm import LlmError
from ..memory.consolidation import MEMORY_CONSOLIDATION_INPUT_CHAR_LIMIT, MEMORY_CONSOLIDATION_OUTPUT_CHAR_LIMIT
from ..memory.consolidation import _append_consolidated_memory, _memory_consolidation_root, _messages_to_memory_transcript, _parse_memory_consolidation_response, _should_auto_consolidate_memory, _run_used_memory_write_tool

MEMORY_CONSOLIDATION_REQUEST_TIMEOUT = 30.0


class MemoryRuntimePort(Protocol):
    """Explicit memory-consolidation dependencies supplied by AgentRuntime."""

    _client: Any
    _config: Any
    _session: Any
    _state_dir: Any
    _tool_context: Any
    _workspace_context: Any

    def _deadline_exceeded(self, deadline: float | None) -> bool: ...

class MemoryConsolidationLifecycle:
    """Cohesive Runtime phase kept outside the turn orchestrator."""

    def __init__(self, runtime: MemoryRuntimePort) -> None:
        self._runtime = runtime

    def consolidate_session_memory(
        self,
        run_messages: list[dict[str, Any]],
        final_content: str,
        deadline: float | None,
    ) -> None:
        runtime = self._runtime
        mode = runtime._config.memory_consolidation
        if mode == "off":
            return
        if runtime._deadline_exceeded(deadline):
            runtime._session.append("memory_consolidation", {"mode": mode, "status": "skipped", "reason": "deadline"})
            return
        if _run_used_memory_write_tool(run_messages):
            runtime._session.append(
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
            runtime._session.append(
                "memory_consolidation",
                {"mode": mode, "status": "skipped", "reason": "no durable signal"},
            )
            return
        extracted = self.llm_memory_consolidation(transcript, deadline)
        if not extracted:
            return
        memory_root = _memory_consolidation_root(
            runtime._workspace_context.primary,
            runtime._state_dir,
            runtime._config.memory_scope,
        )
        written = _append_consolidated_memory(memory_root, runtime._session.session_id, extracted)
        runtime._session.append(
            "memory_consolidation",
            {
                "mode": mode,
                "scope": runtime._config.memory_scope,
                "memory_root": str(memory_root),
                "status": "written" if written else "empty",
                "written": written,
            },
        )

    def llm_memory_consolidation(self, transcript: str, deadline: float | None) -> dict[str, list[str]] | None:
        runtime = self._runtime
        if deadline is None:
            remaining_timeout = float(runtime._config.request_timeout)
        else:
            remaining_timeout = deadline - time.monotonic()
        timeout = min(float(runtime._config.request_timeout), remaining_timeout, MEMORY_CONSOLIDATION_REQUEST_TIMEOUT)
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
            response = call_chat_with_timeout(
                runtime._client,
                messages,
                [],
                timeout=timeout,
                cancel_event=runtime._tool_context.cancel_event,
            )
        except LlmError as exc:
            runtime._session.append("memory_consolidation_error", {"mode": "llm", "error": str(exc)})
            return None
        content = response.message.get("content")
        if not isinstance(content, str) or not content.strip():
            runtime._session.append("memory_consolidation_error", {"mode": "llm", "error": "empty response"})
            return None
        parsed = _parse_memory_consolidation_response(content[:MEMORY_CONSOLIDATION_OUTPUT_CHAR_LIMIT])
        if parsed is None:
            runtime._session.append("memory_consolidation_error", {"mode": "llm", "error": "invalid JSON response"})
            return None
        return parsed

from __future__ import annotations

from dataclasses import replace
import sys
import time
from typing import Any

from .config import AgentConfig
from .llm import OpenAICompatibleClient
from .session.jsonl_store import JsonlSessionStore
from .tools import create_default_registry
from .tools.base import ToolContext


SYSTEM_PROMPT = """You are a local coding agent running inside a user's workspace.

Work carefully and prefer local evidence over guesses.
Use tools to inspect files before editing them.
For multi-step tasks, maintain a concise todo list.
If a requirement is ambiguous and guessing would affect the result, use ask_user.
When editing, prefer apply_patch with the hash tag returned by read_file.
For insertions, use apply_patch with mode=insert_before or mode=insert_after instead of empty replacements.
Do not claim a command or test passed unless you ran it.
Keep final answers concise and include changed files and verification.
"""


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
        self._session = JsonlSessionStore(
            config.workspace,
            session_id=session_id,
            continue_recent=continue_session,
        )
        self._messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *self._session.load_messages(),
        ]
        self._tool_context = ToolContext(
            workspace=config.workspace,
            approval_mode=config.approval_mode,
            session_id=self._session.session_id,
            auto_approve_tools=config.auto_approve_tools,
        )
        if self._show_tool_logs:
            print(f"[session] {self._session.session_id}", file=sys.stderr)

    def run(self, prompt: str) -> str:
        deadline = (
            time.monotonic() + self._config.budget_seconds
            if self._config.budget_seconds is not None
            else None
        )
        self._messages.append({"role": "user", "content": prompt})
        self._session.append("user", {"content": prompt})
        tool_context = replace(self._tool_context, deadline_monotonic=deadline)

        step = 1
        while self._config.max_steps == 0 or step <= self._config.max_steps:
            if self._deadline_exceeded(deadline):
                return self._stop_for_budget()

            self._session.append("llm_request", {"step": step})
            response = self._client.chat(
                self._messages,
                self._registry.schemas(),
                timeout=self._remaining_timeout(deadline),
            )
            message = {**response.message, "role": "assistant"}
            self._messages.append(message)
            self._session.append("assistant", message)

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                content = message.get("content") or ""
                self._session.append("final", {"content": content})
                return content

            for index, tool_call in enumerate(tool_calls):
                if self._deadline_exceeded(deadline):
                    self._append_synthetic_tool_results(tool_calls[index:], self._budget_stop_message())
                    return self._stop_for_budget()
                function = tool_call.get("function") or {}
                name = function.get("name") or ""
                arguments = function.get("arguments") or "{}"
                self._log_tool_start(name, arguments)
                result = self._registry.execute(name, arguments, tool_context)
                self._log_tool_end(name, result.is_error, len(result.content))
                self._append_tool_result(tool_call, name, result.content, is_error=result.is_error)
                if self._deadline_exceeded(deadline):
                    self._append_synthetic_tool_results(tool_calls[index + 1 :], self._budget_stop_message())
                    return self._stop_for_budget()
            step += 1

        return f"Stopped after reaching max_steps={self._config.max_steps}."

    def _remaining_timeout(self, deadline: float | None) -> float:
        if deadline is None:
            return float(self._config.request_timeout)
        remaining = deadline - time.monotonic()
        return min(float(self._config.request_timeout), max(1.0, remaining))

    def _deadline_exceeded(self, deadline: float | None) -> bool:
        return deadline is not None and time.monotonic() >= deadline

    def _budget_stop_message(self) -> str:
        return f"Stopped after reaching budget_seconds={self._config.budget_seconds}."

    def _stop_for_budget(self) -> str:
        content = self._budget_stop_message()
        self._session.append("final", {"content": content})
        return content

    def _append_tool_result(self, tool_call: dict[str, Any], name: str, content: str, *, is_error: bool) -> None:
        self._session.append(
            "tool_result",
            {
                "tool_call_id": tool_call.get("id"),
                "name": name,
                "is_error": is_error,
                "content": content,
            },
        )
        self._messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.get("id"),
                "content": content,
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

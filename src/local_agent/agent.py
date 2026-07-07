from __future__ import annotations

import sys
from typing import Any

from .config import AgentConfig
from .llm import OpenAICompatibleClient
from .session.jsonl_store import JsonlSessionStore
from .tools import create_default_registry
from .tools.base import ToolContext


SYSTEM_PROMPT = """You are a local coding agent running inside a user's workspace.

Work carefully and prefer local evidence over guesses.
Use tools to inspect files before editing them.
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
        )
        if self._show_tool_logs:
            print(f"[session] {self._session.session_id}", file=sys.stderr)

    def run(self, prompt: str) -> str:
        self._messages.append({"role": "user", "content": prompt})
        self._session.append("user", {"content": prompt})

        for step in range(1, self._config.max_steps + 1):
            self._session.append("llm_request", {"step": step})
            response = self._client.chat(self._messages, self._registry.schemas())
            message = {**response.message, "role": "assistant"}
            self._messages.append(message)
            self._session.append("assistant", message)

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                content = message.get("content") or ""
                self._session.append("final", {"content": content})
                return content

            for tool_call in tool_calls:
                function = tool_call.get("function") or {}
                name = function.get("name") or ""
                arguments = function.get("arguments") or "{}"
                self._log_tool_start(name, arguments)
                result = self._registry.execute(name, arguments, self._tool_context)
                self._log_tool_end(name, result.is_error, len(result.content))
                self._session.append(
                    "tool_result",
                    {
                        "tool_call_id": tool_call.get("id"),
                        "name": name,
                        "is_error": result.is_error,
                        "content": result.content,
                    },
                )
                self._messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "content": result.content,
                    }
                )

        return f"Stopped after reaching max_steps={self._config.max_steps}."

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

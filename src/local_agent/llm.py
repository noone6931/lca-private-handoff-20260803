from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import AgentConfig
from .provider_protocol import ProviderProtocolArtifact
from .provider_protocol import classify_provider_content_artifact


class LlmError(RuntimeError):
    """Raised when the LLM API request fails."""


class LlmTimeoutError(LlmError):
    """Raised when a provider request exceeds its configured deadline."""


@dataclass(frozen=True)
class ChatResponse:
    message: dict[str, Any]
    finish_reason: str | None = None
    protocol_artifact: ProviderProtocolArtifact | None = None


class OpenAICompatibleClient:
    def __init__(self, config: AgentConfig):
        self._config = config

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        timeout: float | None = None,
    ) -> ChatResponse:
        url = f"{self._config.api_base_url}/chat/completions"
        payload = {
            "model": self._config.model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        request_timeout = timeout if timeout is not None else self._config.request_timeout
        try:
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LlmError(f"LLM API returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise LlmTimeoutError(f"LLM API request timed out after {request_timeout} seconds.") from exc
            raise LlmError(f"LLM API request failed: {exc}") from exc
        except TimeoutError as exc:
            raise LlmTimeoutError(f"LLM API request timed out after {request_timeout} seconds.") from exc

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            preview = body[:500] + ("...<truncated>" if len(body) > 500 else "")
            raise LlmError(f"LLM API returned non-JSON response: {preview}") from exc
        choices = data.get("choices")
        if not choices:
            raise LlmError(f"LLM API returned no choices: {data}")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise LlmError(f"LLM API returned malformed message: {data}")
        finish_reason = choice.get("finish_reason")
        return ChatResponse(
            message=message,
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            protocol_artifact=classify_provider_content_artifact(self._config.provider, message.get("content")),
        )

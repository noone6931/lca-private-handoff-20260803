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
from .provider_protocol import normalize_provider_dialect_message


VISION_OBSERVATION_SYSTEM_PROMPT = """You are an evidence-first visual observation assistant.
The user question is only a focus area; it is not permission to infer business rules, lifecycle, precedence, legal effect, author intent, or workflow stage.
Return only a compact JSON object with exactly these keys:
- "observations": array of directly visible text, layout, marks, fields, and clearly readable values.
- "uncertainties": array of unclear, unreadable, occluded, or low-confidence visible details.
- "inferences": array of any model inference requested by the question; leave empty when not directly supported.
Do not put inferred content in observations. Do not fabricate unreadable or occluded details. Do not correct OCR text unless the visual evidence clearly shows the correction."""


class LlmError(RuntimeError):
    """Raised when the LLM API request fails."""


class LlmTimeoutError(LlmError):
    """Raised when a provider request exceeds its configured deadline."""


@dataclass(frozen=True)
class ChatResponse:
    message: dict[str, Any]
    finish_reason: str | None = None
    protocol_artifact: ProviderProtocolArtifact | None = None
    protocol_normalizations: tuple[ProviderProtocolArtifact, ...] = ()


class OpenAICompatibleClient:
    def __init__(self, config: AgentConfig):
        self._config = config

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        timeout: float | None = None,
        model: str | None = None,
        tool_choice: dict[str, Any] | str | None = None,
    ) -> ChatResponse:
        return self._complete(messages, tools, timeout=timeout, model=model, tool_choice=tool_choice)

    def inspect_image(
        self,
        *,
        image_base64: str,
        mime_type: str,
        question: str,
        timeout: float | None = None,
    ) -> str:
        model = self._config.vision_model.strip()
        if not model:
            raise LlmError("No explicit vision model is configured for inspect_image. Set AI_VISION_MODEL to a vision-capable model.")
        response = self._complete(
            [
                {"role": "system", "content": VISION_OBSERVATION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Focus question: {question}\n"
                                "Return only the JSON object required by the system instructions. "
                                "Keep direct observations separate from uncertainties and inferences."
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_base64}"}},
                    ],
                }
            ],
            [],
            timeout=timeout,
            model=model,
        )
        content = response.message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise LlmError("Vision model returned no text observation.")
        return content.strip()

    def _complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        timeout: float | None = None,
        model: str | None = None,
        tool_choice: dict[str, Any] | str | None = None,
    ) -> ChatResponse:
        url = f"{self._config.api_base_url}/chat/completions"
        payload = {
            "model": model or self._config.model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
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
        normalized_message, normalizations = normalize_provider_dialect_message(
            message,
            provider=self._config.provider,
        )
        return ChatResponse(
            message=normalized_message,
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            protocol_artifact=classify_provider_content_artifact(self._config.provider, message.get("content")),
            protocol_normalizations=normalizations,
        )

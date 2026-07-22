from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Generator

from ..config import AgentConfig
from .protocol import ProviderProtocolArtifact
from .protocol import classify_provider_content_artifact
from .protocol import normalize_provider_dialect_message
from .stream import ProviderStreamError, ProviderTextDelta, RawChatCompletion
from .stream import iter_chat_completion_response
from .web_search import BailianWebSearchBackend


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
        self.web_search_backend = BailianWebSearchBackend(config) if config.enable_web_search else None

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

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        timeout: float | None = None,
        model: str | None = None,
        tool_choice: dict[str, Any] | str | None = None,
    ) -> Generator[ProviderTextDelta, None, ChatResponse]:
        """Stream only visible text deltas; return one complete normalized response."""

        request, request_timeout = self._request(
            messages,
            tools,
            timeout=timeout,
            model=model,
            tool_choice=tool_choice,
            stream=True,
        )
        try:
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                raw = yield from iter_chat_completion_response(response)
        except ProviderStreamError as exc:
            raise LlmError(f"LLM API returned an invalid stream: {exc}") from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LlmError(f"LLM API returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise LlmTimeoutError(f"LLM API request timed out after {request_timeout} seconds.") from exc
            raise LlmError(f"LLM API request failed: {exc}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise LlmTimeoutError(f"LLM API request timed out after {request_timeout} seconds.") from exc
        except OSError as exc:
            raise LlmError(f"LLM API response stream failed: {exc}") from exc
        return self._response_from_raw(raw)

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
        request, request_timeout = self._request(
            messages,
            tools,
            timeout=timeout,
            model=model,
            tool_choice=tool_choice,
            stream=False,
        )
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
        except (TimeoutError, socket.timeout) as exc:
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
        return self._response_from_raw(
            RawChatCompletion(
                message=message,
                finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            )
        )

    def _request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        timeout: float | None,
        model: str | None,
        tool_choice: dict[str, Any] | str | None,
        stream: bool,
    ) -> tuple[urllib.request.Request, float]:
        payload: dict[str, Any] = {
            "model": model or self._config.model,
            "messages": messages,
        }
        if stream:
            payload["stream"] = True
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        request = urllib.request.Request(
            f"{self._config.api_base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        return request, timeout if timeout is not None else self._config.request_timeout

    def _response_from_raw(self, raw: RawChatCompletion) -> ChatResponse:
        message = raw.message
        normalized_message, normalizations = normalize_provider_dialect_message(
            message,
            provider=self._config.provider,
        )
        return ChatResponse(
            message=normalized_message,
            finish_reason=raw.finish_reason,
            protocol_artifact=classify_provider_content_artifact(self._config.provider, message.get("content")),
            protocol_normalizations=normalizations,
        )

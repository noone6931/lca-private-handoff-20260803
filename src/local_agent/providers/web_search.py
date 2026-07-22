from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from ..config import AgentConfig


MAX_PROVIDER_RESPONSE_BYTES = 1_000_000
MAX_ANSWER_CHARS = 8_000
MAX_SOURCE_TITLE_CHARS = 300
MAX_SOURCE_URL_CHARS = 2_048
MAX_SOURCE_COUNT = 20
MAX_REQUEST_ID_CHARS = 200
MAX_ERROR_CHARS = 500
SUPPORTED_PROVIDERS = frozenset({"bailian", "bailian-intl"})


class WebSearchError(RuntimeError):
    """Raised when sourced provider search cannot be completed truthfully."""


class WebSearchTimeoutError(WebSearchError):
    """Raised when provider search exceeds its request deadline."""


@dataclass(frozen=True)
class WebSearchSource:
    title: str
    url: str


@dataclass(frozen=True)
class WebSearchResponse:
    answer: str
    answer_truncated: bool
    sources: tuple[WebSearchSource, ...]
    provider_source_count: int
    request_id: str


class BailianWebSearchBackend:
    """One native DashScope web-search request with typed source provenance."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        urlopen: Callable[..., Any] | None = None,
    ) -> None:
        if config.provider not in SUPPORTED_PROVIDERS:
            raise WebSearchError("web_search requires a Bailian provider preset.")
        self.provider = config.provider
        self.model = config.web_search_model
        self.request_timeout = float(config.request_timeout)
        self._api_key = config.api_key
        self._endpoint = dashscope_generation_endpoint(config.api_base_url)
        self._urlopen = urlopen or urllib.request.urlopen

    def search(self, query: str, *, timeout: float) -> WebSearchResponse:
        payload = {
            "model": self.model,
            "input": {"messages": [{"role": "user", "content": query}]},
            "parameters": {
                "enable_search": True,
                "search_options": {"search_strategy": "agent", "enable_source": True},
                "result_format": "message",
                "incremental_output": True,
            },
        }
        request = urllib.request.Request(
            self._endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "X-DashScope-SSE": "enable",
            },
            method="POST",
        )
        try:
            with self._urlopen(request, timeout=timeout) as response:
                parsed = _parse_sse_response(response)
                return _redact_response(parsed, api_key=self._api_key, query=query)
        except WebSearchError as exc:
            raise WebSearchError(_redact_exact(str(exc), (self._api_key, query))) from exc
        except urllib.error.HTTPError as exc:
            body = exc.read(MAX_ERROR_CHARS * 4).decode("utf-8", errors="replace")
            message = _http_error_message(exc.code, body)
            raise WebSearchError(_redact_exact(message, (self._api_key, query))) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise WebSearchTimeoutError(f"web_search timed out after {timeout:.1f} seconds.") from exc
            raise WebSearchError("web_search provider request failed.") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise WebSearchTimeoutError(f"web_search timed out after {timeout:.1f} seconds.") from exc
        except OSError as exc:
            raise WebSearchError("web_search provider response failed.") from exc


def dashscope_generation_endpoint(api_base_url: str) -> str:
    parsed = urllib.parse.urlsplit(api_base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise WebSearchError("web_search requires a credential-free HTTPS Bailian API base URL.")
    compatible_suffix = "/compatible-mode/v1"
    path = parsed.path.rstrip("/")
    if not path.endswith(compatible_suffix):
        raise WebSearchError("web_search requires a Bailian compatible-mode/v1 API base URL.")
    generation_path = path[: -len(compatible_suffix)] + "/api/v1/services/aigc/text-generation/generation"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, generation_path, "", ""))


def _parse_sse_response(response: Any) -> WebSearchResponse:
    total_bytes = 0
    answer_parts: list[str] = []
    answer_chars = 0
    answer_truncated = False
    sources: tuple[WebSearchSource, ...] = ()
    provider_source_count = 0
    request_id = ""
    completed = False

    for raw_line in response:
        total_bytes += len(raw_line)
        if total_bytes > MAX_PROVIDER_RESPONSE_BYTES:
            raise WebSearchError("web_search provider response exceeded its byte limit.")
        try:
            line = raw_line.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise WebSearchError("web_search provider returned invalid UTF-8 SSE data.") from exc
        if not line.startswith("data:"):
            continue
        raw_data = line[5:].strip()
        if not raw_data or raw_data == "[DONE]":
            continue
        try:
            item = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise WebSearchError("web_search provider returned malformed SSE JSON.") from exc
        if not isinstance(item, dict):
            raise WebSearchError("web_search provider returned a non-object SSE item.")
        _raise_provider_item_error(item)
        item_request_id = item.get("request_id")
        if isinstance(item_request_id, str) and item_request_id:
            if len(item_request_id) > MAX_REQUEST_ID_CHARS or any(char.isspace() for char in item_request_id):
                raise WebSearchError("web_search provider returned an invalid request identity.")
            if request_id and request_id != item_request_id:
                raise WebSearchError("web_search provider changed request identity mid-stream.")
            request_id = item_request_id
        output = item.get("output")
        if not isinstance(output, dict):
            continue
        choices = output.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
            message = choice.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content:
                    remaining = MAX_ANSWER_CHARS - answer_chars
                    if remaining > 0:
                        answer_parts.append(content[:remaining])
                        answer_chars += min(len(content), remaining)
                    if len(content) > remaining:
                        answer_truncated = True
            if choice.get("finish_reason") == "stop":
                completed = True
        search_info = output.get("search_info")
        if isinstance(search_info, dict) and isinstance(search_info.get("search_results"), list):
            raw_sources = search_info["search_results"]
            provider_source_count = len(raw_sources)
            sources = _parse_sources(raw_sources)

    if not completed:
        raise WebSearchError("web_search provider stream ended without a completed response.")
    if not request_id:
        raise WebSearchError("web_search provider response omitted request provenance.")
    if not sources:
        raise WebSearchError("web_search provider returned no valid source provenance.")
    return WebSearchResponse(
        answer="".join(answer_parts).strip(),
        answer_truncated=answer_truncated,
        sources=sources,
        provider_source_count=provider_source_count,
        request_id=request_id,
    )


def _parse_sources(raw_sources: list[Any]) -> tuple[WebSearchSource, ...]:
    if len(raw_sources) > MAX_SOURCE_COUNT:
        raise WebSearchError(f"web_search provider returned more than {MAX_SOURCE_COUNT} sources.")
    sources: list[WebSearchSource] = []
    seen_urls: set[str] = set()
    for raw_source in raw_sources:
        if not isinstance(raw_source, dict):
            raise WebSearchError("web_search provider returned an invalid source record.")
        title = _single_line(raw_source.get("title"), MAX_SOURCE_TITLE_CHARS)
        url = _source_url(raw_source.get("url"))
        if not title or not url:
            raise WebSearchError("web_search provider returned an invalid source record.")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        sources.append(WebSearchSource(title=title, url=url))
    return tuple(sources)


def _redact_response(
    response: WebSearchResponse,
    *,
    api_key: str,
    query: str,
) -> WebSearchResponse:
    secrets = tuple(value for value in (api_key, query) if value)
    if api_key and any(api_key in source.url for source in response.sources):
        raise WebSearchError("web_search source provenance contained provider credentials.")
    sources = tuple(
        WebSearchSource(title=_redact_exact(source.title, secrets), url=source.url)
        for source in response.sources
    )
    return WebSearchResponse(
        answer=_redact_exact(response.answer, secrets),
        answer_truncated=response.answer_truncated,
        sources=sources,
        provider_source_count=response.provider_source_count,
        request_id=response.request_id,
    )


def _source_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    if len(value) > MAX_SOURCE_URL_CHARS:
        return ""
    try:
        parsed = urllib.parse.urlsplit(value.strip())
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _raise_provider_item_error(item: dict[str, Any]) -> None:
    code = _single_line(item.get("code"), 100)
    if not code:
        return
    message = _single_line(item.get("message"), MAX_ERROR_CHARS)
    suffix = f": {message}" if message else ""
    raise WebSearchError(f"web_search provider error {code}{suffix}")


def _http_error_message(status: int, body: str) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {}
    code = _single_line(payload.get("code"), 100) if isinstance(payload, dict) else ""
    message = _single_line(payload.get("message"), MAX_ERROR_CHARS) if isinstance(payload, dict) else ""
    detail = "/".join(part for part in (code, message) if part)
    suffix = f": {detail}" if detail else ""
    return f"web_search provider returned HTTP {status}{suffix}"


def _single_line(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _redact_exact(value: str, secrets: tuple[str, ...]) -> str:
    redacted = value
    for secret in secrets:
        redacted = redacted.replace(secret, "[redacted]")
    return redacted

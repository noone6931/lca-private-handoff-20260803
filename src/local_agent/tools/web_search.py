from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Protocol

from ..protocol.cancellation import raise_if_cancelled
from ..providers.web_search import WebSearchError
from ..providers.web_search import WebSearchResponse
from ..providers.web_search import WebSearchTimeoutError
from .base import Tool
from .base import ToolContext
from .base import ToolResult


MAX_QUERY_CHARS = 512


class WebSearchBackend(Protocol):
    provider: str
    model: str
    request_timeout: float

    def search(self, query: str, *, timeout: float) -> WebSearchResponse: ...


def web_search_tools(backend: WebSearchBackend) -> tuple[Tool, ...]:
    return (
        Tool(
            name="web_search",
            description=(
                "Search the public web for current information and return a bounded answer with typed source URLs. "
                "This does not open or fetch arbitrary URLs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "One concise public-web search query (1-512 characters).",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            tier="network",
            handler=lambda args, context: _web_search(args, context, backend),
            redact_arguments=True,
        ),
    )


def _web_search(
    args: dict[str, Any],
    context: ToolContext,
    backend: WebSearchBackend,
) -> ToolResult:
    query = str(args["query"]).strip()
    if not query:
        return ToolResult("web_search query must not be empty.", is_error=True)
    if len(query) > MAX_QUERY_CHARS:
        return ToolResult(f"web_search query exceeds the {MAX_QUERY_CHARS} character limit.", is_error=True)
    query_digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    timeout = _request_timeout(context, backend.request_timeout)
    if timeout is None:
        return ToolResult(
            "web_search cannot start because the run deadline is exhausted.",
            is_error=True,
            metadata={"network_read": True, "timed_out": True, "query_digest": query_digest},
        )
    try:
        response = backend.search(query, timeout=timeout)
        raise_if_cancelled(context.cancel_event)
    except WebSearchError as exc:
        return ToolResult(
            str(exc),
            is_error=True,
            metadata={
                "network_read": True,
                "provider": backend.provider,
                "model": backend.model,
                "query_digest": query_digest,
                "structured_output": True,
                "timed_out": isinstance(exc, WebSearchTimeoutError),
            },
        )

    payload = {
        "answer": response.answer,
        "answer_truncated": response.answer_truncated,
        "model": backend.model,
        "provider": backend.provider,
        "provider_request_id": response.request_id,
        "provider_source_count": response.provider_source_count,
        "query_digest": query_digest,
        "sources": [
            {"index": index, "title": source.title, "url": source.url}
            for index, source in enumerate(response.sources, start=1)
        ],
    }
    return ToolResult(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        metadata={
            "network_read": True,
            "provider": backend.provider,
            "model": backend.model,
            "provider_request_id": response.request_id,
            "source_count": len(response.sources),
            "query_digest": query_digest,
            "structured_output": True,
        },
    )


def _request_timeout(context: ToolContext, configured_timeout: float) -> float | None:
    timeout = configured_timeout
    if context.deadline_monotonic is None:
        return timeout
    remaining = context.deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return None
    return min(timeout, max(0.1, remaining))

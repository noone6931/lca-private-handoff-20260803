from __future__ import annotations

import queue
import threading
from typing import Any

from .llm import LlmError
from .llm import LlmTimeoutError


def call_chat_with_timeout(
    client: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    timeout: float | None,
) -> Any:
    """Enforce an outer timeout even when the client ignores its own timeout arg."""

    if timeout is None:
        return client.chat(messages, tools, timeout=timeout)
    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            response = client.chat(messages, tools, timeout=timeout)
        except BaseException as exc:  # noqa: BLE001 - normalized below.
            result_queue.put(("error", exc))
            return
        result_queue.put(("ok", response))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise LlmTimeoutError(f"LLM API request timed out after {timeout} seconds.")
    try:
        status, payload = result_queue.get_nowait()
    except queue.Empty as exc:
        raise LlmError("LLM API request ended without returning a response.") from exc
    if status == "ok":
        return payload
    if isinstance(payload, LlmError):
        raise payload
    raise LlmError(f"LLM client failed: {type(payload).__name__}: {payload}")

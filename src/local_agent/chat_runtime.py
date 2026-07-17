from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from .cancellation import RunCancelled
from .llm import LlmError
from .llm import LlmTimeoutError
from .provider_stream import ProviderTextDelta


def call_chat_with_timeout(
    client: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    timeout: float | None,
    model: str | None = None,
    tool_choice: dict[str, Any] | str | None = None,
    use_stream: bool = False,
    on_text_delta: Callable[[str, int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Any:
    """Enforce an outer timeout even when the client ignores its own timeout arg."""

    kwargs: dict[str, Any] = {"timeout": timeout}
    if model:
        kwargs["model"] = model
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice
    stream_method = getattr(client, "chat_stream", None)
    if use_stream and callable(stream_method):
        return _call_stream_with_timeout(
            lambda: stream_method(messages, tools, **kwargs),
            timeout=timeout,
            on_text_delta=on_text_delta,
            cancel_event=cancel_event,
        )
    if timeout is None and cancel_event is None:
        return client.chat(messages, tools, **kwargs)
    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            response = client.chat(messages, tools, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - normalized below.
            result_queue.put(("error", exc))
            return
        result_queue.put(("ok", response))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    deadline = None if timeout is None else time.perf_counter() + timeout
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise RunCancelled("Run cancelled while waiting for the provider.")
        remaining = None if deadline is None else deadline - time.perf_counter()
        if remaining is not None and remaining <= 0:
            raise LlmTimeoutError(f"LLM API request timed out after {timeout} seconds.")
        wait = 0.05 if remaining is None else min(remaining, 0.05)
        try:
            status, payload = result_queue.get(timeout=wait)
            break
        except queue.Empty:
            continue
    if status == "ok":
        return payload
    _raise_client_error(payload)


def _call_stream_with_timeout(
    stream_factory: Callable[[], Iterator[ProviderTextDelta]],
    *,
    timeout: float | None,
    on_text_delta: Callable[[str, int], None] | None,
    cancel_event: threading.Event | None,
) -> Any:
    if timeout is None and cancel_event is None:
        return _consume_stream(stream_factory(), on_text_delta=on_text_delta)

    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=256)
    cancelled = threading.Event()

    def publish(kind: str, payload: Any) -> bool:
        while not cancelled.is_set() and not (cancel_event is not None and cancel_event.is_set()):
            try:
                result_queue.put((kind, payload), timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def worker() -> None:
        stream: Iterator[ProviderTextDelta] | None = None
        try:
            stream = stream_factory()
            while not cancelled.is_set() and not (cancel_event is not None and cancel_event.is_set()):
                try:
                    event = next(stream)
                except StopIteration as complete:
                    publish("complete", complete.value)
                    return
                if not isinstance(event, ProviderTextDelta):
                    raise LlmError(f"LLM stream yielded unsupported event: {type(event).__name__}.")
                if not publish("delta", event.delta):
                    return
        except BaseException as exc:  # noqa: BLE001 - normalized on the calling thread.
            publish("error", exc)
        finally:
            if (cancelled.is_set() or (cancel_event is not None and cancel_event.is_set())) and stream is not None:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    deadline = None if timeout is None else time.perf_counter() + timeout
    delta_index = 0
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise RunCancelled("Run cancelled while consuming provider output.")
            remaining = None if deadline is None else deadline - time.perf_counter()
            if remaining is not None and remaining <= 0:
                raise LlmTimeoutError(f"LLM API request timed out after {timeout} seconds.")
            wait = 0.05 if remaining is None else min(remaining, 0.05)
            try:
                kind, payload = result_queue.get(timeout=wait)
            except queue.Empty as exc:
                if deadline is not None and time.perf_counter() >= deadline:
                    raise LlmTimeoutError(f"LLM API request timed out after {timeout} seconds.") from exc
                continue
            if kind == "delta":
                if cancel_event is not None and cancel_event.is_set():
                    raise RunCancelled("Run cancelled before a provider delta was published.")
                if on_text_delta is not None:
                    on_text_delta(payload, delta_index)
                delta_index += 1
                continue
            if kind == "complete":
                if payload is None:
                    raise LlmError("LLM stream ended without returning a response.")
                return payload
            _raise_client_error(payload)
    finally:
        cancelled.set()


def _consume_stream(
    stream: Iterator[ProviderTextDelta],
    *,
    on_text_delta: Callable[[str, int], None] | None,
) -> Any:
    delta_index = 0
    while True:
        try:
            event = next(stream)
        except StopIteration as complete:
            if complete.value is None:
                raise LlmError("LLM stream ended without returning a response.")
            return complete.value
        except BaseException as exc:  # noqa: BLE001 - normalized below.
            _raise_client_error(exc)
        if not isinstance(event, ProviderTextDelta):
            raise LlmError(f"LLM stream yielded unsupported event: {type(event).__name__}.")
        if on_text_delta is not None:
            on_text_delta(event.delta, delta_index)
        delta_index += 1


def _raise_client_error(error: BaseException) -> None:
    if isinstance(error, KeyboardInterrupt):
        raise error
    if isinstance(error, LlmError):
        raise error
    raise LlmError(f"LLM client failed: {type(error).__name__}: {error}") from error

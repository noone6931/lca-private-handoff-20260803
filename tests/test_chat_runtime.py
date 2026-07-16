from __future__ import annotations

import threading
import time
import unittest

from local_agent.chat_runtime import call_chat_with_timeout
from local_agent.llm import ChatResponse, LlmError, LlmTimeoutError
from local_agent.provider_stream import ProviderTextDelta


class _ChatOnlyClient:
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, tools, *, timeout=None, model=None, tool_choice=None):
        del messages, tools, timeout, model, tool_choice
        self.calls += 1
        return ChatResponse(message={"role": "assistant", "content": "legacy"}, finish_reason="stop")


class _StreamingClient:
    def __init__(self, deltas: tuple[str, ...] = ("one", "two")) -> None:
        self.deltas = deltas
        self.worker_thread_ids: list[int] = []

    def chat_stream(self, messages, tools, *, timeout=None, model=None, tool_choice=None):
        del messages, tools, timeout, model, tool_choice
        self.worker_thread_ids.append(threading.get_ident())
        for delta in self.deltas:
            yield ProviderTextDelta(delta)
        return ChatResponse(message={"role": "assistant", "content": "".join(self.deltas)}, finish_reason="stop")


class ChatRuntimeTests(unittest.TestCase):
    def test_chat_only_clients_keep_the_existing_compatibility_path(self) -> None:
        client = _ChatOnlyClient()

        response = call_chat_with_timeout(client, [], [], timeout=1, use_stream=True)

        self.assertEqual(response.message["content"], "legacy")
        self.assertEqual(client.calls, 1)

    def test_stream_callbacks_run_on_the_calling_thread_with_monotonic_indices(self) -> None:
        client = _StreamingClient()
        caller_thread = threading.get_ident()
        callbacks: list[tuple[str, int, int]] = []

        response = call_chat_with_timeout(
            client,
            [],
            [],
            timeout=1,
            use_stream=True,
            on_text_delta=lambda delta, index: callbacks.append((delta, index, threading.get_ident())),
        )

        self.assertEqual(response.message["content"], "onetwo")
        self.assertEqual(callbacks, [("one", 0, caller_thread), ("two", 1, caller_thread)])
        self.assertNotEqual(client.worker_thread_ids, [caller_thread])

    def test_timeout_drops_late_chunks_and_does_not_contaminate_the_caller(self) -> None:
        release = threading.Event()
        callbacks: list[str] = []

        class _LateClient:
            def chat_stream(self, messages, tools, *, timeout=None):
                del messages, tools, timeout
                yield ProviderTextDelta("early")
                release.wait(1)
                yield ProviderTextDelta("late")
                return ChatResponse(message={"role": "assistant", "content": "earlylate"}, finish_reason="stop")

        with self.assertRaises(LlmTimeoutError):
            call_chat_with_timeout(
                _LateClient(),
                [],
                [],
                timeout=0.03,
                use_stream=True,
                on_text_delta=lambda delta, index: callbacks.append(f"{index}:{delta}"),
            )
        release.set()
        time.sleep(0.05)

        self.assertEqual(callbacks, ["0:early"])

    def test_mid_stream_failure_never_returns_a_complete_response(self) -> None:
        callbacks: list[str] = []

        class _FailingClient:
            def chat_stream(self, messages, tools, *, timeout=None):
                del messages, tools, timeout
                yield ProviderTextDelta("partial")
                raise ConnectionError("closed")

        with self.assertRaisesRegex(LlmError, "ConnectionError"):
            call_chat_with_timeout(
                _FailingClient(),
                [],
                [],
                timeout=1,
                use_stream=True,
                on_text_delta=lambda delta, index: callbacks.append(delta),
            )

        self.assertEqual(callbacks, ["partial"])

    def test_stream_without_terminal_response_fails_closed(self) -> None:
        class _NoTerminalClient:
            def chat_stream(self, messages, tools, *, timeout=None):
                del messages, tools, timeout
                if False:
                    yield ProviderTextDelta("")

        with self.assertRaisesRegex(LlmError, "without returning a response"):
            call_chat_with_timeout(_NoTerminalClient(), [], [], timeout=None, use_stream=True)


if __name__ == "__main__":
    unittest.main()

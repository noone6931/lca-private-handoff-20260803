from __future__ import annotations

import json
import unittest

from local_agent.provider_stream import ProviderStreamError
from local_agent.provider_stream import iter_chat_completion_response


class _ChunkedResponse:
    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    def read(self, size: int = -1) -> bytes:
        del size
        return self._chunks.pop(0) if self._chunks else b""


def _collect(chunks: list[bytes]):
    stream = iter_chat_completion_response(_ChunkedResponse(chunks), chunk_size=3)
    deltas: list[str] = []
    while True:
        try:
            deltas.append(next(stream).delta)
        except StopIteration as complete:
            return deltas, complete.value


def _sse(*payloads: dict, done: bool = True, newline: str = "\n") -> bytes:
    body = "".join(f"data: {json.dumps(payload, ensure_ascii=False)}{newline}{newline}" for payload in payloads)
    if done:
        body += f"data: [DONE]{newline}{newline}"
    return body.encode("utf-8")


class ProviderStreamTests(unittest.TestCase):
    def test_prefers_incremental_read1_when_the_http_response_supports_it(self) -> None:
        body = _sse({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]})

        class _ReadOneResponse(_ChunkedResponse):
            read1_calls = 0

            def read(self, size: int = -1) -> bytes:
                raise AssertionError("buffer-filling read must not be used for an incremental HTTP response")

            def read1(self, size: int = -1) -> bytes:
                type(self).read1_calls += 1
                return super().read(size)

        response = _ReadOneResponse([body, b""])
        stream = iter_chat_completion_response(response)
        self.assertEqual(next(stream).delta, "ok")
        with self.assertRaises(StopIteration):
            next(stream)
        self.assertGreaterEqual(response.read1_calls, 2)

    def test_text_stream_handles_crlf_comments_multiline_data_and_split_utf8(self) -> None:
        first = (
            ": keepalive\r\n\r\n"
            "data: {\"choices\":[\r\n"
            "data: {\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"你\"},"
            "\"finish_reason\":null}]}\r\n\r\n"
        ).encode("utf-8")
        terminal = _sse(
            {"choices": [{"index": 0, "delta": {"content": "好"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            newline="\r\n",
        )
        body = first + terminal
        marker = body.index("你".encode("utf-8")) + 1
        chunks = [body[:1], body[1:marker], body[marker : marker + 1], body[marker + 1 :]]

        deltas, response = _collect(chunks)

        self.assertEqual(deltas, ["你", "好"])
        self.assertEqual(response.message, {"role": "assistant", "content": "你好"})
        self.assertEqual(response.finish_reason, "stop")

    def test_parallel_fragmented_tool_calls_are_aggregated_without_argument_deltas(self) -> None:
        body = _sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": "Checking.",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_a",
                                    "type": "function",
                                    "function": {"name": "read_", "arguments": "{\"pa"},
                                },
                                {
                                    "index": 1,
                                    "id": "call_b",
                                    "type": "function",
                                    "function": {"name": "search_", "arguments": "{\"qu"},
                                },
                            ],
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 1, "function": {"name": "code", "arguments": "ery\":\"x\"}"}},
                                {"index": 0, "function": {"name": "file", "arguments": "th\":\"a.py\"}"}},
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

        deltas, response = _collect([body[:7], body[7:31], body[31:]])

        self.assertEqual(deltas, ["Checking."])
        self.assertNotIn("a.py", "".join(deltas))
        self.assertEqual(
            response.message["tool_calls"],
            [
                {
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                },
                {
                    "id": "call_b",
                    "type": "function",
                    "function": {"name": "search_code", "arguments": '{"query":"x"}'},
                },
            ],
        )
        self.assertEqual(response.finish_reason, "tool_calls")

    def test_plain_json_response_uses_the_same_response_without_stream_events(self) -> None:
        body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "plain"}, "finish_reason": "stop"}]}
        ).encode("utf-8")

        deltas, response = _collect([body[:2], body[2:]])

        self.assertEqual(deltas, [])
        self.assertEqual(response.message["content"], "plain")
        self.assertEqual(response.finish_reason, "stop")

    def test_done_is_required_for_sse_even_after_finish_reason(self) -> None:
        body = _sse(
            {"choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": "stop"}]},
            done=False,
        )
        with self.assertRaisesRegex(ProviderStreamError, r"without \[DONE\]"):
            _collect([body])

    def test_terminal_choice_is_required_before_done(self) -> None:
        body = _sse({"choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]})
        with self.assertRaisesRegex(ProviderStreamError, "without a terminal choice"):
            _collect([body])

    def test_malformed_json_fails_closed(self) -> None:
        with self.assertRaisesRegex(ProviderStreamError, "malformed JSON"):
            _collect([b"data: {bad}\n\ndata: [DONE]\n\n"])

    def test_missing_or_invalid_choice_index_fails_closed(self) -> None:
        for index_value in (None, 1, -1, "0"):
            choice = {"delta": {"content": "x"}, "finish_reason": "stop"}
            if index_value is not None:
                choice["index"] = index_value
            body = _sse({"choices": [choice]})
            with self.subTest(index=index_value):
                with self.assertRaisesRegex(ProviderStreamError, "choice index"):
                    _collect([body])

    def test_invalid_or_sparse_tool_index_fails_closed(self) -> None:
        invalid = _sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [{"index": "0", "id": "x"}]},
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        sparse = _sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "x",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": "{}"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        with self.assertRaisesRegex(ProviderStreamError, "tool call index"):
            _collect([invalid])
        with self.assertRaisesRegex(ProviderStreamError, "sparse"):
            _collect([sparse])

    def test_incomplete_tool_call_fails_closed(self) -> None:
        body = _sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_x",
                                    "type": "function",
                                    "function": {"name": "read_file"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        with self.assertRaisesRegex(ProviderStreamError, "incomplete tool call"):
            _collect([body])

    def test_truncated_utf8_fails_closed(self) -> None:
        with self.assertRaisesRegex(ProviderStreamError, "invalid UTF-8"):
            _collect([b"data: \xe4\xbd"])


if __name__ == "__main__":
    unittest.main()

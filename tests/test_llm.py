from __future__ import annotations

import json
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from local_agent.config import AgentConfig
from local_agent.llm import LlmError, LlmTimeoutError, OpenAICompatibleClient


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        del size
        body, self._body = self._body, b""
        return body


class LlmClientTests(unittest.TestCase):
    def test_chat_stream_requests_sse_and_returns_one_complete_response(self) -> None:
        captured_payload: dict = {}
        body = (
            'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"hel"},'
            '"finish_reason":null}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        ).encode("utf-8")

        def fake_urlopen(request, timeout):
            del timeout
            captured_payload.update(json.loads(request.data.decode("utf-8")))
            return _FakeResponse(body)

        with tempfile.TemporaryDirectory() as tmp:
            client = OpenAICompatibleClient(
                AgentConfig(
                    provider="openai-compatible",
                    api_base_url="https://example.invalid/v1",
                    api_key="token",
                    model="model",
                    workspace=Path(tmp).resolve(),
                )
            )
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                stream = client.chat_stream([], [])
                deltas = [next(stream).delta, next(stream).delta]
                with self.assertRaises(StopIteration) as stopped:
                    next(stream)

        self.assertEqual(captured_payload["stream"], True)
        self.assertEqual(deltas, ["hel", "lo"])
        self.assertEqual(stopped.exception.value.message["content"], "hello")
        self.assertEqual(stopped.exception.value.finish_reason, "stop")

    def test_chat_stream_accepts_plain_json_without_a_second_request(self) -> None:
        body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "plain"}, "finish_reason": "stop"}]}
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenAICompatibleClient(
                AgentConfig(
                    provider="openai-compatible",
                    api_base_url="https://example.invalid/v1",
                    api_key="token",
                    model="model",
                    workspace=Path(tmp).resolve(),
                )
            )
            with patch("urllib.request.urlopen", return_value=_FakeResponse(body)) as urlopen:
                stream = client.chat_stream([], [])
                with self.assertRaises(StopIteration) as stopped:
                    next(stream)

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(stopped.exception.value.message["content"], "plain")

    def test_chat_stream_wraps_mid_response_transport_failure(self) -> None:
        class _BrokenResponse(_FakeResponse):
            def read(self, size: int = -1) -> bytes:
                del size
                if self._body:
                    body, self._body = self._body, b""
                    return body
                raise ConnectionError("connection reset")

        first = b'data: {"choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenAICompatibleClient(
                AgentConfig(
                    provider="openai-compatible",
                    api_base_url="https://example.invalid/v1",
                    api_key="token",
                    model="model",
                    workspace=Path(tmp).resolve(),
                )
            )
            with patch("urllib.request.urlopen", return_value=_BrokenResponse(first)):
                stream = client.chat_stream([], [])
                self.assertEqual(next(stream).delta, "partial")
                with self.assertRaisesRegex(LlmError, "response stream failed"):
                    next(stream)

    def test_chat_stream_maps_provider_socket_timeout_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenAICompatibleClient(
                AgentConfig(
                    provider="openai-compatible",
                    api_base_url="https://example.invalid/v1",
                    api_key="token",
                    model="model",
                    workspace=Path(tmp).resolve(),
                )
            )
            timeout_error = urllib.error.URLError(socket.timeout("timed out"))
            with patch("urllib.request.urlopen", side_effect=timeout_error) as urlopen:
                with self.assertRaises(LlmTimeoutError):
                    next(client.chat_stream([], [], timeout=0.2))

        self.assertEqual(urlopen.call_count, 1)

    def test_non_json_success_response_is_wrapped_as_llm_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
            )
            client = OpenAICompatibleClient(config)
            with patch("urllib.request.urlopen", return_value=_FakeResponse(b"<html>bad gateway</html>")):
                with self.assertRaisesRegex(LlmError, "non-JSON response"):
                    client.chat([], [])

    def test_finish_reason_is_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
            )
            body = json.dumps(
                {
                    "choices": [
                        {
                            "message": {"content": "done"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            ).encode("utf-8")
            client = OpenAICompatibleClient(config)
            with patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
                response = client.chat([], [])

        self.assertEqual(response.message["content"], "done")
        self.assertEqual(response.finish_reason, "stop")

    def test_empty_tools_are_omitted_for_plain_summary_calls(self) -> None:
        captured_payload: dict = {}

        def fake_urlopen(request, timeout):
            captured_payload.update(json.loads(request.data.decode("utf-8")))
            body = json.dumps({"choices": [{"message": {"content": "summary"}}]}).encode("utf-8")
            return _FakeResponse(body)

        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
            )
            client = OpenAICompatibleClient(config)
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                response = client.chat([{"role": "user", "content": "summarize"}], [])

        self.assertEqual(response.message["content"], "summary")
        self.assertNotIn("tools", captured_payload)
        self.assertNotIn("tool_choice", captured_payload)
        self.assertNotIn("stream", captured_payload)

    def test_chat_can_override_the_model_for_a_runtime_role(self) -> None:
        captured_payload: dict = {}

        def fake_urlopen(request, timeout):
            captured_payload.update(json.loads(request.data.decode("utf-8")))
            body = json.dumps({"choices": [{"message": {"content": "reviewed"}}]}).encode("utf-8")
            return _FakeResponse(body)

        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="main-model",
                reviewer_model="review-model",
                workspace=Path(tmp).resolve(),
            )
            client = OpenAICompatibleClient(config)
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                response = client.chat([], [], model=config.reviewer_model)

        self.assertEqual(response.message["content"], "reviewed")
        self.assertEqual(captured_payload["model"], "review-model")

    def test_inspect_image_uses_a_vision_model_and_keeps_image_payload_in_provider_request(self) -> None:
        captured_payload: dict = {}

        def fake_urlopen(request, timeout):
            captured_payload.update(json.loads(request.data.decode("utf-8")))
            body = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "observations": ["A visible example."],
                                        "uncertainties": [],
                                        "inferences": [],
                                    }
                                )
                            }
                        }
                    ]
                }
            ).encode("utf-8")
            return _FakeResponse(body)

        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="text-model",
                vision_model="qwen-vl-max",
                workspace=Path(tmp).resolve(),
            )
            client = OpenAICompatibleClient(config)
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                observation = client.inspect_image(
                    image_base64="cGl4ZWxz",
                    mime_type="image/png",
                    question="Describe this image.",
                )

        self.assertIn('"observations": ["A visible example."]', observation)
        self.assertEqual(captured_payload["model"], "qwen-vl-max")
        self.assertNotIn("tools", captured_payload)
        self.assertNotIn("stream", captured_payload)
        self.assertEqual(captured_payload["messages"][0]["role"], "system")
        self.assertIn("Return only a compact JSON object", captured_payload["messages"][0]["content"])
        self.assertIn("not permission to infer business rules", captured_payload["messages"][0]["content"])
        self.assertNotIn("Describe this image.", captured_payload["messages"][0]["content"])
        self.assertEqual(captured_payload["messages"][1]["role"], "user")
        content = captured_payload["messages"][1]["content"]
        self.assertIn("Focus question: Describe this image.", content[0]["text"])
        self.assertIn("Return only the JSON object", content[0]["text"])
        self.assertEqual(content[1]["image_url"]["url"], "data:image/png;base64,cGl4ZWxz")

    def test_inspect_image_requires_a_vision_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="text-model",
                workspace=Path(tmp).resolve(),
            )
            with self.assertRaisesRegex(LlmError, "vision-capable"):
                OpenAICompatibleClient(config).inspect_image(
                    image_base64="cGl4ZWxz",
                    mime_type="image/png",
                    question="Describe this image.",
                )

    def test_inspect_image_does_not_infer_vision_capability_from_the_text_model_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="qwen-vl-max",
                workspace=Path(tmp).resolve(),
            )
            with self.assertRaisesRegex(LlmError, "explicit vision model"):
                OpenAICompatibleClient(config).inspect_image(
                    image_base64="cGl4ZWxz",
                    mime_type="image/png",
                    question="Describe this image.",
                )

    def test_bailian_response_exposes_known_text_tool_envelope_as_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="bailian",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
            )
            body = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "<tool_call><function=read_file><parameter=path>hidden.py</parameter></function></tool_call>"
                            }
                        }
                    ]
                }
            ).encode("utf-8")
            client = OpenAICompatibleClient(config)
            with patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
                response = client.chat([], [])

        self.assertIsNotNone(response.protocol_artifact)
        self.assertEqual(response.protocol_artifact.tool_name, "read_file")
        self.assertNotIn("hidden.py", response.protocol_artifact.preview)

    def test_url_error_with_socket_timeout_is_structured_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
            )
            client = OpenAICompatibleClient(config)
            timeout_error = urllib.error.URLError(socket.timeout("timed out"))
            with patch("urllib.request.urlopen", side_effect=timeout_error):
                with self.assertRaises(LlmTimeoutError):
                    client.chat([], [])


if __name__ == "__main__":
    unittest.main()

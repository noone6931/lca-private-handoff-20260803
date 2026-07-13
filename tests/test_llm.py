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

    def read(self) -> bytes:
        return self._body


class LlmClientTests(unittest.TestCase):
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

    def test_inspect_image_uses_a_vision_model_and_keeps_image_payload_in_provider_request(self) -> None:
        captured_payload: dict = {}

        def fake_urlopen(request, timeout):
            captured_payload.update(json.loads(request.data.decode("utf-8")))
            body = json.dumps({"choices": [{"message": {"content": "A visible example."}}]}).encode("utf-8")
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

        self.assertEqual(observation, "A visible example.")
        self.assertEqual(captured_payload["model"], "qwen-vl-max")
        self.assertNotIn("tools", captured_payload)
        self.assertEqual(captured_payload["messages"][0]["role"], "system")
        self.assertIn("Separate direct observations from inferences", captured_payload["messages"][0]["content"])
        self.assertNotIn("Describe this image.", captured_payload["messages"][0]["content"])
        self.assertEqual(captured_payload["messages"][1]["role"], "user")
        content = captured_payload["messages"][1]["content"]
        self.assertEqual(content[0]["text"], "Describe this image.")
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

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.config import AgentConfig
from local_agent.llm import LlmError, OpenAICompatibleClient


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


if __name__ == "__main__":
    unittest.main()

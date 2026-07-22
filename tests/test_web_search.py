from __future__ import annotations

import json
import io
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from local_agent.config import AgentConfig
from local_agent.config import ConfigError
from local_agent.config import load_config
from local_agent.cli import main
from local_agent.providers.llm import OpenAICompatibleClient
from local_agent.providers.web_search import BailianWebSearchBackend
from local_agent.providers.web_search import WebSearchError
from local_agent.providers.web_search import WebSearchResponse
from local_agent.providers.web_search import WebSearchSource
from local_agent.providers.web_search import dashscope_generation_endpoint
from local_agent.tools import create_default_registry
from local_agent.tools import create_runtime_registry
from local_agent.tools.base import ToolContext
from local_agent.tools.base import ToolRegistry
from local_agent.tools.web_search import web_search_tools


class _SseResponse:
    def __init__(self, *items: dict[str, object]):
        self._lines = [f"data: {json.dumps(item)}\n".encode("utf-8") for item in items]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def __iter__(self):
        return iter(self._lines)


class _Backend:
    provider = "bailian"
    model = "qwen-plus"
    request_timeout = 30.0

    def __init__(self, *, error: str = ""):
        self.error = error
        self.calls: list[tuple[str, float]] = []

    def search(self, query: str, *, timeout: float) -> WebSearchResponse:
        self.calls.append((query, timeout))
        if self.error:
            raise WebSearchError(self.error)
        return WebSearchResponse(
            answer="A sourced answer.",
            answer_truncated=False,
            sources=(WebSearchSource(title="Official source", url="https://example.com/source"),),
            provider_source_count=3,
            request_id="request-1",
        )


class WebSearchTests(unittest.TestCase):
    def test_config_is_disabled_by_default_and_requires_bailian_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "token"}, clear=True):
                disabled = load_config(
                    config_path=None,
                    cwd=tmp,
                    provider="bailian",
                    api_base_url=None,
                    api_key=None,
                    model=None,
                    max_steps=None,
                    budget_seconds=None,
                    approval_mode=None,
                )
                enabled = load_config(
                    config_path=None,
                    cwd=tmp,
                    provider="bailian",
                    api_base_url=None,
                    api_key=None,
                    model=None,
                    max_steps=None,
                    budget_seconds=None,
                    approval_mode=None,
                    enable_web_search=True,
                    web_search_model="qwen-plus-search",
                )
        self.assertFalse(disabled.enable_web_search)
        self.assertEqual(disabled.web_search_model, "qwen-plus")
        self.assertTrue(enabled.enable_web_search)
        self.assertEqual(enabled.web_search_model, "qwen-plus-search")

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {"AI_API_BASE_URL": "https://example.invalid/v1", "AI_API_KEY": "token", "AI_MODEL": "model"},
                clear=True,
            ):
                with self.assertRaisesRegex(ConfigError, "supported only for bailian"):
                    load_config(
                        config_path=None,
                        cwd=tmp,
                        provider="openai-compatible",
                        api_base_url=None,
                        api_key=None,
                        model=None,
                        max_steps=None,
                        budget_seconds=None,
                        approval_mode=None,
                        enable_web_search=True,
                    )

    def test_nested_config_and_environment_resolve_explicit_search_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "agent.json"
            config_path.write_text(
                json.dumps({"provider": "bailian", "tools": {"webSearch": {"enabled": True, "model": "nested"}}}),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "token"}, clear=True):
                config = load_config(
                    config_path=str(config_path),
                    cwd=tmp,
                    provider=None,
                    api_base_url=None,
                    api_key=None,
                    model=None,
                    max_steps=None,
                    budget_seconds=None,
                    approval_mode=None,
                )
        self.assertTrue(config.enable_web_search)
        self.assertEqual(config.web_search_model, "nested")

    def test_endpoint_derivation_is_fixed_to_https_compatible_base(self) -> None:
        self.assertEqual(
            dashscope_generation_endpoint("https://dashscope.aliyuncs.com/compatible-mode/v1"),
            "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
        )
        self.assertEqual(
            dashscope_generation_endpoint("https://workspace.example/compatible-mode/v1/"),
            "https://workspace.example/api/v1/services/aigc/text-generation/generation",
        )
        for invalid in (
            "http://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://user:secret@example.com/compatible-mode/v1",
            "https://example.com/v1",
            "https://example.com/compatible-mode/v1?target=other",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(WebSearchError):
                dashscope_generation_endpoint(invalid)

    def test_cli_reports_invalid_enabled_backend_as_configuration_error(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stderr(stderr):
            status = main(
                [
                    "--cwd",
                    tmp,
                    "--provider",
                    "bailian",
                    "--api-base-url",
                    "https://example.com/v1",
                    "--api-key",
                    "token",
                    "--model",
                    "qwen3-coder-next",
                    "--web-search",
                    "probe",
                ]
            )
        self.assertEqual(status, 2)
        self.assertIn("compatible-mode/v1", stderr.getvalue())

    def test_native_sse_request_returns_bounded_typed_sources_and_redacts_exact_values(self) -> None:
        captured: dict[str, object] = {}
        query = "private query"
        api_key = "secret-token"

        def opener(request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _SseResponse(
                {
                    "request_id": "request-1",
                    "output": {
                        "choices": [
                            {"message": {"content": f"Answer for {query} using {api_key}."}, "finish_reason": None}
                        ]
                    },
                },
                {
                    "request_id": "request-1",
                    "output": {
                        "choices": [{"message": {"content": " Done."}, "finish_reason": "stop"}],
                        "search_info": {
                            "search_results": [
                                {"title": f"Official {query}", "url": "https://example.com/one#fragment"},
                                {"title": "Second", "url": "https://example.com/two"},
                            ]
                        },
                    },
                },
            )

        backend = BailianWebSearchBackend(
            _config(api_key=api_key, enable_web_search=True),
            urlopen=opener,
        )
        result = backend.search(query, timeout=12.5)

        request = captured["request"]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(captured["timeout"], 12.5)
        self.assertEqual(payload["model"], "qwen-plus")
        self.assertEqual(payload["input"]["messages"][0]["content"], query)
        self.assertEqual(payload["parameters"]["search_options"]["enable_source"], True)
        self.assertEqual(request.get_header("X-dashscope-sse"), "enable")
        self.assertNotIn(query, result.answer)
        self.assertNotIn(api_key, result.answer)
        self.assertEqual(
            result.sources,
            (
                WebSearchSource(title="Official [redacted]", url="https://example.com/one"),
                WebSearchSource(title="Second", url="https://example.com/two"),
            ),
        )
        self.assertEqual(result.provider_source_count, 2)
        self.assertEqual(result.request_id, "request-1")

    def test_native_sse_fails_closed_without_completion_sources_or_request_identity(self) -> None:
        cases = (
            _SseResponse({"request_id": "r", "output": {"choices": []}}),
            _SseResponse(
                {
                    "request_id": "r",
                    "output": {"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}]},
                }
            ),
            _SseResponse(
                {
                    "output": {
                        "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
                        "search_info": {"search_results": [{"title": "Source", "url": "https://example.com"}]},
                    }
                }
            ),
        )
        for response in cases:
            with self.subTest(response=response):
                backend = BailianWebSearchBackend(
                    _config(enable_web_search=True),
                    urlopen=lambda request, timeout, response=response: response,
                )
                with self.assertRaises(WebSearchError):
                    backend.search("query", timeout=1.0)

    def test_native_sse_rejects_oversized_source_sets_and_urls(self) -> None:
        too_many = [
            {"title": f"Source {index}", "url": f"https://example.com/{index}"}
            for index in range(21)
        ]
        response = _SseResponse(
            {
                "request_id": "r",
                "output": {
                    "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
                    "search_info": {"search_results": too_many},
                },
            }
        )
        backend = BailianWebSearchBackend(
            _config(enable_web_search=True),
            urlopen=lambda request, timeout: response,
        )
        with self.assertRaisesRegex(WebSearchError, "more than 20 sources"):
            backend.search("query", timeout=1.0)

        oversized_url = "https://example.com/" + "x" * 2_100
        response = _SseResponse(
            {
                "request_id": "r",
                "output": {
                    "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
                    "search_info": {"search_results": [{"title": "Source", "url": oversized_url}]},
                },
            }
        )
        backend = BailianWebSearchBackend(
            _config(enable_web_search=True),
            urlopen=lambda request, timeout: response,
        )
        with self.assertRaisesRegex(WebSearchError, "invalid source record"):
            backend.search("query", timeout=1.0)

    def test_tool_is_opt_in_network_read_redacts_arguments_and_reports_truthful_metadata(self) -> None:
        backend = _Backend()
        tool = web_search_tools(backend)[0]
        registry = ToolRegistry([tool])
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp), approval_mode="always-ask")
            result = registry.execute("web_search", {"query": "current fact"}, context)

        self.assertEqual(tool.tier, "network")
        self.assertTrue(tool.redact_arguments)
        self.assertFalse(result.is_error)
        self.assertEqual(backend.calls[0][0], "current fact")
        self.assertEqual(result.metadata["network_read"], True)
        self.assertEqual(result.metadata["provider"], "bailian")
        self.assertEqual(result.metadata["source_count"], 1)
        self.assertNotIn("current fact", result.content)
        safe = registry.session_safe_assistant_message(
            {"tool_calls": [{"function": {"name": "web_search", "arguments": '{"query":"secret"}'}}]}
        )
        self.assertEqual(safe["tool_calls"][0]["function"]["arguments"], "{}")
        self.assertEqual(registry.telemetry_arguments("web_search", {"query": "secret"}), "[redacted by tool owner]")

    def test_tool_deadline_and_provider_errors_fail_closed(self) -> None:
        tool = web_search_tools(_Backend(error="bounded provider failure"))[0]
        registry = ToolRegistry([tool])
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            provider_error = registry.execute(
                "web_search",
                {"query": "fact"},
                ToolContext(workspace=workspace, approval_mode="always-ask"),
            )
            expired = registry.execute(
                "web_search",
                {"query": "fact"},
                ToolContext(
                    workspace=workspace,
                    approval_mode="always-ask",
                    deadline_monotonic=time.monotonic() - 1,
                ),
            )
        self.assertTrue(provider_error.is_error)
        self.assertEqual(provider_error.content, "bounded provider failure")
        self.assertTrue(expired.is_error)
        self.assertEqual(expired.metadata["timed_out"], True)

    def test_runtime_registry_exposes_search_only_for_enabled_provider_client(self) -> None:
        disabled_client = OpenAICompatibleClient(_config(enable_web_search=False))
        enabled_client = OpenAICompatibleClient(_config(enable_web_search=True))
        self.assertNotIn("web_search", create_default_registry().tool_names())
        self.assertNotIn("web_search", create_runtime_registry(disabled_client, False, 60).tool_names())
        self.assertIn("web_search", create_runtime_registry(enabled_client, False, 60).tool_names())


def _config(*, api_key: str = "token", enable_web_search: bool) -> AgentConfig:
    return AgentConfig(
        provider="bailian",
        api_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key=api_key,
        model="qwen3-coder-next",
        workspace=Path("/tmp").resolve(),
        enable_web_search=enable_web_search,
        web_search_model="qwen-plus",
    )

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.config import load_config


class ConfigTests(unittest.TestCase):
    def test_bailian_provider_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "token"}, clear=True):
                config = load_config(
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

        self.assertEqual(config.provider, "bailian")
        self.assertEqual(config.api_base_url, "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.assertEqual(config.api_key, "token")
        self.assertEqual(config.model, "qwen-plus")
        self.assertEqual(config.max_steps, 0)
        self.assertEqual(config.budget_seconds, 600)
        self.assertEqual(config.context_char_budget, 60000)
        self.assertEqual(config.context_recent_messages, 40)

    def test_dashscope_env_auto_selects_bailian(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "token"}, clear=True):
                config = load_config(
                    config_path=None,
                    cwd=tmp,
                    provider=None,
                    api_base_url=None,
                    api_key=None,
                    model=None,
                    max_steps=None,
                    budget_seconds=None,
                    approval_mode=None,
                )

        self.assertEqual(config.provider, "bailian")

    def test_openai_compatible_requires_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"AI_API_KEY": "token", "AI_MODEL": "model"}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "Missing AI_API_BASE_URL"):
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
                    )

    def test_max_steps_zero_means_unlimited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "token"}, clear=True):
                config = load_config(
                    config_path=None,
                    cwd=tmp,
                    provider="bailian",
                    api_base_url=None,
                    api_key=None,
                    model=None,
                    max_steps=0,
                    budget_seconds=None,
                    approval_mode=None,
                )

        self.assertEqual(config.max_steps, 0)

    def test_max_steps_rejects_negative_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "token"}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "max_steps must be >= 0"):
                    load_config(
                        config_path=None,
                        cwd=tmp,
                        provider="bailian",
                        api_base_url=None,
                        api_key=None,
                        model=None,
                        max_steps=-1,
                        budget_seconds=None,
                        approval_mode=None,
                    )

    def test_budget_seconds_zero_disables_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "token"}, clear=True):
                config = load_config(
                    config_path=None,
                    cwd=tmp,
                    provider="bailian",
                    api_base_url=None,
                    api_key=None,
                    model=None,
                    max_steps=None,
                    budget_seconds=0,
                    approval_mode=None,
                )

        self.assertIsNone(config.budget_seconds)

    def test_budget_seconds_rejects_negative_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "token"}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "budget_seconds must be >= 0"):
                    load_config(
                        config_path=None,
                        cwd=tmp,
                        provider="bailian",
                        api_base_url=None,
                        api_key=None,
                        model=None,
                        max_steps=None,
                        budget_seconds=-1,
                        approval_mode=None,
                    )

    def test_budget_seconds_can_come_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {"DASHSCOPE_API_KEY": "token", "AGENT_BUDGET_SECONDS": "30"},
                clear=True,
            ):
                config = load_config(
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

        self.assertEqual(config.budget_seconds, 30)

    def test_dotenv_can_supply_bailian_token_for_one_command_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / ".env").write_text('DASHSCOPE_API_KEY="token-from-dotenv"\n', encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True):
                config = load_config(
                    config_path=None,
                    cwd=str(workspace),
                    provider=None,
                    api_base_url=None,
                    api_key=None,
                    model=None,
                    max_steps=None,
                    budget_seconds=None,
                    approval_mode=None,
                )

        self.assertEqual(config.provider, "bailian")
        self.assertEqual(config.api_key, "token-from-dotenv")

    def test_auto_approve_tools_can_come_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {"DASHSCOPE_API_KEY": "token", "AGENT_AUTO_APPROVE_TOOLS": "run_tests, git_diff"},
                clear=True,
            ):
                config = load_config(
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

        self.assertEqual(config.auto_approve_tools, ("run_tests", "git_diff"))

    def test_auto_approve_tools_rejects_invalid_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "token"}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "invalid tool name"):
                    load_config(
                        config_path=None,
                        cwd=tmp,
                        provider="bailian",
                        api_base_url=None,
                        api_key=None,
                        model=None,
                        max_steps=None,
                        budget_seconds=None,
                        approval_mode=None,
                        auto_approve_tools="run-tests",
                    )

    def test_request_timeout_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "agent.json"
            config_path.write_text('{"request_timeout": 0}', encoding="utf-8")
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "token"}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "request_timeout must be >= 1"):
                    load_config(
                        config_path=str(config_path),
                        cwd=tmp,
                        provider="bailian",
                        api_base_url=None,
                        api_key=None,
                        model=None,
                        max_steps=None,
                        budget_seconds=None,
                        approval_mode=None,
                    )

    def test_context_compaction_settings_can_come_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {
                    "DASHSCOPE_API_KEY": "token",
                    "AGENT_CONTEXT_CHAR_BUDGET": "1000",
                    "AGENT_CONTEXT_RECENT_MESSAGES": "12",
                },
                clear=True,
            ):
                config = load_config(
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

        self.assertEqual(config.context_char_budget, 1000)
        self.assertEqual(config.context_recent_messages, 12)


if __name__ == "__main__":
    unittest.main()

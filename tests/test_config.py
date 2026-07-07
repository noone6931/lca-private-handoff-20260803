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
                    approval_mode=None,
                )

        self.assertEqual(config.provider, "bailian")
        self.assertEqual(config.api_base_url, "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.assertEqual(config.api_key, "token")
        self.assertEqual(config.model, "qwen-plus")
        self.assertEqual(config.max_steps, 20)

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
                        approval_mode=None,
                    )

    def test_max_steps_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "token"}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "max_steps must be >= 1"):
                    load_config(
                        config_path=None,
                        cwd=tmp,
                        provider="bailian",
                        api_base_url=None,
                        api_key=None,
                        model=None,
                        max_steps=0,
                        approval_mode=None,
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
                        approval_mode=None,
                    )


if __name__ == "__main__":
    unittest.main()

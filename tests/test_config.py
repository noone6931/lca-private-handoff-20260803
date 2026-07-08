from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.config import load_config
from local_agent.state import workspace_state_dir


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
        self.assertEqual(config.summary_mode, "auto")
        self.assertEqual(config.memory_consolidation, "off")
        self.assertEqual(config.approval_mode, "always-ask")
        self.assertEqual(config.allowed_dirs, ())

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

    def test_state_dir_uses_workspace_specific_directory_under_explicit_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            state_root = workspace / "state-root"
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "token"}, clear=True):
                config = load_config(
                    config_path=None,
                    cwd=str(workspace),
                    state_dir=str(state_root),
                    provider="bailian",
                    api_base_url=None,
                    api_key=None,
                    model=None,
                    max_steps=None,
                    budget_seconds=None,
                    approval_mode=None,
                )

        self.assertEqual(config.state_dir, workspace_state_dir(state_root.resolve(), workspace))
        self.assertFalse((workspace / ".local-agent").exists())

    def test_state_dir_can_come_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            state_root = workspace / "env-state"
            with patch.dict(
                "os.environ",
                {"DASHSCOPE_API_KEY": "token", "AGENT_STATE_DIR": str(state_root)},
                clear=True,
            ):
                config = load_config(
                    config_path=None,
                    cwd=str(workspace),
                    provider="bailian",
                    api_base_url=None,
                    api_key=None,
                    model=None,
                    max_steps=None,
                    budget_seconds=None,
                    approval_mode=None,
                )

        self.assertEqual(config.state_dir, workspace_state_dir(state_root.resolve(), workspace))

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

    def test_env_file_can_supply_token_outside_target_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as install_tmp, tempfile.TemporaryDirectory() as workspace_tmp:
            install_dir = Path(install_tmp).resolve()
            workspace = Path(workspace_tmp).resolve()
            env_file = install_dir / ".env"
            env_file.write_text('DASHSCOPE_API_KEY="token-from-install-env"\n', encoding="utf-8")
            (workspace / ".env").write_text('DASHSCOPE_API_KEY="token-from-workspace-env"\n', encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True):
                config = load_config(
                    config_path=None,
                    env_file=str(env_file),
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
        self.assertEqual(config.api_key, "token-from-install-env")
        self.assertEqual(config.workspace, workspace)

    def test_explicit_env_file_must_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "Env file not found"):
                    load_config(
                        config_path=None,
                        env_file=str(workspace / "missing.env"),
                        cwd=str(workspace),
                        provider="bailian",
                        api_base_url=None,
                        api_key=None,
                        model=None,
                        max_steps=None,
                        budget_seconds=None,
                        approval_mode=None,
                    )

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
        self.assertEqual(config.tool_approval, {"run_tests": "allow", "git_diff": "allow"})

    def test_tool_approval_can_come_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {"DASHSCOPE_API_KEY": "token", "AGENT_TOOL_APPROVAL": "shell=deny, run_tests=allow"},
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

        self.assertEqual(config.tool_approval, {"shell": "deny", "run_tests": "allow"})

    def test_tools_config_can_set_approval_mode_and_tool_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "agent.json"
            config_path.write_text(
                json.dumps(
                    {
                        "tools": {
                            "approvalMode": "write",
                            "approval": {
                                "shell": "prompt",
                                "write_file": "deny",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "token"}, clear=True):
                config = load_config(
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

        self.assertEqual(config.approval_mode, "write")
        self.assertEqual(config.tool_approval, {"shell": "prompt", "write_file": "deny"})

    def test_tools_config_wins_over_legacy_top_level_approval_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "agent.json"
            config_path.write_text(
                json.dumps(
                    {
                        "approval_mode": "yolo",
                        "tool_approval": {"shell": "allow"},
                        "tools": {
                            "approvalMode": "write",
                            "approval": {
                                "shell": "prompt",
                                "write_file": "deny",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "token"}, clear=True):
                config = load_config(
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

        self.assertEqual(config.approval_mode, "write")
        self.assertEqual(config.tool_approval, {"shell": "prompt", "write_file": "deny"})

    def test_legacy_approval_mode_aliases_are_normalized(self) -> None:
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
                    approval_mode="auto-read",
                )

        self.assertEqual(config.approval_mode, "always-ask")

    def test_tool_approval_explicit_policy_wins_over_auto_approve_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {
                    "DASHSCOPE_API_KEY": "token",
                    "AGENT_AUTO_APPROVE_TOOLS": "shell,run_tests",
                    "AGENT_TOOL_APPROVAL": "shell=deny",
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

        self.assertEqual(config.tool_approval, {"shell": "deny", "run_tests": "allow"})

    def test_tool_approval_rejects_invalid_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {"DASHSCOPE_API_KEY": "token", "AGENT_TOOL_APPROVAL": "shell=maybe"},
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "tool_approval.shell must be one of"):
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
                    )

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

    def test_summary_mode_can_come_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {
                    "DASHSCOPE_API_KEY": "token",
                    "AGENT_SUMMARY_MODE": "llm",
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

        self.assertEqual(config.summary_mode, "llm")

    def test_summary_mode_auto_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {
                    "DASHSCOPE_API_KEY": "token",
                    "AGENT_SUMMARY_MODE": "auto",
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

        self.assertEqual(config.summary_mode, "auto")

    def test_summary_mode_rejects_unknown_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {"DASHSCOPE_API_KEY": "token", "AGENT_SUMMARY_MODE": "remote"},
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "summary_mode must be one of"):
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
                    )

    def test_memory_consolidation_can_come_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {
                    "DASHSCOPE_API_KEY": "token",
                    "AGENT_MEMORY_CONSOLIDATION": "auto",
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

        self.assertEqual(config.memory_consolidation, "auto")

    def test_memory_consolidation_boolean_aliases_are_supported(self) -> None:
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
                    memory_consolidation="true",
                )

        self.assertEqual(config.memory_consolidation, "auto")

    def test_memory_consolidation_rejects_unknown_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {"DASHSCOPE_API_KEY": "token", "AGENT_MEMORY_CONSOLIDATION": "sometimes"},
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "memory_consolidation must be one of"):
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
                    )

    def test_allowed_dirs_can_come_from_cli_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            allowed = workspace / "requirements"
            allowed.mkdir()
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "token"}, clear=True):
                config = load_config(
                    config_path=None,
                    cwd=str(workspace),
                    provider="bailian",
                    api_base_url=None,
                    api_key=None,
                    model=None,
                    max_steps=None,
                    budget_seconds=None,
                    approval_mode=None,
                    allowed_dirs=[str(allowed)],
                )

        self.assertEqual(config.allowed_dirs, (allowed.resolve(),))

    def test_allowed_dirs_can_come_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            docs = workspace / "docs-outside"
            specs = workspace / "specs-outside"
            docs.mkdir()
            specs.mkdir()
            with patch.dict(
                "os.environ",
                {
                    "DASHSCOPE_API_KEY": "token",
                    "AGENT_ALLOWED_DIRS": os.pathsep.join([str(docs), str(specs)]),
                },
                clear=True,
            ):
                config = load_config(
                    config_path=None,
                    cwd=str(workspace),
                    provider="bailian",
                    api_base_url=None,
                    api_key=None,
                    model=None,
                    max_steps=None,
                    budget_seconds=None,
                    approval_mode=None,
                )

        self.assertEqual(config.allowed_dirs, (docs.resolve(), specs.resolve()))

    def test_allowed_dirs_reject_missing_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "token"}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "allowed_dirs entry does not exist"):
                    load_config(
                        config_path=None,
                        cwd=str(workspace),
                        provider="bailian",
                        api_base_url=None,
                        api_key=None,
                        model=None,
                        max_steps=None,
                        budget_seconds=None,
                        approval_mode=None,
                        allowed_dirs=[str(workspace / "missing")],
                    )


if __name__ == "__main__":
    unittest.main()

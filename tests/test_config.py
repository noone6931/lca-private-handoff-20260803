from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.config import load_config
from local_agent.state import workspace_state_dir
from local_agent.tools.process_environment import build_child_process_environment


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
        self.assertEqual(config.context_token_budget, 0)
        self.assertEqual(config.context_recent_messages, 40)
        self.assertEqual(config.summary_mode, "auto")
        self.assertEqual(config.memory_consolidation, "off")
        self.assertEqual(config.memory_scope, "state")
        self.assertEqual(config.approval_mode, "always-ask")
        self.assertEqual(config.allowed_dirs, ())
        self.assertEqual(config.reviewer_model, "")

    def test_reviewer_model_role_resolves_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {"DASHSCOPE_API_KEY": "token", "AI_REVIEWER_MODEL": "qwen-reviewer"},
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

        self.assertEqual(config.model, "qwen-plus")
        self.assertEqual(config.reviewer_model, "qwen-reviewer")

    def test_reviewer_model_role_resolves_from_models_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "agent.json"
            config_path.write_text(
                json.dumps({"models": {"reviewer": "slow-reviewer"}}),
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

        self.assertEqual(config.reviewer_model, "slow-reviewer")

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

    def test_user_config_env_is_shared_by_stable_and_dev_and_precedes_workspace_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            user_config = root / "user-config"
            user_config.mkdir()
            (user_config / ".env").write_text("DASHSCOPE_API_KEY=user-token\n", encoding="utf-8")
            (workspace / ".env").write_text("DASHSCOPE_API_KEY=workspace-token\n", encoding="utf-8")
            explicit = root / "explicit.env"
            explicit.write_text("DASHSCOPE_API_KEY=explicit-token\n", encoding="utf-8")
            with patch.dict("os.environ", {"AGENT_CONFIG_DIR": str(user_config)}, clear=True):
                shared = load_config(
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
            with patch.dict("os.environ", {"AGENT_CONFIG_DIR": str(user_config)}, clear=True):
                overridden = load_config(
                    config_path=None,
                    env_file=str(explicit),
                    cwd=str(workspace),
                    provider="bailian",
                    api_base_url=None,
                    api_key=None,
                    model=None,
                    max_steps=None,
                    budget_seconds=None,
                    approval_mode=None,
                )

        self.assertEqual(shared.api_key, "user-token")
        self.assertEqual(overridden.api_key, "explicit-token")

    def test_workspace_dotenv_projects_only_scoped_credentials_without_process_pollution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            allowed = workspace / "outside"
            allowed.mkdir()
            (workspace / ".env").write_text(
                "\n".join(
                    (
                        'export DASHSCOPE_API_KEY="workspace-token"',
                        "BAILIAN_API_KEY=secondary-token",
                        "dashscope_api_key=mixed-case-token",
                        "AGENT_APPROVAL_MODE=yolo",
                        "AGENT_TOOL_APPROVAL=shell=allow",
                        "AGENT_AUTO_APPROVE_TOOLS=shell",
                        f"AGENT_ALLOWED_DIRS={allowed}",
                        "LCA_ENABLE_WEB_SEARCH=true",
                        "AI_API_BASE_URL=https://untrusted.example/v1",
                        "AI_MODEL=untrusted-model",
                        "AI_VISION_MODEL=untrusted-vision",
                        "AI_REVIEWER_MODEL=untrusted-reviewer",
                        "AGENT_LSP_MODE=external",
                        "PATH=/untrusted/bin",
                        "HOME=/untrusted/home",
                        "XDG_CONFIG_HOME=/untrusted/config",
                        "DATABASE_URL=postgres://untrusted",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
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
                process_snapshot = dict(os.environ)
                child = build_child_process_environment(parent=os.environ)

        self.assertEqual(config.provider, "bailian")
        self.assertEqual(config.api_key, "workspace-token")
        self.assertEqual(config.api_base_url, "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.assertEqual(config.model, "qwen-plus")
        self.assertEqual(config.approval_mode, "always-ask")
        self.assertEqual(config.auto_approve_tools, ())
        self.assertEqual(config.tool_approval, {})
        self.assertEqual(config.allowed_dirs, ())
        self.assertFalse(config.enable_web_search)
        self.assertEqual(config.vision_model, "")
        self.assertEqual(config.reviewer_model, "")
        for key in (
            "DASHSCOPE_API_KEY",
            "BAILIAN_API_KEY",
            "dashscope_api_key",
            "AGENT_APPROVAL_MODE",
            "AGENT_TOOL_APPROVAL",
            "AGENT_AUTO_APPROVE_TOOLS",
            "AGENT_ALLOWED_DIRS",
            "LCA_ENABLE_WEB_SEARCH",
            "AI_API_BASE_URL",
            "AI_MODEL",
            "AI_VISION_MODEL",
            "AI_REVIEWER_MODEL",
            "AGENT_LSP_MODE",
            "PATH",
            "HOME",
            "XDG_CONFIG_HOME",
            "DATABASE_URL",
        ):
            self.assertNotIn(key, process_snapshot)
            self.assertNotIn(key, child.values)

    def test_workspace_dotenv_accepts_only_exact_credential_aliases(self) -> None:
        cases = (
            ("DASHSCOPE_API_KEY", "bailian"),
            ("BAILIAN_API_KEY", "bailian"),
            ("AI_API_KEY", "openai-compatible"),
        )
        for key, expected_provider in cases:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp).resolve()
                (workspace / ".env").write_text(
                    f'export {key}="workspace-token"\n',
                    encoding="utf-8",
                )
                with patch.dict("os.environ", {}, clear=True):
                    config = load_config(
                        config_path=None,
                        cwd=str(workspace),
                        provider=None,
                        api_base_url=(
                            "https://trusted.example/v1"
                            if expected_provider == "openai-compatible"
                            else None
                        ),
                        api_key=None,
                        model="trusted-model" if expected_provider == "openai-compatible" else None,
                        max_steps=None,
                        budget_seconds=None,
                        approval_mode=None,
                    )
                    self.assertNotIn(key, os.environ)

                self.assertEqual(config.provider, expected_provider)
                self.assertEqual(config.api_key, "workspace-token")

    def test_dotenv_credentials_choose_source_before_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / ".env").write_text("DASHSCOPE_API_KEY=workspace-dash\n", encoding="utf-8")
            explicit = root / "explicit.env"
            user_config = root / "user-config"
            user_config.mkdir()

            def loaded(
                process: dict[str, str],
                *,
                explicit_text: str = "",
                user_text: str = "",
            ):
                explicit.write_text(explicit_text, encoding="utf-8")
                (user_config / ".env").write_text(user_text, encoding="utf-8")
                environment = {"AGENT_CONFIG_DIR": str(user_config), **process}
                with patch.dict("os.environ", environment, clear=True):
                    return load_config(
                        config_path=None,
                        env_file=str(explicit),
                        cwd=str(workspace),
                        provider=None,
                        api_base_url="https://trusted.example/v1",
                        api_key=None,
                        model="trusted-model",
                        max_steps=None,
                        budget_seconds=None,
                        approval_mode=None,
                    )

            process_ai = loaded({"AI_API_KEY": "process-ai"})
            explicit_ai = loaded(
                {},
                explicit_text="AI_API_KEY=explicit-ai\n",
                user_text="DASHSCOPE_API_KEY=user-dash\n",
            )
            user_ai = loaded({}, user_text="AI_API_KEY=user-ai\n")
            process_dash = loaded(
                {"DASHSCOPE_API_KEY": "process-dash"},
                explicit_text="AI_API_KEY=explicit-ai\n",
            )

        self.assertEqual((process_ai.provider, process_ai.api_key), ("openai-compatible", "process-ai"))
        self.assertEqual((explicit_ai.provider, explicit_ai.api_key), ("openai-compatible", "explicit-ai"))
        self.assertEqual((user_ai.provider, user_ai.api_key), ("openai-compatible", "user-ai"))
        self.assertEqual((process_dash.provider, process_dash.api_key), ("bailian", "process-dash"))

    def test_configured_api_key_prevents_lower_dotenv_alias_autodetection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            config_path = workspace / "agent.json"
            config_path.write_text(
                json.dumps(
                    {
                        "api_key": "configured-key",
                        "api_base_url": "https://trusted.example/v1",
                        "model": "trusted-model",
                    }
                ),
                encoding="utf-8",
            )
            (workspace / ".env").write_text("DASHSCOPE_API_KEY=workspace-dash\n", encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True):
                config = load_config(
                    config_path=str(config_path),
                    cwd=str(workspace),
                    provider=None,
                    api_base_url=None,
                    api_key=None,
                    model=None,
                    max_steps=None,
                    budget_seconds=None,
                    approval_mode=None,
                )

        self.assertEqual(config.provider, "openai-compatible")
        self.assertEqual(config.api_key, "configured-key")

    def test_trusted_explicit_and_user_dotenv_keep_full_configuration_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            allowed = root / "allowed"
            allowed.mkdir()
            explicit = root / "explicit.env"
            explicit.write_text(
                "\n".join(
                    (
                        "DASHSCOPE_API_KEY=explicit-token",
                        "AI_API_BASE_URL=https://explicit.example/v1",
                        "AI_MODEL=explicit-model",
                        "AGENT_APPROVAL_MODE=yolo",
                        "AGENT_TOOL_APPROVAL=shell=allow",
                        f"AGENT_ALLOWED_DIRS={allowed}",
                        "LCA_ENABLE_WEB_SEARCH=true",
                        "AGENT_LSP_MODE=external",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                explicit_config = load_config(
                    config_path=None,
                    env_file=str(explicit),
                    cwd=str(workspace),
                    provider=None,
                    api_base_url=None,
                    api_key=None,
                    model=None,
                    max_steps=None,
                    budget_seconds=None,
                    approval_mode=None,
                )
                self.assertEqual(os.environ.get("AGENT_LSP_MODE"), "external")

            user_config = root / "user-config"
            user_config.mkdir()
            (user_config / ".env").write_text(
                "DASHSCOPE_API_KEY=user-token\n"
                "AI_API_BASE_URL=https://user.example/v1\n"
                "AI_MODEL=user-model\n"
                "AGENT_APPROVAL_MODE=yolo\n",
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"AGENT_CONFIG_DIR": str(user_config)},
                clear=True,
            ):
                user_config_result = load_config(
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

        self.assertEqual(explicit_config.api_key, "explicit-token")
        self.assertEqual(explicit_config.api_base_url, "https://explicit.example/v1")
        self.assertEqual(explicit_config.model, "explicit-model")
        self.assertEqual(explicit_config.approval_mode, "yolo")
        self.assertEqual(explicit_config.tool_approval, {"shell": "allow"})
        self.assertEqual(explicit_config.allowed_dirs, (allowed.resolve(),))
        self.assertTrue(explicit_config.enable_web_search)
        self.assertEqual(user_config_result.api_key, "user-token")
        self.assertEqual(user_config_result.api_base_url, "https://user.example/v1")
        self.assertEqual(user_config_result.model, "user-model")
        self.assertEqual(user_config_result.approval_mode, "yolo")

    def test_workspace_dotenv_rejects_symlinks_and_nonregular_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            external = root / "external.env"
            external.write_text("DASHSCOPE_API_KEY=external\n", encoding="utf-8")
            cases: list[tuple[str, object]] = [
                ("external symlink", external),
                ("dangling symlink", root / "missing.env"),
                ("directory", None),
            ]
            for name, target in cases:
                with self.subTest(name=name):
                    workspace = root / name.replace(" ", "-")
                    workspace.mkdir()
                    dotenv = workspace / ".env"
                    if name == "directory":
                        dotenv.mkdir()
                    else:
                        dotenv.symlink_to(target)
                    with patch.dict("os.environ", {}, clear=True):
                        with self.assertRaisesRegex(RuntimeError, "regular non-symlink"):
                            load_config(
                                config_path=None,
                                cwd=str(workspace),
                                provider="bailian",
                                api_base_url=None,
                                api_key="token",
                                model=None,
                                max_steps=None,
                                budget_seconds=None,
                                approval_mode=None,
                            )

            internal_workspace = root / "internal"
            internal_workspace.mkdir()
            internal_target = internal_workspace / "credentials.env"
            internal_target.write_text("DASHSCOPE_API_KEY=internal\n", encoding="utf-8")
            (internal_workspace / ".env").symlink_to(internal_target)
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "regular non-symlink"):
                    load_config(
                        config_path=None,
                        cwd=str(internal_workspace),
                        provider="bailian",
                        api_base_url=None,
                        api_key="token",
                        model=None,
                        max_steps=None,
                        budget_seconds=None,
                        approval_mode=None,
                    )

            if hasattr(os, "mkfifo"):
                fifo_workspace = root / "fifo"
                fifo_workspace.mkdir()
                os.mkfifo(fifo_workspace / ".env")
                with patch.dict("os.environ", {}, clear=True):
                    with self.assertRaisesRegex(RuntimeError, "regular non-symlink"):
                        load_config(
                            config_path=None,
                            cwd=str(fifo_workspace),
                            provider="bailian",
                            api_base_url=None,
                            api_key="token",
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
                    "AGENT_CONTEXT_TOKEN_BUDGET": "32000",
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
        self.assertEqual(config.context_token_budget, 32000)
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

    def test_memory_scope_can_come_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {
                    "DASHSCOPE_API_KEY": "token",
                    "AGENT_MEMORY_SCOPE": "project",
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

        self.assertEqual(config.memory_scope, "project")

    def test_memory_scope_aliases_are_supported(self) -> None:
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
                    memory_scope="workspace",
                )

        self.assertEqual(config.memory_scope, "project")

    def test_memory_scope_rejects_unknown_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {"DASHSCOPE_API_KEY": "token", "AGENT_MEMORY_SCOPE": "global"},
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "memory_scope must be one of"):
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

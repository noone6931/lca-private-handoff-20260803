from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent.cli import main
from local_agent.config import ConfigError
from local_agent.config import load_config
from local_agent.execution.isolation_config import IsolationConfigOverrides
from local_agent.protocol.commands import CommandResult
from local_agent.runtime.tool_context import build_isolation_process_runner
from local_agent.tools.process_environment import build_container_control_environment


_DIGEST = "1" * 64
_IMAGE = f"sha256:{'2' * 64}"


class _Commands:
    def dispatch(self, command) -> CommandResult:
        return CommandResult(command.command_id, "session", "run", "ok", {"content": "ok"})


class _CliRuntime:
    config = None

    def __init__(self, config, **_kwargs) -> None:
        type(self).config = config
        self.commands = _Commands()


class IsolationConfigurationTests(unittest.TestCase):
    def test_default_is_off_and_environment_cannot_enable_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".env").write_text(
                "LCA_ISOLATION_MODE=required\nDOCKER_HOST=tcp://attacker\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "DASHSCOPE_API_KEY": "token",
                    "LCA_ISOLATION_MODE": "required",
                    "DOCKER_HOST": "tcp://attacker",
                },
                clear=True,
            ):
                config = _load(workspace)

        self.assertEqual(config.isolation.mode, "off")
        self.assertIsNone(config.isolation.container)
        self.assertIsNone(build_isolation_process_runner(config))

    def test_json_authority_is_strict_and_cli_overrides_policy_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            config_path = workspace / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "isolation": {
                            "mode": "preferred",
                            "profile": "read-only",
                            "backend": "container",
                            "network_policy": "allow",
                            "container": _authority(),
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = _load(
                workspace,
                config_path=config_path,
                isolation_overrides=IsolationConfigOverrides(
                    mode="required",
                    profile="workspace-write",
                    network_policy="deny",
                ),
            )

        self.assertEqual(config.isolation.mode, "required")
        self.assertEqual(config.isolation.profile, "workspace-write")
        self.assertEqual(config.isolation.backend, "container")
        self.assertEqual(config.isolation.network_policy, "deny")
        self.assertEqual(
            config.isolation.container.executable,
            Path("/private/trusted/docker"),
        )
        self.assertEqual(
            config.isolation.container.workspace_transport,
            "direct-bind",
        )
        self.assertIsNone(config.isolation.container.staging_root)
        self.assertIsNotNone(build_isolation_process_runner(config))

    def test_partial_unknown_and_unpinned_authority_fail_closed(self) -> None:
        cases = (
            {"isolation": {"container": {"executable": "/private/docker"}}},
            {
                "isolation": {
                    "container": {
                        key: value
                        for key, value in _authority().items()
                        if key != "workspace_transport"
                    }
                }
            },
            {"isolation": {"silent_fallback": True}},
            {
                "isolation": {
                    "container": {
                        **_authority(),
                        "gate_image": "latest",
                    }
                }
            },
            {
                "isolation": {
                    "container": {
                        **_authority(),
                        "workspace_transport": "staged-copy",
                    }
                }
            },
            {
                "isolation": {
                    "container": {
                        **_authority(),
                        "workspace_transport": "direct-bind",
                        "staging_root": "/private/lca-staging",
                    }
                }
            },
        )
        for payload in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                path = workspace / "config.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    _load(workspace, config_path=path)

    def test_staged_copy_requires_explicit_private_root_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            path = workspace / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "isolation": {
                            "mode": "required",
                            "backend": "container",
                            "container": {
                                **_authority(),
                                "workspace_transport": "staged-copy",
                                "staging_root": "/private/lca-staging",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = _load(workspace, config_path=path)

        assert config.isolation.container is not None
        self.assertEqual(
            config.isolation.container.workspace_transport,
            "staged-copy",
        )
        self.assertEqual(
            config.isolation.container.staging_root,
            Path("/private/lca-staging"),
        )

    def test_container_control_environment_is_fixed_and_parent_independent(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AI_API_KEY": "secret",
                "DOCKER_HOST": "tcp://attacker",
                "DOCKER_CONTEXT": "attacker",
                "HTTP_PROXY": "http://attacker",
                "PATH": "/workspace/bin",
            },
            clear=True,
        ):
            environment = build_container_control_environment(
                client_config_directory=Path("/private/empty-docker-config")
            )

        self.assertEqual(
            set(environment.values),
            {
                "DOCKER_CONFIG",
                "GIT_PAGER",
                "GIT_TERMINAL_PROMPT",
                "HOME",
                "LANG",
                "LC_ALL",
                "MANPAGER",
                "NO_COLOR",
                "PAGER",
                "PATH",
                "PYTHONUNBUFFERED",
                "TMPDIR",
            },
        )
        self.assertEqual(environment.values["PATH"], os.defpath)
        self.assertEqual(
            environment.values["DOCKER_CONFIG"],
            "/private/empty-docker-config",
        )
        self.assertNotIn("AI_API_KEY", environment.values)
        self.assertNotIn("DOCKER_HOST", environment.values)
        self.assertNotIn("HTTP_PROXY", environment.values)

    def test_cli_projects_only_explicit_isolation_authority(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "local_agent.cli.AgentRuntime",
            _CliRuntime,
        ), redirect_stdout(output):
            status = main(
                [
                    "--cwd", tmp,
                    "--provider", "bailian",
                    "--api-key", "token",
                    "--isolation-mode", "required",
                    "--isolation-backend", "container",
                    "--container-executable", "/private/trusted/docker",
                    "--container-executable-sha256", _DIGEST,
                    "--container-socket-path", "/private/run/docker.sock",
                    "--container-client-config-dir", "/private/docker-empty",
                    "--container-gate-image", _IMAGE,
                    "--container-workspace-transport", "direct-bind",
                    "probe",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(output.getvalue().strip(), "ok")
        self.assertEqual(_CliRuntime.config.isolation.mode, "required")
        self.assertEqual(
            _CliRuntime.config.isolation.container.socket_path,
            Path("/private/run/docker.sock"),
        )


def _authority() -> dict[str, str]:
    return {
        "executable": "/private/trusted/docker",
        "executable_sha256": _DIGEST,
        "socket_path": "/private/run/docker.sock",
        "client_config_directory": "/private/docker-empty",
        "gate_image": _IMAGE,
        "workspace_transport": "direct-bind",
    }


def _load(
    workspace: Path,
    *,
    config_path: Path | None = None,
    isolation_overrides: IsolationConfigOverrides | None = None,
):
    with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "token"}, clear=True):
        return load_config(
            config_path=str(config_path) if config_path is not None else None,
            cwd=str(workspace),
            provider="bailian",
            api_base_url=None,
            api_key=None,
            model=None,
            max_steps=None,
            budget_seconds=None,
            approval_mode=None,
            isolation_overrides=isolation_overrides,
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.tools.base import ToolContext
from local_agent.tools.process_environment import build_child_process_environment
from local_agent.tools.shell import run_shell, run_tests


class ProcessEnvironmentTests(unittest.TestCase):
    def test_projection_removes_provider_credentials_and_preserves_parent(self) -> None:
        parent = {
            "PATH": "/trusted/bin",
            "HOME": "/home/sample",
            "JAVA_HOME": "/java8",
            "PAGER": "less",
            "AI_API_KEY": "ai-secret",
            "DashScope_Api_Key": "dash-secret",
            "BAILIAN_API_KEY": "bailian-secret",
        }

        projected = build_child_process_environment(
            parent=parent,
            overrides={"PAGER": "more", "CUSTOM_TOOLCHAIN": "enabled", "ai_api_key": "reinserted"},
        )

        self.assertEqual(projected.values["PATH"], "/trusted/bin")
        self.assertEqual(projected.values["HOME"], "/home/sample")
        self.assertEqual(projected.values["JAVA_HOME"], "/java8")
        self.assertEqual(projected.values["PAGER"], "more")
        self.assertEqual(projected.values["CUSTOM_TOOLCHAIN"], "enabled")
        self.assertEqual(projected.values["GIT_PAGER"], "cat")
        self.assertEqual(projected.values["MANPAGER"], "cat")
        self.assertEqual(projected.values["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(projected.values["PYTHONUNBUFFERED"], "1")
        self.assertEqual(projected.values["NO_COLOR"], "1")
        self.assertNotIn("CI", projected.values)
        for key in ("AI_API_KEY", "DashScope_Api_Key", "BAILIAN_API_KEY", "ai_api_key"):
            self.assertNotIn(key, projected.values)
        self.assertEqual(parent["PAGER"], "less")
        self.assertEqual(parent["AI_API_KEY"], "ai-secret")

    def test_shell_child_receives_sanitized_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            probe = workspace / "probe.py"
            probe.write_text(
                "import json, os\n"
                "keys = ('AI_API_KEY', 'DASHSCOPE_API_KEY', 'BAILIAN_API_KEY', "
                "'CUSTOM_TOOLCHAIN', 'PAGER', 'GIT_PAGER', 'CI')\n"
                "print(json.dumps({key: os.environ.get(key) for key in keys}, sort_keys=True))\n",
                encoding="utf-8",
            )
            parent_values = {
                "AI_API_KEY": "ai-secret",
                "DASHSCOPE_API_KEY": "dash-secret",
                "BAILIAN_API_KEY": "bailian-secret",
                "CUSTOM_TOOLCHAIN": "kept",
                "PAGER": "less",
            }
            with patch.dict(os.environ, parent_values, clear=False):
                result = run_shell(
                    {"command": f"{shlex.quote(sys.executable)} {shlex.quote(str(probe))}"},
                    ToolContext(workspace=workspace, approval_mode="yolo"),
                )
                self.assertEqual(os.environ["AI_API_KEY"], "ai-secret")
                self.assertEqual(os.environ["PAGER"], "less")

        self.assertFalse(result.is_error)
        observed = json.loads(result.content.splitlines()[0])
        self.assertIsNone(observed["AI_API_KEY"])
        self.assertIsNone(observed["DASHSCOPE_API_KEY"])
        self.assertIsNone(observed["BAILIAN_API_KEY"])
        self.assertEqual(observed["CUSTOM_TOOLCHAIN"], "kept")
        self.assertEqual(observed["PAGER"], "less")
        self.assertEqual(observed["GIT_PAGER"], "cat")
        self.assertIsNone(observed["CI"])

    def test_run_tests_overrides_defaults_but_cannot_reinsert_provider_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            tests = workspace / "tests"
            tests.mkdir()
            (tests / "test_child_env.py").write_text(
                "import os, unittest\n"
                "class ChildEnvironmentTests(unittest.TestCase):\n"
                "    def test_environment(self):\n"
                "        self.assertNotIn('AI_API_KEY', os.environ)\n"
                "        self.assertNotIn('DASHSCOPE_API_KEY', os.environ)\n"
                "        self.assertNotIn('BAILIAN_API_KEY', os.environ)\n"
                "        self.assertEqual(os.environ.get('CUSTOM_TOOLCHAIN'), 'explicit')\n"
                "        self.assertEqual(os.environ.get('PAGER'), 'explicit-pager')\n"
                "        self.assertEqual(os.environ.get('GIT_PAGER'), 'cat')\n"
                "        self.assertNotIn('CI', os.environ)\n",
                encoding="utf-8",
            )
            command = (
                "PYTHONPATH=. CUSTOM_TOOLCHAIN=explicit PAGER=explicit-pager "
                "AI_API_KEY=reinserted python3 -m unittest tests.test_child_env"
            )
            parent_values = {
                "AI_API_KEY": "ai-secret",
                "DASHSCOPE_API_KEY": "dash-secret",
                "BAILIAN_API_KEY": "bailian-secret",
                "CUSTOM_TOOLCHAIN": "parent",
            }
            with patch.dict(os.environ, parent_values, clear=False):
                result = run_tests(
                    {"command": command, "timeout": 10},
                    ToolContext(workspace=workspace, approval_mode="yolo"),
                )
                self.assertEqual(os.environ["CUSTOM_TOOLCHAIN"], "parent")
                self.assertEqual(os.environ["AI_API_KEY"], "ai-secret")

        self.assertFalse(result.is_error, result.content)
        self.assertEqual(
            result.metadata["environment_keys"],
            ["AI_API_KEY", "CUSTOM_TOOLCHAIN", "PAGER", "PYTHONPATH"],
        )
        self.assertFalse(result.metadata["sandboxed"])
        self.assertNotIn("dash-secret", str(result.metadata))
        self.assertNotIn("bailian-secret", str(result.metadata))

    def test_environment_reference_cannot_alias_parent_provider_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            tests = workspace / "tests"
            tests.mkdir()
            (tests / "test_alias.py").write_text(
                "import os, unittest\n"
                "class AliasTests(unittest.TestCase):\n"
                "    def test_alias(self): self.assertEqual(os.environ.get('COPIED_KEY'), '')\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"DaShScOpE_aPi_KeY": "must-not-copy"}, clear=False):
                result = run_tests(
                    {"command": "PYTHONPATH=. COPIED_KEY=$DaShScOpE_aPi_KeY python3 -m unittest tests.test_alias"},
                    ToolContext(workspace=workspace, approval_mode="yolo"),
                )

        self.assertFalse(result.is_error, result.content)


if __name__ == "__main__":
    unittest.main()

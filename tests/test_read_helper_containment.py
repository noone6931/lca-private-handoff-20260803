from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.tools.base import ToolContext
from local_agent.tools.base import ToolResult
from local_agent.tools.git import _git_raw
from local_agent.tools.git import capture_git_baseline
from local_agent.tools.git import git_diff
from local_agent.tools.git import git_status
from local_agent.tools.search import search_code


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class GitReadHelperContainmentTests(unittest.TestCase):
    def _init_repo(self, workspace: Path, files: dict[str, str]) -> None:
        subprocess.run(["git", "init"], cwd=workspace, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=workspace, check=True)
        for relative_path, content in files.items():
            target = workspace / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=workspace, check=True)
        subprocess.run(["git", "commit", "-m", "baseline"], cwd=workspace, capture_output=True, check=True)

    def _write_marker_helper(self, path: Path, marker: Path) -> None:
        path.write_text(
            f"#!{sys.executable}\n"
            "from pathlib import Path\n"
            "import sys\n"
            f"Path({str(marker)!r}).write_text('called', encoding='utf-8')\n"
            "if len(sys.argv) > 1:\n"
            "    candidate = Path(sys.argv[-1])\n"
            "    if candidate.is_file():\n"
            "        print(candidate.read_text(encoding='utf-8', errors='replace'), end='')\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def test_git_diff_disables_external_diff_environment_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            marker = workspace / "external-diff.marker"
            helper = workspace / "external-diff-helper"
            self._init_repo(workspace, {"sample.txt": "before\n"})
            self._write_marker_helper(helper, marker)
            (workspace / "sample.txt").write_text("after\n", encoding="utf-8")

            with patch.dict(os.environ, {"GIT_EXTERNAL_DIFF": str(helper)}, clear=False):
                result = git_diff({}, ToolContext(workspace=workspace, approval_mode="yolo"))

            marker_exists = marker.exists()

        self.assertFalse(result.is_error, result.content)
        self.assertFalse(marker_exists)
        self.assertIn("after", result.content)
        self.assertEqual(result.metadata["external_process"], "git")
        self.assertFalse(result.metadata["sandboxed"])

    def test_git_diff_disables_attribute_textconv_and_command_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            textconv_marker = workspace / "textconv.marker"
            command_marker = workspace / "command.marker"
            textconv_helper = workspace / "textconv-helper"
            command_helper = workspace / "command-helper"
            self._write_marker_helper(textconv_helper, textconv_marker)
            self._write_marker_helper(command_helper, command_marker)
            self._init_repo(
                workspace,
                {
                    ".gitattributes": "*.textprobe diff=textprobe\n*.commandprobe diff=commandprobe\n",
                    "sample.textprobe": "before textconv\n",
                    "sample.commandprobe": "before command\n",
                },
            )
            subprocess.run(
                ["git", "config", "diff.textprobe.textconv", str(textconv_helper)],
                cwd=workspace,
                check=True,
            )
            subprocess.run(
                ["git", "config", "diff.commandprobe.command", str(command_helper)],
                cwd=workspace,
                check=True,
            )
            (workspace / "sample.textprobe").write_text("after textconv\n", encoding="utf-8")
            (workspace / "sample.commandprobe").write_text("after command\n", encoding="utf-8")

            result = git_diff({}, ToolContext(workspace=workspace, approval_mode="yolo"))

            textconv_marker_exists = textconv_marker.exists()
            command_marker_exists = command_marker.exists()

        self.assertFalse(result.is_error, result.content)
        self.assertFalse(textconv_marker_exists)
        self.assertFalse(command_marker_exists)
        self.assertIn("after textconv", result.content)
        self.assertIn("after command", result.content)

    def test_git_status_disables_configured_fsmonitor_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            marker = workspace / "fsmonitor.marker"
            helper = workspace / "fsmonitor-helper"
            self._init_repo(workspace, {"sample.txt": "before\n"})
            self._write_marker_helper(helper, marker)
            subprocess.run(["git", "config", "core.fsmonitor", str(helper)], cwd=workspace, check=True)
            subprocess.run(["git", "config", "core.fsmonitorHookVersion", "2"], cwd=workspace, check=True)
            (workspace / "sample.txt").write_text("after\n", encoding="utf-8")

            result = git_status({}, ToolContext(workspace=workspace, approval_mode="yolo"))

            marker_exists = marker.exists()

        self.assertFalse(result.is_error, result.content)
        self.assertFalse(marker_exists)
        self.assertIn("sample.txt", result.content)

    def test_conversion_filters_fail_closed_before_status_diff_or_baseline(self) -> None:
        for filter_kind in ("clean", "process", "smudge"):
            with self.subTest(filter_kind=filter_kind), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                marker = workspace / f"{filter_kind}.marker"
                helper = workspace / f"{filter_kind}-helper"
                self._init_repo(
                    workspace,
                    {
                        ".gitattributes": "*.txt filter=lcaevil\n",
                        "sample.txt": "before\n",
                    },
                )
                self._write_marker_helper(helper, marker)
                subprocess.run(
                    ["git", "config", f"filter.lcaevil.{filter_kind}", str(helper)],
                    cwd=workspace,
                    check=True,
                )
                (workspace / "sample.txt").write_text("after\n", encoding="utf-8")
                context = ToolContext(workspace=workspace, approval_mode="yolo")

                status = git_status({}, context)
                self._assert_filter_denial(status)
                self.assertFalse(marker.exists())

                diff = git_diff({}, context)
                self._assert_filter_denial(diff)
                self.assertFalse(marker.exists())

                baseline = capture_git_baseline(workspace)
                self.assertTrue(baseline["is_git_repo"])
                self.assertEqual(baseline["denial_kind"], "external_filter_unsupported")
                self.assertEqual(baseline["reason"], "read_tier_external_filter_unsupported")
                self.assertEqual(baseline["filter_count"], 1)
                self.assertEqual(baseline["external_process"], "git")
                self.assertFalse(baseline["sandboxed"])
                self.assertEqual(baseline["status_short"], "")
                self.assertFalse(marker.exists())

    def test_empty_filter_config_is_not_treated_as_an_executable_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._init_repo(
                workspace,
                {
                    ".gitattributes": "*.txt filter=empty\n",
                    "sample.txt": "before\n",
                },
            )
            subprocess.run(["git", "config", "filter.empty.clean", ""], cwd=workspace, check=True)
            (workspace / "sample.txt").write_text("after\n", encoding="utf-8")

            result = git_status({}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertFalse(result.is_error, result.content)
        self.assertIn("sample.txt", result.content)

    def test_filter_config_timeout_preserves_confirmed_repository_identity(self) -> None:
        def timeout_config(
            workspace: str | os.PathLike[str],
            args: list[str],
        ) -> subprocess.CompletedProcess[str]:
            if args and args[0] == "config":
                raise subprocess.TimeoutExpired(
                    ["git", "config"],
                    timeout=30,
                    output="secret config output",
                    stderr="secret config error",
                )
            if args == ["rev-parse", "--is-inside-work-tree"]:
                return subprocess.CompletedProcess(args, 0, "true\n", "")
            if args == ["rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, "confirmed-head\n", "")
            self.fail(f"unexpected git invocation: {args!r}")

        context = ToolContext(workspace=Path("/tmp/confirmed-repository"), approval_mode="yolo")
        with patch("local_agent.tools.git._run_git", side_effect=timeout_config):
            status = git_status({}, context)
        with patch("local_agent.tools.git._run_git", side_effect=timeout_config):
            baseline = capture_git_baseline(context.workspace)

        self.assertTrue(status.is_error)
        self.assertEqual(status.metadata["git_repository"], True)
        self.assertEqual(status.metadata["execution_status"], "not_run")
        self.assertEqual(status.metadata["denial_kind"], "external_filter_unsupported")
        self.assertEqual(status.metadata["reason"], "read_tier_external_filter_preflight_failed")
        self.assertEqual(status.metadata["external_process"], "git")
        self.assertFalse(status.metadata["sandboxed"])
        self.assertNotIn("secret config", status.content)

        self.assertTrue(baseline["is_git_repo"])
        self.assertEqual(baseline["head_revision"], "confirmed-head")
        self.assertEqual(baseline["denial_kind"], "external_filter_unsupported")
        self.assertEqual(baseline["reason"], "read_tier_external_filter_preflight_failed")
        self.assertEqual(baseline["external_process"], "git")
        self.assertFalse(baseline["sandboxed"])
        self.assertEqual(baseline["status_short"], "")
        self.assertNotIn("secret config", baseline["error"])

    def test_worktree_probe_start_failure_remains_unconfirmed(self) -> None:
        with patch(
            "local_agent.tools.git._run_git",
            side_effect=FileNotFoundError("git executable unavailable"),
        ):
            baseline = capture_git_baseline(Path("/tmp/unconfirmed-repository"))

        self.assertFalse(baseline["is_git_repo"])
        self.assertNotIn("denial_kind", baseline)
        self.assertNotIn("external_process", baseline)

    def _assert_filter_denial(self, result: ToolResult) -> None:
        self.assertTrue(result.is_error)
        metadata = result.metadata
        self.assertEqual(metadata["git_repository"], True)
        self.assertEqual(metadata["execution_status"], "not_run")
        self.assertEqual(metadata["denial_kind"], "external_filter_unsupported")
        self.assertEqual(metadata["reason"], "read_tier_external_filter_unsupported")
        self.assertEqual(metadata["filter_count"], 1)
        self.assertEqual(metadata["external_process"], "git")
        self.assertFalse(metadata["sandboxed"])
        self.assertIn("Read-tier external filters are unsupported", result.content)

    def test_git_config_environment_injection_cannot_start_fsmonitor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helper = workspace / "fsmonitor-helper"
            self._init_repo(workspace, {"sample.txt": "before\n"})
            (workspace / "sample.txt").write_text("after\n", encoding="utf-8")

            for source, environment in (
                (
                    "count",
                    {
                        "GIT_CONFIG_COUNT": "2",
                        "GIT_CONFIG_KEY_0": "core.fsmonitor",
                        "GIT_CONFIG_VALUE_0": str(helper),
                        "GIT_CONFIG_KEY_1": "core.fsmonitorHookVersion",
                        "GIT_CONFIG_VALUE_1": "2",
                    },
                ),
                (
                    "parameters",
                    {
                        "GIT_CONFIG_PARAMETERS": (
                            f"'core.fsmonitor'='{helper}' 'core.fsmonitorHookVersion'='2'"
                        )
                    },
                ),
            ):
                with self.subTest(source=source):
                    marker = workspace / f"{source}.marker"
                    self._write_marker_helper(helper, marker)
                    with patch.dict(os.environ, environment, clear=False):
                        result = git_status({}, ToolContext(workspace=workspace, approval_mode="yolo"))
                    self.assertFalse(result.is_error, result.content)
                    self.assertFalse(marker.exists())
                    self.assertIn("sample.txt", result.content)

    def test_git_child_environment_is_sanitized_without_mutating_parent(self) -> None:
        observed: dict[str, object] = {}

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            observed["command"] = command
            observed["environment"] = kwargs["env"]
            return subprocess.CompletedProcess(command, 0, "true\n", "")

        parent = {
            "Ai_Api_Key": "ai-secret",
            "BAILIAN_API_KEY": "bailian-secret",
            "dashscope_api_key": "dash-secret",
            "PATH": "/trusted/bin",
            "JAVA_HOME": "/java8",
            "PAGER": "less",
            "GIT_EXTERNAL_DIFF": "/tmp/helper",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_PARAMETERS": "'core.fsmonitor'='/tmp/helper'",
        }
        with (
            patch.dict(os.environ, parent, clear=True),
            patch("local_agent.tools.git.subprocess.run", side_effect=fake_run),
        ):
            _git_raw(Path("/tmp"), ["rev-parse", "--is-inside-work-tree"])
            self.assertEqual(os.environ["Ai_Api_Key"], "ai-secret")
            self.assertEqual(os.environ["GIT_EXTERNAL_DIFF"], "/tmp/helper")

        environment = observed["environment"]
        self.assertIsInstance(environment, dict)
        assert isinstance(environment, dict)
        for key in (
            "Ai_Api_Key",
            "BAILIAN_API_KEY",
            "dashscope_api_key",
            "GIT_EXTERNAL_DIFF",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_PARAMETERS",
        ):
            self.assertNotIn(key, environment)
        self.assertEqual(environment["PATH"], "/trusted/bin")
        self.assertEqual(environment["JAVA_HOME"], "/java8")
        self.assertEqual(environment["PAGER"], "less")
        self.assertEqual(environment["GIT_PAGER"], "cat")
        self.assertEqual(environment["MANPAGER"], "cat")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["PYTHONUNBUFFERED"], "1")
        self.assertEqual(environment["NO_COLOR"], "1")

        command = observed["command"]
        self.assertIsInstance(command, list)
        assert isinstance(command, list)
        self.assertEqual(command[:4], ["git", "--no-optional-locks", "-c", "core.fsmonitor=false"])

    def test_capture_git_baseline_remains_useful_under_containment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._init_repo(workspace, {"sample.txt": "before\n"})
            (workspace / "sample.txt").write_text("after\n", encoding="utf-8")

            baseline = capture_git_baseline(workspace)

        self.assertTrue(baseline["is_git_repo"])
        self.assertIn("sample.txt", baseline["status_short"])
        self.assertIn("sample.txt", baseline["diff_name_status"])


@unittest.skipUnless(shutil.which("rg"), "ripgrep is not installed")
class RipgrepReadHelperContainmentTests(unittest.TestCase):
    def test_search_code_ignores_configured_preprocessor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = workspace / "sample.txt"
            marker = workspace / "pre.marker"
            helper = workspace / "pre-helper"
            config = workspace / "ripgrep.conf"
            source.write_text("needle in source\n", encoding="utf-8")
            helper.write_text(
                f"#!{sys.executable}\n"
                "from pathlib import Path\n"
                "import sys\n"
                f"Path({str(marker)!r}).write_text('called', encoding='utf-8')\n"
                "print(Path(sys.argv[-1]).read_text(encoding='utf-8'), end='')\n",
                encoding="utf-8",
            )
            helper.chmod(0o755)
            config.write_text(f"--pre={helper}\n", encoding="utf-8")

            with patch.dict(os.environ, {"RIPGREP_CONFIG_PATH": str(config)}, clear=False):
                result = search_code(
                    {"pattern": "needle", "path": "."},
                    ToolContext(workspace=workspace, approval_mode="yolo"),
                )

            marker_exists = marker.exists()

        self.assertFalse(result.is_error, result.content)
        self.assertFalse(marker_exists)
        self.assertIn("needle in source", result.content)
        self.assertEqual(result.metadata["external_process"], "ripgrep")
        self.assertFalse(result.metadata["sandboxed"])

    def test_search_child_environment_is_sanitized_and_no_config_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "sample.txt").write_text("needle\n", encoding="utf-8")
            observed: dict[str, object] = {}

            def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                observed["command"] = command
                observed["environment"] = kwargs["env"]
                return subprocess.CompletedProcess(command, 0, "sample.txt:1:1:needle\n", "")

            parent = {
                "ai_api_key": "ai-secret",
                "Bailian_Api_Key": "bailian-secret",
                "DASHSCOPE_API_KEY": "dash-secret",
                "PATH": "/trusted/bin",
                "JAVA_HOME": "/java8",
                "PAGER": "less",
                "RIPGREP_CONFIG_PATH": "/tmp/ripgrep.conf",
            }
            with (
                patch.dict(os.environ, parent, clear=True),
                patch("local_agent.tools.search.subprocess.run", side_effect=fake_run),
            ):
                result = search_code(
                    {"pattern": "needle", "path": "."},
                    ToolContext(workspace=workspace, approval_mode="yolo"),
                )
                self.assertEqual(os.environ["ai_api_key"], "ai-secret")

        self.assertFalse(result.is_error, result.content)
        command = observed["command"]
        self.assertIsInstance(command, list)
        assert isinstance(command, list)
        self.assertIn("--no-config", command)
        environment = observed["environment"]
        self.assertIsInstance(environment, dict)
        assert isinstance(environment, dict)
        for key in ("ai_api_key", "Bailian_Api_Key", "DASHSCOPE_API_KEY"):
            self.assertNotIn(key, environment)
        self.assertEqual(environment["PATH"], "/trusted/bin")
        self.assertEqual(environment["JAVA_HOME"], "/java8")
        self.assertEqual(environment["PAGER"], "less")
        self.assertEqual(environment["GIT_PAGER"], "cat")
        self.assertEqual(environment["MANPAGER"], "cat")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["PYTHONUNBUFFERED"], "1")
        self.assertEqual(environment["NO_COLOR"], "1")


if __name__ == "__main__":
    unittest.main()

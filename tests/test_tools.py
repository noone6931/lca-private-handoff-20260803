from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.tools.base import Tool, ToolContext, ToolRegistry
from local_agent.tools.files import read_file, write_file
from local_agent.tools.git import git_diff
from local_agent.tools.search import list_files
from local_agent.tools.search import search_code
from local_agent.tools.shell import run_shell, run_tests


class ToolTests(unittest.TestCase):
    def test_list_files_skips_agent_and_cache_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "src").mkdir()
            (workspace / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
            (workspace / ".local-agent" / "sessions").mkdir(parents=True)
            (workspace / ".local-agent" / "sessions" / "s.jsonl").write_text("{}", encoding="utf-8")
            (workspace / "__pycache__").mkdir()
            (workspace / "__pycache__" / "app.pyc").write_text("cache", encoding="utf-8")

            result = list_files({}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertFalse(result.is_error)
        self.assertIn("src/app.py", result.content)
        self.assertNotIn(".local-agent", result.content)
        self.assertNotIn("__pycache__", result.content)

    def test_list_files_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            result = list_files({"path": "../outside"}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertTrue(result.is_error)
        self.assertIn("Path escapes workspace", result.content)

    def test_search_code_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            result = search_code(
                {"pattern": "secret", "path": "../outside"},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertTrue(result.is_error)
        self.assertIn("Path escapes workspace", result.content)

    @unittest.skipIf(shutil.which("rg") is None, "ripgrep is not installed")
    def test_search_code_returns_relative_paths_and_truncates_total_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "a.txt").write_text("needle one\n", encoding="utf-8")
            (workspace / "b.txt").write_text("needle two\n", encoding="utf-8")

            result = search_code(
                {"pattern": "needle", "max_results": 1},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertFalse(result.is_error)
        self.assertNotIn(str(workspace), result.content)
        self.assertNotIn("./", result.content)
        self.assertIn("needle", result.content)
        self.assertIn("... truncated after 1 matches", result.content)

    def test_read_file_rejects_large_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "large.txt"
            target.write_text("x" * (256 * 1024 + 1), encoding="utf-8")

            result = read_file({"path": "large.txt"}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertTrue(result.is_error)
        self.assertIn("File too large", result.content)

    def test_read_file_truncates_long_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "many.txt"
            target.write_text("".join(f"{i}\n" for i in range(500)), encoding="utf-8")

            result = read_file({"path": "many.txt"}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertFalse(result.is_error)
        self.assertIn("... truncated after line 400", result.content)

    def test_write_file_refuses_to_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "README.md"
            target.write_text("existing\n", encoding="utf-8")

            result = write_file(
                {"path": "README.md", "content": "replacement\n"},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertTrue(result.is_error)
        self.assertIn("Refusing to overwrite", result.content)

    def test_run_tests_runs_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            result = run_tests(
                {"command": "python3 -c \"print('ok')\"", "timeout": 10},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertFalse(result.is_error)
        self.assertIn("ok", result.content)
        self.assertIn("[exit_code] 0", result.content)

    def test_shell_rejects_dangerous_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            result = run_shell(
                {"command": "rm -rf /", "timeout": 10},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertTrue(result.is_error)
        self.assertIn("Refusing dangerous command", result.content)

    def test_write_tool_approval_in_non_interactive_stdin_returns_tool_error(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_write",
                    description="sample write",
                    tier="write",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "ok", "is_error": False})(),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="ask")
            with patch("sys.stdin.isatty", return_value=False):
                result = registry.execute("sample_write", "{}", context)

        self.assertTrue(result.is_error)
        self.assertIn("requires approval", result.content)
        self.assertIn("stdin is not interactive", result.content)

    def test_write_tool_approval_eof_returns_tool_error(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_write",
                    description="sample write",
                    tier="write",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "ok", "is_error": False})(),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="ask")
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=EOFError):
                result = registry.execute("sample_write", "{}", context)

        self.assertTrue(result.is_error)
        self.assertIn("stdin closed", result.content)

    def test_git_diff_explains_untracked_files_when_diff_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            subprocess.run(["git", "init"], cwd=workspace, text=True, capture_output=True, check=True)
            (workspace / "README.md").write_text("hello\n", encoding="utf-8")

            result = git_diff({}, ToolContext(workspace=workspace, approval_mode="yolo"))

        self.assertFalse(result.is_error)
        self.assertIn("(empty diff)", result.content)
        self.assertIn("?? README.md", result.content)
        self.assertIn("git diff does not show untracked files", result.content)

    def test_tool_registry_validates_required_enum_and_extra_args(self) -> None:
        calls: list[dict] = []

        def handler(args, context):
            calls.append(args)
            return type("Result", (), {"content": "ok", "is_error": False})()

        registry = ToolRegistry(
            [
                Tool(
                    name="sample",
                    description="sample",
                    tier="read",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "enum": ["project"]},
                            "count": {"type": "integer", "minimum": 1, "maximum": 3},
                        },
                        "required": ["name", "count"],
                        "additionalProperties": False,
                    },
                    handler=handler,
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="yolo")
            ok = registry.execute("sample", '{"name": "project", "count": "2"}', context)
            bad_enum = registry.execute("sample", '{"name": "other", "count": 1}', context)
            extra = registry.execute("sample", '{"name": "project", "count": 1, "extra": true}', context)

        self.assertFalse(ok.is_error)
        self.assertEqual(calls[0]["count"], 2)
        self.assertTrue(bad_enum.is_error)
        self.assertIn("must be one of", bad_enum.content)
        self.assertTrue(extra.is_error)
        self.assertIn("Unexpected argument", extra.content)


if __name__ == "__main__":
    unittest.main()

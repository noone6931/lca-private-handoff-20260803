from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.patch.anchored import hash_text
from local_agent.tools.base import Tool, ToolContext, ToolRegistry
from local_agent.tools.files import patch_file, read_file, rollback_patch, write_file
from local_agent.tools.git import git_diff
from local_agent.tools.interaction import ask_user
from local_agent.tools.search import list_files
from local_agent.tools.search import search_code
from local_agent.tools.shell import run_shell, run_tests
from local_agent.tools.todo import todo_add, todo_read, todo_update


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

    def test_patch_file_dry_run_previews_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "README.md"
            original = "old\n"
            target.write_text(original, encoding="utf-8")

            result = patch_file(
                {
                    "path": "README.md",
                    "tag": hash_text(original),
                    "start_line": 1,
                    "end_line": 1,
                    "old_text": "old",
                    "new_text": "new",
                    "dry_run": True,
                },
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )
            persisted = target.read_text(encoding="utf-8")

        self.assertFalse(result.is_error)
        self.assertIn("Patch preview only", result.content)
        self.assertIn("+new", result.content)
        self.assertEqual(persisted, original)

    def test_rollback_patch_restores_latest_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "README.md"
            original = "old\n"
            context = ToolContext(workspace=workspace, approval_mode="yolo", session_id="s1")
            target.write_text(original, encoding="utf-8")

            patch_result = patch_file(
                {
                    "path": "README.md",
                    "tag": hash_text(original),
                    "start_line": 1,
                    "end_line": 1,
                    "old_text": "old",
                    "new_text": "new",
                },
                context,
            )
            rollback_result = rollback_patch({}, context)
            second_rollback = rollback_patch({}, context)
            persisted = target.read_text(encoding="utf-8")

        self.assertFalse(patch_result.is_error)
        self.assertIn("Patch id:", patch_result.content)
        self.assertFalse(rollback_result.is_error)
        self.assertIn("Rolled back patch", rollback_result.content)
        self.assertIn("+old", rollback_result.content)
        self.assertEqual(persisted, original)
        self.assertTrue(second_rollback.is_error)
        self.assertIn("No unapplied patch record", second_rollback.content)

    def test_rollback_patch_refuses_when_file_changed_after_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "README.md"
            original = "old\n"
            context = ToolContext(workspace=workspace, approval_mode="yolo", session_id="s1")
            target.write_text(original, encoding="utf-8")

            patch_result = patch_file(
                {
                    "path": "README.md",
                    "tag": hash_text(original),
                    "start_line": 1,
                    "end_line": 1,
                    "old_text": "old",
                    "new_text": "new",
                },
                context,
            )
            target.write_text("manual\n", encoding="utf-8")
            rollback_result = rollback_patch({}, context)
            persisted = target.read_text(encoding="utf-8")

        self.assertFalse(patch_result.is_error)
        self.assertTrue(rollback_result.is_error)
        self.assertIn("Refusing rollback", rollback_result.content)
        self.assertEqual(persisted, "manual\n")

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

    def test_shell_timeout_is_clamped_to_remaining_budget(self) -> None:
        calls: list[dict] = []

        def fake_run(*args, **kwargs):
            calls.append(kwargs)
            return type("Completed", (), {"stdout": "ok\n", "stderr": "", "returncode": 0})()

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            context = ToolContext(
                workspace=workspace,
                approval_mode="yolo",
                deadline_monotonic=110.0,
            )
            with patch("local_agent.tools.shell.time.monotonic", return_value=100.0):
                with patch("local_agent.tools.shell.subprocess.run", side_effect=fake_run):
                    result = run_shell({"command": "echo ok", "timeout": 600}, context)

        self.assertFalse(result.is_error)
        self.assertEqual(calls[0]["timeout"], 10)

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

    def test_auto_approve_tool_bypasses_prompt_in_ask_mode(self) -> None:
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
            context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="ask",
                auto_approve_tools=("sample_write",),
            )
            with patch("sys.stdin.isatty", return_value=False):
                result = registry.execute("sample_write", "{}", context)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "ok")

    def test_state_tool_does_not_require_approval_in_ask_mode(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="sample_state",
                    description="sample state",
                    tier="state",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=lambda args, context: type("Result", (), {"content": "ok", "is_error": False})(),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="ask")
            with patch("sys.stdin.isatty", return_value=False):
                result = registry.execute("sample_state", "{}", context)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "ok")

    def test_todo_add_update_and_read_use_session_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            context = ToolContext(workspace=workspace, approval_mode="ask", session_id="session-1")

            added = todo_add({"id": "T1", "task": "Wire budget seconds"}, context)
            updated = todo_update({"id": "T1", "status": "done", "note": "covered by tests"}, context)
            read = todo_read({}, context)

        self.assertFalse(added.is_error)
        self.assertFalse(updated.is_error)
        self.assertFalse(read.is_error)
        self.assertIn("[done] T1: Wire budget seconds", read.content)
        self.assertIn("covered by tests", read.content)

    def test_ask_user_non_interactive_returns_tool_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="ask")
            with patch("sys.stdin.isatty", return_value=False):
                result = ask_user({"question": "Continue?"}, context)

        self.assertTrue(result.is_error)
        self.assertIn("stdin is not interactive", result.content)

    def test_ask_user_non_interactive_can_use_default_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="ask")
            with patch("sys.stdin.isatty", return_value=False):
                result = ask_user({"question": "Continue?", "default_answer": "skip"}, context)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "skip")

    def test_ask_user_returns_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="ask")
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="yes"):
                result = ask_user({"question": "Continue?"}, context)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "yes")

    def test_ask_user_timeout_can_use_default_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp).resolve(), approval_mode="ask")
            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("local_agent.tools.interaction._read_timed_answer", return_value=None),
            ):
                result = ask_user(
                    {"question": "Continue?", "timeout_seconds": 1, "default_answer": "continue"},
                    context,
                )

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "continue")

    def test_ask_user_uses_remaining_budget_as_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(
                workspace=Path(tmp).resolve(),
                approval_mode="ask",
                deadline_monotonic=105.0,
            )
            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("local_agent.tools.interaction.time.monotonic", return_value=100.0),
                patch("local_agent.tools.interaction._read_timed_answer", return_value="yes") as read_answer,
            ):
                result = ask_user({"question": "Continue?"}, context)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "yes")
        self.assertEqual(read_answer.call_args.args[1], 5)

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

from __future__ import annotations

import io
import unittest

from local_agent.frontends.terminal.command_registry import TerminalCommandRegistry


class _FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def approval_summary(self) -> str:
        return "approval summary"

    def status_summary(self) -> str:
        return "status summary"

    def tool_summary(self) -> str:
        return "tools summary"

    def workspace_summary(self) -> str:
        return "workspace summary"

    def add_workspace_root(self, path: str) -> None:
        self.calls.append(("workspace-add", path))

    def remove_workspace_root(self, path: str) -> None:
        self.calls.append(("workspace-remove", path))

    def reset_workspace_roots(self) -> None:
        self.calls.append(("workspace-reset", None))

    def move_workspace(self, path: str) -> None:
        self.calls.append(("workspace-move", path))

    def set_session_approval_mode(self, mode: str) -> None:
        self.calls.append(("approval-mode", mode))

    def set_session_tool_policy(self, tool: str, policy: str) -> None:
        self.calls.append((policy, tool))

    def reset_session_tool_policy(self, tool: str) -> None:
        self.calls.append(("approval-reset", tool))


class TerminalCommandRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = TerminalCommandRegistry()
        self.runtime = _FakeRuntime()
        self.output = io.StringIO()

    def test_same_metadata_drives_help_completion_and_dispatch(self) -> None:
        workspace = next(command for command in self.registry.commands if command.name == "/workspace")

        self.registry.dispatch(self.runtime, "/help", self.output)
        completions = self.registry.completions("/wor")
        result = self.registry.dispatch(self.runtime, "/workspace list", self.output)

        self.assertIn(workspace.usage, self.output.getvalue())
        self.assertEqual([completion.text for completion in completions], ["/workspace"])
        self.assertIs(completions[0].metadata, workspace)
        self.assertTrue(result.handled)
        self.assertIn("workspace summary", self.output.getvalue())

    def test_workspace_and_approval_subcommands_are_completed_from_metadata(self) -> None:
        workspace = self.registry.completions("/workspace r")
        approval = self.registry.completions("/approval ")
        modes = self.registry.completions("/approval mode w")

        self.assertEqual([completion.text for completion in workspace], ["remove", "reset"])
        self.assertEqual([completion.text for completion in approval], ["mode", "allow", "prompt", "deny", "reset"])
        self.assertEqual([completion.text for completion in modes], ["write"])
        self.assertEqual(modes[0].metadata.name, "mode")

    def test_dispatch_uses_shlex_for_quoted_paths(self) -> None:
        result = self.registry.dispatch(
            self.runtime,
            '/workspace add "/tmp/project with spaces"',
            self.output,
        )
        alias_result = self.registry.dispatch(
            self.runtime,
            '/add-dir "/tmp/docs with spaces"',
            self.output,
        )

        self.assertTrue(result.handled)
        self.assertTrue(alias_result.handled)
        self.assertEqual(
            self.runtime.calls,
            [
                ("workspace-add", "/tmp/project with spaces"),
                ("workspace-add", "/tmp/docs with spaces"),
            ],
        )

    def test_non_slash_input_is_not_handled(self) -> None:
        result = self.registry.dispatch(self.runtime, "workspace list", self.output)

        self.assertFalse(result.handled)
        self.assertEqual(self.output.getvalue(), "")
        self.assertEqual(self.runtime.calls, [])

    def test_unknown_command_points_to_help(self) -> None:
        result = self.registry.dispatch(self.runtime, "/wat", self.output)

        self.assertTrue(result.handled)
        self.assertIn("Unknown command: /wat", self.output.getvalue())
        self.assertIn("Type /help for commands.", self.output.getvalue())

    def test_exit_alias_returns_exit_intent_without_calling_runtime(self) -> None:
        result = self.registry.dispatch(self.runtime, "/quit", self.output)

        self.assertTrue(result.handled)
        self.assertTrue(result.exit_requested)
        self.assertEqual(self.runtime.calls, [])


if __name__ == "__main__":
    unittest.main()

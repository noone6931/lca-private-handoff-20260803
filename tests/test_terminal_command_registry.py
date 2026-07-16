from __future__ import annotations

import unittest

from local_agent.frontends.terminal.command_registry import TerminalCommandRegistry


class TerminalCommandRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = TerminalCommandRegistry()

    def test_same_metadata_drives_help_completion_and_dispatch(self) -> None:
        workspace = next(command for command in self.registry.commands if command.name == "/workspace")

        help_result = self.registry.dispatch("/help")
        completions = self.registry.completions("/wor")
        result = self.registry.dispatch("/workspace list")

        self.assertIn(workspace.usage, help_result.output[0])
        self.assertEqual([completion.text for completion in completions], ["/workspace"])
        self.assertIs(completions[0].metadata, workspace)
        self.assertTrue(result.handled)
        self.assertEqual(result.command.type, "ListWorkspaceRoots")

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
            '/workspace add "/tmp/project with spaces"',
        )
        alias_result = self.registry.dispatch(
            '/add-dir "/tmp/docs with spaces"',
        )

        self.assertTrue(result.handled)
        self.assertTrue(alias_result.handled)
        self.assertEqual(result.command.type, "AddWorkspaceRoot")
        self.assertEqual(result.command.payload, {"path": "/tmp/project with spaces"})
        self.assertEqual(alias_result.command.payload, {"path": "/tmp/docs with spaces"})

    def test_non_slash_input_is_not_handled(self) -> None:
        result = self.registry.dispatch("workspace list")

        self.assertFalse(result.handled)
        self.assertEqual(result.output, ())
        self.assertIsNone(result.command)

    def test_unknown_command_points_to_help(self) -> None:
        result = self.registry.dispatch("/wat")

        self.assertTrue(result.handled)
        self.assertEqual(result.output, ("Unknown command: /wat", "Type /help for commands."))

    def test_exit_alias_returns_exit_intent_without_calling_runtime(self) -> None:
        result = self.registry.dispatch("/quit")

        self.assertTrue(result.handled)
        self.assertTrue(result.exit_requested)
        self.assertIsNone(result.command)

    def test_runtime_commands_are_returned_as_typed_agent_commands(self) -> None:
        cases = {
            "/status": ("GetStatus", {}),
            "/tools": ("ListTools", {}),
            "/approval mode write": ("SetApprovalMode", {"mode": "write"}),
            "/approval deny shell": ("SetToolApproval", {"tool": "shell", "policy": "deny"}),
            "/approval reset shell": ("ResetToolApproval", {"tool": "shell"}),
            "/workspace remove /tmp/docs": ("RemoveWorkspaceRoot", {"path": "/tmp/docs"}),
            "/workspace reset": ("ResetWorkspaceRoots", {}),
            "/move /tmp/new": ("MoveWorkspace", {"path": "/tmp/new"}),
        }

        for text, (command_type, payload) in cases.items():
            with self.subTest(text=text):
                result = self.registry.dispatch(text)
                self.assertEqual(result.command.type, command_type)
                self.assertEqual(result.command.payload, payload)


if __name__ == "__main__":
    unittest.main()

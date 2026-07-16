"""Canonical metadata and typed parsing for terminal slash commands.

This module deliberately has no prompt-toolkit or Runtime dependency: one
registry owns help text, completion candidates, and command parsing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import shlex

from ...protocol.commands import AgentCommand
from ...protocol.commands import new_command


class TerminalCommandAction(StrEnum):
    HELP = "help"
    STATUS = "status"
    TOOLS = "tools"
    WORKSPACE = "workspace"
    ADD_DIR = "add_dir"
    MOVE = "move"
    APPROVAL = "approval"
    EXIT = "exit"


@dataclass(frozen=True)
class TerminalCommandMetadata:
    """Declarative command information shared by help, completion, and dispatch."""

    name: str
    description: str
    usage: str
    action: TerminalCommandAction
    aliases: tuple[str, ...] = ()
    subcommands: tuple["TerminalCommandMetadata", ...] = ()
    choices: tuple[tuple[str, str], ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


@dataclass(frozen=True)
class TerminalCommandCompletion:
    text: str
    description: str
    start_position: int
    metadata: TerminalCommandMetadata


@dataclass(frozen=True)
class TerminalCommandDispatch:
    """The result is transport-neutral so callers choose how to leave chat."""

    handled: bool
    exit_requested: bool = False
    command: AgentCommand | None = None
    output: tuple[str, ...] = ()


_WORKSPACE_SUBCOMMANDS: tuple[TerminalCommandMetadata, ...] = (
    TerminalCommandMetadata(
        "list",
        "Show primary, configured, and session roots.",
        "/workspace list",
        TerminalCommandAction.WORKSPACE,
    ),
    TerminalCommandMetadata(
        "add",
        "Add a session-only directory.",
        "/workspace add PATH",
        TerminalCommandAction.WORKSPACE,
    ),
    TerminalCommandMetadata(
        "remove",
        "Remove a session-added directory.",
        "/workspace remove PATH",
        TerminalCommandAction.WORKSPACE,
    ),
    TerminalCommandMetadata(
        "reset",
        "Remove every session-added directory.",
        "/workspace reset",
        TerminalCommandAction.WORKSPACE,
    ),
)

_APPROVAL_SUBCOMMANDS: tuple[TerminalCommandMetadata, ...] = (
    TerminalCommandMetadata(
        "mode",
        "Set the default approval mode.",
        "/approval mode always-ask|write|yolo",
        TerminalCommandAction.APPROVAL,
        choices=(
            ("always-ask", "Prompt for every non-read tool."),
            ("write", "Allow read and write tools; prompt for commands."),
            ("yolo", "Allow all tools without prompts."),
        ),
    ),
    TerminalCommandMetadata(
        "allow",
        "Allow one tool for this session.",
        "/approval allow TOOL",
        TerminalCommandAction.APPROVAL,
    ),
    TerminalCommandMetadata(
        "prompt",
        "Prompt before one tool for this session.",
        "/approval prompt TOOL",
        TerminalCommandAction.APPROVAL,
    ),
    TerminalCommandMetadata(
        "deny",
        "Deny one tool for this session.",
        "/approval deny TOOL",
        TerminalCommandAction.APPROVAL,
    ),
    TerminalCommandMetadata(
        "reset",
        "Clear one session tool policy.",
        "/approval reset TOOL",
        TerminalCommandAction.APPROVAL,
    ),
)

BUILTIN_TERMINAL_COMMANDS: tuple[TerminalCommandMetadata, ...] = (
    TerminalCommandMetadata(
        "/help",
        "Show this help.",
        "/help or /?",
        TerminalCommandAction.HELP,
        aliases=("/?",),
    ),
    TerminalCommandMetadata(
        "/status",
        "Show session, workspace, provider, budget, and approval summary.",
        "/status",
        TerminalCommandAction.STATUS,
    ),
    TerminalCommandMetadata(
        "/tools",
        "List available tool names.",
        "/tools",
        TerminalCommandAction.TOOLS,
    ),
    TerminalCommandMetadata(
        "/workspace",
        "Manage additional workspace directories.",
        "/workspace list|add PATH|remove PATH|reset",
        TerminalCommandAction.WORKSPACE,
        subcommands=_WORKSPACE_SUBCOMMANDS,
    ),
    TerminalCommandMetadata(
        "/add-dir",
        "Add a session-only workspace directory.",
        "/add-dir PATH",
        TerminalCommandAction.ADD_DIR,
    ),
    TerminalCommandMetadata(
        "/move",
        "Move this session to a new primary workspace.",
        "/move PATH",
        TerminalCommandAction.MOVE,
    ),
    TerminalCommandMetadata(
        "/approval",
        "Show or change tool approval settings.",
        "/approval [mode MODE|allow TOOL|prompt TOOL|deny TOOL|reset TOOL]",
        TerminalCommandAction.APPROVAL,
        subcommands=_APPROVAL_SUBCOMMANDS,
    ),
    TerminalCommandMetadata(
        "/exit",
        "Exit terminal chat.",
        "/exit or /quit",
        TerminalCommandAction.EXIT,
        aliases=("/quit",),
    ),
)


class TerminalCommandRegistry:
    """Built-in slash command registry; extension commands can join this seam later."""

    def __init__(self, commands: tuple[TerminalCommandMetadata, ...] = BUILTIN_TERMINAL_COMMANDS) -> None:
        self._commands = commands

    @property
    def commands(self) -> tuple[TerminalCommandMetadata, ...]:
        return self._commands

    def help_text(self) -> str:
        lines = ["Commands:"]
        for command in self._commands:
            lines.append(f"{command.usage:<46} {command.description}")
            for subcommand in command.subcommands:
                lines.append(f"{subcommand.usage:<46} {subcommand.description}")
        return "\n".join(lines)

    def completions(self, text_before_cursor: str) -> tuple[TerminalCommandCompletion, ...]:
        """Complete only a single-line slash command; natural language remains untouched."""

        if not text_before_cursor.startswith("/") or "\n" in text_before_cursor:
            return ()
        words = text_before_cursor.split()
        if not words:
            return ()
        if text_before_cursor.endswith((" ", "\t")):
            words.append("")
        if len(words) == 1:
            return self._matching_commands(self._commands, words[0])

        root = self._find_command(self._commands, words[0])
        if root is None:
            return ()
        if len(words) == 2:
            return self._matching_commands(root.subcommands, words[1])
        if len(words) == 3 and root.name == "/approval" and words[1] == "mode":
            mode = self._find_command(root.subcommands, "mode")
            if mode is not None:
                return tuple(
                    TerminalCommandCompletion(
                        text=value,
                        description=description,
                        start_position=-len(words[2]),
                        metadata=mode,
                    )
                    for value, description in mode.choices
                    if value.startswith(words[2])
                )
        return ()

    def dispatch(
        self,
        command: str,
    ) -> TerminalCommandDispatch:
        """Parse a slash command without invoking Runtime policy or mutation owners."""

        if not command.startswith("/"):
            return TerminalCommandDispatch(handled=False)
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            return TerminalCommandDispatch(handled=True, output=(f"error: {exc}",))
        if not parts:
            return TerminalCommandDispatch(handled=False)
        root = self._find_command(self._commands, parts[0])
        if root is None:
            return TerminalCommandDispatch(
                handled=True,
                output=(f"Unknown command: {parts[0]}", "Type /help for commands."),
            )
        return self._dispatch_known(root, parts)

    def is_exit_command(self, command: str) -> bool:
        """Check exit intent from registry metadata without dispatching another command."""

        if not command.startswith("/"):
            return False
        try:
            parts = shlex.split(command)
        except ValueError:
            return False
        if len(parts) != 1:
            return False
        root = self._find_command(self._commands, parts[0])
        return root is not None and root.action is TerminalCommandAction.EXIT

    def _dispatch_known(
        self,
        root: TerminalCommandMetadata,
        parts: list[str],
    ) -> TerminalCommandDispatch:
        if root.action is TerminalCommandAction.HELP:
            return TerminalCommandDispatch(handled=True, output=(self.help_text(),))
        if root.action is TerminalCommandAction.STATUS:
            return self._command("GetStatus")
        if root.action is TerminalCommandAction.TOOLS:
            return self._command("ListTools")
        if root.action is TerminalCommandAction.EXIT:
            return TerminalCommandDispatch(handled=True, exit_requested=True)
        if root.action is TerminalCommandAction.ADD_DIR:
            return self._path_command(root, parts, "AddWorkspaceRoot")
        if root.action is TerminalCommandAction.MOVE:
            return self._path_command(root, parts, "MoveWorkspace")
        if root.action is TerminalCommandAction.WORKSPACE:
            return self._dispatch_workspace(root, parts)
        if root.action is TerminalCommandAction.APPROVAL:
            return self._dispatch_approval(root, parts)
        raise ValueError(f"Unsupported terminal command action: {root.action}")

    def _dispatch_workspace(
        self,
        root: TerminalCommandMetadata,
        parts: list[str],
    ) -> TerminalCommandDispatch:
        if len(parts) == 2 and parts[1] == "list":
            return self._command("ListWorkspaceRoots")
        if len(parts) == 3 and parts[1] == "add":
            return self._command("AddWorkspaceRoot", {"path": parts[2]})
        if len(parts) == 3 and parts[1] == "remove":
            return self._command("RemoveWorkspaceRoot", {"path": parts[2]})
        if len(parts) == 2 and parts[1] == "reset":
            return self._command("ResetWorkspaceRoots")
        return self._usage(root)

    def _dispatch_approval(
        self,
        root: TerminalCommandMetadata,
        parts: list[str],
    ) -> TerminalCommandDispatch:
        if len(parts) == 1:
            return self._command("GetApproval")
        if len(parts) == 3 and parts[1] == "mode":
            return self._command("SetApprovalMode", {"mode": parts[2]})
        if len(parts) == 3 and parts[1] in {"allow", "prompt", "deny"}:
            return self._command("SetToolApproval", {"tool": parts[2], "policy": parts[1]})
        if len(parts) == 3 and parts[1] == "reset":
            return self._command("ResetToolApproval", {"tool": parts[2]})
        return self._usage(root)

    def _path_command(
        self,
        root: TerminalCommandMetadata,
        parts: list[str],
        command_type: str,
    ) -> TerminalCommandDispatch:
        if len(parts) != 2:
            return self._usage(root)
        return self._command(command_type, {"path": parts[1]})

    @staticmethod
    def _command(command_type: str, payload: dict[str, str] | None = None) -> TerminalCommandDispatch:
        return TerminalCommandDispatch(handled=True, command=new_command(command_type, payload))

    @staticmethod
    def _usage(command: TerminalCommandMetadata) -> TerminalCommandDispatch:
        return TerminalCommandDispatch(handled=True, output=(f"Usage: {command.usage}",))

    @staticmethod
    def _find_command(
        commands: tuple[TerminalCommandMetadata, ...],
        token: str,
    ) -> TerminalCommandMetadata | None:
        return next((command for command in commands if token in command.names), None)

    def _matching_commands(
        self,
        commands: tuple[TerminalCommandMetadata, ...],
        prefix: str,
    ) -> tuple[TerminalCommandCompletion, ...]:
        completions: list[TerminalCommandCompletion] = []
        for command in commands:
            for name in command.names:
                if name.startswith(prefix):
                    completions.append(
                        TerminalCommandCompletion(
                            text=name,
                            description=command.description,
                            start_position=-len(prefix),
                            metadata=command,
                        )
                    )
        return tuple(completions)

"""Canonical metadata and dispatch for terminal slash commands.

The terminal frontend and CLI currently own their adapters independently.  This
module deliberately has no prompt-toolkit dependency: one command registry is
the source for help text, completion candidates, and command dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import shlex
from typing import Protocol, TextIO

from ...config import ConfigError


class TerminalRuntime(Protocol):
    """The small runtime surface used by built-in terminal commands."""

    def approval_summary(self) -> str: ...

    def status_summary(self) -> str: ...

    def tool_summary(self) -> str: ...

    def workspace_summary(self) -> str: ...

    def add_workspace_root(self, path: str) -> object: ...

    def remove_workspace_root(self, path: str) -> object: ...

    def reset_workspace_roots(self) -> None: ...

    def move_workspace(self, path: str) -> object: ...

    def set_session_approval_mode(self, mode: str) -> None: ...

    def set_session_tool_policy(self, tool: str, policy: str) -> None: ...

    def reset_session_tool_policy(self, tool: str) -> None: ...


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
        runtime: TerminalRuntime,
        command: str,
        output: TextIO,
    ) -> TerminalCommandDispatch:
        """Handle a slash command, returning unhandled input to the caller unchanged."""

        if not command.startswith("/"):
            return TerminalCommandDispatch(handled=False)
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            print(f"error: {exc}", file=output)
            return TerminalCommandDispatch(handled=True)
        if not parts:
            return TerminalCommandDispatch(handled=False)
        root = self._find_command(self._commands, parts[0])
        if root is None:
            print(f"Unknown command: {parts[0]}", file=output)
            print("Type /help for commands.", file=output)
            return TerminalCommandDispatch(handled=True)
        try:
            return self._dispatch_known(runtime, root, parts, output)
        except (ConfigError, RuntimeError, ValueError) as exc:
            print(f"error: {exc}", file=output)
            return TerminalCommandDispatch(handled=True)

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
        runtime: TerminalRuntime,
        root: TerminalCommandMetadata,
        parts: list[str],
        output: TextIO,
    ) -> TerminalCommandDispatch:
        if root.action is TerminalCommandAction.HELP:
            print(self.help_text(), file=output)
            return TerminalCommandDispatch(handled=True)
        if root.action is TerminalCommandAction.STATUS:
            print(runtime.status_summary(), file=output)
            return TerminalCommandDispatch(handled=True)
        if root.action is TerminalCommandAction.TOOLS:
            print(runtime.tool_summary(), file=output)
            return TerminalCommandDispatch(handled=True)
        if root.action is TerminalCommandAction.EXIT:
            return TerminalCommandDispatch(handled=True, exit_requested=True)
        if root.action is TerminalCommandAction.ADD_DIR:
            if len(parts) != 2:
                self._print_usage(root, output)
            else:
                runtime.add_workspace_root(parts[1])
                print(runtime.workspace_summary(), file=output)
            return TerminalCommandDispatch(handled=True)
        if root.action is TerminalCommandAction.MOVE:
            if len(parts) != 2:
                self._print_usage(root, output)
            else:
                runtime.move_workspace(parts[1])
                print(runtime.workspace_summary(), file=output)
            return TerminalCommandDispatch(handled=True)
        if root.action is TerminalCommandAction.WORKSPACE:
            self._dispatch_workspace(runtime, root, parts, output)
            return TerminalCommandDispatch(handled=True)
        if root.action is TerminalCommandAction.APPROVAL:
            self._dispatch_approval(runtime, root, parts, output)
            return TerminalCommandDispatch(handled=True)
        raise ValueError(f"Unsupported terminal command action: {root.action}")

    def _dispatch_workspace(
        self,
        runtime: TerminalRuntime,
        root: TerminalCommandMetadata,
        parts: list[str],
        output: TextIO,
    ) -> None:
        if len(parts) == 2 and parts[1] == "list":
            print(runtime.workspace_summary(), file=output)
            return
        if len(parts) == 3 and parts[1] == "add":
            runtime.add_workspace_root(parts[2])
            print(runtime.workspace_summary(), file=output)
            return
        if len(parts) == 3 and parts[1] == "remove":
            runtime.remove_workspace_root(parts[2])
            print(runtime.workspace_summary(), file=output)
            return
        if len(parts) == 2 and parts[1] == "reset":
            runtime.reset_workspace_roots()
            print(runtime.workspace_summary(), file=output)
            return
        self._print_usage(root, output)

    def _dispatch_approval(
        self,
        runtime: TerminalRuntime,
        root: TerminalCommandMetadata,
        parts: list[str],
        output: TextIO,
    ) -> None:
        if len(parts) == 1:
            print(runtime.approval_summary(), file=output)
            return
        if len(parts) == 3 and parts[1] == "mode":
            runtime.set_session_approval_mode(parts[2])
            print(runtime.approval_summary(), file=output)
            return
        if len(parts) == 3 and parts[1] in {"allow", "prompt", "deny"}:
            runtime.set_session_tool_policy(parts[2], parts[1])
            print(runtime.approval_summary(), file=output)
            return
        if len(parts) == 3 and parts[1] == "reset":
            runtime.reset_session_tool_policy(parts[2])
            print(runtime.approval_summary(), file=output)
            return
        self._print_usage(root, output)

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

    @staticmethod
    def _print_usage(command: TerminalCommandMetadata, output: TextIO) -> None:
        print(f"Usage: {command.usage}", file=output)

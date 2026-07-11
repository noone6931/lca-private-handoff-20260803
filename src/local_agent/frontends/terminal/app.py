from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import TextIO

from ...agent import AgentRuntime
from ...terminal_io import silenced_terminal_input
from .renderer import TerminalEventSink


CommandHandler = Callable[[AgentRuntime, str, TextIO], None]


@dataclass(frozen=True)
class SlashCommandCompletion:
    """A terminal command completion independent of the optional frontend package."""

    text: str
    description: str
    start_position: int


_ROOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/help", "Show available chat commands."),
    ("/?", "Show available chat commands."),
    ("/status", "Show session, workspace, provider, and budget status."),
    ("/tools", "List available tool names."),
    ("/workspace", "Manage additional workspace directories."),
    ("/add-dir", "Add a session-only workspace directory."),
    ("/move", "Move this session to a new primary workspace."),
    ("/approval", "Show or change tool approval settings."),
    ("/exit", "Exit terminal chat."),
    ("/quit", "Exit terminal chat."),
)

_WORKSPACE_SUBCOMMANDS: tuple[tuple[str, str], ...] = (
    ("list", "Show primary, configured, and session roots."),
    ("add", "Add a session-only directory."),
    ("remove", "Remove a session-added directory."),
    ("reset", "Remove every session-added directory."),
)

_APPROVAL_SUBCOMMANDS: tuple[tuple[str, str], ...] = (
    ("mode", "Set the default approval mode."),
    ("allow", "Allow one tool for this session."),
    ("prompt", "Prompt before one tool for this session."),
    ("deny", "Deny one tool for this session."),
    ("reset", "Clear one session tool policy."),
)

_APPROVAL_MODES: tuple[tuple[str, str], ...] = (
    ("always-ask", "Prompt for every non-read tool."),
    ("write", "Allow read and write tools; prompt for commands."),
    ("yolo", "Allow all tools without prompts."),
)


def slash_command_completions(text_before_cursor: str) -> tuple[SlashCommandCompletion, ...]:
    """Return slash-command completions without affecting normal or multiline prompts."""

    if not text_before_cursor.startswith("/") or "\n" in text_before_cursor:
        return ()
    words = text_before_cursor.split()
    if not words:
        return ()
    if text_before_cursor.endswith((" ", "\t")):
        words.append("")
    if len(words) == 1:
        return _matching_completions(_ROOT_COMMANDS, words[0])
    if words[0] == "/workspace" and len(words) == 2:
        return _matching_completions(_WORKSPACE_SUBCOMMANDS, words[1])
    if words[0] == "/approval":
        if len(words) == 2:
            return _matching_completions(_APPROVAL_SUBCOMMANDS, words[1])
        if len(words) == 3 and words[1] == "mode":
            return _matching_completions(_APPROVAL_MODES, words[2])
    return ()


def _matching_completions(
    candidates: tuple[tuple[str, str], ...],
    prefix: str,
) -> tuple[SlashCommandCompletion, ...]:
    return tuple(
        SlashCommandCompletion(text=text, description=description, start_position=-len(prefix))
        for text, description in candidates
        if text.startswith(prefix)
    )


def run_terminal_chat(
    runtime: AgentRuntime,
    *,
    command_handler: CommandHandler | None = None,
    history_path: Path | None = None,
    input_stream=None,
    output_stream=None,
) -> int:
    output = output_stream or sys.stdout
    prompt = _build_prompt(history_path)
    print("local-agent chat. Type /help for commands, /exit to quit.", file=output)
    while True:
        try:
            text = prompt(input_stream=input_stream).strip()
        except EOFError:
            print(file=output)
            return 0
        except KeyboardInterrupt:
            print("interrupted", file=output)
            continue
        if not text:
            continue
        if text in {"/exit", "/quit"}:
            return 0
        if text.startswith("/"):
            if command_handler is None:
                print(f"Unknown command: {text.split()[0]}", file=output)
            else:
                command_handler(runtime, text, output)
            continue
        try:
            with silenced_terminal_input():
                runtime.run(text)
        except KeyboardInterrupt:
            print("interrupted", file=output)


def create_terminal_event_sink(*, show_tools: bool = True, stream=None) -> TerminalEventSink:
    return TerminalEventSink(stream=stream, show_tools=show_tools)


def _build_prompt(history_path: Path | None):
    try:
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
    except ImportError:
        return _PlainPrompt()
    history = FileHistory(str(history_path)) if history_path is not None else None
    bindings = KeyBindings()

    @bindings.add("escape", "enter")
    def _(event) -> None:
        event.app.current_buffer.validate_and_handle()

    class _SlashCommandCompleter(Completer):
        def get_completions(self, document, complete_event):
            del complete_event
            for candidate in slash_command_completions(document.text_before_cursor):
                yield Completion(
                    candidate.text,
                    start_position=candidate.start_position,
                    display_meta=candidate.description,
                )

    session = PromptSession(
        history=history,
        multiline=True,
        key_bindings=bindings,
        completer=_SlashCommandCompleter(),
        complete_while_typing=True,
    )

    def prompt(*, input_stream=None) -> str:
        if input_stream is not None:
            line = input_stream.readline()
            if line == "":
                raise EOFError
            return line
        return session.prompt("> ")

    return prompt


class _PlainPrompt:
    def __call__(self, *, input_stream=None) -> str:
        if input_stream is not None:
            line = input_stream.readline()
            if line == "":
                raise EOFError
            return line
        return input("> ")

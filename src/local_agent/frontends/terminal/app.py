from __future__ import annotations

from pathlib import Path
import sys

from ...agent import AgentRuntime
from ...protocol.commands import CommandResult
from ...protocol.commands import new_command
from ...platform.terminal import silenced_terminal_input
from .command_registry import TerminalCommandCompletion
from .command_registry import TerminalCommandRegistry
from .interactions import TerminalInteractionController
from .renderer import TerminalEventSink


def slash_command_completions(text_before_cursor: str) -> tuple[TerminalCommandCompletion, ...]:
    """Return command completions only while the user is entering a slash command."""

    if not is_slash_command_input(text_before_cursor):
        return ()
    return TerminalCommandRegistry().completions(text_before_cursor)


def is_slash_command_input(text: str) -> bool:
    """Return whether slash-command completion applies to the current chat input."""

    return text.lstrip().startswith("/")


def run_terminal_chat(
    runtime: AgentRuntime,
    *,
    command_registry: TerminalCommandRegistry | None = None,
    interaction_controller: TerminalInteractionController | None = None,
    history_path: Path | None = None,
    input_stream=None,
    output_stream=None,
) -> int:
    output = output_stream or sys.stdout
    prompt = _build_prompt(history_path)
    registry = command_registry or TerminalCommandRegistry()
    interactions = interaction_controller or TerminalInteractionController(
        input_stream=input_stream,
        output_stream=output,
    )
    set_interaction_handler = getattr(runtime, "set_interaction_handler", None)
    if callable(set_interaction_handler):
        set_interaction_handler(interactions)
    print("local-agent chat. Type /help for commands, /exit to quit.", file=output)
    try:
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
            if text.startswith("/"):
                dispatched = registry.dispatch(text)
                _print_lines(dispatched.output, output)
                if dispatched.exit_requested:
                    return 0
                if dispatched.command is not None:
                    _render_command_result(runtime.commands.dispatch(dispatched.command), output)
                continue
            try:
                with silenced_terminal_input():
                    runtime.commands.dispatch(new_command("SubmitPrompt", {"prompt": text}))
            except KeyboardInterrupt:
                print("interrupted", file=output)
    finally:
        if callable(set_interaction_handler):
            set_interaction_handler(None)


def create_terminal_event_sink(*, show_tools: bool = True, stream=None) -> TerminalEventSink:
    return TerminalEventSink(stream=stream, show_tools=show_tools)


def _render_command_result(result: CommandResult, output) -> None:
    if result.ok:
        text = result.payload.get("text")
        if text is not None:
            print(str(text), file=output)
        return
    print(f"error: {result.error_message or result.error_code or 'Command failed.'}", file=output)


def _print_lines(lines: tuple[str, ...], output) -> None:
    for line in lines:
        print(line, file=output)


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
        event.current_buffer.insert_text("\n")

    @bindings.add("enter")
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

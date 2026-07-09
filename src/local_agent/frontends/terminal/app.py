from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sys
from typing import TextIO

from ...agent import AgentRuntime
from .renderer import TerminalEventSink


CommandHandler = Callable[[AgentRuntime, str, TextIO], None]


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
            runtime.run(text)
        except KeyboardInterrupt:
            print("interrupted", file=output)


def create_terminal_event_sink(*, show_tools: bool = True, stream=None) -> TerminalEventSink:
    return TerminalEventSink(stream=stream, show_tools=show_tools)


def _build_prompt(history_path: Path | None):
    try:
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

    session = PromptSession(history=history, multiline=True, key_bindings=bindings)

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

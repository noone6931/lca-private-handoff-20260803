from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..composer_history import ComposerHistory
from .command_registry import TerminalCommandCompletion
from .command_registry import TerminalCommandRegistry


class TerminalPrompt(Protocol):
    def __call__(self, *, input_stream=None) -> str: ...

    def rebind_history(self, history_path: Path | None) -> None: ...


class TerminalHistoryRebindError(RuntimeError):
    """Report that a moved chat must continue without persistent input history."""


def slash_command_completions(text_before_cursor: str) -> tuple[TerminalCommandCompletion, ...]:
    """Return command completions only while the user is entering a slash command."""

    if not is_slash_command_input(text_before_cursor):
        return ()
    return TerminalCommandRegistry().completions(text_before_cursor)


def is_slash_command_input(text: str) -> bool:
    """Return whether slash-command completion applies to the current chat input."""

    return text.lstrip().startswith("/")


def build_terminal_prompt(history: ComposerHistory) -> TerminalPrompt:
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.key_binding import KeyBindings
    except ImportError:
        return PlainTerminalPrompt(history)

    def build_session(composer_history: ComposerHistory):
        bindings = KeyBindings()

        @bindings.add("escape", "enter")
        def _(event) -> None:
            event.current_buffer.insert_text("\n")

        @bindings.add("enter")
        def _(event) -> None:
            event.app.current_buffer.validate_and_handle()

        @bindings.add("up")
        def _(event) -> None:
            _navigate_buffer(composer_history, event.current_buffer, -1)

        @bindings.add("down")
        def _(event) -> None:
            _navigate_buffer(composer_history, event.current_buffer, 1)

        class _SlashCommandCompleter(Completer):
            def get_completions(self, document, complete_event):
                del complete_event
                for candidate in slash_command_completions(document.text_before_cursor):
                    yield Completion(
                        candidate.text,
                        start_position=candidate.start_position,
                        display_meta=candidate.description,
                    )

        return PromptSession(
            multiline=True,
            key_bindings=bindings,
            completer=_SlashCommandCompleter(),
            complete_while_typing=True,
        )

    return PromptToolkitTerminalPrompt(history, build_session)


class PromptToolkitTerminalPrompt:
    def __init__(self, history: ComposerHistory, session_factory) -> None:
        self._history = history
        self._session_factory = session_factory
        self._session = self._session_factory(self._history)

    def __call__(self, *, input_stream=None) -> str:
        if input_stream is not None:
            line = input_stream.readline()
            if line == "":
                raise EOFError
            return line
        return self._session.prompt("> ")

    def rebind_history(self, history_path: Path | None) -> None:
        if not self._history.rebind(history_path) and history_path is not None:
            raise TerminalHistoryRebindError(
                "Workspace moved, but persistent terminal history is disabled for this chat."
            )


class PlainTerminalPrompt:
    def __init__(self, history: ComposerHistory) -> None:
        self._history = history

    def __call__(self, *, input_stream=None) -> str:
        if input_stream is not None:
            line = input_stream.readline()
            if line == "":
                raise EOFError
            return line
        return input("> ")

    def rebind_history(self, history_path: Path | None) -> None:
        if not self._history.rebind(history_path) and history_path is not None:
            raise TerminalHistoryRebindError(
                "Workspace moved, but persistent terminal history is disabled for this chat."
            )


def _navigate_buffer(history: ComposerHistory, buffer, direction: int) -> None:
    text = buffer.text
    recalled = history.navigate(direction, text, buffer.cursor_position)
    if recalled is not None:
        buffer.text = recalled
        buffer.cursor_position = len(recalled)
        return
    if direction < 0:
        buffer.cursor_up(count=1)
    else:
        buffer.cursor_down(count=1)

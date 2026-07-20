from __future__ import annotations

from pathlib import Path
from typing import Protocol

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


def build_terminal_prompt(history_path: Path | None) -> TerminalPrompt:
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
    except ImportError:
        return PlainTerminalPrompt()

    def build_session(path: Path | None):
        history = FileHistory(str(path)) if path is not None else None
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

        return PromptSession(
            history=history,
            multiline=True,
            key_bindings=bindings,
            completer=_SlashCommandCompleter(),
            complete_while_typing=True,
        )

    return PromptToolkitTerminalPrompt(history_path, build_session)


class PromptToolkitTerminalPrompt:
    def __init__(self, history_path: Path | None, session_factory) -> None:
        self._session_factory = session_factory
        self._history_path = _canonical_history_path(history_path)
        self._session = self._session_factory(self._history_path)

    def __call__(self, *, input_stream=None) -> str:
        if input_stream is not None:
            line = input_stream.readline()
            if line == "":
                raise EOFError
            return line
        return self._session.prompt("> ")

    def rebind_history(self, history_path: Path | None) -> None:
        next_path = _canonical_history_path(history_path)
        if next_path == self._history_path:
            return
        try:
            next_session = self._session_factory(next_path)
        except (OSError, ValueError) as exc:
            fallback_session = self._session_factory(None)
            self._history_path = None
            self._session = fallback_session
            raise TerminalHistoryRebindError(
                "Workspace moved, but persistent terminal history is disabled for this chat."
            ) from exc
        self._history_path = next_path
        self._session = next_session


class PlainTerminalPrompt:
    def __call__(self, *, input_stream=None) -> str:
        if input_stream is not None:
            line = input_stream.readline()
            if line == "":
                raise EOFError
            return line
        return input("> ")

    def rebind_history(self, history_path: Path | None) -> None:
        del history_path


def _canonical_history_path(history_path: Path | None) -> Path | None:
    if history_path is None:
        return None
    return history_path.expanduser().resolve()

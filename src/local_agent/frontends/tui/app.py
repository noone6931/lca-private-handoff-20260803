from __future__ import annotations

import sys
from collections.abc import Callable

from ..composer_history import ComposerHistory
from ..terminal.app import run_terminal_chat
from .controller import TuiController
from .mailbox import TuiMailbox
from .model import TuiProjector
from .screen import run_inline_screen
from .screen import streams_are_tty
from .worker import TuiWorker
from .worker import TuiRuntimePort


def tui_is_supported(*, input_stream=None, output_stream=None) -> bool:
    if not streams_are_tty(input_stream, output_stream):
        return False
    return True


def run_tui(
    runtime: TuiRuntimePort,
    mailbox: TuiMailbox,
    *,
    input_stream=None,
    output_stream=None,
    screen_runner: Callable[[TuiController], int] | None = None,
    initial_prompt: str | None = None,
    composer_history: ComposerHistory | None = None,
) -> int:
    """Run the inline TUI, falling back to the existing REPL on unsupported terminals."""

    history = composer_history or ComposerHistory(None)
    if screen_runner is None and not tui_is_supported(input_stream=input_stream, output_stream=output_stream):
        print("Interactive TUI unavailable; using terminal chat.", file=output_stream or sys.stderr)
        prompt_input = prepend_initial_prompt(input_stream or sys.stdin, initial_prompt)
        return run_terminal_chat(
            runtime,
            composer_history=history,
            input_stream=prompt_input,
            output_stream=output_stream,
        )
    worker = TuiWorker(runtime, mailbox)
    controller = TuiController(mailbox, TuiProjector(), worker, composer_history=history)
    runner = screen_runner or run_inline_screen
    worker.start()
    try:
        if initial_prompt:
            controller.submit_initial_prompt(initial_prompt)
        return runner(controller)
    finally:
        worker.close()
        mailbox.close()


def prepend_initial_prompt(source, prompt: str | None):
    return _InitialPromptInput(source, prompt)


class _InitialPromptInput:
    def __init__(self, source, prompt: str | None) -> None:
        self._source = source
        self._prompt = prompt.strip() if prompt else None

    def readline(self, *args, **kwargs):
        if self._prompt is not None:
            prompt = self._prompt
            self._prompt = None
            return prompt + "\n"
        return self._source.readline(*args, **kwargs)

    def isatty(self) -> bool:
        return bool(getattr(self._source, "isatty", lambda: False)())

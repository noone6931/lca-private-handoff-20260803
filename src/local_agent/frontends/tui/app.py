from __future__ import annotations

import sys
from collections.abc import Callable

from ..terminal.app import run_terminal_chat
from .controller import TuiController
from .mailbox import TuiMailbox
from .model import TuiProjector
from .screen import run_curses_screen
from .screen import streams_are_tty
from .worker import TuiWorker
from .worker import TuiRuntimePort


def tui_is_supported(*, input_stream=None, output_stream=None) -> bool:
    if not streams_are_tty(input_stream, output_stream):
        return False
    try:
        import curses  # noqa: F401
    except ImportError:
        return False
    return True


def run_tui(
    runtime: TuiRuntimePort,
    mailbox: TuiMailbox,
    *,
    input_stream=None,
    output_stream=None,
    screen_runner: Callable[[TuiController], int] | None = None,
) -> int:
    """Run the TUI, falling back to the existing REPL on unsupported terminals."""

    if screen_runner is None and not tui_is_supported(input_stream=input_stream, output_stream=output_stream):
        print("Full-screen TUI unavailable; using terminal chat.", file=output_stream or sys.stderr)
        return run_terminal_chat(runtime, input_stream=input_stream, output_stream=output_stream)
    worker = TuiWorker(runtime, mailbox)
    controller = TuiController(mailbox, TuiProjector(), worker)
    runner = screen_runner or run_curses_screen
    worker.start()
    try:
        return runner(controller)
    finally:
        worker.close()
        mailbox.close()

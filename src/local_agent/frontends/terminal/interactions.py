from __future__ import annotations

import asyncio
from enum import Enum
import sys
from typing import TextIO

from ...protocol.interactions import InteractionRequest
from ...protocol.interactions import InteractionResult
from ...platform.terminal import terminal_input_prompt


class InputState(str, Enum):
    CHAT = "chat"
    ASK = "ask"
    APPROVAL = "approval"


_CANCELLED = object()


class TerminalInteractionController:
    """Own nested ask/approval focus while a synchronous Runtime is running.

    The main terminal prompt is deliberately not consulted in ASK/APPROVAL states.
    This follows OMP's focus ownership model without requiring its asynchronous TUI
    runtime: slash text belongs to the focused interaction, and `/cancel` returns a
    cancelled tool result before control returns to CHAT.
    """

    def __init__(self, *, input_stream: TextIO | None = None, output_stream: TextIO | None = None) -> None:
        self._input_stream = input_stream
        self._output = output_stream or sys.stdout
        self._state = InputState.CHAT

    @property
    def state(self) -> InputState:
        return self._state

    def request_interaction(self, request: InteractionRequest) -> InteractionResult:
        state = InputState.ASK if request.kind == "ask" else InputState.APPROVAL
        prompt = "[agent question] answer> " if state is InputState.ASK else "[approval] response> "
        original_state = self._state
        self._state = state
        try:
            print(request.prompt, file=self._output)
            print("Press Esc, Ctrl-C, or type /cancel to cancel.", file=self._output)
            while True:
                result = self._read(prompt, request.timeout_seconds)
                if result.status != "answered":
                    return result
                value = (result.value or "").strip()
                if value == "/cancel":
                    return InteractionResult("cancelled")
                if value.startswith("/"):
                    print(
                        "This input is answering an Agent question. Type /cancel, then return to the main prompt "
                        "to run terminal commands.",
                        file=self._output,
                    )
                    continue
                return InteractionResult("answered", value)
        finally:
            self._state = original_state

    def _read(self, prompt: str, timeout_seconds: float | None) -> InteractionResult:
        if timeout_seconds is not None and timeout_seconds <= 0:
            return InteractionResult("timed_out")
        if self._input_stream is not None:
            print(prompt, end="", file=self._output, flush=True)
            line = self._input_stream.readline()
            if line == "":
                return InteractionResult("eof")
            return InteractionResult("answered", line.rstrip("\r\n"))
        return _prompt_toolkit_interaction(prompt, timeout_seconds)


def _prompt_toolkit_interaction(prompt: str, timeout_seconds: float | None) -> InteractionResult:
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.key_binding import KeyBindings
    except ImportError:
        return _fallback_input(prompt)

    bindings = KeyBindings()

    @bindings.add("escape")
    def _cancel(event) -> None:
        event.app.exit(result=_CANCELLED)

    session = PromptSession(multiline=False, key_bindings=bindings)
    try:
        with terminal_input_prompt(sys.stdin):
            if timeout_seconds is None:
                value = session.prompt(prompt)
            else:
                value = asyncio.run(asyncio.wait_for(session.prompt_async(prompt), timeout=timeout_seconds))
    except TimeoutError:
        return InteractionResult("timed_out")
    except EOFError:
        return InteractionResult("eof")
    except KeyboardInterrupt:
        return InteractionResult("cancelled")
    if value is _CANCELLED:
        return InteractionResult("cancelled")
    return InteractionResult("answered", str(value))


def _fallback_input(prompt: str) -> InteractionResult:
    try:
        with terminal_input_prompt(sys.stdin):
            return InteractionResult("answered", input(prompt))
    except EOFError:
        return InteractionResult("eof")
    except KeyboardInterrupt:
        return InteractionResult("cancelled")

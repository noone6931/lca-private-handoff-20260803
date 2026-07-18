from __future__ import annotations

import base64
import codecs
import copy
import os
import select
import signal
import sys
from typing import Any

try:
    import termios
except ImportError:  # pragma: no cover - exercised by platform capability checks
    termios = None  # type: ignore[assignment]

from .controller import TuiController
from .input import BracketedPasteDecoder
from .input import TuiInputEvent
from .native_renderer import NativeScrollbackRenderer


_BRACKETED_PASTE_ENABLE = b"\x1b[?2004h"
_TERMINAL_MODES_DISABLE = b"\x1b[?2004l\x1b[?1007l\x1b[0m\x1b[?25h"


def run_inline_screen(controller: TuiController) -> int:
    session = _TerminalSession()
    renderer = NativeScrollbackRenderer()
    reader = _TerminalInputReader()
    with session:
        with _TerminalSignalHandlers(session, renderer) as signals:
            try:
                while not controller.exit_requested:
                    controller.poll()
                    width, height = _terminal_size()
                    controller.update_viewport(width, height)
                    renderer.render(controller.state, controller.view, width, height)
                    clipboard_text = controller.take_clipboard_text()
                    if clipboard_text is not None:
                        _copy_with_osc52(clipboard_text)
                    events = reader.read(timeout=0.05)
                    if signals.consume_interrupt():
                        controller.handle_key("CTRL_C")
                    for event in events:
                        _handle_input_event(controller, renderer, event)
            finally:
                renderer.close()
    return 0


class _TerminalInputReader:
    def __init__(self, input_stream=None, decoder: BracketedPasteDecoder | None = None) -> None:
        self._input = input_stream or sys.stdin
        self._decoder = decoder or BracketedPasteDecoder()
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")

    def read(self, *, timeout: float) -> tuple[TuiInputEvent, ...]:
        fd = self._input.fileno()
        readable, _, _ = select.select([fd], [], [], max(timeout, 0.0))
        if not readable:
            return self._decoder.expire() or self._decoder.flush_normal()
        data = os.read(fd, 65536)
        if not data:
            return (TuiInputEvent("key", "CTRL_Q"),)
        return self._decoder.feed(self._utf8.decode(data))


class _TerminalSession:
    def __init__(self, input_stream=None, output_stream=None) -> None:
        self._input = input_stream or sys.stdin
        self._output = output_stream or sys.stdout
        self._original: list[Any] | None = None
        self._active = False

    def __enter__(self):
        self.resume()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.suspend()

    def resume(self) -> None:
        if self._active:
            return
        if termios is None:
            raise RuntimeError("inline terminal mode requires POSIX termios")
        fd = self._input.fileno()
        if self._original is None:
            self._original = termios.tcgetattr(fd)
        configured = copy.deepcopy(self._original)
        configured[0] &= ~(
            getattr(termios, "ICRNL", 0)
            | getattr(termios, "IXON", 0)
            | getattr(termios, "IXOFF", 0)
        )
        configured[3] &= ~(termios.ECHO | termios.ICANON)
        configured[6][termios.VMIN] = 1
        configured[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSADRAIN, configured)
        self._write(_BRACKETED_PASTE_ENABLE)
        self._active = True

    def suspend(self) -> None:
        if not self._active:
            return
        self._write(_TERMINAL_MODES_DISABLE)
        if self._original is not None:
            termios.tcsetattr(self._input.fileno(), termios.TCSADRAIN, self._original)
        self._active = False

    def _write(self, payload: bytes) -> None:
        fd = self._output.fileno()
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("terminal output closed")
            offset += written


class _TerminalSignalHandlers:
    def __init__(self, session: _TerminalSession, renderer: NativeScrollbackRenderer) -> None:
        self._session = session
        self._renderer = renderer
        self._original: dict[int, Any] = {}
        self._interrupt_requested = False

    def __enter__(self):
        signum = getattr(signal, "SIGINT", None)
        if signum is not None:
            self._original[signum] = signal.getsignal(signum)
            signal.signal(signum, self._request_interrupt)
        for name in ("SIGHUP", "SIGTERM"):
            signum = getattr(signal, name, None)
            if signum is not None:
                self._original[signum] = signal.getsignal(signum)
                signal.signal(signum, self._interrupt)
        signum = getattr(signal, "SIGTSTP", None)
        if signum is not None:
            self._original[signum] = signal.getsignal(signum)
            signal.signal(signum, self._suspend)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        for signum, handler in self._original.items():
            signal.signal(signum, handler)

    @staticmethod
    def _interrupt(signum, frame) -> None:
        del signum, frame
        raise KeyboardInterrupt

    def _request_interrupt(self, signum, frame) -> None:
        del signum, frame
        self._interrupt_requested = True

    def consume_interrupt(self) -> bool:
        requested = self._interrupt_requested
        self._interrupt_requested = False
        return requested

    def _suspend(self, signum, frame) -> None:
        del frame
        self._renderer.suspend()
        self._session.suspend()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
        signal.signal(signum, self._suspend)
        self._session.resume()
        self._renderer.resume()


def _handle_input_event(
    controller: TuiController,
    renderer: NativeScrollbackRenderer,
    event: TuiInputEvent,
) -> None:
    if event.kind == "key":
        key = event.value
        if renderer.overlay_active and not controller.view.palette:
            key = {"UP": "WHEEL_UP", "DOWN": "WHEEL_DOWN"}.get(key, key)
        controller.handle_key(key)
    elif event.kind == "paste":
        controller.handle_paste(event.value)
    elif event.kind == "notice":
        controller.show_notice(event.value)


def _terminal_size(output_stream=None) -> tuple[int, int]:
    output = output_stream or sys.stdout
    try:
        size = os.get_terminal_size(output.fileno())
    except (AttributeError, OSError, ValueError):
        return 80, 24
    return max(size.columns, 20), max(size.lines, 6)


def _copy_with_osc52(text: str, output_stream=None) -> None:
    output = output_stream or sys.stdout
    try:
        payload = base64.b64encode(text.encode("utf-8"))
        os.write(output.fileno(), b"\x1b]52;c;" + payload + b"\x07")
    except (AttributeError, OSError, ValueError):
        return


def streams_are_tty(input_stream=None, output_stream=None) -> bool:
    input_value = input_stream or sys.stdin
    output_value = output_stream or sys.stdout
    return bool(
        os.name == "posix"
        and termios is not None
        and getattr(input_value, "isatty", lambda: False)()
        and getattr(output_value, "isatty", lambda: False)()
    )

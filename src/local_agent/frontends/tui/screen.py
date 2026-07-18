from __future__ import annotations

import base64
import os
import signal
import sys
from typing import Any

from .controller import TuiController
from .input import BracketedPasteDecoder
from .input import TuiInputEvent
from .view import render_frame


_BRACKETED_PASTE_ENABLE = b"\x1b[?2004h"
_BRACKETED_PASTE_DISABLE = b"\x1b[?2004l"
_NCURSES_MOUSE_BUTTON_STRIDE = 6


def run_curses_screen(controller: TuiController) -> int:
    import curses

    def wrapped(stdscr) -> int:
        return _screen_loop(stdscr, controller, curses)

    result = curses.wrapper(wrapped)
    return int(result or 0)


def _screen_loop(stdscr, controller: TuiController, curses_module: Any) -> int:
    _configure_screen(stdscr, curses_module)
    decoder = BracketedPasteDecoder()
    with _TerminalModes() as terminal_modes:
        with _TerminalSignalHandlers(stdscr, curses_module, terminal_modes=terminal_modes) as signals:
            while not controller.exit_requested:
                controller.poll()
                height, width = stdscr.getmaxyx()
                controller.update_viewport(width, height)
                frame = render_frame(controller.state, controller.view, width, height)
                _draw_frame(stdscr, frame, width, curses_module)
                clipboard_text = controller.take_clipboard_text()
                if clipboard_text is not None:
                    _copy_with_osc52(clipboard_text)
                events = _read_inputs(stdscr, curses_module, decoder)
                if signals.consume_interrupt():
                    controller.handle_key("CTRL_C")
                for event in events:
                    _handle_input_event(controller, event)
    return 0


def _configure_screen(stdscr, curses_module: Any) -> None:
    stdscr.keypad(True)
    stdscr.timeout(50)
    try:
        curses_module.curs_set(1)
    except curses_module.error:
        pass
    try:
        curses_module.mousemask(curses_module.ALL_MOUSE_EVENTS)
        curses_module.mouseinterval(0)
    except (AttributeError, curses_module.error):
        pass
    if not curses_module.has_colors():
        return
    try:
        curses_module.start_color()
        curses_module.use_default_colors()
        curses_module.init_pair(1, curses_module.COLOR_CYAN, -1)
        curses_module.init_pair(2, curses_module.COLOR_BLACK, curses_module.COLOR_CYAN)
    except curses_module.error:
        pass


def _draw_frame(stdscr, frame, width: int, curses_module: Any) -> None:
    stdscr.erase()
    for row, line in enumerate(frame.lines):
        attributes = 0
        if row == 0:
            attributes = curses_module.A_BOLD | _color_pair(curses_module, 1)
        elif row in frame.accent_rows:
            attributes = curses_module.A_BOLD | _color_pair(curses_module, 1)
        elif row == len(frame.lines) - 1:
            attributes = curses_module.A_REVERSE
        try:
            stdscr.addnstr(row, 0, line, max(width - 1, 0), attributes)
        except curses_module.error:
            continue
    try:
        stdscr.move(frame.cursor_y, frame.cursor_x)
    except curses_module.error:
        pass
    stdscr.refresh()


def _read_inputs(
    stdscr,
    curses_module: Any,
    decoder: BracketedPasteDecoder,
) -> tuple[TuiInputEvent, ...]:
    try:
        value = stdscr.get_wch()
    except curses_module.error:
        expired = decoder.expire()
        return expired or decoder.flush_normal()
    if isinstance(value, str):
        return decoder.feed(value)
    if value == curses_module.KEY_ENTER:
        return decoder.feed("\n")
    if value == getattr(curses_module, "KEY_MOUSE", object()):
        return _mouse_inputs(curses_module)
    mapping = {
        curses_module.KEY_BACKSPACE: "BACKSPACE",
        curses_module.KEY_DC: "DELETE",
        curses_module.KEY_DOWN: "DOWN",
        curses_module.KEY_END: "END",
        curses_module.KEY_HOME: "HOME",
        curses_module.KEY_LEFT: "LEFT",
        curses_module.KEY_NPAGE: "PAGE_DOWN",
        curses_module.KEY_PPAGE: "PAGE_UP",
        curses_module.KEY_RESIZE: "RESIZE",
        curses_module.KEY_RIGHT: "RIGHT",
        curses_module.KEY_UP: "UP",
    }
    key = mapping.get(value)
    return (TuiInputEvent("key", key),) if key is not None else ()


def _mouse_inputs(curses_module: Any) -> tuple[TuiInputEvent, ...]:
    try:
        _, _, _, _, state = curses_module.getmouse()
    except (AttributeError, curses_module.error):
        return ()
    if state & getattr(curses_module, "BUTTON4_PRESSED", 0):
        return (TuiInputEvent("key", "WHEEL_UP"),)
    button4 = getattr(curses_module, "BUTTON4_PRESSED", 0)
    button5 = getattr(
        curses_module,
        "BUTTON5_PRESSED",
        button4 << _NCURSES_MOUSE_BUTTON_STRIDE,
    )
    if state & button5:
        return (TuiInputEvent("key", "WHEEL_DOWN"),)
    return ()


def _handle_input_event(controller: TuiController, event: TuiInputEvent) -> None:
    if event.kind == "key":
        controller.handle_key(event.value)
    elif event.kind == "paste":
        controller.handle_paste(event.value)
    elif event.kind == "notice":
        controller.show_notice(event.value)


def _color_pair(curses_module: Any, pair: int) -> int:
    try:
        return curses_module.color_pair(pair)
    except curses_module.error:
        return 0


def _copy_with_osc52(text: str) -> None:
    try:
        payload = base64.b64encode(text.encode("utf-8"))
        os.write(sys.stdout.fileno(), b"\x1b]52;c;" + payload + b"\x07")
    except (AttributeError, OSError, ValueError):
        return


class _TerminalModes:
    def __init__(self, output=None) -> None:
        self._output = output or sys.stdout
        self._enabled = False

    def __enter__(self):
        self.resume()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.suspend()

    def resume(self) -> None:
        if not self._enabled:
            self._write(_BRACKETED_PASTE_ENABLE)
            self._enabled = True

    def suspend(self) -> None:
        if self._enabled:
            self._write(_BRACKETED_PASTE_DISABLE)
            self._enabled = False

    def _write(self, payload: bytes) -> None:
        try:
            os.write(self._output.fileno(), payload)
        except (AttributeError, OSError, ValueError):
            return


class _TerminalSignalHandlers:
    def __init__(self, stdscr, curses_module: Any, *, terminal_modes: _TerminalModes | None = None) -> None:
        self._stdscr = stdscr
        self._curses = curses_module
        self._terminal_modes = terminal_modes
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
        if self._terminal_modes is not None:
            self._terminal_modes.suspend()
        self._curses.endwin()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
        signal.signal(signum, self._suspend)
        try:
            self._curses.reset_prog_mode()
            self._stdscr.refresh()
        except self._curses.error:
            pass
        if self._terminal_modes is not None:
            self._terminal_modes.resume()


def streams_are_tty(input_stream=None, output_stream=None) -> bool:
    input_value = input_stream or sys.stdin
    output_value = output_stream or sys.stdout
    return bool(
        getattr(input_value, "isatty", lambda: False)()
        and getattr(output_value, "isatty", lambda: False)()
    )

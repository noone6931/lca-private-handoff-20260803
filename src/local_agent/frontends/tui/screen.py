from __future__ import annotations

import base64
import os
import signal
import sys
from typing import Any

from .controller import TuiController
from .view import render_frame


def run_curses_screen(controller: TuiController) -> int:
    import curses

    def wrapped(stdscr) -> int:
        return _screen_loop(stdscr, controller, curses)

    result = curses.wrapper(wrapped)
    return int(result or 0)


def _screen_loop(stdscr, controller: TuiController, curses_module: Any) -> int:
    _configure_screen(stdscr, curses_module)
    with _TerminalSignalHandlers(stdscr, curses_module) as signals:
        while not controller.exit_requested:
            controller.poll()
            height, width = stdscr.getmaxyx()
            frame = render_frame(controller.state, controller.view, width, height)
            _draw_frame(stdscr, frame, width, curses_module)
            clipboard_text = controller.take_clipboard_text()
            if clipboard_text is not None:
                _copy_with_osc52(clipboard_text)
            key = _read_key(stdscr, curses_module)
            if signals.consume_interrupt():
                controller.handle_key("CTRL_C")
            if key is not None:
                controller.handle_key(key)
    return 0


def _configure_screen(stdscr, curses_module: Any) -> None:
    stdscr.keypad(True)
    stdscr.timeout(50)
    try:
        curses_module.curs_set(1)
    except curses_module.error:
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


def _read_key(stdscr, curses_module: Any) -> str | None:
    try:
        value = stdscr.get_wch()
    except curses_module.error:
        return None
    if value == "\x1b":
        return _escape_sequence(stdscr, curses_module)
    if isinstance(value, str):
        return {
            "\x03": "CTRL_C",
            "\x06": "CTRL_F",
            "\x10": "CTRL_P",
            "\x11": "CTRL_Q",
            "\x19": "CTRL_Y",
            "\x7f": "BACKSPACE",
            "\b": "BACKSPACE",
            "\n": "ENTER",
            "\r": "ENTER",
        }.get(value, value)
    mapping = {
        curses_module.KEY_BACKSPACE: "BACKSPACE",
        curses_module.KEY_DC: "DELETE",
        curses_module.KEY_DOWN: "DOWN",
        curses_module.KEY_END: "END",
        curses_module.KEY_ENTER: "ENTER",
        curses_module.KEY_HOME: "HOME",
        curses_module.KEY_LEFT: "LEFT",
        curses_module.KEY_NPAGE: "PAGE_DOWN",
        curses_module.KEY_PPAGE: "PAGE_UP",
        curses_module.KEY_RESIZE: "RESIZE",
        curses_module.KEY_RIGHT: "RIGHT",
        curses_module.KEY_UP: "UP",
    }
    return mapping.get(value)


def _escape_sequence(stdscr, curses_module: Any) -> str:
    stdscr.timeout(20)
    try:
        following = stdscr.get_wch()
    except curses_module.error:
        return "ESC"
    finally:
        stdscr.timeout(50)
    if following in {"\n", "\r", curses_module.KEY_ENTER}:
        return "ALT_ENTER"
    return "ESC"


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


class _TerminalSignalHandlers:
    def __init__(self, stdscr, curses_module: Any) -> None:
        self._stdscr = stdscr
        self._curses = curses_module
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
        self._curses.endwin()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
        signal.signal(signum, self._suspend)
        try:
            self._curses.reset_prog_mode()
            self._stdscr.refresh()
        except self._curses.error:
            pass


def streams_are_tty(input_stream=None, output_stream=None) -> bool:
    input_value = input_stream or sys.stdin
    output_value = output_stream or sys.stdout
    return bool(
        getattr(input_value, "isatty", lambda: False)()
        and getattr(output_value, "isatty", lambda: False)()
    )

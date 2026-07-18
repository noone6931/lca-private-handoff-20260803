from __future__ import annotations

from contextlib import contextmanager
import sys
from typing import Any, Iterator, TextIO

try:
    import termios as _termios
except ImportError:  # pragma: no cover - POSIX-only enhancement.
    _termios = None  # type: ignore[assignment]


_ACTIVE_GUARDS: list[TerminalInputSilencer] = []


class TerminalInputSilencer:
    def __init__(self, stdin: TextIO | None = None) -> None:
        self._stdin = stdin or sys.stdin
        self._fd: int | None = None
        self._original_attrs: Any = None
        self._enabled = False
        self._paused = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def __enter__(self) -> TerminalInputSilencer:
        if _termios is None or not _isatty(self._stdin):
            return self
        try:
            self._fd = self._stdin.fileno()
            self._original_attrs = _termios.tcgetattr(self._fd)
            self._set_echo(False)
        except (AttributeError, OSError, ValueError):
            self._fd = None
            self._original_attrs = None
            return self
        self._enabled = True
        _ACTIVE_GUARDS.append(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._enabled:
            self.flush_input()
            self._restore_original()
        self._enabled = False
        self._paused = False
        if self in _ACTIVE_GUARDS:
            _ACTIVE_GUARDS.remove(self)

    def pause_for_prompt(self) -> None:
        if not self._enabled or self._paused:
            return
        self.flush_input()
        self._restore_original()
        self._paused = True

    def resume_after_prompt(self) -> None:
        if not self._enabled or not self._paused:
            return
        self._set_echo(False)
        self._paused = False

    def flush_input(self) -> None:
        if _termios is None or self._fd is None:
            return
        try:
            _termios.tcflush(self._fd, _termios.TCIFLUSH)
        except (AttributeError, OSError, ValueError):
            return

    def _restore_original(self) -> None:
        if _termios is None or self._fd is None or self._original_attrs is None:
            return
        try:
            _termios.tcsetattr(self._fd, _termios.TCSADRAIN, self._original_attrs)
        except (AttributeError, OSError, ValueError):
            return

    def _set_echo(self, enabled: bool) -> None:
        if _termios is None or self._fd is None:
            return
        attrs = list(_termios.tcgetattr(self._fd))
        if enabled:
            attrs[3] |= _termios.ECHO
        else:
            attrs[3] &= ~_termios.ECHO
        _termios.tcsetattr(self._fd, _termios.TCSADRAIN, attrs)


@contextmanager
def silenced_terminal_input(stdin: TextIO | None = None) -> Iterator[TerminalInputSilencer]:
    with TerminalInputSilencer(stdin) as guard:
        yield guard


@contextmanager
def terminal_input_prompt(stdin: TextIO | None = None) -> Iterator[None]:
    guard = _active_guard(stdin or sys.stdin)
    if guard is None:
        yield
        return
    guard.pause_for_prompt()
    try:
        yield
    finally:
        guard.resume_after_prompt()


def _active_guard(stdin: TextIO) -> TerminalInputSilencer | None:
    for guard in reversed(_ACTIVE_GUARDS):
        if guard._stdin is stdin:
            return guard
    return None


def _isatty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False

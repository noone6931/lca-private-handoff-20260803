from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile


COMPOSER_HISTORY_FILENAME = "composer_history.jsonl"
MAX_HISTORY_ENTRIES = 200
MAX_HISTORY_ENTRY_BYTES = 64 * 1024
MAX_HISTORY_RECORD_BYTES = MAX_HISTORY_ENTRY_BYTES * 6 + 128
MAX_HISTORY_FILE_BYTES = 4 * 1024 * 1024
_FORMAT_VERSION = 1


@dataclass(frozen=True)
class HistorySnapshot:
    persistent_entries: tuple[str, ...]
    local_entries: tuple[str, ...]
    persistence_enabled: bool
    error: str = ""


class ComposerHistory:
    """Synchronous persistent prompt history and shell-style recall owner."""

    def __init__(self, path: Path | None) -> None:
        self._path: Path | None = None
        self._persistent: list[str] = []
        self._local: list[str] = []
        self._cursor: int | None = None
        self._draft = ""
        self._last_recalled: str | None = None
        self._persistence_enabled = False
        self._error = ""
        self.rebind(path)

    @property
    def snapshot(self) -> HistorySnapshot:
        return HistorySnapshot(
            tuple(self._persistent),
            tuple(self._local),
            self._persistence_enabled,
            self._error,
        )

    @property
    def path(self) -> Path | None:
        return self._path

    def append(self, prompt: str) -> bool:
        value = prompt.strip()
        if not value or len(value.encode("utf-8")) > MAX_HISTORY_ENTRY_BYTES:
            return False
        entries = self._entries()
        if entries and entries[-1] == value:
            self.reset_navigation()
            return False
        self._local.append(value)
        compact = self._trim_memory()
        self.reset_navigation()
        if not self._persistence_enabled or self._path is None:
            return True
        try:
            record = _encode_record(value)
            current_size = self._path.stat().st_size if self._path.exists() else 0
            if compact or current_size + len(record) > MAX_HISTORY_FILE_BYTES:
                self._rewrite()
            else:
                self._append_record(record)
        except (OSError, ValueError):
            self._disable("Persistent composer history is unavailable for this frontend run.")
        return True

    def navigate(self, direction: int, text: str, cursor: int) -> str | None:
        if direction not in {-1, 1}:
            raise ValueError("History direction must be -1 or 1.")
        if "\n" in text or cursor not in {0, len(text)}:
            return None
        entries = self._entries()
        if not entries:
            return None
        if self._cursor is None:
            if direction > 0 or text:
                return None
            self._draft = text
            self._cursor = len(entries) - 1
        else:
            if text != self._last_recalled:
                return None
            next_cursor = self._cursor + direction
            if next_cursor < 0:
                next_cursor = 0
            if next_cursor >= len(entries):
                self.reset_navigation()
                return self._draft
            self._cursor = next_cursor
        self._last_recalled = entries[self._cursor]
        return self._last_recalled

    def reset_navigation(self) -> None:
        self._cursor = None
        self._draft = ""
        self._last_recalled = None

    def rebind(self, path: Path | None) -> bool:
        if path is not None:
            try:
                canonical = path.expanduser().resolve()
            except (OSError, ValueError):
                canonical = None
            if canonical is not None and canonical == self._path and self._persistence_enabled:
                return True
        self._path = None
        self._persistent.clear()
        self._local.clear()
        self.reset_navigation()
        self._persistence_enabled = False
        self._error = ""
        if path is None:
            return False
        try:
            if canonical is None:
                raise ValueError("invalid composer history path")
            canonical.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not canonical.parent.is_dir():
                raise OSError("history parent is not a directory")
            self._path = canonical
            self._persistent = self._load(canonical)
            if canonical.exists():
                os.chmod(canonical, 0o600)
            self._persistence_enabled = True
            return True
        except (OSError, TypeError, UnicodeError, ValueError, json.JSONDecodeError):
            self._disable("Persistent composer history is unavailable for this frontend run.")
            return False

    def _entries(self) -> list[str]:
        return [*self._persistent, *self._local]

    def _trim_memory(self) -> bool:
        changed = False
        while len(self._persistent) + len(self._local) > MAX_HISTORY_ENTRIES:
            changed = True
            if self._persistent:
                self._persistent.pop(0)
            else:
                self._local.pop(0)
        while _records_size(self._entries()) > MAX_HISTORY_FILE_BYTES:
            changed = True
            if self._persistent:
                self._persistent.pop(0)
            elif len(self._local) > 1:
                self._local.pop(0)
            else:
                break
        return changed

    @staticmethod
    def _load(path: Path) -> list[str]:
        if not path.exists():
            return []
        if not path.is_file() or path.stat().st_size > MAX_HISTORY_FILE_BYTES:
            raise ValueError("invalid composer history file")
        payload = path.read_bytes()
        if payload and not payload.endswith(b"\n"):
            raise ValueError("partial composer history record")
        text = payload.decode("utf-8")
        entries: list[str] = []
        for line in text.splitlines():
            if not line or len(line.encode("utf-8")) > MAX_HISTORY_RECORD_BYTES:
                raise ValueError("invalid composer history record")
            record = json.loads(line)
            if set(record) != {"v", "prompt"} or record["v"] != _FORMAT_VERSION:
                raise ValueError("invalid composer history record")
            prompt = record["prompt"]
            if not isinstance(prompt, str) or not prompt or len(prompt.encode("utf-8")) > MAX_HISTORY_ENTRY_BYTES:
                raise ValueError("invalid composer history prompt")
            entries.append(prompt)
        return entries[-MAX_HISTORY_ENTRIES:]

    def _append_record(self, record: bytes) -> None:
        assert self._path is not None
        descriptor = os.open(self._path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "ab", closefd=False) as stream:
                stream.write(record)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)

    def _rewrite(self) -> None:
        assert self._path is not None
        payload = b"".join(_encode_record(prompt) for prompt in self._entries())
        if len(payload) > MAX_HISTORY_FILE_BYTES:
            raise ValueError("composer history exceeds its total budget")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".composer-history-", dir=self._path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self._path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _disable(self, message: str) -> None:
        self._path = None
        self._persistence_enabled = False
        self._error = message


def composer_history_path(state_dir: Path) -> Path:
    return state_dir / COMPOSER_HISTORY_FILENAME


def _encode_record(prompt: str) -> bytes:
    return (json.dumps({"v": _FORMAT_VERSION, "prompt": prompt}, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _records_size(entries: list[str]) -> int:
    return sum(len(_encode_record(entry)) for entry in entries)

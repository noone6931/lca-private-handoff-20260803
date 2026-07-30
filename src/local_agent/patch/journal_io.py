from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import BinaryIO


def write_journal_temporary(path: Path, payload: bytes, mode: int) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            write_journal_payload(handle, payload)
    except BaseException:
        discard_journal_temporary(temporary)
        raise
    return temporary


def write_journal_payload(handle: BinaryIO, payload: bytes) -> None:
    handle.write(payload)
    handle.flush()
    os.fsync(handle.fileno())


def discard_journal_temporary(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def journal_state(path: Path) -> tuple[bool | None, bytes | None, int | None]:
    try:
        return True, path.read_bytes(), stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return False, None, None
    except OSError:
        return None, None, None


__all__ = [
    "discard_journal_temporary",
    "journal_state",
    "write_journal_payload",
    "write_journal_temporary",
]

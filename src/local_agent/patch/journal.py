from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, BinaryIO


def append_patch_record(path: Path, record: dict[str, Any]) -> None:
    """Atomically append one JSONL record with os.replace as the commit boundary."""

    encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = path.read_bytes()
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        current = b""
        mode = 0o600

    temporary = _write_temporary_file(path, current + encoded, mode)
    try:
        os.replace(temporary, path)
    except BaseException:
        _discard_temporary(temporary)
        raise


def _write_temporary_file(path: Path, payload: bytes, mode: int) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            _write_payload(handle, payload)
    except BaseException:
        _discard_temporary(temporary)
        raise
    return temporary


def _write_payload(handle: BinaryIO, payload: bytes) -> None:
    handle.write(payload)
    handle.flush()
    os.fsync(handle.fileno())


def _discard_temporary(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass

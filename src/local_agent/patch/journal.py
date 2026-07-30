from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from .journal_contracts import PatchJournalMutationResult
from .journal_io import discard_journal_temporary
from .journal_io import write_journal_temporary
from .journal_recovery import restore_after_replace_error


def patch_journal_path(
    state_directory: Path,
    session_id: str | None,
) -> Path:
    return state_directory / "patches" / f"{session_id or 'default'}.jsonl"


def append_patch_record(path: Path, record: dict[str, Any]) -> None:
    """Atomically append one JSONL record with os.replace as the commit boundary."""

    encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = path.read_bytes()
        mode = stat.S_IMODE(path.stat().st_mode)
        existed = True
    except FileNotFoundError:
        current = b""
        mode = 0o600
        existed = False

    payload = current + encoded
    temporary = write_journal_temporary(path, payload, mode)
    try:
        os.replace(temporary, path)
    except BaseException as exc:
        discard_journal_temporary(temporary)
        setattr(
            exc,
            "patch_journal_result",
            restore_after_replace_error(
                path,
                before=current,
                after=payload,
                mode=mode,
                existed=existed,
                replace_file=os.replace,
            ),
        )
        raise


__all__ = [
    "PatchJournalMutationResult",
    "append_patch_record",
    "patch_journal_path",
]

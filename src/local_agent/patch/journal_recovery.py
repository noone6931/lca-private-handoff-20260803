from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .journal_contracts import PatchJournalMutationResult
from .journal_io import discard_journal_temporary
from .journal_io import journal_state
from .journal_io import write_journal_temporary


def restore_after_replace_error(
    path: Path,
    *,
    before: bytes,
    after: bytes,
    mode: int,
    existed: bool,
    replace_file: Callable[[Path, Path], None],
) -> PatchJournalMutationResult:
    expected_before = (True, before, mode) if existed else (False, None, None)
    observed = journal_state(path)
    if observed == expected_before:
        return PatchJournalMutationResult("restored", False, False, "replace_failed")
    if observed[:2] != (True, after):
        return PatchJournalMutationResult(
            "indeterminate",
            True,
            None,
            "journal_state_unexpected",
        )
    temporary: Path | None = None
    try:
        if existed:
            temporary = write_journal_temporary(path, before, mode)
            replace_file(temporary, path)
        else:
            path.unlink()
    except BaseException:
        pass
    finally:
        if temporary is not None:
            discard_journal_temporary(temporary)
    if journal_state(path) == expected_before:
        return PatchJournalMutationResult(
            "restored",
            False,
            False,
            "replace_failed_after_commit",
        )
    final = journal_state(path)
    return PatchJournalMutationResult(
        "indeterminate",
        True,
        final[:2] == (True, after) if final[0] is not None else None,
        "journal_compensation_failed",
    )


__all__ = ["restore_after_replace_error"]

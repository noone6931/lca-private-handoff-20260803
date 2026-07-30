from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PatchJournalMutationResult:
    state: Literal["restored", "indeterminate"]
    journal_changed: bool
    record_persisted: bool | None
    error_kind: str

    def metadata(self) -> dict[str, object]:
        return {
            "state": self.state,
            "journal_changed": self.journal_changed,
            "record_persisted": self.record_persisted,
            "error_kind": self.error_kind,
        }


__all__ = ["PatchJournalMutationResult"]

from __future__ import annotations

from dataclasses import dataclass

from ..composer_history import HistorySnapshot
from ..composer_history import MAX_HISTORY_ENTRIES


MAX_HISTORY_SEARCH_QUERY_BYTES = 4 * 1024
MAX_HISTORY_SEARCH_PREVIEW_BYTES = 4 * 1024


@dataclass(frozen=True)
class HistorySearchView:
    active: bool = False
    query: str = ""
    match_preview: str = ""
    position: int = 0
    match_count: int = 0
    status: str = "inactive"


class ComposerHistorySearch:
    """Bounded reverse search over one immutable composer-history snapshot."""

    def __init__(self) -> None:
        self._active = False
        self._entries: tuple[str, ...] = ()
        self._matches: tuple[str, ...] = ()
        self._query = ""
        self._selected = 0
        self._draft = ""
        self._draft_cursor = 0

    @property
    def view(self) -> HistorySearchView:
        match = self._current_match()
        if not self._active:
            status = "inactive"
        elif not self._query:
            status = "empty"
        elif match is None:
            status = "no_match"
        else:
            status = "match"
        return HistorySearchView(
            active=self._active,
            query=self._query,
            match_preview=_bounded_utf8(match or "", MAX_HISTORY_SEARCH_PREVIEW_BYTES),
            position=self._selected + 1 if match is not None else 0,
            match_count=len(self._matches),
            status=status,
        )

    def open(self, snapshot: HistorySnapshot, draft: str, cursor: int) -> HistorySearchView:
        self.reset()
        self._active = True
        self._entries = _unique_newest_entries(snapshot)
        self._draft = draft
        self._draft_cursor = min(max(cursor, 0), len(draft))
        return self.view

    def update_query(self, query: str) -> bool:
        if not self._active or len(query.encode("utf-8")) > MAX_HISTORY_SEARCH_QUERY_BYTES:
            return False
        self._query = query
        self._selected = 0
        folded = query.casefold()
        self._matches = (
            tuple(entry for entry in self._entries if folded in entry.casefold())
            if folded
            else ()
        )
        return True

    def move_older(self) -> HistorySearchView:
        if self._matches:
            self._selected = min(self._selected + 1, len(self._matches) - 1)
        return self.view

    def move_newer(self) -> HistorySearchView:
        if self._matches:
            self._selected = max(self._selected - 1, 0)
        return self.view

    def accept(self) -> str | None:
        match = self._current_match()
        if match is None:
            return None
        self.reset()
        return match

    def cancel(self) -> tuple[str, int]:
        draft = self._draft
        cursor = self._draft_cursor
        self.reset()
        return draft, cursor

    def reset(self) -> None:
        self._active = False
        self._entries = ()
        self._matches = ()
        self._query = ""
        self._selected = 0
        self._draft = ""
        self._draft_cursor = 0

    def _current_match(self) -> str | None:
        if not self._matches:
            return None
        return self._matches[self._selected]


def _unique_newest_entries(snapshot: HistorySnapshot) -> tuple[str, ...]:
    entries = (*snapshot.persistent_entries, *snapshot.local_entries)[-MAX_HISTORY_ENTRIES:]
    seen: set[str] = set()
    newest: list[str] = []
    for entry in reversed(entries):
        if entry in seen:
            continue
        seen.add(entry)
        newest.append(entry)
    return tuple(newest)


def _bounded_utf8(value: str, byte_limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_limit:
        return value
    return encoded[:byte_limit].decode("utf-8", errors="ignore")

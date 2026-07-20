from __future__ import annotations

import unittest

from local_agent.frontends.composer_history import HistorySnapshot
from local_agent.frontends.composer_history import MAX_HISTORY_ENTRIES
from local_agent.frontends.tui.history_search import ComposerHistorySearch
from local_agent.frontends.tui.history_search import MAX_HISTORY_SEARCH_QUERY_BYTES


def _snapshot(*entries: str) -> HistorySnapshot:
    return HistorySnapshot(tuple(entries), (), True)


class ComposerHistorySearchTests(unittest.TestCase):
    def test_casefold_literal_search_is_newest_first_and_exactly_deduplicated(self) -> None:
        search = ComposerHistorySearch()
        search.open(_snapshot("Alpha first", "beta", "ALPHA latest", "beta"), "draft", 2)

        self.assertTrue(search.update_query("alpha"))
        self.assertEqual(search.view.match_preview, "ALPHA latest")
        self.assertEqual(search.view.match_count, 2)
        self.assertEqual(search.move_older().match_preview, "Alpha first")
        self.assertEqual(search.move_older().match_preview, "Alpha first")
        self.assertEqual(search.move_newer().match_preview, "ALPHA latest")
        self.assertEqual(search.move_newer().match_preview, "ALPHA latest")

    def test_query_change_resets_to_newest_match_without_regex_semantics(self) -> None:
        search = ComposerHistorySearch()
        search.open(_snapshot("literal .* older", "other", "literal .* newest"), "", 0)
        search.update_query("literal")
        search.move_older()

        self.assertTrue(search.update_query(".*"))
        self.assertEqual(search.view.position, 1)
        self.assertEqual(search.view.match_preview, "literal .* newest")

    def test_empty_and_missing_query_expose_no_history_text(self) -> None:
        search = ComposerHistorySearch()
        search.open(_snapshot("secret prompt"), "draft", 3)

        self.assertEqual(search.view.status, "empty")
        self.assertEqual(search.view.match_preview, "")
        search.update_query("missing")
        self.assertEqual(search.view.status, "no_match")
        self.assertEqual(search.view.match_preview, "")
        self.assertIsNone(search.accept())

    def test_accept_and_cancel_have_distinct_draft_lifecycles(self) -> None:
        search = ComposerHistorySearch()
        search.open(_snapshot("selected"), "draft text", 3)
        search.update_query("select")
        self.assertEqual(search.accept(), "selected")
        self.assertFalse(search.view.active)

        search.open(_snapshot("selected"), "multi\nline", 2)
        search.update_query("select")
        self.assertEqual(search.cancel(), ("multi\nline", 2))
        self.assertFalse(search.view.active)

    def test_snapshot_and_query_budgets_are_bounded(self) -> None:
        entries = tuple(f"entry {index}" for index in range(MAX_HISTORY_ENTRIES + 20))
        search = ComposerHistorySearch()
        search.open(_snapshot(*entries), "", 0)

        self.assertTrue(search.update_query("entry"))
        self.assertEqual(search.view.match_count, MAX_HISTORY_ENTRIES)
        previous = search.view
        self.assertFalse(search.update_query("x" * (MAX_HISTORY_SEARCH_QUERY_BYTES + 1)))
        self.assertEqual(search.view, previous)


if __name__ == "__main__":
    unittest.main()

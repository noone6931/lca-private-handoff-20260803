from __future__ import annotations

import unittest

from local_agent.frontends.tui.composer_layout import layout_composer
from local_agent.frontends.tui.composer_layout import MAX_COMPOSER_ROWS
from local_agent.frontends.tui.composer_layout import move_composer_cursor_vertical
from local_agent.frontends.tui.text import cell_width


class TuiComposerLayoutTests(unittest.TestCase):
    def test_physical_newlines_empty_lines_and_trailing_line_are_preserved(self) -> None:
        text = "first\n\nlast\n"

        layout = layout_composer(text, len(text), 20)

        self.assertEqual(layout.rows, ("> first", "| ", "| last", "| "))
        self.assertEqual(layout.total_rows, 4)
        self.assertEqual(layout.cursor_row, 3)
        self.assertNotIn("\\n", "".join(layout.rows))

    def test_long_physical_line_soft_wraps_by_display_cells(self) -> None:
        layout = layout_composer("abcdefgh", 8, 6)

        self.assertEqual(layout.rows, ("> abc", "| def", "| gh"))
        self.assertEqual(layout.cursor_row, 2)
        self.assertEqual(layout.cursor_col, 4)
        self.assertTrue(all(cell_width(row) <= 6 for row in layout.rows))

    def test_empty_composer_and_cursor_boundaries_are_stable(self) -> None:
        empty = layout_composer("", 0, 4)
        start = layout_composer("abc", -5, 8)
        end = layout_composer("abc", 99, 8)

        self.assertEqual(empty.rows, ("> ",))
        self.assertEqual((empty.cursor_row, empty.cursor_col), (0, 2))
        self.assertEqual(start.cursor_col, 2)
        self.assertEqual(end.cursor_col, 5)

    def test_wide_combining_and_non_bmp_clusters_are_atomic(self) -> None:
        text = "中e\u0301👩‍💻Z"
        layout = layout_composer(text, len(text), 6)

        self.assertEqual(layout.rows, ("> 中e\u0301", "| 👩‍💻Z"))
        self.assertEqual(layout.cursor_col, 5)
        self.assertTrue(all(cell_width(row) <= 6 for row in layout.rows))
        self.assertFalse(any(stop.source_offset in {4, 5} for stop in layout.visual_rows[1].cursor_stops))

    def test_control_and_bidi_characters_are_sanitized_without_mutating_source_offsets(self) -> None:
        text = "a\x1b\u202eb"

        layout = layout_composer(text, len(text), 12)

        self.assertEqual(layout.rows, ("> ab",))
        self.assertEqual(layout.visual_rows[0].source_end, len(text))
        self.assertEqual(layout.visual_rows[0].cursor_stops[-1].source_offset, len(text))
        self.assertNotIn("\x1b", layout.rows[0])
        self.assertNotIn("\u202e", layout.rows[0])

    def test_visible_window_is_bounded_and_follows_cursor(self) -> None:
        text = "\n".join(f"line-{index}" for index in range(10))

        bottom = layout_composer(text, len(text), 24)
        top = layout_composer(text, 0, 24)
        short = layout_composer(text, len(text), 24, row_budget=3)

        self.assertEqual(len(bottom.rows), MAX_COMPOSER_ROWS)
        self.assertEqual(bottom.cursor_row, MAX_COMPOSER_ROWS - 1)
        self.assertGreater(bottom.window_start, 0)
        self.assertEqual(top.window_start, 0)
        self.assertEqual(top.cursor_row, 0)
        self.assertEqual(len(short.rows), 3)
        self.assertEqual(short.cursor_row, 2)

    def test_resize_reflows_and_keeps_cursor_visible(self) -> None:
        text = "one two three four five six"

        wide = layout_composer(text, 14, 24)
        narrow = layout_composer(text, 14, 8)

        self.assertGreater(narrow.total_rows, wide.total_rows)
        self.assertLess(narrow.cursor_row, len(narrow.rows))
        self.assertLess(narrow.cursor_col, 8)

    def test_layout_accepts_the_input_cap_and_rejects_larger_text(self) -> None:
        exact = "x" * (64 * 1024)

        layout = layout_composer(exact, len(exact), 80)

        self.assertEqual(len(layout.rows), MAX_COMPOSER_ROWS)
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            layout_composer(exact + "x", len(exact) + 1, 80)

    def test_vertical_move_preserves_preferred_cell_and_clamps_short_lines(self) -> None:
        text = "123456\nx\n123456"
        first_down = move_composer_cursor_vertical(text, 5, 20, 1, None)
        second_down = move_composer_cursor_vertical(
            text, first_down.cursor, 20, 1, first_down.preferred_column
        )

        self.assertTrue(first_down.moved)
        self.assertEqual(first_down.cursor, text.index("x") + 1)
        self.assertEqual(first_down.preferred_column, 5)
        self.assertTrue(second_down.moved)
        self.assertEqual(second_down.cursor, text.rindex("123456") + 5)
        self.assertEqual(second_down.preferred_column, 5)

    def test_vertical_move_traverses_soft_wrap_and_stops_at_boundaries(self) -> None:
        text = "abcdefghij"
        down = move_composer_cursor_vertical(text, 2, 6, 1, None)
        back = move_composer_cursor_vertical(text, down.cursor, 6, -1, down.preferred_column)
        top = move_composer_cursor_vertical(text, 0, 6, -1, None)
        bottom = move_composer_cursor_vertical(text, len(text), 6, 1, None)

        self.assertTrue(down.moved)
        self.assertGreater(down.cursor, 2)
        self.assertTrue(back.moved)
        self.assertEqual(back.cursor, 2)
        self.assertFalse(top.moved)
        self.assertFalse(bottom.moved)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from scripts.sync_project_excel import parse_markdown_tables
from scripts.sync_project_excel import parse_table
from scripts.sync_project_excel import split_markdown_row


class SyncProjectExcelTests(unittest.TestCase):
    def test_split_markdown_row_preserves_code_and_escaped_pipes(self) -> None:
        self.assertEqual(
            split_markdown_row(r"| Mode | `off|auto|llm` and left\|right |"),
            [" Mode ", " `off|auto|llm` and left|right "],
        )

    def test_blank_lines_do_not_split_one_section_into_duplicate_sheets(self) -> None:
        sheets = parse_markdown_tables(
            """## Todo

| ID | Status |
|---|---|
| T-1 | done |

| T-2 | next |

## Risk

| ID | Status |
|---|---|
| R-1 | open |
"""
        )

        self.assertEqual([sheet.name for sheet in sheets], ["Todo", "Risk"])
        self.assertEqual(sheets[0].rows[-1], ["T-2", "next"])

    def test_mismatched_table_width_fails_instead_of_creating_stray_columns(self) -> None:
        with self.assertRaisesRegex(ValueError, "has 3 columns; expected 2"):
            parse_table(["| A | B |", "|---|---|", "| 1 | 2 | 3 |"])


if __name__ == "__main__":
    unittest.main()

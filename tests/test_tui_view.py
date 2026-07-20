from __future__ import annotations

import unittest

from local_agent.frontends.tui.model import ToolEntry
from local_agent.frontends.tui.model import TranscriptEntry
from local_agent.frontends.tui.model import TuiState
from local_agent.frontends.tui.text import cell_width
from local_agent.frontends.tui.text import clip_cells
from local_agent.frontends.tui.text import wrap_cells
from local_agent.frontends.tui.view import TuiView
from local_agent.frontends.tui.view import TuiViewport
from local_agent.frontends.tui.view import render_frame
from local_agent.frontends.tui.view import render_inline_frame
from local_agent.frontends.tui.view import transcript_lines


class TuiViewTests(unittest.TestCase):
    def test_queued_follow_up_footer_is_bounded_metadata_not_prompt_text(self) -> None:
        frame = render_frame(
            TuiState(),
            TuiView(queued_prompt_bytes=42),
            100,
            10,
        )
        footer = frame.lines[-1]

        self.assertIn("follow-up queued (42 bytes; Alt-Up restores)", footer)
        self.assertNotIn("queued prompt contents", footer)
        self.assertNotIn("\x1b", footer)

    def test_unicode_width_and_wrapping_are_cell_bounded(self) -> None:
        self.assertEqual(cell_width("A中"), 3)
        self.assertEqual(clip_cells("A中文", 4), "A中")
        self.assertEqual(wrap_cells("A中文B", 4), ("A中", "文B"))

    def test_emoji_clusters_are_measured_and_wrapped_atomically(self) -> None:
        self.assertEqual(cell_width("👩‍💻"), 2)
        self.assertEqual(cell_width("🇨🇳"), 2)
        self.assertEqual(clip_cells("A👩‍💻B", 3), "A👩‍💻")
        self.assertEqual(wrap_cells("A👩‍💻中B", 4), ("A👩‍💻", "中B"))

    def test_markdown_source_keeps_structure_with_narrow_role_marker(self) -> None:
        source = """回答开头

### 关键约束与设计
- 第一项包含中文和 emoji 👩‍💻，并且足够长以触发换行
  - 嵌套项
```python
print("中文👩‍💻")
```
| 单元 | 约束 |
| --- | --- |"""

        rows = transcript_lines((TranscriptEntry("a1", "assistant", source),), 32)

        self.assertEqual(rows[0], "• 回答开头")
        self.assertIn("  ### 关键约束与设计", rows)
        self.assertIn("  ```python", rows)
        self.assertIn('  print("中文👩‍💻")', rows)
        self.assertIn("  | 单元 | 约束 |", rows)
        self.assertTrue(any(row.startswith("    ") and "触发换行" in row for row in rows))
        self.assertFalse(any(row.startswith("           ") for row in rows if row.strip()))
        self.assertTrue(all(cell_width(row) <= 32 for row in rows))

    def test_markdown_reflows_from_source_at_each_width(self) -> None:
        entry = TranscriptEntry("a1", "assistant", "- 中文👩‍💻 content that wraps by width")

        narrow = transcript_lines((entry,), 18)
        wide = transcript_lines((entry,), 48)

        self.assertGreater(len(narrow), len(wide))
        self.assertEqual(wide[0], "• - 中文👩‍💻 content that wraps by width")
        self.assertTrue(all(cell_width(row) <= 18 for row in narrow))

    def test_frame_is_stable_and_never_exceeds_viewport(self) -> None:
        state = TuiState(
            session_id="session-123",
            provider="bailian",
            workspace="/tmp/project",
            busy=True,
            status="running",
            transcript=(
                TranscriptEntry("u1", "user", "请检查 src/main.py"),
                TranscriptEntry("a1", "assistant", "Working\tcarefully", provisional=True),
            ),
            tools=(ToolEntry(1, "read_file", "completed", "10 chars"),),
            todos=("read", "patch", "test"),
        )
        frame = render_frame(state, TuiView(input_text="continue", cursor=8), 100, 14)

        self.assertEqual(len(frame.lines), 14)
        self.assertTrue(all(cell_width(line) == 100 for line in frame.lines))
        self.assertIn("LCA  RUNNING", frame.lines[0])
        self.assertTrue(any("TOOLS" in line for line in frame.lines))
        self.assertTrue(any("provider bailian" in line for line in frame.lines))
        self.assertTrue(any("workspace /tmp/project" in line for line in frame.lines))
        self.assertTrue(any("read_file" in line for line in frame.lines))
        self.assertTrue(any("10 chars" in line for line in frame.lines))
        self.assertIn("> continue", frame.lines[-2])

    def test_narrow_frame_hides_side_pane_without_overlap(self) -> None:
        state = TuiState(
            transcript=(TranscriptEntry("a1", "assistant", "x" * 200),),
            tools=(ToolEntry(1, "read_file", "completed"),),
        )
        frame = render_frame(state, TuiView(), 30, 8)

        self.assertEqual(len(frame.lines), 8)
        self.assertTrue(all(cell_width(line) == 30 for line in frame.lines))
        self.assertFalse(any("TOOLS" in line for line in frame.lines))

    def test_empty_transcript_renders_responsive_lca_home_logo(self) -> None:
        wide = render_frame(TuiState(), TuiView(), 80, 14)
        narrow = render_frame(TuiState(), TuiView(), 24, 8)

        self.assertTrue(any("/ ___|" in line for line in wide.lines))
        self.assertTrue(any("LOCAL CODING AGENT" in line for line in wide.lines))
        self.assertTrue(wide.accent_rows)
        self.assertTrue(any(line.strip() == "LCA" for line in narrow.lines))
        self.assertTrue(all(cell_width(line) == 24 for line in narrow.lines))

    def test_inline_frame_keeps_only_provisional_transcript_in_mutable_tail(self) -> None:
        state = TuiState(
            provider="bailian",
            transcript=(
                TranscriptEntry("u1", "user", "already committed"),
                TranscriptEntry("a1", "assistant", "streaming now", provisional=True),
            ),
        )

        frame = render_inline_frame(state, TuiView(), 80, 20)

        rendered = "\n".join(frame.lines)
        self.assertNotIn("already committed", rendered)
        self.assertIn("streaming now", rendered)
        self.assertIn("provider bailian", rendered)
        self.assertLess(len(frame.lines), 20)

    def test_home_logo_yields_to_transcript_content(self) -> None:
        state = TuiState(transcript=(TranscriptEntry("a1", "assistant", "ready"),))
        frame = render_frame(state, TuiView(), 80, 14)

        self.assertTrue(any("• ready" in line for line in frame.lines))
        self.assertFalse(any("LOCAL CODING AGENT" in line for line in frame.lines))

    def test_reference_viewport_widths_are_exact(self) -> None:
        state = TuiState(transcript=(TranscriptEntry("a1", "assistant", "Unicode 中文 output"),))

        for width in (40, 80, 120):
            with self.subTest(width=width):
                frame = render_frame(state, TuiView(), width, 12)
                self.assertEqual(len(frame.lines), 12)
                self.assertTrue(all(cell_width(line) == width for line in frame.lines))

    def test_interaction_prompt_owns_focused_input_row(self) -> None:
        frame = render_frame(
            TuiState(),
            TuiView(
                input_text="y",
                cursor=1,
                focus="approval",
                interaction_prompt="Allow shell?",
            ),
            60,
            8,
        )

        self.assertIn("Allow shell?", frame.lines[-3])
        self.assertIn("approve> y", frame.lines[-2])

    def test_history_search_footer_is_bounded_and_sanitizes_match_preview(self) -> None:
        frame = render_frame(
            TuiState(),
            TuiView(
                input_text="qu\x1b\u202eery",
                cursor=8,
                focus="history_search",
                history_search_match="safe\x1b[31m\u202eevil\nnext" + "x" * 200,
                history_search_position=2,
                history_search_count=3,
                history_search_status="match",
            ),
            80,
            8,
        )

        rendered = "\n".join(frame.lines)
        self.assertIn("history> query", rendered)
        self.assertIn("history search 2/3", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertTrue(all(cell_width(line) == 80 for line in frame.lines))

    def test_empty_history_search_does_not_render_any_history_entry(self) -> None:
        frame = render_frame(
            TuiState(),
            TuiView(
                focus="history_search",
                history_search_match="must not render",
                history_search_status="empty",
            ),
            80,
            8,
        )

        rendered = "\n".join(frame.lines)
        self.assertIn("history search: type a query", rendered)
        self.assertNotIn("must not render", rendered)

    def test_long_composer_keeps_text_near_cursor_visible(self) -> None:
        frame = render_frame(
            TuiState(),
            TuiView(input_text="prefix-" + "x" * 40 + "-cursor-tail", cursor=59),
            24,
            8,
        )

        self.assertIn("cursor-tail", frame.lines[-2])
        self.assertLess(frame.cursor_x, 24)

    def test_multiline_composer_uses_physical_rows_and_true_visual_cursor(self) -> None:
        text = "first line\nsecond line"
        frame = render_frame(
            TuiState(transcript=(TranscriptEntry("a1", "assistant", "body"),)),
            TuiView(input_text=text, cursor=text.index("second") + 3),
            24,
            10,
        )

        rendered = "\n".join(frame.lines)
        self.assertIn("> first line", rendered)
        self.assertIn("| second line", rendered)
        self.assertNotIn("first line\\nsecond line", rendered)
        self.assertEqual(frame.lines[frame.cursor_y].strip(), "| second line")
        self.assertEqual(frame.cursor_x, len("| sec"))

    def test_small_viewport_clamps_composer_and_preserves_header_body_footer(self) -> None:
        state = TuiState(transcript=(TranscriptEntry("a1", "assistant", "body remains"),))
        view = TuiView(
            input_text="\n".join(f"line-{index}" for index in range(10)),
            cursor=69,
            palette=tuple(f"command {index}" for index in range(8)),
        )

        frame = render_frame(state, view, 30, 6)

        self.assertEqual(len(frame.lines), 6)
        self.assertIn("LCA", frame.lines[0])
        self.assertEqual(frame.lines[1].strip(), "")
        self.assertIn("command 0", frame.lines[2])
        self.assertIn("Enter send", frame.lines[-1])
        self.assertLess(frame.cursor_y, len(frame.lines) - 1)

    def test_inline_and_full_frames_share_composer_height_and_cursor_projection(self) -> None:
        text = "alpha beta gamma\ndelta"
        view = TuiView(input_text=text, cursor=len(text))

        full = render_frame(TuiState(), view, 16, 12)
        inline = render_inline_frame(TuiState(), view, 16, 12)

        full_prompt = tuple(line.rstrip() for line in full.lines if line.startswith(("> ", "| ")))
        inline_prompt = tuple(line.rstrip() for line in inline.lines if line.startswith(("> ", "| ")))
        self.assertEqual(full_prompt, inline_prompt)
        self.assertEqual(full.cursor_x, inline.cursor_x)
        self.assertEqual(full.lines[full.cursor_y].rstrip(), inline.lines[inline.cursor_y].rstrip())

    def test_long_single_message_and_multiple_turns_expose_history_position(self) -> None:
        state = TuiState(
            transcript=(
                TranscriptEntry("u1", "user", "question"),
                TranscriptEntry("a1", "assistant", "long answer " * 80),
                TranscriptEntry("u2", "user", "follow up"),
                TranscriptEntry("a2", "assistant", "final answer " * 40),
            )
        )
        live_frame = render_frame(state, TuiView(), 48, 10)
        frame = render_frame(
            state,
            TuiView(viewport=TuiViewport(top=0, follow_bottom=False)),
            48,
            10,
        )

        self.assertNotIn("history ", live_frame.lines[-1])
        self.assertIn("history ", frame.lines[-1])
        self.assertRegex(frame.lines[-1], r"history \d+-\d+/\d+")
        self.assertNotIn("PageUp", frame.lines[-1])
        self.assertTrue(any("final answer" in line for line in live_frame.lines))
        self.assertTrue(any("question" in line for line in frame.lines))


if __name__ == "__main__":
    unittest.main()

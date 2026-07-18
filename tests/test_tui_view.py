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


class TuiViewTests(unittest.TestCase):
    def test_unicode_width_and_wrapping_are_cell_bounded(self) -> None:
        self.assertEqual(cell_width("A中"), 3)
        self.assertEqual(clip_cells("A中文", 4), "A中")
        self.assertEqual(wrap_cells("A中文B", 4), ("A中", "文B"))

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

    def test_home_logo_yields_to_transcript_content(self) -> None:
        state = TuiState(transcript=(TranscriptEntry("a1", "assistant", "ready"),))
        frame = render_frame(state, TuiView(), 80, 14)

        self.assertTrue(any("assistant> ready" in line for line in frame.lines))
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

    def test_long_composer_keeps_text_near_cursor_visible(self) -> None:
        frame = render_frame(
            TuiState(),
            TuiView(input_text="prefix-" + "x" * 40 + "-cursor-tail", cursor=59),
            24,
            8,
        )

        self.assertIn("cursor-tail", frame.lines[-2])
        self.assertLess(frame.cursor_x, 24)

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

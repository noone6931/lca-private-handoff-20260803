from __future__ import annotations

import unittest
from unittest.mock import patch

from local_agent.frontends.tui.model import TranscriptEntry
from local_agent.frontends.tui.model import TuiState
from local_agent.frontends.tui.native_renderer import NativeScrollbackRenderer
from local_agent.frontends.tui.native_renderer import _cursor_row_after_reflow
from local_agent.frontends.tui.view import TuiFrame
from local_agent.frontends.tui.view import TuiView


class _Output:
    def fileno(self) -> int:
        return 9


class TuiNativeRendererTests(unittest.TestCase):
    def test_settled_rows_commit_once_without_alt_screen_or_scrollback_clear(self) -> None:
        chunks = []

        def write(_fd, payload):
            chunks.append(payload)
            return len(payload)

        renderer = NativeScrollbackRenderer(_Output())
        state = TuiState(transcript=(TranscriptEntry("u1", "user", "hello native history"),))
        with patch("local_agent.frontends.tui.native_renderer.os.write", side_effect=write):
            renderer.render(TuiState(), TuiView(), 80, 24)
            chunks.clear()
            renderer.render(state, TuiView(), 80, 24)
            renderer.render(state, TuiView(input_text="draft", cursor=5), 80, 24)
            renderer.render(state, TuiView(input_text="draft", cursor=5), 60, 18)

        rendered = b"".join(chunks)
        self.assertEqual(rendered.count("› hello native history".encode()), 1)
        self.assertNotIn(b"\x1b[?1049h", rendered)
        self.assertNotIn(b"\x1b[?1007h", rendered)
        self.assertNotIn(b"\x1b[3J", rendered)

    def test_provisional_text_stays_live_until_settled(self) -> None:
        chunks = []

        def write(_fd, payload):
            chunks.append(payload)
            return len(payload)

        renderer = NativeScrollbackRenderer(_Output())
        provisional = TuiState(
            transcript=(TranscriptEntry("a1", "assistant", "streaming answer", provisional=True),)
        )
        settled = TuiState(transcript=(TranscriptEntry("a1", "assistant", "streaming answer"),))
        with patch("local_agent.frontends.tui.native_renderer.os.write", side_effect=write):
            renderer.render(provisional, TuiView(), 80, 24)
            chunks.clear()
            renderer.render(settled, TuiView(), 80, 24)
            committed = b"".join(chunks)
            chunks.clear()
            renderer.render(settled, TuiView(input_text="x", cursor=1), 80, 24)

        self.assertEqual(committed.count("• streaming answer".encode()), 1)
        self.assertNotIn("• streaming answer".encode(), b"".join(chunks))

    def test_different_authoritative_final_commits_both_messages_once(self) -> None:
        chunks = []

        def write(_fd, payload):
            chunks.append(payload)
            return len(payload)

        renderer = NativeScrollbackRenderer(_Output())
        provisional = TranscriptEntry("a1", "assistant", "draft", provisional=True)
        settled = TuiState(
            transcript=(
                TranscriptEntry("a1", "assistant", "draft"),
                TranscriptEntry("final:r1", "assistant", "safe final", authoritative=True),
            )
        )
        with patch("local_agent.frontends.tui.native_renderer.os.write", side_effect=write):
            renderer.render(TuiState(transcript=(provisional,)), TuiView(), 80, 24)
            chunks.clear()
            renderer.render(settled, TuiView(), 80, 24)
            renderer.render(settled, TuiView(input_text="x", cursor=1), 80, 24)

        rendered = b"".join(chunks)
        self.assertEqual(rendered.count("• draft".encode()), 1)
        self.assertEqual(rendered.count(b"* safe final"), 1)

    def test_markdown_rows_commit_once_without_wide_hanging_indent(self) -> None:
        chunks = []

        def write(_fd, payload):
            chunks.append(payload)
            return len(payload)

        renderer = NativeScrollbackRenderer(_Output())
        state = TuiState(
            transcript=(
                TranscriptEntry(
                    "a1",
                    "assistant",
                    "Intro\n\n### Heading\n- list item\n```python\nprint('ok')\n```\n| A | B |",
                ),
            )
        )
        with patch("local_agent.frontends.tui.native_renderer.os.write", side_effect=write):
            renderer.render(state, TuiView(), 48, 20)
            renderer.render(state, TuiView(input_text="draft", cursor=5), 48, 20)
            renderer.render(state, TuiView(input_text="draft", cursor=5), 40, 20)

        rendered = b"".join(chunks)
        self.assertEqual(rendered.count(b"### Heading"), 1)
        self.assertEqual(rendered.count(b"```python"), 1)
        self.assertEqual(rendered.count(b"| A | B |"), 1)
        self.assertNotIn(b"           ### Heading", rendered)

    def test_search_alone_borrows_alternate_screen_and_restores_normal_tail(self) -> None:
        chunks = []

        def write(_fd, payload):
            chunks.append(payload)
            return len(payload)

        renderer = NativeScrollbackRenderer(_Output())
        state = TuiState(transcript=(TranscriptEntry("u1", "user", "needle"),))
        with patch("local_agent.frontends.tui.native_renderer.os.write", side_effect=write):
            renderer.render(state, TuiView(), 80, 24)
            renderer.render(state, TuiView(focus="search", input_text="needle"), 80, 24)
            self.assertTrue(renderer.overlay_active)
            renderer.render(state, TuiView(), 80, 24)
            self.assertFalse(renderer.overlay_active)

        rendered = b"".join(chunks)
        self.assertEqual(rendered.count(b"\x1b[?1049h"), 1)
        self.assertEqual(rendered.count(b"\x1b[?1049l"), 1)
        self.assertEqual(rendered.count(b"\x1b[?1007h"), 1)
        self.assertEqual(rendered.count(b"\x1b[?1007l"), 1)

    def test_resize_recomputes_physical_rows_for_live_region_cleanup(self) -> None:
        frame = TuiFrame(("x" * 79, "> draft", "footer"), cursor_y=1, cursor_x=7)

        self.assertEqual(_cursor_row_after_reflow(frame, 80, 20), 5)


if __name__ == "__main__":
    unittest.main()

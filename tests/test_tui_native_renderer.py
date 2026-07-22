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


class _ScrollbackTerminal:
    """Minimal ANSI model for the cursor/erase sequences emitted by this renderer."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.screen = [[" "] * width for _ in range(height)]
        self.scrollback: list[str] = []
        self.row = height - 1
        self.col = 0

    def write(self, _fd, payload: bytes) -> int:
        text = payload.decode("utf-8")
        index = 0
        while index < len(text):
            if text.startswith("\x1b[", index):
                end = index + 2
                while end < len(text) and not text[end].isalpha():
                    end += 1
                if end < len(text):
                    self._csi(text[index + 2 : end], text[end])
                    index = end + 1
                    continue
            char = text[index]
            if char == "\r":
                self.col = 0
            elif char == "\n":
                self._linefeed()
            elif ord(char) >= 32:
                if self.col < self.width:
                    self.screen[self.row][self.col] = char
                    self.col += 1
            index += 1
        return len(payload)

    def _csi(self, raw_params: str, command: str) -> None:
        params = raw_params.lstrip("?")
        first = int(params.split(";", 1)[0] or "1") if params.lstrip(";").replace(";", "").isdigit() else 1
        if command == "A":
            self.row = max(self.row - first, 0)
        elif command == "B":
            self.row = min(self.row + first, self.height - 1)
        elif command == "C":
            self.col = min(self.col + first, self.width - 1)
        elif command == "J":
            self.screen[self.row][self.col :] = [" "] * (self.width - self.col)
            for row in range(self.row + 1, self.height):
                self.screen[row] = [" "] * self.width
        elif command == "K" and params in {"2", ""}:
            self.screen[self.row] = [" "] * self.width

    def _linefeed(self) -> None:
        if self.row < self.height - 1:
            self.row += 1
            return
        self.scrollback.append("".join(self.screen[0]).rstrip())
        self.screen = [*self.screen[1:], [" "] * self.width]


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

    def test_multiline_composer_stays_in_mutable_tail_and_shrinks_cleanly(self) -> None:
        chunks = []

        def write(_fd, payload):
            chunks.append(payload)
            return len(payload)

        renderer = NativeScrollbackRenderer(_Output())
        state = TuiState(transcript=(TranscriptEntry("a1", "assistant", "settled"),))
        with patch("local_agent.frontends.tui.native_renderer.os.write", side_effect=write):
            renderer.render(state, TuiView(input_text="first\nsecond", cursor=12), 24, 12)
            renderer.render(state, TuiView(input_text="short", cursor=5), 24, 12)
            renderer.render(state, TuiView(input_text="short", cursor=5), 18, 8)

        rendered = b"".join(chunks)
        self.assertEqual(rendered.count("• settled".encode()), 1)
        self.assertIn(b"> first", rendered)
        self.assertIn(b"| second", rendered)
        self.assertIn(b"\x1b[J", rendered)
        self.assertNotIn(b"\\n", rendered)
        self.assertNotIn(b"\x1b[?1049h", rendered)
        self.assertNotIn(b"\x1b[?1007h", rendered)
        self.assertNotIn(b"\x1b[3J", rendered)

    def test_mutable_tail_is_reserved_before_header_and_reused_for_approval_updates(self) -> None:
        chunks = []

        def write(_fd, payload):
            chunks.append(payload)
            return len(payload)

        renderer = NativeScrollbackRenderer(_Output())
        requested = TuiState(session_id="20260722", busy=True, status="approval: run_tests")
        decided = TuiState(session_id="20260722", busy=True, status="approval allow_once")
        with patch("local_agent.frontends.tui.native_renderer.os.write", side_effect=write):
            renderer.render(requested, TuiView(interaction_prompt="Allow run_tests?"), 80, 12)
            first = b"".join(chunks)
            chunks.clear()
            renderer.render(decided, TuiView(), 80, 12)
            second = b"".join(chunks)

        header = b" LCA  RUNNING  session 20260722"
        self.assertIn(header, first)
        self.assertLess(first.find(b"\r\n"), first.find(header))
        self.assertIn(b"\x1b[", first[: first.find(header)])
        self.assertIn(b"\x1b[J", second)
        self.assertNotIn(b"\r\n\r\n\r\n\r\n", second[: second.find(header)])

    def test_approval_repaints_never_commit_header_to_physical_scrollback(self) -> None:
        terminal = _ScrollbackTerminal(80, 12)
        renderer = NativeScrollbackRenderer(_Output())
        states = (
            TuiState(session_id="20260722", busy=True, status="approval: run_tests"),
            TuiState(session_id="20260722", busy=True, status="approval allow_once"),
            TuiState(session_id="20260722", busy=True, status="running"),
        )
        views = (TuiView(interaction_prompt="Allow run_tests?"), TuiView(), TuiView())

        with patch("local_agent.frontends.tui.native_renderer.os.write", side_effect=terminal.write):
            for state, view in zip(states, views, strict=True):
                renderer.render(state, view, 80, 12)

        self.assertFalse(any("LCA" in line for line in terminal.scrollback))
        visible_headers = [line for row in terminal.screen if "LCA" in (line := "".join(row).rstrip())]
        self.assertEqual(len(visible_headers), 1)
        self.assertIn("running", visible_headers[0])


if __name__ == "__main__":
    unittest.main()

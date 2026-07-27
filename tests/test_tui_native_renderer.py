from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from local_agent.frontends.tui.model import project_agent_event
from local_agent.frontends.tui.model import TranscriptEntry
from local_agent.frontends.tui.model import TuiProjector
from local_agent.frontends.tui.model import TuiState
from local_agent.frontends.tui.native_renderer import NativeScrollbackRenderer
from local_agent.frontends.tui.native_renderer import _cursor_row_after_reflow
from local_agent.frontends.tui.view import render_inline_frame
from local_agent.frontends.tui.view import TuiFrame
from local_agent.frontends.tui.view import TuiView
from local_agent.protocol.events import AgentEvent


FIXTURES = Path(__file__).with_name("fixtures")


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
    def test_raw_ask_session_typing_does_not_repeat_header_or_candidate(self) -> None:
        events = []
        projector = TuiProjector()
        fixture = FIXTURES / "t269_ask_user_session.jsonl"
        for line in fixture.read_text(encoding="utf-8").splitlines():
            payload = json.loads(line)["payload"]
            event = AgentEvent(**payload)
            events.append(event)
            projected = project_agent_event(event)
            if projected is not None:
                projector.apply(projected)
        self.assertEqual(sum(event.type == "AssistantMessage" for event in events), 1)
        self.assertEqual(
            next(event for event in events if event.type == "AssistantMessage").payload["message_id"],
            "418177913bb7426c8a19e607a1274664",
        )
        views = tuple(
            TuiView(
                focus="ask",
                interaction_prompt="请告诉我你想做什么项目？",
                input_text=text,
                cursor=len(text),
            )
            for text in ("", "项", "项目", "项目网站")
        )
        frame = render_inline_frame(projector.state, views[0], 80, 12)
        header = frame.lines[0].rstrip().encode("utf-8")
        candidate = "你好！我注意到你的请求".encode("utf-8")
        chunks: list[bytes] = []
        updates: list[bytes] = []
        renderer = NativeScrollbackRenderer(_Output())

        def write(_fd, payload):
            chunks.append(payload)
            return len(payload)

        with patch("local_agent.frontends.tui.native_renderer.os.write", side_effect=write):
            for index, view in enumerate(views):
                start = len(chunks)
                renderer.render(projector.state, view, 80, 12)
                if index:
                    updates.append(b"".join(chunks[start:]))

        rendered = b"".join(chunks)
        self.assertEqual(rendered.count(header), 1)
        self.assertEqual(rendered.count(candidate), 1)
        self.assertTrue(all(header not in update for update in updates))
        self.assertTrue(all(candidate not in update for update in updates))
        self.assertTrue(all(b"\r\n" not in update for update in updates))

    def test_cursor_only_update_moves_cursor_without_repainting_rows(self) -> None:
        chunks = []
        renderer = NativeScrollbackRenderer(_Output())
        state = TuiState(session_id="cursor", busy=True, status="running")
        with patch(
            "local_agent.frontends.tui.native_renderer.os.write",
            side_effect=lambda _fd, payload: chunks.append(payload) or len(payload),
        ):
            renderer.render(state, TuiView(input_text="abcdef", cursor=6), 40, 10)
            chunks.clear()
            renderer.render(state, TuiView(input_text="abcdef", cursor=2), 40, 10)

        update = b"".join(chunks)
        self.assertNotIn(b"\x1b[2K", update)
        self.assertNotIn(b"abcdef", update)
        self.assertNotIn(b"\r\n", update)
        expected = render_inline_frame(
            state,
            TuiView(input_text="abcdef", cursor=2),
            40,
            10,
        ).cursor_x
        self.assertIn(f"\x1b[{expected}C".encode("ascii"), update)

    def test_cjk_multiline_same_shape_repaints_only_changed_rows(self) -> None:
        chunks = []
        renderer = NativeScrollbackRenderer(_Output())
        state = TuiState(
            session_id="cjk",
            busy=True,
            status="running",
            transcript=(
                TranscriptEntry("a1", "assistant", "候选内容保持唯一", provisional=True),
            ),
        )
        first = TuiView(input_text="第一行\n第二行甲", cursor=9)
        second = TuiView(input_text="第一行\n第二行乙", cursor=9)
        with patch(
            "local_agent.frontends.tui.native_renderer.os.write",
            side_effect=lambda _fd, payload: chunks.append(payload) or len(payload),
        ):
            renderer.render(state, first, 32, 10)
            chunks.clear()
            renderer.render(state, second, 32, 10)

        update = b"".join(chunks)
        self.assertIn("第二行乙".encode(), update)
        self.assertNotIn("候选内容保持唯一".encode(), update)
        self.assertNotIn(b"LCA", update)
        self.assertNotIn(b"\r\n", update)

    def test_interaction_transition_repaints_once_then_typing_is_differential(self) -> None:
        chunks = []
        renderer = NativeScrollbackRenderer(_Output())
        state = TuiState(
            session_id="ask",
            busy=True,
            status="running",
            transcript=(TranscriptEntry("a1", "assistant", "candidate-once", provisional=True),),
        )
        arrived = TuiView(focus="ask", interaction_prompt="Question?")
        typing = TuiView(
            focus="ask",
            interaction_prompt="Question?",
            input_text="answer",
            cursor=6,
        )
        with patch(
            "local_agent.frontends.tui.native_renderer.os.write",
            side_effect=lambda _fd, payload: chunks.append(payload) or len(payload),
        ):
            renderer.render(state, TuiView(), 48, 10)
            chunks.clear()
            renderer.render(state, arrived, 48, 10)
            transition = b"".join(chunks)
            chunks.clear()
            renderer.render(state, typing, 48, 10)
            update = b"".join(chunks)

        self.assertIn(b"\x1b[J", transition)
        self.assertEqual(transition.count(b"candidate-once"), 1)
        self.assertNotIn(b"candidate-once", update)
        self.assertNotIn(b"LCA", update)
        self.assertNotIn(b"\r\n", update)

    def test_differential_typing_preserves_physical_scrollback_and_live_screen(self) -> None:
        terminal = _ScrollbackTerminal(48, 10)
        renderer = NativeScrollbackRenderer(_Output())
        state = TuiState(
            session_id="physical",
            busy=True,
            status="running",
            transcript=(TranscriptEntry("a1", "assistant", "candidate-live", provisional=True),),
        )
        with patch(
            "local_agent.frontends.tui.native_renderer.os.write",
            side_effect=terminal.write,
        ):
            for text in ("", "a", "answer", "answer updated"):
                renderer.render(
                    state,
                    TuiView(
                        focus="ask",
                        interaction_prompt="Question?",
                        input_text=text,
                        cursor=len(text),
                    ),
                    48,
                    10,
                )

        self.assertFalse(any("LCA" in line or "candidate-live" in line for line in terminal.scrollback))
        screen = "\n".join("".join(row).rstrip() for row in terminal.screen)
        self.assertEqual(screen.count("LCA"), 1)
        self.assertEqual(screen.count("candidate-live"), 1)
        self.assertIn("answer updated", screen)

    def test_candidate_replacement_and_cancel_remain_structural_repaints(self) -> None:
        chunks = []
        renderer = NativeScrollbackRenderer(_Output())
        first = TuiState(
            session_id="replace",
            busy=True,
            status="running",
            transcript=(TranscriptEntry("a1", "assistant", "first candidate", provisional=True),),
        )
        replacement = TuiState(
            session_id="replace",
            busy=True,
            status="running",
            transcript=(TranscriptEntry("a2", "assistant", "replacement candidate", provisional=True),),
        )
        cancelled = TuiState(session_id="replace", busy=True, status="running")
        with patch(
            "local_agent.frontends.tui.native_renderer.os.write",
            side_effect=lambda _fd, payload: chunks.append(payload) or len(payload),
        ):
            renderer.render(first, TuiView(), 48, 10)
            chunks.clear()
            renderer.render(replacement, TuiView(), 48, 10)
            replaced = b"".join(chunks)
            chunks.clear()
            renderer.render(cancelled, TuiView(), 48, 10)
            cancelled_bytes = b"".join(chunks)

        self.assertIn(b"\x1b[J", replaced)
        self.assertEqual(replaced.count(b"replacement candidate"), 1)
        self.assertIn(b"\x1b[J", cancelled_bytes)
        self.assertNotIn(b"replacement candidate", cancelled_bytes)

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

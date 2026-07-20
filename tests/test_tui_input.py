from __future__ import annotations

import unittest

from local_agent.frontends.tui.input import BRACKETED_PASTE_END
from local_agent.frontends.tui.input import BRACKETED_PASTE_START
from local_agent.frontends.tui.input import BracketedPasteDecoder


class TuiInputTests(unittest.TestCase):
    def test_fragmented_multiline_utf8_paste_is_one_atomic_event(self) -> None:
        decoder = BracketedPasteDecoder()

        self.assertEqual(decoder.feed("\x1b[20"), ())
        self.assertEqual(decoder.feed("0~第一行\r"), ())
        self.assertEqual(decoder.feed("\nsecond\n"), ())
        self.assertEqual(decoder.feed("\x1b[20"), ())
        events = decoder.feed("1~")

        self.assertEqual([(event.kind, event.value) for event in events], [("paste", "第一行\nsecond\n")])
        self.assertFalse(decoder.paste_active)

    def test_enter_inside_paste_is_never_emitted_as_key(self) -> None:
        decoder = BracketedPasteDecoder()

        events = decoder.feed(BRACKETED_PASTE_START + "one\ntwo\rthree" + BRACKETED_PASTE_END)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "paste")
        self.assertEqual(events[0].value, "one\ntwo\nthree")

    def test_oversize_and_missing_end_fail_closed(self) -> None:
        now = [0.0]
        decoder = BracketedPasteDecoder(byte_limit=8, incomplete_seconds=1.0, clock=lambda: now[0])

        events = decoder.feed(BRACKETED_PASTE_START + "012345678")
        self.assertEqual([event.kind for event in events], ["notice"])
        self.assertTrue(decoder.paste_active)
        self.assertEqual(decoder.feed("ignored\n"), ())
        self.assertEqual(decoder.feed(BRACKETED_PASTE_END), ())
        self.assertFalse(decoder.paste_active)

        self.assertEqual(decoder.feed(BRACKETED_PASTE_START + "short"), ())
        now[0] = 2.0
        expired = decoder.expire()
        self.assertEqual([event.kind for event in expired], ["notice"])
        self.assertNotIn("short", repr(expired))

    def test_regular_escape_is_flushed_without_becoming_paste(self) -> None:
        now = [0.0]
        decoder = BracketedPasteDecoder(clock=lambda: now[0])

        self.assertEqual(decoder.feed("\x1b"), ())
        now[0] = 1.0

        self.assertEqual([(event.kind, event.value) for event in decoder.flush_normal()], [("key", "ESC")])

    def test_fragmented_terminal_key_sequences_decode_without_leaking_bytes(self) -> None:
        decoder = BracketedPasteDecoder()

        self.assertEqual(decoder.feed("\x1b["), ())
        events = decoder.feed("A\x1b[6~\x1bO")
        self.assertEqual(
            [(event.kind, event.value) for event in events],
            [("key", "UP"), ("key", "PAGE_DOWN")],
        )
        self.assertEqual(
            [(event.kind, event.value) for event in decoder.feed("D")],
            [("key", "LEFT")],
        )

    def test_ctrl_r_has_a_typed_history_search_mapping(self) -> None:
        decoder = BracketedPasteDecoder()

        self.assertEqual(
            [(event.kind, event.value) for event in decoder.feed("\x12")],
            [("key", "CTRL_R")],
        )

    def test_incomplete_timeout_tracks_idle_time_not_total_paste_duration(self) -> None:
        now = [0.0]
        decoder = BracketedPasteDecoder(incomplete_seconds=1.0, clock=lambda: now[0])
        decoder.feed(BRACKETED_PASTE_START + "first")
        now[0] = 0.8
        decoder.feed(" second")
        now[0] = 1.2
        self.assertEqual(decoder.expire(), ())
        events = decoder.feed(BRACKETED_PASTE_END)
        self.assertEqual(events[0].value, "first second")


if __name__ == "__main__":
    unittest.main()

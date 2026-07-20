from __future__ import annotations

import unittest

from local_agent.frontends.tui.pending_prompt import PendingPromptQueue


class PendingPromptQueueTests(unittest.TestCase):
    def test_single_slot_admission_take_restore_and_clear(self) -> None:
        queue = PendingPromptQueue(byte_limit=16)

        self.assertEqual(queue.admit(""), "empty")
        self.assertEqual(queue.admit("  "), "empty")
        self.assertEqual(queue.admit("0123456789abcdefg"), "too_large")
        self.assertEqual(queue.admit("next\nturn"), "admitted")
        self.assertEqual(queue.admit("replacement"), "full")
        self.assertEqual(queue.pending.text, "next\nturn")  # type: ignore[union-attr]
        self.assertEqual(queue.pending.byte_count, 9)  # type: ignore[union-attr]

        pending = queue.take()
        self.assertIsNotNone(pending)
        self.assertIsNone(queue.pending)
        self.assertTrue(queue.restore(pending))  # type: ignore[arg-type]
        self.assertFalse(queue.restore(pending))  # type: ignore[arg-type]
        queue.clear()
        self.assertIsNone(queue.pending)

    def test_utf8_limit_is_measured_in_bytes(self) -> None:
        queue = PendingPromptQueue(byte_limit=4)

        self.assertEqual(queue.admit("好"), "admitted")
        queue.clear()
        self.assertEqual(queue.admit("好好"), "too_large")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from local_agent.cancellation import RunCancellation
from local_agent.cancellation import RunCancelled
from local_agent.tools.base import ToolContext
from local_agent.tools.base import Tool
from local_agent.tools.base import ToolRegistry
from local_agent.tools.base import ToolResult
from local_agent.tools.shell import run_shell


class CancellationTests(unittest.TestCase):
    def test_controller_accepts_requests_only_during_active_run(self) -> None:
        cancellation = RunCancellation()

        self.assertFalse(cancellation.request())
        cancellation.begin()
        self.assertTrue(cancellation.request())
        with self.assertRaises(RunCancelled):
            cancellation.raise_if_requested()
        cancellation.finish()

        self.assertFalse(cancellation.requested)
        self.assertFalse(cancellation.request())

    def test_pending_cancel_is_observed_when_queued_run_begins(self) -> None:
        cancellation = RunCancellation()

        self.assertTrue(cancellation.request(include_next=True))
        cancellation.begin()

        with self.assertRaises(RunCancelled):
            cancellation.raise_if_requested()
        cancellation.finish()
        self.assertFalse(cancellation.requested)

    def test_shell_process_observes_cancel_token(self) -> None:
        cancel = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(Path(tmp), "yolo", cancel_event=cancel)
            late_marker = Path(tmp) / "late.txt"
            timer = threading.Timer(0.05, cancel.set)
            timer.start()
            started = time.monotonic()
            try:
                with self.assertRaises(RunCancelled):
                    run_shell({"command": "sleep 0.3; printf late > late.txt", "timeout": 5}, context)
            finally:
                timer.cancel()
            elapsed = time.monotonic() - started
            time.sleep(0.35)
            self.assertFalse(late_marker.exists())

        self.assertLess(elapsed, 1.0)

    def test_completed_tool_result_is_not_hidden_by_late_cancel(self) -> None:
        cancel = threading.Event()

        def handler(arguments, context):
            del arguments, context
            cancel.set()
            return ToolResult("write completed")

        registry = ToolRegistry([Tool("sample_write", "sample", {"type": "object"}, "write", handler)])
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(Path(tmp), "yolo", cancel_event=cancel)
            result = registry.execute("sample_write", {}, context)

        self.assertEqual(result.content, "write completed")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from local_agent.tools.base import ToolContext
from local_agent.tools.process_output import BoundedByteCapture
from local_agent.tools.process_output import CapturedText
from local_agent.tools.process_output import ProcessOutputCapture
from local_agent.tools.process_output import StreamCaptureSummary
from local_agent.tools.process_output import project_process_tool_output
from local_agent.tools.shell import run_shell


class ProcessOutputTests(unittest.TestCase):
    def test_small_utf8_crlf_and_split_multibyte_chunks_remain_exact(self) -> None:
        capture = BoundedByteCapture(limit_bytes=64, head_bytes=32)
        encoded = "start-😀-结束\r\n".encode()
        for chunk in (encoded[:8], encoded[8:10], encoded[10:13], encoded[13:]):
            capture.push(chunk)

        result = capture.finish()

        self.assertEqual(result.text, "start-😀-结束\r\n")
        self.assertEqual(result.summary.observed_bytes, len(encoded))
        self.assertEqual(result.summary.captured_bytes, len(encoded))
        self.assertEqual(result.summary.dropped_bytes, 0)
        self.assertFalse(result.summary.truncated)

    def test_head_tail_capture_reports_exact_bytes_without_newlines(self) -> None:
        capture = BoundedByteCapture(limit_bytes=12, head_bytes=6)
        capture.push(b"abc")
        capture.push(b"def123456XYZ")

        result = capture.finish()

        self.assertTrue(result.text.startswith("abcdef"))
        self.assertTrue(result.text.endswith("456XYZ"))
        self.assertIn("observed_bytes=15", result.text)
        self.assertIn("captured_bytes=12", result.text)
        self.assertIn("dropped_bytes=3", result.text)
        self.assertEqual(result.summary, StreamCaptureSummary(15, 12, 3, True))

    def test_exact_capture_cap_is_complete_without_a_truncation_marker(self) -> None:
        capture = BoundedByteCapture(limit_bytes=6, head_bytes=3)
        capture.push(b"abcdef")

        result = capture.finish()

        self.assertEqual(result.text, "abcdef")
        self.assertEqual(result.summary, StreamCaptureSummary(6, 6, 0, False))
        self.assertNotIn("truncated", result.text)

    def test_invalid_utf8_and_cap_boundary_have_deterministic_replacement(self) -> None:
        complete = BoundedByteCapture(limit_bytes=16, head_bytes=8)
        complete.push(b"a\xffb")
        truncated = BoundedByteCapture(limit_bytes=6, head_bytes=3)
        truncated.push("A😀BCDEF".encode())

        complete_result = complete.finish()
        truncated_result = truncated.finish()

        self.assertEqual(complete_result.text, "a�b")
        self.assertIn("�", truncated_result.text)
        self.assertTrue(truncated_result.summary.truncated)
        self.assertEqual(truncated_result.summary.captured_bytes, 6)

    def test_tool_projection_retains_head_tail_metadata_and_terminal_line(self) -> None:
        stdout = CapturedText("A" * 100, StreamCaptureSummary(100, 100, 0, False))
        stderr = CapturedText("B" * 100, StreamCaptureSummary(100, 100, 0, False))
        capture = ProcessOutputCapture(stdout, stderr)
        completed = type(
            "Completed",
            (),
            {"stdout": stdout.text, "stderr": stderr.text, "output_capture": capture},
        )()

        projection, observed_capture = project_process_tool_output(
            completed,
            terminal_line="[exit_code] 7",
            display_limit_chars=140,
        )
        metadata = projection.metadata(observed_capture)

        self.assertLessEqual(len(projection.content), 140)
        self.assertIn("tool output display truncated", projection.content)
        self.assertTrue(projection.content.endswith("[exit_code] 7"))
        self.assertTrue(projection.output_truncated)
        self.assertEqual(metadata["output_capture"]["total"]["observed_bytes"], 200)
        self.assertGreater(metadata["output_capture"]["display"]["dropped_chars"], 0)
        self.assertTrue(metadata["output_truncated"])

    def test_shell_tool_result_is_bounded_and_never_drops_exit_or_capture_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            probe = workspace / "flood.py"
            probe.write_text(
                "import sys\n"
                "sys.stdout.write('A' * 40000)\n"
                "sys.stderr.write('B' * 40000)\n",
                encoding="utf-8",
            )
            result = run_shell(
                {"command": f"{shlex.quote(sys.executable)} {shlex.quote(str(probe))}"},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertFalse(result.is_error)
        self.assertLessEqual(len(result.content), 30_000)
        self.assertIn("tool output display truncated", result.content)
        self.assertTrue(result.content.endswith("[exit_code] 0"))
        self.assertTrue(result.metadata["output_truncated"])
        self.assertEqual(result.metadata["output_capture"]["stdout"]["observed_bytes"], 40_000)
        self.assertEqual(result.metadata["output_capture"]["stderr"]["observed_bytes"], 40_000)
        self.assertNotIn("A" * 1000, str(result.metadata))
        self.assertFalse(result.metadata["sandboxed"])

    @unittest.skipUnless(os.name == "posix", "timeout shell command uses POSIX quoting")
    def test_shell_timeout_retains_bounded_output_and_terminal_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            probe = workspace / "timeout.py"
            probe.write_text(
                "import sys, time\n"
                "sys.stdout.write('before-timeout')\n"
                "sys.stdout.flush()\n"
                "time.sleep(5)\n",
                encoding="utf-8",
            )
            result = run_shell(
                {"command": f"{shlex.quote(sys.executable)} {shlex.quote(str(probe))}", "timeout": 1},
                ToolContext(workspace=workspace, approval_mode="yolo"),
            )

        self.assertTrue(result.is_error)
        self.assertIn("before-timeout", result.content)
        self.assertTrue(result.content.endswith("[timeout] Command timed out after 1 seconds."))
        self.assertIn("output_capture", result.metadata)
        self.assertFalse(result.metadata["sandboxed"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import codecs
import subprocess
from dataclasses import dataclass
from typing import Any

from .base import ToolResult


PROCESS_STREAM_CAPTURE_LIMIT_BYTES = 256 * 1024
PROCESS_STREAM_HEAD_BYTES = PROCESS_STREAM_CAPTURE_LIMIT_BYTES // 2
PROCESS_TOTAL_CAPTURE_LIMIT_BYTES = PROCESS_STREAM_CAPTURE_LIMIT_BYTES * 2
PROCESS_PIPE_READ_CHUNK_BYTES = 64 * 1024
PROCESS_TOOL_DISPLAY_LIMIT_CHARS = 30_000


@dataclass(frozen=True)
class StreamCaptureSummary:
    observed_bytes: int
    captured_bytes: int
    dropped_bytes: int
    truncated: bool

    def to_metadata(self) -> dict[str, int | bool]:
        return {
            "observed_bytes": self.observed_bytes,
            "captured_bytes": self.captured_bytes,
            "dropped_bytes": self.dropped_bytes,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class CapturedText:
    text: str
    summary: StreamCaptureSummary


@dataclass(frozen=True)
class ProcessOutputCapture:
    stdout: CapturedText
    stderr: CapturedText

    @property
    def truncated(self) -> bool:
        return self.stdout.summary.truncated or self.stderr.summary.truncated

    def to_metadata(self) -> dict[str, Any]:
        stdout = self.stdout.summary
        stderr = self.stderr.summary
        total = StreamCaptureSummary(
            observed_bytes=stdout.observed_bytes + stderr.observed_bytes,
            captured_bytes=stdout.captured_bytes + stderr.captured_bytes,
            dropped_bytes=stdout.dropped_bytes + stderr.dropped_bytes,
            truncated=stdout.truncated or stderr.truncated,
        )
        return {
            "stdout": stdout.to_metadata(),
            "stderr": stderr.to_metadata(),
            "total": total.to_metadata(),
        }


@dataclass(frozen=True)
class ToolOutputProjection:
    content: str
    output_truncated: bool
    display_observed_chars: int
    display_captured_chars: int
    display_dropped_chars: int

    def metadata(self, capture: ProcessOutputCapture) -> dict[str, Any]:
        output_capture = capture.to_metadata()
        output_capture["display"] = {
            "observed_chars": self.display_observed_chars,
            "captured_chars": self.display_captured_chars,
            "dropped_chars": self.display_dropped_chars,
            "truncated": self.display_dropped_chars > 0,
        }
        return {
            "output_capture": output_capture,
            "output_truncated": self.output_truncated,
        }


class BoundedByteCapture:
    def __init__(
        self,
        *,
        limit_bytes: int = PROCESS_STREAM_CAPTURE_LIMIT_BYTES,
        head_bytes: int = PROCESS_STREAM_HEAD_BYTES,
    ) -> None:
        if limit_bytes < 1 or head_bytes < 0 or head_bytes > limit_bytes:
            raise ValueError("Invalid bounded process capture limits.")
        self._limit = limit_bytes
        self._head_limit = head_bytes
        self._tail_limit = limit_bytes - head_bytes
        self._head = bytearray()
        self._tail = bytearray()
        self._observed = 0

    def push(self, data: bytes) -> None:
        if not data:
            return
        self._observed += len(data)
        view = memoryview(data)
        head_remaining = self._head_limit - len(self._head)
        if head_remaining > 0:
            take = min(head_remaining, len(view))
            self._head.extend(view[:take])
            view = view[take:]
        if not view or self._tail_limit == 0:
            return
        if len(view) >= self._tail_limit:
            self._tail.clear()
            self._tail.extend(view[-self._tail_limit :])
            return
        overflow = max(0, len(self._tail) + len(view) - self._tail_limit)
        if overflow:
            del self._tail[:overflow]
        self._tail.extend(view)

    def finish(self) -> CapturedText:
        captured = len(self._head) + len(self._tail)
        dropped = max(0, self._observed - captured)
        summary = StreamCaptureSummary(
            observed_bytes=self._observed,
            captured_bytes=captured,
            dropped_bytes=dropped,
            truncated=dropped > 0,
        )
        if dropped:
            head = _decode_utf8(self._head)
            tail = _decode_utf8(self._tail)
            marker = (
                "\n...[process output truncated: "
                f"observed_bytes={self._observed}, captured_bytes={captured}, "
                f"dropped_bytes={dropped}; showing bounded head+tail]...\n"
            )
            text = head + marker + tail
        else:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            text = decoder.decode(self._head, final=not self._tail)
            if self._tail:
                text += decoder.decode(self._tail, final=True)
        self._head.clear()
        self._tail.clear()
        return CapturedText(text=text, summary=summary)


class CapturedCompletedProcess(subprocess.CompletedProcess[str]):
    def __init__(self, args: str | list[str], returncode: int, capture: ProcessOutputCapture) -> None:
        super().__init__(args, returncode, capture.stdout.text, capture.stderr.text)
        self.output_capture = capture


class CapturedTimeoutExpired(subprocess.TimeoutExpired):
    def __init__(self, command: str | list[str], timeout: float, capture: ProcessOutputCapture) -> None:
        super().__init__(command, timeout, output=capture.stdout.text, stderr=capture.stderr.text)
        self.output_capture = capture


def process_output_capture(value: object) -> ProcessOutputCapture:
    capture = getattr(value, "output_capture", None)
    if isinstance(capture, ProcessOutputCapture):
        return capture
    stdout = getattr(value, "stdout", None) or ""
    stderr = getattr(value, "stderr", None) or ""
    return ProcessOutputCapture(_capture_existing_text(str(stdout)), _capture_existing_text(str(stderr)))


def project_process_tool_output(
    value: object,
    *,
    terminal_line: str,
    label_stdout: bool = False,
    display_limit_chars: int = PROCESS_TOOL_DISPLAY_LIMIT_CHARS,
) -> tuple[ToolOutputProjection, ProcessOutputCapture]:
    capture = process_output_capture(value)
    stdout = capture.stdout.text
    stderr = capture.stderr.text
    body = ""
    if stdout:
        body = ("[stdout]\n" if label_stdout else "") + stdout
    if stderr:
        body += "\n[stderr]\n" + stderr
    footer = ("\n" if body else "") + terminal_line
    content, captured_chars, dropped_chars = _bounded_display(body, footer, display_limit_chars)
    projection = ToolOutputProjection(
        content=content,
        output_truncated=capture.truncated or dropped_chars > 0,
        display_observed_chars=len(body) + len(footer),
        display_captured_chars=captured_chars,
        display_dropped_chars=dropped_chars,
    )
    return projection, capture


def process_tool_result(
    value: object,
    *,
    terminal_line: str,
    is_error: bool,
    metadata: dict[str, Any] | None = None,
    label_stdout: bool = False,
) -> ToolResult:
    projection, capture = project_process_tool_output(
        value,
        terminal_line=terminal_line,
        label_stdout=label_stdout,
    )
    result_metadata = {**(metadata or {}), **projection.metadata(capture)}
    result_metadata.setdefault("sandboxed", False)
    return ToolResult(
        projection.content,
        is_error=is_error,
        metadata=result_metadata,
    )


def _bounded_display(body: str, footer: str, limit: int) -> tuple[str, int, int]:
    if limit < len(footer) + 1:
        raise ValueError("Process tool display limit cannot preserve the terminal status line.")
    full = body + footer
    if len(full) <= limit:
        return full, len(full), 0
    marker = ""
    retained_body = 0
    for _ in range(4):
        available = max(0, limit - len(footer) - len(marker))
        retained_body = min(len(body), available)
        dropped = len(body) - retained_body
        marker = f"\n...[tool output display truncated: dropped_chars={dropped}; showing bounded head+tail]...\n"
    available = max(0, limit - len(footer) - len(marker))
    retained_body = min(len(body), available)
    head = retained_body // 2
    tail = retained_body - head
    kept = body[:head] + (body[-tail:] if tail else "")
    dropped = len(body) - retained_body
    content = kept[:head] + marker + kept[head:] + footer
    return content, retained_body + len(footer), dropped


def _capture_existing_text(text: str) -> CapturedText:
    observed = len(text.encode("utf-8", errors="replace"))
    summary = StreamCaptureSummary(observed, observed, 0, False)
    return CapturedText(text=text, summary=summary)


def _decode_utf8(data: bytes | bytearray) -> str:
    return bytes(data).decode("utf-8", errors="replace")

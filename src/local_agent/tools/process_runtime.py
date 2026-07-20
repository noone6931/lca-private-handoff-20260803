from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from ..protocol.cancellation import CancellationSignal
from ..protocol.cancellation import RunCancelled
from .process_output import BoundedByteCapture
from .process_output import CapturedCompletedProcess
from .process_output import CapturedTimeoutExpired
from .process_output import PROCESS_PIPE_READ_CHUNK_BYTES
from .process_output import ProcessOutputCapture


_PROCESS_POLL_SECONDS = 0.01
_PIPE_CHUNKS_PER_SWEEP = 4
_TERMINATE_GRACE_SECONDS = 0.5


@dataclass
class _PipeCapture:
    stream: BinaryIO
    capture: BoundedByteCapture
    eof: bool = False

    def drain(self) -> int:
        if self.eof:
            return 0
        drained_bytes = 0
        for _ in range(_PIPE_CHUNKS_PER_SWEEP):
            try:
                chunk = os.read(self.stream.fileno(), PROCESS_PIPE_READ_CHUNK_BYTES)
            except BlockingIOError:
                return drained_bytes
            except InterruptedError:
                continue
            if not chunk:
                self.eof = True
                self.stream.close()
                return drained_bytes
            self.capture.push(chunk)
            drained_bytes += len(chunk)
        return drained_bytes

    def close(self) -> None:
        if not self.eof:
            self.eof = True
            self.stream.close()


class _ProcessPipes:
    def __init__(self, stdout: BinaryIO, stderr: BinaryIO) -> None:
        os.set_blocking(stdout.fileno(), False)
        os.set_blocking(stderr.fileno(), False)
        self._stdout = _PipeCapture(stdout, BoundedByteCapture())
        self._stderr = _PipeCapture(stderr, BoundedByteCapture())

    @property
    def eof(self) -> bool:
        return self._stdout.eof and self._stderr.eof

    def drain(self) -> int:
        stdout_bytes = self._stdout.drain()
        stderr_bytes = self._stderr.drain()
        return stdout_bytes + stderr_bytes

    def close(self) -> None:
        self._stdout.close()
        self._stderr.close()

    def finish(self) -> ProcessOutputCapture:
        return ProcessOutputCapture(self._stdout.capture.finish(), self._stderr.capture.finish())


def run_process(
    command: str | list[str],
    *,
    cwd: Path,
    shell: bool,
    timeout: float,
    cancel_event: CancellationSignal | None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with bounded binary output and one process-group lifecycle."""

    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        shell=shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    assert process.stdout is not None and process.stderr is not None
    try:
        pipes = _ProcessPipes(process.stdout, process.stderr)
    except BaseException:
        _abort_process_startup(process)
        raise
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _terminate_process(process, pipes)
                raise RunCancelled("Run cancelled while a local process was active.")
            progress = pipes.drain()
            if process.poll() is not None and pipes.eof:
                capture = pipes.finish()
                return CapturedCompletedProcess(command, process.returncode, capture)
            if time.monotonic() >= deadline:
                capture = _terminate_process(process, pipes)
                raise CapturedTimeoutExpired(command, timeout, capture)
            if progress == 0:
                time.sleep(_PROCESS_POLL_SECONDS)
    except (RunCancelled, subprocess.TimeoutExpired):
        raise
    except BaseException:
        _terminate_process(process, pipes)
        raise


def _terminate_process(process: subprocess.Popen[bytes], pipes: _ProcessPipes) -> ProcessOutputCapture:
    if os.name == "posix":
        _signal_process(process, signal.SIGTERM)
    elif process.poll() is None:
        process.terminate()
    complete = _wait_for_process_and_pipes(process, pipes, _TERMINATE_GRACE_SECONDS)
    group_alive = os.name == "posix" and _process_group_exists(process.pid)
    if group_alive:
        _signal_process(process, signal.SIGKILL)
    elif os.name != "posix" and process.poll() is None:
        process.kill()
    if not complete or group_alive:
        _wait_for_process_and_pipes(process, pipes, _TERMINATE_GRACE_SECONDS)
    pipes.drain()
    pipes.close()
    if process.poll() is None:
        try:
            process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            pass
    return pipes.finish()


def _abort_process_startup(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        _signal_process(process, signal.SIGKILL)
    elif process.poll() is None:
        process.kill()
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    try:
        process.wait(timeout=0.1)
    except subprocess.TimeoutExpired:
        pass


def _wait_for_process_and_pipes(
    process: subprocess.Popen[bytes],
    pipes: _ProcessPipes,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        progress = pipes.drain()
        if process.poll() is not None and pipes.eof:
            return True
        if time.monotonic() >= deadline:
            return False
        if progress == 0:
            time.sleep(_PROCESS_POLL_SECONDS)


def _signal_process(process: subprocess.Popen[bytes], signum: signal.Signals) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            return
    elif signum == signal.SIGTERM:
        process.terminate()
    else:
        process.kill()


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

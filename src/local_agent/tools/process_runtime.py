from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

from ..protocol.cancellation import CancellationSignal
from ..protocol.cancellation import RunCancelled


def run_process(
    command: str | list[str],
    *,
    cwd: Path,
    shell: bool,
    timeout: float,
    cancel_event: CancellationSignal | None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess in one bounded process-group lifecycle."""

    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        shell=shell,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    deadline = time.monotonic() + timeout
    last_stdout: str | None = None
    last_stderr: str | None = None
    while True:
        if cancel_event is not None and cancel_event.is_set():
            _terminate_process(process)
            raise RunCancelled("Run cancelled while a local process was active.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stdout, stderr = _terminate_process(process)
            raise subprocess.TimeoutExpired(
                command,
                timeout,
                output=stdout if stdout is not None else last_stdout,
                stderr=stderr if stderr is not None else last_stderr,
            )
        try:
            stdout, stderr = process.communicate(timeout=min(remaining, 0.05))
        except subprocess.TimeoutExpired as exc:
            if isinstance(exc.stdout, str):
                last_stdout = exc.stdout
            if isinstance(exc.stderr, str):
                last_stderr = exc.stderr
            continue
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _terminate_process(process: subprocess.Popen[str]) -> tuple[str | None, str | None]:
    if os.name == "posix":
        _signal_process(process, signal.SIGTERM)
    elif process.poll() is None:
        process.terminate()
    try:
        stdout, stderr = process.communicate(timeout=0.5)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else None
        stderr = exc.stderr if isinstance(exc.stderr, str) else None
        if os.name == "posix":
            _signal_process(process, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
        try:
            final_stdout, final_stderr = process.communicate(timeout=0.5)
        except subprocess.TimeoutExpired as final_exc:
            final_stdout = final_exc.stdout if isinstance(final_exc.stdout, str) else stdout
            final_stderr = final_exc.stderr if isinstance(final_exc.stderr, str) else stderr
            _close_process_pipes(process)
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
        return final_stdout, final_stderr
    if os.name == "posix" and _process_group_exists(process.pid):
        _signal_process(process, signal.SIGKILL)
    return stdout, stderr


def _close_process_pipes(process: subprocess.Popen[str]) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _signal_process(process: subprocess.Popen[str], signum: signal.Signals) -> None:
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

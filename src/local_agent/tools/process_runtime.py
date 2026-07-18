from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from ..protocol.cancellation import CancellationSignal
from ..protocol.cancellation import RunCancelled


def run_process(
    command: str | list[str],
    *,
    cwd: Path,
    shell: bool,
    timeout: int,
    cancel_event: CancellationSignal | None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with the legacy path unless cooperative cancellation is active."""

    if cancel_event is None:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            shell=shell,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
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
        if cancel_event.is_set():
            _terminate_process(process)
            raise RunCancelled("Run cancelled while a local process was active.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process(process)
            raise subprocess.TimeoutExpired(
                command,
                timeout,
                output=last_stdout,
                stderr=last_stderr,
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


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    _signal_process(process, signal.SIGTERM)
    try:
        process.communicate(timeout=0.5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            _signal_process(process, signal.SIGKILL)
        else:
            process.kill()
        process.communicate()


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

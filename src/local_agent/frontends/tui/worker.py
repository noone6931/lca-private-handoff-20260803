from __future__ import annotations

from dataclasses import dataclass
from queue import Empty
from queue import Full
from queue import Queue
from threading import Event
from threading import Lock
from threading import Thread
import time
import uuid
from typing import Protocol

from ...protocol.cancellation import RunCancellation
from ...protocol.commands import AgentCommand
from ...protocol.commands import CommandResult
from ...protocol.interactions import InteractionRequest
from ...protocol.interactions import InteractionResult
from .mailbox import TuiMailbox
from .messages import TuiCommandCompleted
from .messages import TuiInteractionClosed
from .messages import TuiInteractionPending
from .messages import TuiWorkerFailed
from .text import sanitize_terminal_text


class _CommandPort(Protocol):
    cancellation: RunCancellation

    def dispatch(self, command: AgentCommand) -> CommandResult: ...


class TuiRuntimePort(Protocol):
    commands: _CommandPort

    def set_interaction_handler(self, handler) -> None: ...


@dataclass
class _PendingInteraction:
    done: Event
    result: InteractionResult | None = None


class TuiInteractionBridge:
    """Block the Runtime worker while the UI thread owns focused input."""

    def __init__(self, mailbox: TuiMailbox) -> None:
        self._mailbox = mailbox
        self._pending: dict[str, _PendingInteraction] = {}
        self._lock = Lock()
        self._closed = False

    def request_interaction(self, request: InteractionRequest) -> InteractionResult:
        if request.timeout_seconds is not None and request.timeout_seconds <= 0:
            return InteractionResult("timed_out")
        request_id = uuid.uuid4().hex
        pending = _PendingInteraction(Event())
        with self._lock:
            if self._closed or self._pending:
                return InteractionResult("eof")
            self._pending[request_id] = pending
        display_request = InteractionRequest(
            request.kind,
            sanitize_terminal_text(request.prompt)[:8192],
            request.timeout_seconds,
        )
        if not self._mailbox.put(TuiInteractionPending(request_id, display_request)):
            with self._lock:
                self._pending.pop(request_id, None)
            return InteractionResult("cancelled")
        deadline = None
        if request.timeout_seconds is not None:
            deadline = time.monotonic() + max(request.timeout_seconds, 0.0)
        while True:
            if pending.done.is_set():
                break
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                with self._lock:
                    self._pending.pop(request_id, None)
                self._mailbox.put(TuiInteractionClosed(request_id, "timed_out"))
                return InteractionResult("timed_out")
            if pending.done.wait(remaining):
                break
        with self._lock:
            self._pending.pop(request_id, None)
        return pending.result or InteractionResult("cancelled")

    def resolve(self, request_id: str, result: InteractionResult) -> bool:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or pending.done.is_set():
                return False
            pending.result = result
            pending.done.set()
            return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pending = tuple(
                (request_id, item)
                for request_id, item in self._pending.items()
                if not item.done.is_set()
            )
            for _, item in pending:
                item.result = InteractionResult("eof")
                item.done.set()
        for request_id, item in pending:
            self._mailbox.put(TuiInteractionClosed(request_id, "eof"))


class TuiWorker:
    """Single command worker; Runtime and provider calls never run on the UI thread."""

    def __init__(
        self,
        runtime: TuiRuntimePort,
        mailbox: TuiMailbox,
        *,
        command_capacity: int = 8,
        interaction_bridge: TuiInteractionBridge | None = None,
    ) -> None:
        if command_capacity < 1:
            raise ValueError("TUI worker command capacity must be positive.")
        self._runtime = runtime
        self._mailbox = mailbox
        self._commands: Queue[AgentCommand | None] = Queue(maxsize=command_capacity)
        self._interactions = interaction_bridge or TuiInteractionBridge(mailbox)
        self._thread = Thread(target=self._run, name="local-agent-runtime", daemon=True)
        self._prompt_lock = Lock()
        self._prompt_commands = 0
        self._active_prompt = False
        self._started = False
        self._closed = False

    @property
    def interaction_bridge(self) -> TuiInteractionBridge:
        return self._interactions

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._runtime.set_interaction_handler(self._interactions)
        self._thread.start()

    def submit(self, command: AgentCommand) -> bool:
        if self._closed:
            return False
        if command.type == "SubmitPrompt":
            with self._prompt_lock:
                self._prompt_commands += 1
        try:
            self._commands.put_nowait(command)
        except Full:
            self._finish_prompt(command, active=False)
            return False
        return True

    def request_cancel(self) -> bool:
        with self._prompt_lock:
            if self._prompt_commands == 0:
                return False
            active = self._active_prompt
        return self._runtime.commands.cancellation.request(include_next=not active)

    def close(self, timeout: float = 2.0) -> None:
        if self._closed:
            return
        self._closed = True
        self.request_cancel()
        self._interactions.close()
        try:
            self._commands.put_nowait(None)
        except Full:
            pass
        if self._started:
            self._thread.join(max(timeout, 0.0))

    def _run(self) -> None:
        try:
            while True:
                if self._closed:
                    return
                try:
                    command = self._commands.get(timeout=0.1)
                except Empty:
                    if self._closed:
                        return
                    continue
                if command is None:
                    return
                if command.type == "SubmitPrompt":
                    with self._prompt_lock:
                        self._active_prompt = True
                try:
                    result = self._runtime.commands.dispatch(command)
                except KeyboardInterrupt:
                    self._mailbox.put(TuiWorkerFailed(command, "interrupted"))
                except BaseException as exc:  # noqa: BLE001 - worker failure must not kill the TUI.
                    self._mailbox.put(TuiWorkerFailed(command, type(exc).__name__))
                else:
                    self._mailbox.put(TuiCommandCompleted(command, result))
                finally:
                    self._finish_prompt(command, active=True)
        finally:
            self._runtime.set_interaction_handler(None)

    def _finish_prompt(self, command: AgentCommand, *, active: bool) -> None:
        if command.type != "SubmitPrompt":
            return
        with self._prompt_lock:
            if active:
                self._active_prompt = False
            self._prompt_commands = max(self._prompt_commands - 1, 0)

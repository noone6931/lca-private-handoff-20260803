from __future__ import annotations

import threading
import time
import unittest

from local_agent.cancellation import RunCancellation
from local_agent.frontends.tui.mailbox import TuiMailbox
from local_agent.frontends.tui.messages import TuiCommandCompleted
from local_agent.frontends.tui.messages import TuiInteractionClosed
from local_agent.frontends.tui.messages import TuiInteractionPending
from local_agent.frontends.tui.worker import TuiInteractionBridge
from local_agent.frontends.tui.worker import TuiWorker
from local_agent.protocol.commands import CommandResult
from local_agent.protocol.commands import new_command
from local_agent.protocol.interactions import InteractionRequest
from local_agent.protocol.interactions import InteractionResult


class _FakeRuntime:
    def __init__(self) -> None:
        self.commands = self
        self.cancellation = RunCancellation()
        self.handler = None
        self.dispatch_threads: list[str] = []

    def set_interaction_handler(self, handler) -> None:
        self.handler = handler

    def dispatch(self, command):
        self.dispatch_threads.append(threading.current_thread().name)
        return CommandResult(command.command_id, "s1", "r1", "ok", {"content": "done"})


class TuiWorkerTests(unittest.TestCase):
    def test_runtime_dispatch_runs_on_dedicated_worker(self) -> None:
        runtime = _FakeRuntime()
        mailbox = TuiMailbox(capacity=8)
        worker = TuiWorker(runtime, mailbox)  # type: ignore[arg-type]
        worker.start()
        command = new_command("SubmitPrompt", {"prompt": "hello"})
        try:
            self.assertTrue(worker.submit(command))
            message = mailbox.get(timeout=1)
        finally:
            worker.close()

        self.assertIsInstance(message, TuiCommandCompleted)
        self.assertEqual(runtime.dispatch_threads, ["local-agent-runtime"])
        self.assertIsNone(runtime.handler)

    def test_session_command_does_not_latch_cancel_for_next_prompt(self) -> None:
        runtime = _FakeRuntime()
        worker = TuiWorker(runtime, TuiMailbox(capacity=8))  # type: ignore[arg-type]

        self.assertTrue(worker.submit(new_command("GetStatus")))
        self.assertFalse(worker.request_cancel())
        runtime.cancellation.begin()
        try:
            self.assertFalse(runtime.cancellation.requested)
        finally:
            runtime.cancellation.finish()
            worker.close()

    def test_close_cancels_active_dispatch_before_worker_clears_handler(self) -> None:
        runtime = _BlockingRuntime()
        mailbox = TuiMailbox(capacity=8)
        worker = TuiWorker(runtime, mailbox)  # type: ignore[arg-type]
        worker.start()
        command = new_command("SubmitPrompt", {"prompt": "wait"})
        self.assertTrue(worker.submit(command))
        self.assertTrue(runtime.entered.wait(1))

        worker.close(timeout=1)

        self.assertFalse(worker.is_alive)
        self.assertIsNone(runtime.handler)
        self.assertEqual(runtime.handler_threads[-1], "local-agent-runtime")
        self.assertLess(runtime.elapsed, 0.5)

    def test_interaction_bridge_round_trips_typed_result(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        bridge = TuiInteractionBridge(mailbox)
        observed: list[InteractionResult] = []

        thread = threading.Thread(
            target=lambda: observed.append(bridge.request_interaction(InteractionRequest("ask", "Scope?")))
        )
        thread.start()
        pending = mailbox.get(timeout=1)
        self.assertIsInstance(pending, TuiInteractionPending)
        assert isinstance(pending, TuiInteractionPending)

        self.assertTrue(bridge.resolve(pending.request_id, InteractionResult("answered", "src")))
        thread.join(1)

        self.assertEqual(observed, [InteractionResult("answered", "src")])
        self.assertFalse(bridge.resolve(pending.request_id, InteractionResult("cancelled")))

    def test_interaction_timeout_closes_without_late_resolution(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        bridge = TuiInteractionBridge(mailbox)

        result = bridge.request_interaction(InteractionRequest("approval", "Allow?", timeout_seconds=0))

        self.assertEqual(result.status, "timed_out")
        self.assertIsNone(mailbox.get(timeout=0))

    def test_timed_out_visible_interaction_emits_correlated_close(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        bridge = TuiInteractionBridge(mailbox)
        observed = []
        thread = threading.Thread(
            target=lambda: observed.append(
                bridge.request_interaction(InteractionRequest("ask", "Scope?", timeout_seconds=0.03))
            )
        )
        thread.start()
        pending = mailbox.get(timeout=1)
        thread.join(1)
        closed = mailbox.get(timeout=1)

        self.assertIsInstance(pending, TuiInteractionPending)
        self.assertIsInstance(closed, TuiInteractionClosed)
        assert isinstance(pending, TuiInteractionPending)
        assert isinstance(closed, TuiInteractionClosed)
        self.assertEqual(closed.request_id, pending.request_id)
        self.assertEqual(observed[0].status, "timed_out")

    def test_interaction_prompt_is_sanitized_before_crossing_threads(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        bridge = TuiInteractionBridge(mailbox)
        thread = threading.Thread(
            target=lambda: bridge.request_interaction(InteractionRequest("ask", "safe\x1b[31m\ttext"))
        )
        thread.start()
        pending = mailbox.get(timeout=1)
        self.assertIsInstance(pending, TuiInteractionPending)
        assert isinstance(pending, TuiInteractionPending)
        bridge.resolve(pending.request_id, InteractionResult("cancelled"))
        thread.join(1)

        self.assertNotIn("\x1b", pending.request.prompt)
        self.assertIn("    text", pending.request.prompt)

    def test_answer_settles_once_when_bridge_closes_before_waiter_resumes(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        bridge = TuiInteractionBridge(mailbox)
        observed: list[InteractionResult] = []
        thread = threading.Thread(
            target=lambda: observed.append(bridge.request_interaction(InteractionRequest("ask", "Scope?")))
        )
        thread.start()
        pending = mailbox.get(timeout=1)
        self.assertIsInstance(pending, TuiInteractionPending)
        assert isinstance(pending, TuiInteractionPending)

        self.assertTrue(bridge.resolve(pending.request_id, InteractionResult("answered", "src")))
        bridge.close()
        thread.join(1)

        self.assertEqual(observed, [InteractionResult("answered", "src")])
        self.assertIsNone(mailbox.get(timeout=0))


class _BlockingRuntime(_FakeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.handler_threads: list[str] = []
        self.elapsed = 0.0

    def set_interaction_handler(self, handler) -> None:
        super().set_interaction_handler(handler)
        self.handler_threads.append(threading.current_thread().name)

    def dispatch(self, command):
        started = time.monotonic()
        self.cancellation.begin()
        self.entered.set()
        try:
            while not self.cancellation.requested:
                time.sleep(0.005)
            raise KeyboardInterrupt
        finally:
            self.elapsed = time.monotonic() - started
            self.cancellation.finish()


if __name__ == "__main__":
    unittest.main()

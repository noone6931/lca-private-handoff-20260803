from __future__ import annotations

import time

from local_agent.cancellation import RunCancellation
from local_agent.frontends.tui.app import run_tui
from local_agent.frontends.tui.mailbox import TuiMailbox


class _Commands:
    def __init__(self) -> None:
        self.cancellation = RunCancellation()

    def dispatch(self, command):
        if command.type == "SubmitPrompt":
            self.cancellation.begin()
            try:
                while not self.cancellation.requested:
                    time.sleep(0.005)
                raise KeyboardInterrupt
            finally:
                self.cancellation.finish()
        raise AssertionError(f"Unexpected Runtime command: {command.type}")


class _Runtime:
    def __init__(self) -> None:
        self.commands = _Commands()
        self.handler = None

    def set_interaction_handler(self, handler) -> None:
        self.handler = handler


if __name__ == "__main__":
    raise SystemExit(run_tui(_Runtime(), TuiMailbox(capacity=64)))  # type: ignore[arg-type]

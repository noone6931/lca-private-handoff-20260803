from __future__ import annotations

import time
import sys

from local_agent.cancellation import RunCancellation
from local_agent.frontends.tui.app import run_tui
from local_agent.frontends.tui.mailbox import TuiMailbox
from local_agent.protocol.commands import CommandResult


class _Commands:
    def __init__(self) -> None:
        self.cancellation = RunCancellation()

    def dispatch(self, command):
        if command.type == "ListWorkspaceRoots":
            return CommandResult(
                command.command_id,
                "fixture-session",
                None,
                "ok",
                {"text": "Workspace roots (revision 0):\n- primary: /tmp/lca-tui-fixture"},
            )
        if command.type == "SubmitPrompt":
            prompt = str(command.payload.get("prompt", ""))
            if prompt == "slow":
                time.sleep(0.35)
                return CommandResult(
                    command.command_id,
                    "fixture-session",
                    "fixture-run-slow",
                    "ok",
                    {"text": "SUBMITTED:'slow'"},
                )
            if prompt != "wait":
                return CommandResult(
                    command.command_id,
                    "fixture-session",
                    "fixture-run",
                    "ok",
                    {"text": f"SUBMITTED:{prompt!r}"},
                )
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
    initial_prompt = sys.argv[1] if len(sys.argv) > 1 else None
    raise SystemExit(
        run_tui(_Runtime(), TuiMailbox(capacity=64), initial_prompt=initial_prompt)  # type: ignore[arg-type]
    )

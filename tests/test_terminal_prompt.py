from __future__ import annotations

from pathlib import Path
import unittest

from local_agent.frontends.terminal.prompt import PromptToolkitTerminalPrompt
from local_agent.frontends.terminal.prompt import TerminalHistoryRebindError


class _Session:
    def __init__(self, value: str) -> None:
        self.value = value

    def prompt(self, marker: str) -> str:
        return f"{marker}{self.value}"


class TerminalPromptTests(unittest.TestCase):
    def test_rebind_rebuilds_once_for_a_new_canonical_history_path(self) -> None:
        paths: list[Path | None] = []

        def factory(path: Path | None) -> _Session:
            paths.append(path)
            return _Session(str(path))

        prompt = PromptToolkitTerminalPrompt(Path("/state/one/../one/history"), factory)
        prompt.rebind_history(Path("/state/one/history"))
        prompt.rebind_history(Path("/state/two/history"))

        self.assertEqual(paths, [Path("/state/one/history"), Path("/state/two/history")])
        self.assertEqual(prompt(), "> /state/two/history")

    def test_rebind_failure_disables_persistence_without_retaining_old_partition(self) -> None:
        paths: list[Path | None] = []

        def factory(path: Path | None) -> _Session:
            paths.append(path)
            if path == Path("/state/blocked/history"):
                raise OSError("read-only history directory")
            return _Session("no-history" if path is None else str(path))

        prompt = PromptToolkitTerminalPrompt(Path("/state/old/history"), factory)

        with self.assertRaises(TerminalHistoryRebindError):
            prompt.rebind_history(Path("/state/blocked/history"))

        self.assertEqual(paths, [Path("/state/old/history"), Path("/state/blocked/history"), None])
        self.assertEqual(prompt(), "> no-history")


if __name__ == "__main__":
    unittest.main()

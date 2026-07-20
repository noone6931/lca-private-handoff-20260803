from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path
import tempfile
import unittest

from local_agent.frontends.composer_history import ComposerHistory
from local_agent.frontends.terminal.prompt import _navigate_buffer
from local_agent.frontends.terminal.prompt import build_terminal_prompt
from local_agent.frontends.terminal.prompt import PlainTerminalPrompt
from local_agent.frontends.terminal.prompt import PromptToolkitTerminalPrompt
from local_agent.frontends.terminal.prompt import TerminalHistoryRebindError


class _Session:
    def __init__(self, value: str) -> None:
        self.value = value

    def prompt(self, marker: str) -> str:
        return f"{marker}{self.value}"


class TerminalPromptTests(unittest.TestCase):
    def test_rebind_updates_shared_owner_without_building_a_second_history(self) -> None:
        histories: list[ComposerHistory] = []

        def factory(history: ComposerHistory) -> _Session:
            histories.append(history)
            return _Session("shared")

        with tempfile.TemporaryDirectory() as tmp:
            history = ComposerHistory(Path(tmp) / "one" / "history.jsonl")
            prompt = PromptToolkitTerminalPrompt(history, factory)
            prompt.rebind_history(Path(tmp) / "one" / "history.jsonl")
            prompt.rebind_history(Path(tmp) / "two" / "history.jsonl")

            self.assertEqual(histories, [history])
            self.assertEqual(history.path, (Path(tmp) / "two" / "history.jsonl").resolve())
            self.assertEqual(prompt(), "> shared")

    def test_rebind_failure_disables_persistence_without_retaining_old_partition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = ComposerHistory(root / "old" / "history.jsonl")
            prompt = PromptToolkitTerminalPrompt(history, lambda owner: _Session("shared"))
            blocked = root / "blocked"
            blocked.write_text("not a directory", encoding="utf-8")

            with self.assertRaises(TerminalHistoryRebindError):
                prompt.rebind_history(blocked / "history.jsonl")

            self.assertFalse(history.snapshot.persistence_enabled)
            self.assertIsNone(history.path)

    def test_prompt_toolkit_adapter_uses_shared_navigation_and_preserves_editor_movement(self) -> None:
        class Buffer:
            def __init__(self, text: str = "", cursor: int = 0) -> None:
                self.text = text
                self.cursor_position = cursor
                self.moves: list[str] = []

            def cursor_up(self, *, count: int) -> None:
                self.moves.append(f"up:{count}")

            def cursor_down(self, *, count: int) -> None:
                self.moves.append(f"down:{count}")

        history = ComposerHistory(None)
        history.append("saved prompt")
        buffer = Buffer()

        _navigate_buffer(history, buffer, -1)

        self.assertEqual(buffer.text, "saved prompt")
        self.assertEqual(buffer.cursor_position, len("saved prompt"))
        multiline = Buffer("one\ntwo", 2)
        _navigate_buffer(history, multiline, -1)
        self.assertEqual(multiline.moves, ["up:1"])

    def test_plain_prompt_rebinds_the_same_shared_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = ComposerHistory(None)
            prompt = PlainTerminalPrompt(history)

            prompt.rebind_history(Path(tmp) / "history.jsonl")

            self.assertEqual(history.path, (Path(tmp) / "history.jsonl").resolve())

            prompt.rebind_history(None)
            self.assertIsNone(history.path)

    @unittest.skipUnless(find_spec("prompt_toolkit"), "prompt_toolkit is optional")
    def test_real_prompt_toolkit_adapter_uses_the_shared_owner(self) -> None:
        history = ComposerHistory(None)

        prompt = build_terminal_prompt(history)

        self.assertIsInstance(prompt, PromptToolkitTerminalPrompt)
        self.assertIs(prompt._history, history)


if __name__ == "__main__":
    unittest.main()

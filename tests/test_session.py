from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_agent.session.jsonl_store import JsonlSessionStore, SessionError


class SessionStoreTests(unittest.TestCase):
    def test_session_store_can_use_state_dir_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            state_dir = root / "state" / "workspace-key"
            workspace.mkdir()

            store = JsonlSessionStore(workspace, state_dir=state_dir)
            store.append("user", {"content": "hello"})

            self.assertTrue((state_dir / "sessions" / f"{store.session_id}.jsonl").exists())
            self.assertFalse((workspace / ".local-agent").exists())

    def test_load_messages_reconstructs_user_assistant_and_tool_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            store = JsonlSessionStore(workspace)
            store.append("user", {"content": "read README"})
            store.append(
                "assistant",
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
                        }
                    ],
                },
            )
            store.append(
                "tool_result",
                {
                    "tool_call_id": "call_1",
                    "name": "read_file",
                    "is_error": False,
                    "content": "README contents",
                },
            )

            reopened = JsonlSessionStore(workspace, session_id=store.session_id)
            messages = reopened.load_messages()

        self.assertEqual(
            messages,
            [
                {"role": "user", "content": "read README"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "README contents"},
            ],
        )

    def test_load_messages_adds_missing_assistant_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            store = JsonlSessionStore(workspace)
            store.append("user", {"content": "hello"})
            store.append("assistant", {"content": "hi"})

            messages = store.load_messages()

        self.assertEqual(
            messages,
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        )

    def test_load_messages_normalizes_null_assistant_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            store = JsonlSessionStore(workspace)
            store.append("assistant", {"role": None, "content": "hi"})

            messages = store.load_messages()

        self.assertEqual(messages, [{"role": "assistant", "content": "hi"}])

    def test_continue_recent_opens_latest_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            first = JsonlSessionStore(workspace)
            first.append("user", {"content": "first"})
            second = JsonlSessionStore(workspace)
            second.append("user", {"content": "second"})

            latest = JsonlSessionStore(workspace, continue_recent=True)

        self.assertEqual(latest.session_id, second.session_id)

    def test_missing_session_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            with self.assertRaisesRegex(SessionError, "Session not found"):
                JsonlSessionStore(workspace, session_id="missing")

    def test_invalid_session_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            with self.assertRaisesRegex(SessionError, "Invalid session id"):
                JsonlSessionStore(workspace, session_id="../escape")

    def test_load_messages_trims_recent_history_without_leading_tool_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            store = JsonlSessionStore(workspace)
            store.append("assistant", {"role": "assistant", "content": None, "tool_calls": []})
            store.append("tool_result", {"tool_call_id": "call_1", "content": "orphaned by trim"})
            store.append("user", {"content": "recent"})
            store.append("assistant", {"role": "assistant", "content": "answer"})

            messages = store.load_messages(max_messages=3)

        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[-1]["content"], "answer")

    def test_load_messages_drops_trailing_unpaired_tool_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            store = JsonlSessionStore(workspace)
            store.append("user", {"content": "read README"})
            store.append(
                "assistant",
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
                        }
                    ],
                },
            )

            messages = store.load_messages()

        self.assertEqual(messages, [{"role": "user", "content": "read README"}])

    def test_load_latest_workspace_roots_uses_last_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            store = JsonlSessionStore(workspace)
            store.append("workspace_roots_changed", {"revision": 1, "session_roots": ["/first"]})
            store.append("workspace_roots_changed", {"revision": 2, "session_roots": ["/second"]})

            snapshot = store.load_latest_workspace_roots()

        self.assertEqual(snapshot, {"revision": 2, "session_roots": ["/second"]})


if __name__ == "__main__":
    unittest.main()

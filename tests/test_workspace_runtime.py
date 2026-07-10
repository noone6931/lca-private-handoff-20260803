from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_agent.agent import AgentRuntime
from local_agent.config import AgentConfig
from local_agent.design_evidence import cross_root_design_evidence_roots
from local_agent.protocol.events import ListEventSink


class WorkspaceRuntimeTests(unittest.TestCase):
    def test_add_remove_updates_file_tool_context_and_provider_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            primary = root / "primary"
            external = root / "external"
            primary.mkdir()
            external.mkdir()
            target = external / "spec.md"
            target.write_text("# Spec\n", encoding="utf-8")
            runtime = AgentRuntime(_config(primary), show_tool_logs=False)

            before = runtime._registry.execute("read_file", {"path": str(target)}, runtime._tool_context)
            added = runtime.add_workspace_root(str(external))
            after = runtime._registry.execute("read_file", {"path": str(target)}, runtime._tool_context)
            system = _system_content(runtime)
            runtime.remove_workspace_root(str(external))
            removed = runtime._registry.execute("read_file", {"path": str(target)}, runtime._tool_context)

        self.assertTrue(before.is_error)
        self.assertEqual(added, external)
        self.assertFalse(after.is_error)
        self.assertEqual(system.count("[Workspace roots]"), 1)
        self.assertIn(str(external), system)
        self.assertTrue(removed.is_error)
        self.assertEqual(runtime._tool_context.workspace, primary)

    def test_session_root_is_replayed_and_missing_root_emits_error_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            primary = root / "primary"
            external = root / "external"
            primary.mkdir()
            external.mkdir()
            state_dir = root / "state"
            first = AgentRuntime(_config(primary, state_dir=state_dir), show_tool_logs=False)
            first.add_workspace_root(str(external))

            restored = AgentRuntime(
                _config(primary, state_dir=state_dir),
                show_tool_logs=False,
                session_id=first._session.session_id,
            )
            self.assertIn(str(external), restored.workspace_summary())

            external.rmdir()
            sink = ListEventSink()
            missing = AgentRuntime(
                _config(primary, state_dir=state_dir),
                show_tool_logs=False,
                session_id=first._session.session_id,
                event_sink=sink,
            )

        self.assertNotIn(str(external), missing.workspace_summary())
        self.assertTrue(
            any(event.type == "ErrorEvent" and event.payload.get("kind") == "workspace_root_restore" for event in sink.events)
        )

    def test_workspace_changes_are_rejected_while_runtime_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            primary = root / "primary"
            external = root / "external"
            primary.mkdir()
            external.mkdir()
            runtime = AgentRuntime(_config(primary), show_tool_logs=False)
            runtime._is_running = True

            with self.assertRaisesRegex(RuntimeError, "idle"):
                runtime.add_workspace_root(str(external))

    def test_added_code_root_participates_in_cross_root_design_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            primary = root / "backend"
            frontend = root / "frontend"
            (primary / "src").mkdir(parents=True)
            (frontend / "src").mkdir(parents=True)
            runtime = AgentRuntime(_config(primary), show_tool_logs=False)
            runtime.add_workspace_root(str(frontend))

            roots = cross_root_design_evidence_roots(
                primary,
                runtime._workspace_context.additional_roots,
                "请给出前后端改造设计方案",
            )

        self.assertEqual(roots, (str(primary), str(frontend)))


def _config(workspace: Path, *, state_dir: Path | None = None) -> AgentConfig:
    return AgentConfig(
        provider="openai-compatible",
        api_base_url="https://example.invalid/v1",
        api_key="token",
        model="model",
        workspace=workspace,
        state_dir=state_dir,
        max_steps=0,
        budget_seconds=None,
        approval_mode="yolo",
    )


def _system_content(runtime: AgentRuntime) -> str:
    messages = runtime._provider_safe_runtime_messages(runtime._messages, [])
    return str(next(message["content"] for message in messages if message.get("role") == "system"))


if __name__ == "__main__":
    unittest.main()

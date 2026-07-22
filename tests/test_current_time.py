from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from local_agent.agent import AgentRuntime
from local_agent.config import AgentConfig
from local_agent.platform.current_time import CurrentTimeSnapshot
from local_agent.platform.current_time import current_time_snapshot
from local_agent.platform.current_time import messages_with_current_time_context
from local_agent.tools import create_default_registry
from local_agent.tools.base import ToolContext
from local_agent.tools.base import ToolRegistry
from local_agent.tools.current_time import current_time_tools


FIXED = CurrentTimeSnapshot(
    utc_iso="2026-07-22T10:15:30+00:00",
    local_iso="2026-07-22T18:15:30+08:00",
    local_date="2026-07-22",
    timezone_name="CST",
    utc_offset="+08:00",
)


class CurrentTimeTests(unittest.TestCase):
    def test_snapshot_projects_one_aware_instant_to_utc_and_local_time(self) -> None:
        snapshot = current_time_snapshot(
            datetime(2026, 7, 22, 10, 15, 30, 987654, tzinfo=timezone.utc),
            local_timezone=timezone(timedelta(hours=8), "CST"),
        )

        self.assertEqual(snapshot, FIXED)
        self.assertEqual(snapshot.as_dict()["schema"], "current_time_v1")
        self.assertEqual(snapshot.as_dict()["source"], "system_clock")

    def test_snapshot_rejects_naive_time_instead_of_guessing_timezone(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            current_time_snapshot(datetime(2026, 7, 22, 10, 15, 30))

    def test_context_is_immutable_and_refreshes_exactly_one_typed_block(self) -> None:
        original = [{"role": "system", "content": "base policy"}, {"role": "user", "content": "today?"}]
        first = messages_with_current_time_context(original, FIXED)
        newer = CurrentTimeSnapshot(
            utc_iso="2026-07-23T00:00:00+00:00",
            local_iso="2026-07-23T08:00:00+08:00",
            local_date="2026-07-23",
            timezone_name="CST",
            utc_offset="+08:00",
        )
        refreshed = messages_with_current_time_context(first, newer)

        self.assertEqual(original[0]["content"], "base policy")
        self.assertEqual(str(first[0]["content"]).count("[Current time]"), 1)
        self.assertEqual(str(refreshed[0]["content"]).count("[Current time]"), 1)
        self.assertIn("2026-07-23", str(refreshed[0]["content"]))
        self.assertNotIn("2026-07-22T18:15:30", str(refreshed[0]["content"]))
        self.assertIn("does not prove current repository or external-world state", str(refreshed[0]["content"]))

    def test_context_creates_a_system_projection_when_one_is_absent(self) -> None:
        original = [{"role": "user", "content": "what time is it?"}]

        projected = messages_with_current_time_context(original, FIXED)

        self.assertEqual(projected[0]["role"], "system")
        self.assertEqual(projected[1:], original)

    def test_tool_is_read_only_structured_and_rejects_extra_arguments(self) -> None:
        registry = ToolRegistry(list(current_time_tools(lambda: FIXED)))
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp), approval_mode="yolo")
            result = registry.execute("current_time", {}, context)
            invalid = registry.execute("current_time", {"timezone": "UTC"}, context)

        self.assertFalse(result.is_error)
        self.assertEqual(json.loads(result.content), FIXED.as_dict())
        self.assertEqual(result.metadata["clock_source"], "system_clock")
        self.assertTrue(result.metadata["structured_output"])
        self.assertTrue(invalid.is_error)
        self.assertIn("unexpected", invalid.content.casefold())

    def test_default_registry_exposes_exactly_one_current_time_tool(self) -> None:
        registry = create_default_registry()
        schema = next(
            item for item in registry.schemas()
            if item["function"]["name"] == "current_time"
        )

        self.assertEqual(registry.tool_names().count("current_time"), 1)
        self.assertEqual(schema["function"]["parameters"]["properties"], {})
        self.assertFalse(schema["function"]["parameters"]["additionalProperties"])

    def test_provider_context_refreshes_time_without_persisting_it_to_session_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            runtime = AgentRuntime(
                AgentConfig(
                    provider="openai-compatible",
                    api_base_url="https://example.invalid/v1",
                    api_key="token",
                    model="model",
                    workspace=workspace,
                    max_steps=0,
                    budget_seconds=None,
                    approval_mode="yolo",
                ),
                show_tool_logs=False,
            )
            runtime._session.append("test_marker", {})
            with patch("local_agent.runtime.provider_context.current_time_snapshot", return_value=FIXED):
                projected = runtime._provider_context_phase.provider_safe_runtime_messages(runtime._messages, [])
            session_text = runtime._session.path.read_text(encoding="utf-8")

        system = str(next(message["content"] for message in projected if message.get("role") == "system"))
        self.assertIn(FIXED.local_iso, system)
        self.assertNotIn("[Current time]", json.dumps(runtime._messages))
        self.assertNotIn("[Current time]", session_text)


if __name__ == "__main__":
    unittest.main()

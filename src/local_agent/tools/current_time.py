from __future__ import annotations

import json
from collections.abc import Callable

from ..platform.current_time import CurrentTimeSnapshot
from ..platform.current_time import current_time_snapshot
from .base import Tool
from .base import ToolContext
from .base import ToolResult


SnapshotProvider = Callable[[], CurrentTimeSnapshot]


def current_time_tools(
    snapshot_provider: SnapshotProvider = current_time_snapshot,
) -> tuple[Tool, ...]:
    return (
        Tool(
            name="current_time",
            description=(
                "Return the current UTC and host-local date and time from the runtime system clock. "
                "Use this before interpreting relative dates such as today or tomorrow."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            tier="read",
            handler=lambda args, context: _current_time(args, context, snapshot_provider),
        ),
    )


def _current_time(
    _args: dict[str, object],
    _context: ToolContext,
    snapshot_provider: SnapshotProvider,
) -> ToolResult:
    snapshot = snapshot_provider()
    return ToolResult(
        json.dumps(snapshot.as_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        metadata={
            "clock_read": True,
            "clock_source": snapshot.source,
            "structured_output": True,
        },
    )

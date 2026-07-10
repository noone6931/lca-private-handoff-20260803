from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any


@dataclass
class RunStats:
    run_id: str
    prompt_chars: int
    started_monotonic: float
    llm_requests: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    useless_tool_results: int = 0
    synthetic_tool_results: int = 0
    compactions: int = 0
    llm_context_summaries: int = 0
    local_context_summaries: int = 0
    tool_counts: dict[str, int] = field(default_factory=dict)
    guard_start: dict[str, int] = field(default_factory=dict)
    steer_start: dict[str, int] = field(default_factory=dict)


class RunCollector:
    """Own per-run telemetry while Runtime owns event/session delivery."""

    def __init__(self) -> None:
        self._stats: RunStats | None = None
        self._pending_compaction_summary_mode: str | None = None

    def start(
        self,
        run_id: str,
        prompt: str,
        started_monotonic: float,
        *,
        guard_start: dict[str, int],
        steer_start: dict[str, int],
    ) -> None:
        self._stats = RunStats(
            run_id=run_id,
            prompt_chars=len(prompt),
            started_monotonic=started_monotonic,
            guard_start=dict(guard_start),
            steer_start=dict(steer_start),
        )
        self._pending_compaction_summary_mode = None

    def record_llm_request(self) -> None:
        if self._stats is not None:
            self._stats.llm_requests += 1

    def mark_llm_context_summary(self) -> None:
        self._pending_compaction_summary_mode = "llm"

    def mark_local_context_summary(self) -> None:
        self._pending_compaction_summary_mode = "local"

    def record_context_compaction(self) -> None:
        if self._stats is not None:
            self._stats.compactions += 1
            if self._pending_compaction_summary_mode == "llm":
                self._stats.llm_context_summaries += 1
            elif self._pending_compaction_summary_mode == "local":
                self._stats.local_context_summaries += 1
        self._pending_compaction_summary_mode = None

    def record_tool_started(self, name: str) -> None:
        if self._stats is None:
            return
        self._stats.tool_calls += 1
        self._stats.tool_counts[name] = self._stats.tool_counts.get(name, 0) + 1

    def record_tool_finished(self, *, is_error: bool) -> None:
        if self._stats is not None and is_error:
            self._stats.tool_errors += 1

    def record_tool_result(self, *, is_error: bool, useless: bool) -> None:
        if self._stats is not None and useless and not is_error:
            self._stats.useless_tool_results += 1

    def record_synthetic_tool_result(self) -> None:
        if self._stats is None:
            return
        self._stats.synthetic_tool_results += 1
        self._stats.tool_errors += 1

    def finish(
        self,
        reason: str,
        *,
        guard_values: dict[str, int],
        steering_values: dict[str, int],
    ) -> dict[str, Any]:
        stats = self._stats
        if stats is None:
            return {"termination_reason": reason}
        guard_hits = {
            key: value - stats.guard_start.get(key, 0)
            for key, value in guard_values.items()
        }
        steering_counts = {
            key: value - stats.steer_start.get(key, 0)
            for key, value in steering_values.items()
        }
        return {
            "run_id": stats.run_id,
            "termination_reason": reason,
            "elapsed_ms": _elapsed_ms_since(stats.started_monotonic),
            "prompt_chars": stats.prompt_chars,
            "llm_requests": stats.llm_requests,
            "tool_calls": stats.tool_calls,
            "tool_errors": stats.tool_errors,
            "useless_tool_results": stats.useless_tool_results,
            "synthetic_tool_results": stats.synthetic_tool_results,
            "compactions": stats.compactions,
            "llm_context_summaries": stats.llm_context_summaries,
            "local_context_summaries": stats.local_context_summaries,
            "tool_counts": dict(sorted(stats.tool_counts.items())),
            "guard_hits": {key: value for key, value in guard_hits.items() if value},
            "steering_counts": {key: value for key, value in steering_counts.items() if value},
        }


def _elapsed_ms_since(started_monotonic: float) -> int:
    try:
        return max(0, int((time.monotonic() - started_monotonic) * 1000))
    except Exception:  # noqa: BLE001 - run summary must never break task completion.
        return 0

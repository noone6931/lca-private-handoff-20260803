from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Mapping


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
    effective_compactions: int = 0
    zero_gain_compactions: int = 0
    consecutive_zero_gain_compactions: int = 0
    max_consecutive_zero_gain_compactions: int = 0
    compaction_estimated_token_reduction: int = 0
    llm_context_summaries: int = 0
    local_context_summaries: int = 0
    file_discovery_calls: int = 0
    file_discovery_incomplete_results: int = 0
    file_discovery_no_match_results: int = 0
    unknown_tool_calls: int = 0
    unknown_tool_suggestions: int = 0
    filename_search_misuse_calls: int = 0
    provider_schema_violations: int = 0
    session_evidence_hits: int = 0
    session_evidence_misses: int = 0
    session_evidence_stale: int = 0
    session_evidence_invalidations: int = 0
    session_evidence_reused_paths: list[str] = field(default_factory=list)
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

    def record_context_compaction(
        self,
        *,
        estimated_tokens_before: int | None = None,
        estimated_tokens_after: int | None = None,
    ) -> None:
        if self._stats is not None:
            self._stats.compactions += 1
            if estimated_tokens_before is not None and estimated_tokens_after is not None:
                reduction = max(0, estimated_tokens_before - estimated_tokens_after)
                self._stats.compaction_estimated_token_reduction += reduction
                if reduction:
                    self._stats.effective_compactions += 1
                    self._stats.consecutive_zero_gain_compactions = 0
                else:
                    self._stats.zero_gain_compactions += 1
                    self._stats.consecutive_zero_gain_compactions += 1
                    self._stats.max_consecutive_zero_gain_compactions = max(
                        self._stats.max_consecutive_zero_gain_compactions,
                        self._stats.consecutive_zero_gain_compactions,
                    )
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
        if name == "glob_files":
            self._stats.file_discovery_calls += 1

    def record_tool_finished(self, *, is_error: bool) -> None:
        if self._stats is not None and is_error:
            self._stats.tool_errors += 1

    def record_tool_result(
        self,
        *,
        name: str,
        is_error: bool,
        useless: bool,
        metadata: Mapping[str, Any],
    ) -> None:
        if self._stats is None:
            return
        if useless and not is_error:
            self._stats.useless_tool_results += 1
        if name == "glob_files":
            if metadata.get("complete") is False:
                self._stats.file_discovery_incomplete_results += 1
            if metadata.get("negative_evidence_type") in {"path_no_match", "exact_path_missing"}:
                self._stats.file_discovery_no_match_results += 1
        if metadata.get("unknown_tool"):
            self._stats.unknown_tool_calls += 1
            suggestions = metadata.get("suggested_tools")
            if isinstance(suggestions, (list, tuple)) and suggestions:
                self._stats.unknown_tool_suggestions += 1
        if metadata.get("filename_search_misuse"):
            self._stats.filename_search_misuse_calls += 1
        if metadata.get("provider_schema_violation"):
            self._stats.provider_schema_violations += 1

    def record_synthetic_tool_result(self) -> None:
        if self._stats is None:
            return
        self._stats.synthetic_tool_results += 1
        self._stats.tool_errors += 1

    def record_session_evidence(
        self,
        *,
        hits: int,
        misses: int,
        stale: int,
        invalidations: int,
        reused_paths: list[str],
    ) -> None:
        if self._stats is None:
            return
        self._stats.session_evidence_hits += hits
        self._stats.session_evidence_misses += misses
        self._stats.session_evidence_stale += stale
        self._stats.session_evidence_invalidations += invalidations
        for path in reused_paths:
            if path not in self._stats.session_evidence_reused_paths:
                self._stats.session_evidence_reused_paths.append(path)

    def record_session_evidence_invalidation(self, count: int) -> None:
        if self._stats is not None and count > 0:
            self._stats.session_evidence_invalidations += count

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
            "effective_compactions": stats.effective_compactions,
            "zero_gain_compactions": stats.zero_gain_compactions,
            "max_consecutive_zero_gain_compactions": stats.max_consecutive_zero_gain_compactions,
            "compaction_estimated_token_reduction": stats.compaction_estimated_token_reduction,
            "llm_context_summaries": stats.llm_context_summaries,
            "local_context_summaries": stats.local_context_summaries,
            "file_discovery_calls": stats.file_discovery_calls,
            "file_discovery_incomplete_results": stats.file_discovery_incomplete_results,
            "file_discovery_no_match_results": stats.file_discovery_no_match_results,
            "unknown_tool_calls": stats.unknown_tool_calls,
            "unknown_tool_suggestions": stats.unknown_tool_suggestions,
            "filename_search_misuse_calls": stats.filename_search_misuse_calls,
            "provider_schema_violations": stats.provider_schema_violations,
            "session_evidence": {
                "hits": stats.session_evidence_hits,
                "misses": stats.session_evidence_misses,
                "stale": stats.session_evidence_stale,
                "invalidations": stats.session_evidence_invalidations,
                "reused_paths": list(stats.session_evidence_reused_paths),
            },
            "tool_counts": dict(sorted(stats.tool_counts.items())),
            "guard_hits": {key: value for key, value in guard_hits.items() if value},
            "steering_counts": {key: value for key, value in steering_counts.items() if value},
        }


def _elapsed_ms_since(started_monotonic: float) -> int:
    try:
        return max(0, int((time.monotonic() - started_monotonic) * 1000))
    except Exception:  # noqa: BLE001 - run summary must never break task completion.
        return 0

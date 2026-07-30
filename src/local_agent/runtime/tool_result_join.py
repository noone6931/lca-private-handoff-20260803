from __future__ import annotations

from typing import Any

from ..steering.tool_loop import is_filename_search_misuse
from ..tools.base import ToolResult
from ..tools.execution_interrupt import interrupted_tool_result


def join_tool_result(
    runtime: Any,
    *,
    tool_call: dict[str, Any],
    name: str,
    arguments: str | dict[str, Any],
    result: ToolResult,
) -> None:
    runtime._log_tool_end(name, result.is_error, len(result.content))
    runtime._append_tool_result(
        tool_call,
        name,
        result.content,
        is_error=result.is_error,
        useless=result.useless,
        metadata={
            **dict(result.metadata),
            "filename_search_misuse": is_filename_search_misuse(name, arguments),
        },
    )
    runtime._run.reset_forced_final_answer_continuations()
    runtime._evidence_phase.record_tool_choice_result(
        name,
        arguments,
        result,
        tool_call_id=str(tool_call.get("id") or ""),
    )
    runtime._evidence_phase.record_successful_patch_preview(name, arguments, result)
    runtime._evidence_phase.record_read_file_evidence(name, arguments, result)
    runtime._evidence_phase.record_tool_evidence(name, arguments, result)
    runtime._evidence_phase.invalidate_stale_source_evidence_after_write(
        name,
        arguments,
        result,
    )
    runtime._observe_soft_tool_requirement(name, arguments, result)


__all__ = ["interrupted_tool_result", "join_tool_result"]

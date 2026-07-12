from __future__ import annotations

import hashlib
import json
import math
from typing import Any

COMPACTION_TOOL_CONTENT_CHAR_LIMIT = 6000
SUMMARY_INPUT_CHAR_LIMIT = 12000
SUMMARY_OUTPUT_CHAR_LIMIT = 4000
SUMMARY_REQUEST_TIMEOUT = 30.0

DEFAULT_RESERVE_CHARS = 16384 * 4
DEFAULT_RESERVE_TOKENS = 4096
MIN_RESERVE_RATIO = 0.15

USELESS_TOOL_RESULT_NOTICE = "[Uneventful tool result elided during local context pruning]"
SUPERSEDED_TOOL_RESULT_NOTICE = "[Superseded by a newer equivalent tool result during local context pruning]"


def estimate_message_chars(messages: list[dict[str, Any]]) -> int:
    return sum(len(json.dumps(message, ensure_ascii=False, default=str)) for message in messages)


def estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(
        estimate_text_tokens(json.dumps(message, ensure_ascii=False, default=str))
        for message in messages
    )


def estimate_text_tokens(content: str) -> int:
    """Cheap local token estimate for budget decisions when no tokenizer is bundled."""
    if not content:
        return 0
    cjk_chars = 0
    ascii_like_chars = 0
    other_chars = 0
    for char in content:
        if "\u4e00" <= char <= "\u9fff":
            cjk_chars += 1
        elif ord(char) < 128:
            ascii_like_chars += 1
        else:
            other_chars += 1
    return max(1, int(math.ceil(cjk_chars + other_chars / 2 + ascii_like_chars / 4)))


def resolve_compaction_threshold_chars(context_window_chars: int) -> int:
    if context_window_chars <= 1:
        return 0
    reserve = _resolve_budget_reserve_chars(context_window_chars)
    return max(0, min(context_window_chars - 1, context_window_chars - reserve))


def resolve_compaction_threshold_tokens(context_window_tokens: int) -> int:
    if context_window_tokens <= 1:
        return 0
    reserve = _resolve_budget_reserve_tokens(context_window_tokens)
    return max(0, min(context_window_tokens - 1, context_window_tokens - reserve))


def valid_recent_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    recent = list(messages)
    while recent and recent[0].get("role") == "tool":
        recent = recent[1:]
    return drop_trailing_unpaired_tool_calls(recent)


def prune_context_tool_outputs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    superseded_tool_call_ids = _superseded_tool_call_ids(messages)
    pruned: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "tool":
            pruned.append(message)
            continue
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id in superseded_tool_call_ids:
            pruned.append(_tool_message_with_notice(message, SUPERSEDED_TOOL_RESULT_NOTICE))
        elif message.get("_lca_useless") is True and message.get("_lca_is_error") is not True:
            pruned.append(_tool_message_with_notice(message, USELESS_TOOL_RESULT_NOTICE))
        else:
            pruned.append(message)
    return pruned


def truncate_recent_tool_outputs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    truncated: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "tool" and isinstance(content, str):
            truncated.append(_truncate_tool_message(message, content))
        else:
            truncated.append(message)
    return truncated


def provider_safe_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    for message in messages:
        safe.append({key: value for key, value in message.items() if not key.startswith("_lca_")})
    return safe


def snippets_for_role(messages: list[dict[str, Any]], role: str, *, limit: int) -> list[str]:
    snippets: list[str] = []
    for message in messages:
        if message.get("role") != role:
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        snippets.append(f"- {_one_line(content)}")
    return snippets[-limit:]


def assistant_snippets(messages: list[dict[str, Any]], *, limit: int) -> list[str]:
    snippets: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        tool_calls = message.get("tool_calls") or []
        if isinstance(content, str) and content.strip():
            snippets.append(f"- {_one_line(content)}")
        elif tool_calls:
            names = []
            for tool_call in tool_calls:
                if isinstance(tool_call, dict):
                    function = tool_call.get("function") or {}
                    if isinstance(function, dict) and function.get("name"):
                        names.append(str(function["name"]))
            if names:
                snippets.append(f"- Requested tools: {', '.join(names)}")
    return snippets[-limit:]


def tool_snippets(messages: list[dict[str, Any]], *, limit: int) -> list[str]:
    snippets: list[str] = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            snippets.append(f"- {_one_line(content)}")
    return snippets[-limit:]


def messages_to_summary_transcript(messages: list[dict[str, Any]], *, max_chars: int) -> str:
    lines: list[str] = []
    total = 0
    for message in messages:
        rendered = _render_summary_transcript_message(message)
        if not rendered:
            continue
        remaining = max_chars - total
        if remaining <= 0:
            break
        if len(rendered) > remaining:
            rendered = rendered[: max(0, remaining - 14)] + "...<truncated>"
        lines.append(rendered)
        total += len(rendered) + 1
    if total >= max_chars:
        lines.append("...<transcript truncated for summary request>")
    return "\n".join(lines)


def summary_request_content(
    transcript: str,
    current_user_request: str | None,
    todo_summary: list[str],
) -> str:
    parts = ["Earlier transcript:", transcript]
    if current_user_request:
        parts.extend(["", "Current user request:", current_user_request])
    if todo_summary:
        parts.extend(["", "Open todos:", "\n".join(todo_summary)])
    parts.append(
        "\nReturn a compact summary for the next model call. "
        "Preserve constraints and completed actions; omit noise."
    )
    return "\n".join(parts)


def format_llm_compaction_summary(
    summary: str,
    current_user_request: str | None,
    todo_summary: list[str],
) -> str:
    lines = [
        "Earlier conversation was summarized by the configured LLM to stay within the context budget.",
        "Preserve these facts while continuing the current task.",
        "",
        "Summary:",
        summary,
    ]
    if current_user_request:
        lines.extend(
            [
                "",
                "The current user request remains in user-role conversation context.",
                "- After completing explicitly requested tool calls, answer the requested final response instead of exploring further unless more information is truly necessary.",
            ]
        )
    if todo_summary:
        lines.extend(["", "Open todos:", *todo_summary])
    return "\n".join(lines)


def summary_cache_key(transcript: str, current_user_request: str | None, todo_summary: list[str]) -> str:
    payload = json.dumps(
        {
            "transcript": transcript,
            "current_user_request": current_user_request,
            "todo_summary": todo_summary,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def drop_trailing_unpaired_tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trimmed = list(messages)
    while trimmed:
        index = _last_assistant_with_tool_calls_index(trimmed)
        if index is None:
            return trimmed
        expected = _assistant_tool_call_ids(trimmed[index])
        following = {
            message.get("tool_call_id")
            for message in trimmed[index + 1 :]
            if message.get("role") == "tool"
        }
        if expected.issubset(following):
            return trimmed
        trimmed = trimmed[:index]
    return trimmed


def _resolve_budget_reserve_chars(context_window_chars: int) -> int:
    proportional_reserve = max(1, int(context_window_chars * MIN_RESERVE_RATIO))
    default_reserve = max(proportional_reserve, DEFAULT_RESERVE_CHARS)
    default_reserve_is_impossible = default_reserve >= context_window_chars - proportional_reserve
    reserve_exceeds_window = default_reserve >= context_window_chars
    return proportional_reserve if default_reserve_is_impossible or reserve_exceeds_window else default_reserve


def _resolve_budget_reserve_tokens(context_window_tokens: int) -> int:
    proportional_reserve = max(1, int(context_window_tokens * MIN_RESERVE_RATIO))
    default_reserve = max(proportional_reserve, DEFAULT_RESERVE_TOKENS)
    default_reserve_is_impossible = default_reserve >= context_window_tokens - proportional_reserve
    reserve_exceeds_window = default_reserve >= context_window_tokens
    return proportional_reserve if default_reserve_is_impossible or reserve_exceeds_window else default_reserve


def _tool_message_with_notice(message: dict[str, Any], notice: str) -> dict[str, Any]:
    copied = dict(message)
    copied["content"] = notice
    return copied


def _superseded_tool_call_ids(messages: list[dict[str, Any]]) -> set[str]:
    tool_calls_by_id = _tool_calls_by_id(messages)
    latest_by_key: dict[str, str] = {}
    superseded: set[str] = set()
    for message in messages:
        if message.get("role") != "tool":
            continue
        tool_call_id = message.get("tool_call_id")
        if not isinstance(tool_call_id, str):
            continue
        if message.get("_lca_is_error") is True:
            continue
        tool_call = tool_calls_by_id.get(tool_call_id)
        if tool_call is None:
            continue
        key = _tool_supersede_key(tool_call)
        if key is None:
            continue
        previous = latest_by_key.get(key)
        if previous is not None:
            superseded.add(previous)
        latest_by_key[key] = tool_call_id
    return superseded


def _tool_calls_by_id(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    tool_calls: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        raw_tool_calls = message.get("tool_calls") or []
        if not isinstance(raw_tool_calls, list):
            continue
        for tool_call in raw_tool_calls:
            if isinstance(tool_call, dict) and isinstance(tool_call.get("id"), str):
                tool_calls[tool_call["id"]] = tool_call
    return tool_calls


def _tool_supersede_key(tool_call: dict[str, Any]) -> str | None:
    function = tool_call.get("function") or {}
    if not isinstance(function, dict):
        return None
    name = str(function.get("name") or "")
    if name not in {
        "read_file",
        "search_code",
        "lsp_workspace_symbols",
        "lsp_document_symbols",
        "lsp_symbols",
        "lsp_definition",
        "lsp_references",
        "lsp_diagnostics",
    }:
        return None
    return _tool_call_signature(name, function.get("arguments") or "{}")


def _tool_call_signature(name: str, arguments: str | dict[str, Any]) -> str:
    if isinstance(arguments, dict):
        parsed: Any = arguments
    else:
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            parsed = arguments
    payload = {"name": name, "arguments": parsed}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _truncate_tool_message(message: dict[str, Any], content: str) -> dict[str, Any]:
    if len(content) <= COMPACTION_TOOL_CONTENT_CHAR_LIMIT:
        return message
    omitted = len(content) - COMPACTION_TOOL_CONTENT_CHAR_LIMIT
    copied = dict(message)
    copied["content"] = (
        content[:COMPACTION_TOOL_CONTENT_CHAR_LIMIT]
        + f"\n...<truncated {omitted} chars from tool output during local context compaction>"
    )
    return copied


def _last_assistant_with_tool_calls_index(messages: list[dict[str, Any]]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "assistant" and _assistant_tool_call_ids(message):
            return index
    return None


def _assistant_tool_call_ids(message: dict[str, Any]) -> set[str]:
    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        return set()
    ids: set[str] = set()
    for tool_call in tool_calls:
        if isinstance(tool_call, dict) and isinstance(tool_call.get("id"), str):
            ids.add(tool_call["id"])
    return ids


def _render_summary_transcript_message(message: dict[str, Any]) -> str:
    role = str(message.get("role") or "unknown")
    if role == "assistant":
        tool_calls = message.get("tool_calls") or []
        if isinstance(tool_calls, list) and tool_calls:
            names: list[str] = []
            for tool_call in tool_calls:
                if isinstance(tool_call, dict):
                    function = tool_call.get("function") or {}
                    if isinstance(function, dict) and function.get("name"):
                        names.append(str(function["name"]))
            if names:
                return f"assistant tool_calls: {', '.join(names)}"
    if role == "tool":
        tool_call_id = message.get("tool_call_id") or "unknown"
        content = message.get("content")
        return f"tool[{tool_call_id}]: {_one_line(str(content or ''), max_chars=1200)}"
    content = message.get("content")
    if content is None:
        return ""
    return f"{role}: {_one_line(str(content), max_chars=1200)}"


def _one_line(content: str, *, max_chars: int = 240) -> str:
    normalized = " ".join(content.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 14] + "...<truncated>"

"""Session memory consolidation helpers owned outside the Runtime loop."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .storage import ProjectMemoryStore
from .storage import ProjectMemoryStoreError
from .storage import PROJECT_MEMORY_NAMES

MEMORY_CONSOLIDATION_INPUT_CHAR_LIMIT = 14000
MEMORY_CONSOLIDATION_OUTPUT_CHAR_LIMIT = 8000
MEMORY_CONSOLIDATION_MIN_AUTO_CHARS = 500
MEMORY_CONSOLIDATION_MAX_ITEMS_PER_BUCKET = 5
MEMORY_CONSOLIDATION_MAX_ITEM_CHARS = 700
MEMORY_CONSOLIDATION_BUCKETS = PROJECT_MEMORY_NAMES
MEMORY_CONSOLIDATION_WRITE_TOOLS = {"memory_write", "learn"}

from ..runtime.prompt import _one_line
from ..runtime.prompt import _strip_workflow_nudge


@dataclass(frozen=True)
class ProjectMemoryConsolidationResult:
    written: dict[str, int]
    failed: dict[str, dict[str, object]]


def _messages_to_memory_transcript(
    messages: list[dict[str, Any]],
    final_content: str,
    *,
    max_chars: int,
) -> str:
    lines: list[str] = []
    total = 0
    for message in messages:
        rendered = _render_memory_transcript_message(message)
        if not rendered:
            continue
        remaining = max_chars - total
        if remaining <= 0:
            break
        if len(rendered) > remaining:
            rendered = rendered[: max(0, remaining - 14)] + "...<truncated>"
        lines.append(rendered)
        total += len(rendered) + 1
    if final_content.strip() and not _last_assistant_content_is(messages, final_content):
        rendered = f"final: {_one_line(final_content, max_chars=1200)}"
        remaining = max_chars - total
        if remaining > 0:
            if len(rendered) > remaining:
                rendered = rendered[: max(0, remaining - 14)] + "...<truncated>"
            lines.append(rendered)
            total += len(rendered) + 1
    if total >= max_chars:
        lines.append("...<transcript truncated for memory consolidation>")
    return "\n".join(lines)


def _render_memory_transcript_message(message: dict[str, Any]) -> str:
    role = str(message.get("role") or "unknown")
    if role == "system":
        return ""
    if role == "user":
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            return ""
        return f"user: {_one_line(_strip_workflow_nudge(content), max_chars=1200)}"
    if role == "assistant":
        tool_names = _assistant_tool_call_names(message)
        content = message.get("content")
        if tool_names:
            prefix = f"assistant tool_calls: {', '.join(tool_names)}"
            if isinstance(content, str) and content.strip():
                return f"{prefix}; note: {_one_line(content, max_chars=600)}"
            return prefix
        if isinstance(content, str) and content.strip():
            return f"assistant: {_one_line(content, max_chars=1200)}"
        return ""
    if role == "tool":
        name = str(message.get("_lca_tool_name") or "tool")
        error = " error" if message.get("_lca_is_error") is True else ""
        content = message.get("content")
        return f"{name}{error}: {_one_line(str(content or ''), max_chars=1200)}"
    content = message.get("content")
    if content is None:
        return ""
    return f"{role}: {_one_line(str(content), max_chars=1200)}"


def _assistant_tool_call_names(message: dict[str, Any]) -> list[str]:
    names: list[str] = []
    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        return names
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function") or {}
        if isinstance(function, dict) and function.get("name"):
            names.append(str(function["name"]))
    return names


def _last_assistant_content_is(messages: list[dict[str, Any]], final_content: str) -> bool:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        return isinstance(content, str) and content == final_content
    return False


def _run_used_memory_write_tool(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        if message.get("role") != "assistant":
            continue
        if any(name in MEMORY_CONSOLIDATION_WRITE_TOOLS for name in _assistant_tool_call_names(message)):
            return True
    return False


def _should_auto_consolidate_memory(
    transcript: str,
    messages: list[dict[str, Any]],
    final_content: str,
) -> bool:
    lowered = f"{transcript}\n{final_content}".lower()
    durable_keywords = {
        "always",
        "convention",
        "decision",
        "learn",
        "lesson",
        "memory",
        "prefer",
        "remember",
        "以后",
        "偏好",
        "决策",
        "学到",
        "惯例",
        "经验",
        "记住",
        "约定",
    }
    if any(keyword in lowered for keyword in durable_keywords):
        return True
    if len(transcript) < MEMORY_CONSOLIDATION_MIN_AUTO_CHARS:
        return False
    has_tool_result = any(message.get("role") == "tool" for message in messages)
    if has_tool_result:
        return True
    return len(final_content.strip()) >= MEMORY_CONSOLIDATION_MIN_AUTO_CHARS


def _parse_memory_consolidation_response(content: str) -> dict[str, list[str]] | None:
    raw = _extract_json_object_text(content)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    parsed: dict[str, list[str]] = {}
    for bucket in MEMORY_CONSOLIDATION_BUCKETS:
        value = data.get(bucket, [])
        if value is None:
            value = []
        if not isinstance(value, list):
            return None
        items: list[str] = []
        seen: set[str] = set()
        for raw_item in value:
            if not isinstance(raw_item, str):
                continue
            item = _clean_consolidated_memory_item(raw_item)
            if not item:
                continue
            key = _normalized_memory_item_key(item)
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            if len(items) >= MEMORY_CONSOLIDATION_MAX_ITEMS_PER_BUCKET:
                break
        parsed[bucket] = items
    return parsed


def _extract_json_object_text(content: str) -> str | None:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        return None
    return stripped[start : end + 1]


def _clean_consolidated_memory_item(item: str) -> str:
    cleaned = " ".join(item.replace("\x00", "").split())
    if len(cleaned) > MEMORY_CONSOLIDATION_MAX_ITEM_CHARS:
        cleaned = cleaned[: MEMORY_CONSOLIDATION_MAX_ITEM_CHARS - 14].rstrip() + "...<truncated>"
    return cleaned


def _memory_consolidation_root(workspace: Path, state_dir: Path, scope: str) -> Path:
    if scope == "project":
        return workspace / ".local-agent" / "memory"
    return state_dir / "memory"


def _append_consolidated_memory(
    memory_dir: Path,
    session_id: str,
    items_by_bucket: dict[str, list[str]],
) -> dict[str, int]:
    written: dict[str, int] = {}
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    for bucket in MEMORY_CONSOLIDATION_BUCKETS:
        items = items_by_bucket.get(bucket) or []
        if not items:
            continue
        path = memory_dir / f"{bucket}.md"
        existing = ""
        if path.exists():
            try:
                existing = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                existing = ""
        pending: list[tuple[str, str]] = []
        for item in items:
            digest = _memory_item_digest(bucket, item)
            if f"lca-memory:{digest}" in existing:
                continue
            pending.append((digest, item))
        if not pending:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## {stamp} - consolidated from session {session_id}\n\n")
            for digest, item in pending:
                handle.write(f"<!-- lca-memory:{digest} -->\n- {item}\n")
        written[bucket] = len(pending)
    return written


def _append_project_consolidated_memory(
    workspace: Path,
    session_id: str,
    items_by_bucket: dict[str, list[str]],
) -> ProjectMemoryConsolidationResult:
    written: dict[str, int] = {}
    failed: dict[str, dict[str, object]] = {}
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        store = ProjectMemoryStore(workspace)
    except ProjectMemoryStoreError as exc:
        return ProjectMemoryConsolidationResult(
            written={},
            failed={
                bucket: {
                    "reason": exc.kind,
                    "workspace_changed": exc.workspace_changed,
                }
                for bucket in MEMORY_CONSOLIDATION_BUCKETS
                if items_by_bucket.get(bucket)
            },
        )
    for bucket in MEMORY_CONSOLIDATION_BUCKETS:
        items = items_by_bucket.get(bucket) or []
        if not items:
            continue
        try:
            document = store.read(bucket, allow_custom=False)
        except ProjectMemoryStoreError as exc:
            failed[bucket] = {
                "reason": exc.kind,
                "workspace_changed": exc.workspace_changed,
            }
            continue
        existing = document.text if document is not None else ""
        pending: list[tuple[str, str]] = []
        for item in items:
            digest = _memory_item_digest(bucket, item)
            if f"lca-memory:{digest}" in existing:
                continue
            pending.append((digest, item))
        if not pending:
            continue
        payload = f"\n## {stamp} - consolidated from session {session_id}\n\n"
        payload += "".join(
            f"<!-- lca-memory:{digest} -->\n- {item}\n"
            for digest, item in pending
        )
        try:
            store.append(bucket, payload)
        except ProjectMemoryStoreError as exc:
            failed[bucket] = {
                "reason": exc.kind,
                "workspace_changed": exc.workspace_changed,
            }
            continue
        written[bucket] = len(pending)
    return ProjectMemoryConsolidationResult(written=written, failed=failed)


def _memory_item_digest(bucket: str, item: str) -> str:
    payload = f"{bucket}\0{_normalized_memory_item_key(item)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _normalized_memory_item_key(item: str) -> str:
    return " ".join(item.casefold().split())

"""Tool gateway signatures, path normalization, and runtime metadata helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .llm import LlmError, LlmTimeoutError
from .patch.anchored import PatchError, display_workspace_path, resolve_workspace_path
from .tool_choice_queue import ToolChoiceDecision
from .tools.base import ToolResult
from .runtime_prompt import _one_line
from .runtime_prompt import _parse_tool_arguments

def _tool_call_signature(name: str, arguments: str | dict[str, Any]) -> str:
    if isinstance(arguments, dict):
        normalized_arguments: Any = arguments
    else:
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            parsed = arguments
        normalized_arguments = parsed
    payload = {
        "name": name,
        "arguments": normalized_arguments,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _intersect_optional_tool_allowlist(
    current: set[str] | None,
    next_allowed: set[str] | frozenset[str],
) -> set[str]:
    allowed = set(next_allowed)
    if current is None:
        return allowed
    return current.intersection(allowed)


def _tool_choice_steering_signature(decision: ToolChoiceDecision, result_count: int) -> str:
    payload = {
        "rule_id": decision.rule_id,
        "missing": decision.missing_requirements,
        "results": result_count,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _tool_choice_signature_count(signatures: set[str], rule_id: str | None) -> int:
    prefix = f'"rule_id": "{rule_id}"' if rule_id else '"rule_id": null'
    return sum(1 for signature in signatures if prefix in signature)


def _tool_choice_steering_message(decision: ToolChoiceDecision, current_user_request: str | None) -> str:
    allowed = ", ".join(sorted(decision.allowed_tool_names)) or "(no tools currently allowed)"
    preferred = ", ".join(decision.preferred_tool_names) or "(none)"
    missing = ", ".join(decision.missing_requirements) or "(none)"
    hints = "\n".join(f"- call hint: {hint}" for hint in decision.tool_call_hints)
    request = _one_line(current_user_request or "", max_chars=800)
    if decision.force_final_answer_without_tools:
        return (
            "[Runtime tool choice queue]\n"
            "The bounded exploration budget is exhausted. Your next response must be the final answer without tool calls. "
            "Use only collected evidence, include searched scope and incomplete/truncated limits, and do not infer absence "
            "from omitted results.\n"
            f"- rule: {decision.rule_id or 'unknown'}\n"
            f"- reason: {decision.reason}\n"
            f"- original request: {request}"
        )
    return (
        "[Runtime tool choice queue]\n"
        "A required workflow gate is not satisfied yet. Use the allowed tool set for the next step; "
        "do not answer as final until the missing requirement is satisfied or you can explicitly explain why it cannot be satisfied.\n"
        f"- rule: {decision.rule_id or 'unknown'}\n"
        f"- missing: {missing}\n"
        f"- preferred next tools: {preferred}\n"
        f"- allowed tools now: {allowed}\n"
        f"- reason: {decision.reason}\n"
        f"{hints + chr(10) if hints else ''}"
        f"- original request: {request}"
    )


def _tool_choice_result_path(arguments: str | dict[str, Any], result: ToolResult) -> str | None:
    parsed: Any = arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    path = parsed.get("path")
    if path is not None:
        return str(path)
    changed_path = result.metadata.get("changed_path") if isinstance(result.metadata, Mapping) else None
    return str(changed_path) if isinstance(changed_path, str) and changed_path.strip() else None


def _tool_call_uses_dry_run(arguments: str | dict[str, Any]) -> bool:
    if isinstance(arguments, dict):
        return bool(arguments.get("dry_run"))
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return False
    return bool(parsed.get("dry_run")) if isinstance(parsed, dict) else False


def _source_evidence_matches_path(
    display_path: str,
    resolved_path: object,
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
) -> bool:
    if not isinstance(resolved_path, str) or not resolved_path:
        return False
    try:
        return resolve_workspace_path(workspace, display_path, allowed_dirs) == Path(resolved_path).resolve()
    except (PatchError, OSError):
        return False


def _request_requires_patch_preview(request: str | None) -> bool:
    lowered = (request or "").lower()
    if any(marker in lowered for marker in {"skip preview", "skip dry_run", "跳过预览", "无需预览"}):
        return False
    if "dry_run" in lowered or "dry run" in lowered:
        return True
    preview_markers = {"必须预览", "先预览", "预览后", "预览 diff", "预览补丁", "patch preview"}
    return any(marker in lowered for marker in preview_markers)


def _patch_preview_signature(args: dict[str, Any], resolved_path: Path) -> str:
    payload = {
        "path": str(resolved_path),
        "tag": args.get("tag"),
        "start_line": args.get("start_line"),
        "end_line": args.get("end_line"),
        "old_text": args.get("old_text"),
        "new_text": args.get("new_text"),
        "mode": args.get("mode") or "replace",
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _search_pattern_key(name: str, arguments: str | dict[str, Any]) -> str | None:
    if name != "search_code":
        return None
    if isinstance(arguments, dict):
        parsed: Any = arguments
    else:
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    pattern = parsed.get("pattern")
    if not isinstance(pattern, str):
        return None
    normalized = " ".join(pattern.strip().lower().split())
    return normalized or None


def _lsp_symbol_query_key(name: str, arguments: str | dict[str, Any]) -> str | None:
    if name not in {"lsp_symbols", "lsp_workspace_symbols", "lsp_document_symbols"}:
        return None
    if isinstance(arguments, dict):
        parsed: Any = arguments
    else:
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    query = parsed.get("query")
    if not isinstance(query, str):
        return None
    normalized = " ".join(query.strip().lower().split())
    return normalized or None


def _semantic_exploration_key(
    name: str,
    arguments: str | dict[str, Any],
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
) -> str | None:
    if name != "list_files":
        return None
    parsed = _parse_tool_arguments(arguments)
    raw_path = str(parsed.get("path") or ".").strip() or "."
    if raw_path in {"", "."}:
        return None
    try:
        path = resolve_workspace_path(workspace, raw_path, allowed_dirs)
    except PatchError:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = workspace / path
    return _semantic_directory_key(path, workspace, allowed_dirs)


def _semantic_directory_key(path: Path, workspace: Path, allowed_dirs: tuple[Path, ...]) -> str | None:
    parts = _path_parts_relative_to_known_root(path, (workspace, *allowed_dirs))
    if not parts:
        return None
    if "src" in parts:
        src_index = parts.index("src")
        if src_index > 0:
            parts = parts[:src_index]
    elif len(parts) > 2:
        parts = parts[:2]
    key_parts = [part for part in parts[:3] if part not in {"", ".", "/"}]
    if not key_parts:
        return None
    return "/".join(key_parts)


def _path_parts_relative_to_known_root(path: Path, roots: tuple[Path, ...]) -> list[str]:
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path.absolute()
    best_relative: Path | None = None
    best_depth = -1
    for root in roots:
        try:
            resolved_root = root.resolve(strict=False)
            relative = resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        depth = len(resolved_root.parts)
        if depth > best_depth:
            best_relative = relative
            best_depth = depth
    candidate = best_relative if best_relative is not None else resolved
    return [part for part in candidate.parts if part not in {"", ".", candidate.anchor}]


def _read_file_path_key(
    name: str,
    arguments: str | dict[str, Any],
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
) -> str | None:
    if name != "read_file":
        return None
    if isinstance(arguments, dict):
        parsed: Any = arguments
    else:
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    raw_path = parsed.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    try:
        path = resolve_workspace_path(workspace, raw_path, allowed_dirs)
    except PatchError:
        return raw_path
    return str(path)


def _read_file_range_key(
    name: str,
    arguments: str | dict[str, Any],
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
) -> tuple[str, int, str] | None:
    if name != "read_file":
        return None
    parsed = _parse_tool_arguments(arguments)
    raw_path = parsed.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    start_line = _read_file_line_number(parsed.get("start_line"), default=1)
    if start_line is None:
        return None
    end_value = parsed.get("end_line")
    if end_value is None:
        end_key = "default"
    else:
        end_line = _read_file_line_number(end_value, default=1)
        if end_line is None or end_line < start_line:
            return None
        end_key = str(end_line)
    try:
        path = resolve_workspace_path(workspace, raw_path, allowed_dirs)
    except PatchError:
        path_key = raw_path
    else:
        path_key = str(path)
    return (path_key, start_line, end_key)


def _read_file_line_number(value: object, *, default: int) -> int | None:
    if value is None:
        return default
    try:
        line_number = int(value)
    except (TypeError, ValueError):
        return None
    return line_number if line_number >= 1 else None


def _display_read_file_range_key(
    range_key: tuple[str, int, str],
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
) -> str:
    subject = _display_read_file_range_subject(range_key, workspace, allowed_dirs)
    _, start_line, end_key = range_key
    if end_key == "default":
        return f"{subject} from line {start_line}"
    return f"{subject} lines {start_line}-{end_key}"


def _display_read_file_range_subject(
    range_key: tuple[str, int, str],
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
) -> str:
    path_key = range_key[0]
    try:
        return display_workspace_path(workspace, Path(path_key), allowed_dirs)
    except (OSError, RuntimeError, ValueError):
        return path_key


def _llm_failure_reason(error: LlmError) -> str:
    if isinstance(error, LlmTimeoutError):
        return "llm_timeout"
    return "provider_error"


def _validate_runtime_tool_name(tool: str) -> str:
    normalized = tool.strip()
    if not normalized or not all(char.isalnum() or char == "_" for char in normalized):
        raise ValueError(f"invalid tool name: {tool}")
    return normalized

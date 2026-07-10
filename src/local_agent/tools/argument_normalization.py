from __future__ import annotations

from typing import Any


def normalize_compatibility_arguments(name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Normalize only observed provider argument variants before schema validation.

    This is deliberately a narrow protocol boundary. It must not infer arbitrary
    field names or weaken a tool's declared schema, permissions, or safety checks.
    """

    normalized = dict(arguments)
    notes: list[str] = []

    if name == "apply_patch":
        _rename_alias(normalized, "file_hash", "tag", notes)
        _rename_alias(normalized, "file_hash_tag", "tag", notes)
        _rename_alias(normalized, "source_hash_tag", "tag", notes)
        _rename_alias(normalized, "hash_tag", "tag", notes)
        _rename_alias(normalized, "old_str", "old_text", notes)
        _rename_alias(normalized, "new_str", "new_text", notes)
        _normalize_patch_mode(normalized, notes)
        _normalize_patch_line_numbers(normalized, notes)
        _normalize_dry_run_boolean(normalized, notes)
    elif name == "run_tests":
        _rename_alias(normalized, "cmd", "command", notes)
    elif name in {"todo_add", "todo_update"}:
        _rename_alias(normalized, "key", "id", notes)
        _rename_alias(normalized, "content", "task", notes)
        _normalize_todo_status(normalized, notes)

    return normalized, tuple(notes)


def _rename_alias(arguments: dict[str, Any], alias: str, canonical: str, notes: list[str]) -> None:
    if alias not in arguments:
        return
    alias_value = arguments.pop(alias)
    if canonical in arguments:
        if arguments[canonical] != alias_value:
            raise ValueError(f"Conflicting compatibility arguments: {alias} and {canonical} differ.")
        notes.append(f"ignored redundant {alias}; using {canonical}")
        return
    arguments[canonical] = alias_value
    notes.append(f"{alias} -> {canonical}")


def _normalize_patch_mode(arguments: dict[str, Any], notes: list[str]) -> None:
    if arguments.get("mode") == "edit":
        arguments["mode"] = "replace"
        notes.append("mode edit -> replace")


def _normalize_patch_line_numbers(arguments: dict[str, Any], notes: list[str]) -> None:
    for field in ("start_line", "end_line"):
        value = arguments.get(field)
        if not isinstance(value, str) or not value.strip().lstrip("-").isdigit():
            continue
        arguments[field] = int(value.strip())
        notes.append(f"{field} string -> integer")


def _normalize_dry_run_boolean(arguments: dict[str, Any], notes: list[str]) -> None:
    value = arguments.get("dry_run")
    if not isinstance(value, str):
        return
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        return
    arguments["dry_run"] = normalized == "true"
    notes.append(f"dry_run string -> boolean ({normalized})")


def _normalize_todo_status(arguments: dict[str, Any], notes: list[str]) -> None:
    if arguments.get("status") == "pending":
        arguments["status"] = "todo"
        notes.append("status pending -> todo")

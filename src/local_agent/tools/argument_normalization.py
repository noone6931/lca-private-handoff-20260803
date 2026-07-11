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
    elif name == "glob_files":
        _normalize_glob_pattern(normalized, notes)
        _normalize_glob_path_scope(normalized, notes)
        _normalize_glob_paths(normalized, notes)
        _normalize_bounded_integer(normalized, "limit", notes)
    elif name == "search_code":
        _rename_alias(normalized, "maxResults", "max_results", notes)
        _normalize_bounded_integer(normalized, "max_results", notes)
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


def _normalize_bounded_integer(arguments: dict[str, Any], field: str, notes: list[str]) -> None:
    value = arguments.get(field)
    if not isinstance(value, str) or not value.strip().isdigit():
        return
    arguments[field] = int(value.strip())
    notes.append(f"{field} string -> integer")


def _normalize_glob_pattern(arguments: dict[str, Any], notes: list[str]) -> None:
    """Accept OMP-style ``path`` + ``pattern`` without exposing a second schema."""

    pattern = arguments.pop("pattern", None)
    if pattern is None:
        return
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("glob_files compatibility pattern must be a non-empty string.")
    paths = arguments.get("paths")
    canonical_paths = [pattern]
    if paths is None:
        arguments["paths"] = canonical_paths
        notes.append("pattern -> paths[0]")
        return
    if paths != canonical_paths:
        raise ValueError("Conflicting compatibility arguments: pattern and paths differ.")
    notes.append("ignored redundant pattern; using paths")


def _normalize_glob_path_scope(arguments: dict[str, Any], notes: list[str]) -> None:
    """Accept the observed provider ``path`` scope without exposing a second schema."""

    raw_scope = arguments.pop("path", None)
    if not isinstance(raw_scope, str) or not raw_scope.strip():
        return
    paths = arguments.get("paths")
    if paths is None:
        arguments["paths"] = [raw_scope]
        notes.append("path -> paths[0]")
        return
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        return
    scoped_paths: list[str] = []
    for path in paths:
        if path.startswith("/") or path.startswith("~"):
            scoped_paths.append(path)
        else:
            scoped_paths.append(f"{raw_scope.rstrip('/')}/{path}")
    arguments["paths"] = scoped_paths
    notes.append("path scope applied to relative paths")


def _normalize_glob_paths(arguments: dict[str, Any], notes: list[str]) -> None:
    """Drop accidental blank siblings, but never guess a missing discovery scope."""

    paths = arguments.get("paths")
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        return
    non_empty = [path.strip() for path in paths if path.strip()]
    if not non_empty:
        raise ValueError(
            "glob_files paths must contain a non-empty authorized path or pattern; use the workspace roots from runtime context."
        )
    if len(non_empty) != len(paths):
        arguments["paths"] = non_empty
        notes.append("removed empty paths entries")

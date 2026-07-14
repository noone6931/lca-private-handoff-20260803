"""Bounded inventory glob contract shared by queue hints and tool validation."""
from __future__ import annotations

import glob
import json
from pathlib import Path
from typing import Any, Iterable


INVENTORY_GLOB_MARKERS = (
    "pom.xml",
    "package.json",
    "pyproject.toml",
    "build.gradle",
    "build.gradle.kts",
    "go.mod",
    "Cargo.toml",
    "src/**/*.*",
    "app/**/*.*",
    "lib/**/*.*",
    "routes/**/*.*",
    "templates/**/*.*",
)
MAX_INVENTORY_GLOB_PATHS = 32


def inventory_glob_patterns_for_roots(roots: Iterable[str]) -> list[str]:
    cleaned_roots = _clean_roots(roots)
    if not cleaned_roots:
        return []
    per_root = max(1, MAX_INVENTORY_GLOB_PATHS // len(cleaned_roots))
    markers = INVENTORY_GLOB_MARKERS[:per_root]
    patterns: list[str] = []
    for cleaned in cleaned_roots[:MAX_INVENTORY_GLOB_PATHS]:
        patterns.extend(f"{cleaned}/**/{marker}" for marker in markers)
    return patterns[:MAX_INVENTORY_GLOB_PATHS]


def inventory_glob_arguments_for_roots(roots: Iterable[str]) -> dict[str, Any] | None:
    """Return the bounded canonical call shape for one inventory directive."""

    cleaned_roots = _clean_roots(roots)
    if not cleaned_roots or len(cleaned_roots) > MAX_INVENTORY_GLOB_PATHS:
        return None
    return {
        "paths": inventory_glob_patterns_for_roots(cleaned_roots),
        "limit": 200,
        "hidden": False,
        "gitignore": True,
    }


def inventory_glob_call_hint(roots: Iterable[str]) -> str:
    cleaned_roots = _clean_roots(roots)
    arguments = inventory_glob_arguments_for_roots(cleaned_roots)
    if arguments is None:
        if not cleaned_roots:
            return "No workspace root is available for the current glob_files inventory contract."
        return (
            f"Too many required roots for one glob_files call ({len(cleaned_roots)} > "
            f"{MAX_INVENTORY_GLOB_PATHS}). The current single-call inventory contract cannot represent every "
            "active root within the glob_files schema; do not emit a glob_files call for this directive. "
            "Stop and report that bounded inventory discovery needs a smaller active-root batch."
        )
    return (
        "Use this bounded inventory discovery call exactly (do not send an empty paths entry or bare directory): "
        f"glob_files({json.dumps(arguments, ensure_ascii=False)})"
    )


def glob_inventory_denial_reason(
    arguments: dict[str, Any],
    *,
    required_roots: Iterable[str],
    workspace: Path,
) -> str | None:
    """Reject required runtime inventory globs that are broad or miss roots."""

    raw_paths = arguments.get("paths")
    if not isinstance(raw_paths, list) or not all(isinstance(path, str) for path in raw_paths):
        return None
    roots = tuple(str(Path(root).resolve()) for root in required_roots if str(root).strip())
    if len(roots) > MAX_INVENTORY_GLOB_PATHS:
        return (
            f"Runtime workspace inventory restriction: {len(roots)} required roots cannot fit in one "
            f"glob_files call with max {MAX_INVENTORY_GLOB_PATHS} paths. The current directive cannot proceed "
            "as a single runtime inventory call; reduce the active root set before retrying."
        )
    missing = [
        root
        for root in roots
        if not any(_path_is_bounded_inventory_pattern(raw_path, root, workspace) for raw_path in raw_paths)
    ]
    if not missing:
        return None
    rendered = ", ".join(missing)
    return (
        "Runtime workspace inventory restriction: glob_files must use bounded manifest/source patterns for each "
        f"currently uncovered workspace root. Missing bounded root scopes: {rendered}. "
        "Use root-prefixed manifest/source patterns, not a bare directory or recursive all-files glob."
    )


def _clean_roots(roots: Iterable[str]) -> tuple[str, ...]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for root in roots:
        value = str(root).rstrip("/")
        if not value or value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    return tuple(cleaned)


def _path_is_bounded_inventory_pattern(raw_path: str, root: str, workspace: Path) -> bool:
    value = str(raw_path).strip()
    if not value:
        return False
    resolved_root = str(Path(root).resolve())
    prefix = _literal_prefix(value)
    if not prefix:
        if Path(root).resolve() != workspace.resolve():
            return False
        prefix_path = str(workspace.resolve())
        pattern_text = value
    else:
        prefix_path = _resolve_prefix(prefix, workspace)
        pattern_text = value[len(prefix) :].lstrip("/")
    if prefix_path != resolved_root and not prefix_path.startswith(resolved_root + "/"):
        return False
    if _is_bare_recursive_pattern(prefix_path, pattern_text, resolved_root):
        return False
    return _is_bounded_selector_pattern(value, prefix_path, pattern_text)


def _literal_prefix(value: str) -> str:
    parts = Path(value).parts
    if not parts:
        return ""
    prefix_parts: list[str] = []
    for part in parts:
        if glob.has_magic(part):
            break
        prefix_parts.append(part)
    if not prefix_parts:
        return ""
    return str(Path(*prefix_parts))


def _resolve_prefix(prefix: str, workspace: Path) -> str:
    path = Path(prefix).expanduser()
    if not path.is_absolute():
        path = workspace / path
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _is_bare_recursive_pattern(prefix_path: str, pattern_text: str, root: str) -> bool:
    normalized = pattern_text.strip().strip("/")
    if prefix_path == root and normalized in {"", "*", "**", "**/*", "**/**"}:
        return True
    return normalized in {"**/*", "**/**", "**", "*"}


def _is_bounded_selector_pattern(value: str, prefix_path: str, pattern_text: str) -> bool:
    """Return true for root-scoped selectors narrower than an all-tree scan."""

    if not glob.has_magic(value):
        path = Path(prefix_path)
        if path.exists() and path.is_dir():
            return False
        return bool(path.name and path.name not in {".", ".."})

    selector = _last_selector_segment(pattern_text)
    if selector is None:
        return False
    return _selector_has_literal_filter(selector)


def _last_selector_segment(pattern_text: str) -> str | None:
    parts = [part for part in Path(pattern_text.strip().strip("/")).parts if part not in {"", "**"}]
    if not parts:
        return None
    return parts[-1]


def _selector_has_literal_filter(selector: str) -> bool:
    if selector in {"*", "?", "[!]*", "[^]*"}:
        return False
    literal = "".join(ch for ch in selector if ch.isalnum() or ch in {"_", "-", "."})
    if literal.strip(".-_"):
        return True
    return "." in selector and any(ch in selector for ch in {"*", "?", "["})

"""Bounded inventory glob hints for ToolChoiceQueue discovery directives."""
from __future__ import annotations

import json
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

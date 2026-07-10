from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


_DESIGN_PROMPT_MARKERS = ("design", "architecture", "方案", "设计", "架构")
_CODE_ROOT_MARKERS = (
    "pom.xml",
    "package.json",
    "pyproject.toml",
    "build.gradle",
    "build.gradle.kts",
    "src",
)
_CODE_SOURCE_SUFFIXES = frozenset(
    {
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".py",
        ".ts",
        ".tsx",
        ".vue",
        ".go",
        ".rs",
        ".cs",
    }
)


def cross_root_design_evidence_roots(
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
    prompt: str,
) -> tuple[str, ...]:
    """Return code roots that must each contribute a source read for a cross-root design task."""
    lowered = (prompt or "").lower()
    if not any(marker in lowered for marker in _DESIGN_PROMPT_MARKERS):
        return ()
    roots: list[str] = []
    for candidate in (workspace, *allowed_dirs):
        resolved = candidate.resolve()
        rendered = str(resolved)
        if rendered not in roots and _looks_like_code_root(resolved):
            roots.append(rendered)
    return tuple(roots) if len(roots) >= 2 else ()


def missing_design_evidence_roots(
    roots: Iterable[str],
    read_paths: Iterable[str | None],
) -> tuple[str, ...]:
    source_paths = [path for path in read_paths if isinstance(path, str) and _is_code_source_path(path)]
    missing: list[str] = []
    for root in roots:
        if not any(_path_is_within(path, root) for path in source_paths):
            missing.append(str(root))
    return tuple(missing)


def _looks_like_code_root(path: Path) -> bool:
    return path.is_dir() and any((path / marker).exists() for marker in _CODE_ROOT_MARKERS)


def _is_code_source_path(path: str) -> bool:
    return Path(path).suffix.lower() in _CODE_SOURCE_SUFFIXES


def _path_is_within(path: str, root: str) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True

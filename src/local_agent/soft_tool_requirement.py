from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .patch.anchored import PatchError
from .patch.anchored import resolve_workspace_path
from .workspace.startup import iter_authored_skill_files
from .workspace.startup import read_skill_metadata
from .tools.base import ToolResult


MAX_SOFT_TOOL_REQUIREMENT_STEERS = 3
ALLOWED_DIR_REQUIREMENT_KEYWORDS = {
    "requirement",
    "requirements",
    "spec",
    "specs",
    "prd",
    "需求",
    "需求目录",
    "需求文档",
    "读取需求",
    "外部需求",
}
ALLOWED_DIR_DOC_SUFFIXES = {".md", ".txt", ".rst", ".html", ".htm"}
ALLOWED_DIR_DOC_NAME_KEYWORDS = {
    "requirement",
    "requirements",
    "spec",
    "prd",
    "handoff",
    "需求",
    "文档",
    "说明",
    "方案",
}
MAX_ALLOWED_DIR_DOC_CANDIDATES = 8


@dataclass
class SoftToolRequirement:
    kind: str
    allowed_dirs: tuple[Path, ...]
    candidate_files: tuple[Path, ...] = ()
    steers: int = 0
    satisfied: bool = False


def initial_soft_tool_requirement(
    prompt: str,
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
    *,
    max_skill_description_chars: int,
) -> SoftToolRequirement | None:
    skill_file = _mentioned_authored_skill_file(
        prompt,
        workspace,
        max_skill_description_chars=max_skill_description_chars,
    )
    if skill_file is not None:
        return SoftToolRequirement(kind="authored_skill", allowed_dirs=(), candidate_files=(skill_file,))
    if not allowed_dirs or not any(keyword in prompt.lower() for keyword in ALLOWED_DIR_REQUIREMENT_KEYWORDS):
        return None
    if _has_direct_requirement_document(workspace):
        # The primary workspace is already the supplied requirement package.
        # Additional roots are code roots in this workflow, not a second
        # mandatory document source.
        return None
    candidates = allowed_dir_requirement_doc_candidates(allowed_dirs)
    if not candidates:
        return None
    return SoftToolRequirement(
        kind="allowed_dir_requirements",
        allowed_dirs=allowed_dirs,
        candidate_files=candidates,
    )


def soft_tool_requirement_message(requirement: SoftToolRequirement) -> str:
    if requirement.kind == "authored_skill":
        return "\n".join(
            [
                "[Runtime tool requirement]",
                "This task explicitly references a project-authored skill. Read the skill instructions before applying it.",
                "Use only read_file until this requirement is satisfied.",
                "",
                "Required skill file:",
                *[f"- {path}" for path in requirement.candidate_files],
                "",
                "Required next evidence: call read_file on the relevant SKILL.md file above. "
                "Do not answer from skill metadata alone.",
            ]
        )
    lines = [
        "[Runtime tool requirement]",
        "This task explicitly references external requirements/spec documents. "
        "Before searching or concluding from the primary code workspace, read evidence from an allowed directory.",
        "Use only list_files/read_file until this requirement is satisfied.",
        "",
        "Allowed directories:",
        *[f"- {path}" for path in requirement.allowed_dirs],
    ]
    if requirement.candidate_files:
        lines.extend(
            [
                "",
                "Candidate requirement/spec files; prefer read_file on the most relevant ones first:",
                *[f"- {path}" for path in requirement.candidate_files],
            ]
        )
    lines.extend(
        [
            "",
            "Required next evidence: call read_file with a path under one of the allowed directories. "
            "Do not answer or search the primary code workspace until at least one allowed-directory document has been read.",
        ]
    )
    return "\n".join(lines)


def advance_soft_tool_requirement(requirement: SoftToolRequirement) -> bool:
    if requirement.satisfied or requirement.steers >= MAX_SOFT_TOOL_REQUIREMENT_STEERS:
        return False
    requirement.steers += 1
    return True


def soft_tool_requirement_stop_message(requirement: SoftToolRequirement | None) -> str:
    if requirement is None:
        return "Stopped because a required tool step was not completed."
    if requirement.kind == "authored_skill":
        return (
            "Stopped because the task explicitly referenced a project skill, but the assistant did not "
            "read that skill's SKILL.md after repeated reminders. Retry or explicitly ask it to read the "
            "skill file first."
        )
    return (
        "Stopped because the task required reading requirement/spec documents from an allowed directory, "
        "but the assistant did not call read_file on any allowed-directory document after repeated reminders. "
        "Retry with the same --allow-dir, or explicitly name the requirement file path."
    )


def observe_soft_tool_requirement(
    requirement: SoftToolRequirement | None,
    *,
    name: str,
    arguments: str | dict[str, object],
    result: ToolResult,
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
) -> Path | None:
    if requirement is None or requirement.satisfied or result.is_error or name != "read_file":
        return None
    arguments_dict = _parse_arguments(arguments)
    raw_path = arguments_dict.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    try:
        path = resolve_workspace_path(workspace, raw_path, allowed_dirs)
    except PatchError:
        return None
    if not _path_satisfies(requirement, path):
        return None
    requirement.satisfied = True
    return path


def _mentioned_authored_skill_file(
    prompt: str,
    workspace: Path,
    *,
    max_skill_description_chars: int,
) -> Path | None:
    lowered = prompt.lower()
    skills_dir = workspace / ".local-agent" / "skills"
    for skill_file in iter_authored_skill_files(skills_dir):
        metadata = read_skill_metadata(
            workspace,
            skill_file,
            max_description_chars=max_skill_description_chars,
        )
        if metadata is None or metadata.get("hide"):
            continue
        names = {str(metadata["name"]).lower(), skill_file.parent.name.lower()}
        if any(name and name in lowered for name in names):
            return skill_file
    return None


def allowed_dir_doc_candidates(allowed_dirs: tuple[Path, ...]) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for root in allowed_dirs:
        if not root.exists() or not root.is_dir():
            continue
        candidates.extend(
            path for path in _iter_allowed_dir_files(root) if path.suffix.lower() in ALLOWED_DIR_DOC_SUFFIXES
        )
    candidates.sort(key=_allowed_dir_doc_sort_key)
    return tuple(candidates[:MAX_ALLOWED_DIR_DOC_CANDIDATES])


def allowed_dir_requirement_doc_candidates(allowed_dirs: tuple[Path, ...]) -> tuple[Path, ...]:
    """Return clearly named requirement/spec files, never generic code-root docs."""

    return tuple(
        path
        for path in allowed_dir_doc_candidates(allowed_dirs)
        if any(keyword in path.name.lower() for keyword in ALLOWED_DIR_DOC_NAME_KEYWORDS)
    )


def _has_direct_requirement_document(workspace: Path) -> bool:
    try:
        return any(
            child.is_file()
            and child.suffix.lower() in ALLOWED_DIR_DOC_SUFFIXES
            and any(keyword in child.name.lower() for keyword in ALLOWED_DIR_DOC_NAME_KEYWORDS)
            for child in workspace.iterdir()
        )
    except OSError:
        return False


def _iter_allowed_dir_files(root: Path):
    skipped = {".git", ".local-agent", ".venv", "__pycache__", "node_modules", "target", "dist", "build"}
    for child in sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if child.is_dir():
            if child.name in skipped or child.name.startswith("."):
                continue
            yield from _iter_allowed_dir_files(child)
        elif child.is_file():
            yield child


def _allowed_dir_doc_sort_key(path: Path) -> tuple[int, str]:
    lowered = path.name.lower()
    return (0 if any(keyword in lowered for keyword in ALLOWED_DIR_DOC_NAME_KEYWORDS) else 1, str(path).lower())


def _path_satisfies(requirement: SoftToolRequirement, path: Path) -> bool:
    if requirement.kind == "authored_skill":
        return any(_same_path(path, candidate) for candidate in requirement.candidate_files)
    if requirement.kind == "allowed_dir_requirements":
        return any(_is_under(path, root) for root in requirement.allowed_dirs)
    return False


def _same_path(path: Path, candidate: Path) -> bool:
    try:
        return path.resolve() == candidate.resolve()
    except OSError:
        return False


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _parse_arguments(arguments: str | dict[str, object]) -> dict[str, object]:
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = [
    "MAX_SOFT_TOOL_REQUIREMENT_STEERS",
    "SoftToolRequirement",
    "advance_soft_tool_requirement",
    "initial_soft_tool_requirement",
    "observe_soft_tool_requirement",
    "soft_tool_requirement_message",
    "soft_tool_requirement_stop_message",
]

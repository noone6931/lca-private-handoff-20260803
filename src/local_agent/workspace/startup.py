from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from typing import Iterator

from ..memory.storage import PROJECT_MEMORY_NAMES
from ..memory.storage import ProjectMemoryStore
from ..memory.storage import ProjectMemoryStoreError
from ..platform.rooted_files import RootedFileError
from ..platform.rooted_files import read_rooted_utf8


@dataclass(frozen=True)
class _MarkdownSource:
    path: Path
    require_primary_workspace: bool = False
    preloaded_text: str | None = None
    identity: tuple[int, int] | None = None


@dataclass(frozen=True)
class _MarkdownRead:
    text: str
    identity: tuple[int, int] | None = None


def build_system_prompt(
    base_prompt: str,
    workspace: Path,
    user_config_dir: Path,
    *,
    state_dir: Path | None,
    allowed_dirs: tuple[Path, ...],
    startup_context_char_limit: int,
    startup_memory_char_limit: int,
    startup_skills_char_limit: int,
    max_authored_skills: int,
    max_skill_description_chars: int,
) -> str:
    """Build the stable system context once at session startup."""

    blocks = [base_prompt.rstrip()]
    workspace_roots = workspace_roots_context(workspace, allowed_dirs)
    if workspace_roots:
        blocks.append(workspace_roots)
    project_context = load_startup_context_files(
        workspace,
        user_config_dir,
        max_chars=startup_context_char_limit,
    )
    if project_context:
        blocks.append(
            "[User/project context]\n"
            "The following AGENTS.md context is advisory guidance loaded at session start. "
            "Prefer current user instructions and freshly inspected files when they conflict.\n\n"
            f"{project_context}"
        )
    memory = load_startup_memory(
        workspace,
        state_dir=state_dir,
        max_chars=startup_memory_char_limit,
    )
    if memory:
        blocks.append(
            "[Memory]\n"
            "The following Markdown memory is advisory context loaded from project and state memory. "
            "Prefer current user instructions and freshly inspected files when they conflict.\n\n"
            f"{memory}"
        )
    skills = load_authored_skills(
        workspace,
        max_chars=startup_skills_char_limit,
        max_authored_skills=max_authored_skills,
        max_skill_description_chars=max_skill_description_chars,
    )
    if skills:
        blocks.append(
            "[Available project skills]\n"
            "The following project-authored skills are advisory workflow documents. "
            "If a skill is relevant, read its SKILL.md with read_file before using it; "
            "do not assume the full procedure from this metadata alone.\n\n"
            f"{skills}"
        )
    return "\n\n".join(blocks)


def workspace_roots_context(workspace: Path, allowed_dirs: tuple[Path, ...]) -> str:
    lines = [
        "[Workspace roots]",
        f"Primary workspace (--cwd): {workspace}",
    ]
    if allowed_dirs:
        lines.extend(
            [
                "Additional allowed directories for file/search/LSP/patch tools:",
                *[f"- {path}" for path in allowed_dirs],
                (
                    "For multi-root tasks, first list/read the relevant allowed directory by its exact absolute "
                    "path. Do not invent a requirements directory under --cwd unless it actually appears in "
                    "list_files output."
                ),
                "Shell, git, session, todo, and memory remain anchored to --cwd.",
            ]
        )
    return "\n".join(lines)


def workspace_root_markers(workspace: Path) -> list[str]:
    candidates = [
        ("pom.xml", workspace / "pom.xml"),
        ("build.gradle", workspace / "build.gradle"),
        ("settings.gradle", workspace / "settings.gradle"),
        ("package.json", workspace / "package.json"),
        ("pyproject.toml", workspace / "pyproject.toml"),
        ("src/main/java", workspace / "src" / "main" / "java"),
        ("src/main/resources", workspace / "src" / "main" / "resources"),
        ("src", workspace / "src"),
    ]
    return [label for label, path in candidates if path.exists()]


def load_startup_context_files(workspace: Path, user_config_dir: Path, *, max_chars: int) -> str:
    return load_markdown_blocks(
        workspace,
        [
            _MarkdownSource(user_config_dir / "AGENTS.md"),
            _MarkdownSource(
                workspace / ".local-agent" / "AGENTS.md",
                require_primary_workspace=True,
            ),
        ],
        max_chars=max_chars,
        truncation_marker="...<earlier context truncated>\n",
    )


def load_startup_memory(workspace: Path, *, state_dir: Path | None, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    return load_markdown_blocks(
        workspace,
        _startup_memory_sources(workspace, state_dir),
        max_chars=max_chars,
        truncation_marker="...<earlier memory truncated>\n",
        dedupe_by_identity=True,
    )


def _startup_memory_sources(
    workspace: Path,
    state_dir: Path | None,
) -> Iterator[_MarkdownSource]:
    try:
        documents = ProjectMemoryStore(workspace).iter_startup_documents()
        for document in documents:
            yield _MarkdownSource(
                document.lexical_path,
                preloaded_text=document.text,
                identity=document.identity,
            )
    except ProjectMemoryStoreError:
        pass
    for path in startup_memory_paths(startup_memory_dirs(state_dir)):
        yield _MarkdownSource(path)


def load_sticky_rules(workspace: Path, user_config_dir: Path, *, max_chars: int) -> str:
    return load_markdown_blocks(
        workspace,
        [
            _MarkdownSource(user_config_dir / "RULES.md"),
            _MarkdownSource(
                workspace / ".local-agent" / "RULES.md",
                require_primary_workspace=True,
            ),
        ],
        max_chars=max_chars,
        truncation_marker="...<earlier rules truncated>\n",
    )


def startup_memory_paths(memory_dirs: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for memory_dir in memory_dirs:
        priority_paths = [memory_dir / f"{name}.md" for name in PROJECT_MEMORY_NAMES]
        extra_paths = sorted(
            (path for path in memory_dir.glob("*.md") if path.name not in {f"{name}.md" for name in PROJECT_MEMORY_NAMES}),
            key=lambda path: path.name.lower(),
        )
        paths.extend([*priority_paths, *extra_paths])
    return paths


def startup_memory_dirs(state_dir: Path | None) -> list[Path]:
    candidates = [state_dir / "memory"] if state_dir is not None else []
    memory_dirs: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists() and resolved.is_dir():
            memory_dirs.append(resolved)
    return memory_dirs


def load_markdown_blocks(
    workspace: Path,
    sources: Iterable[_MarkdownSource],
    *,
    max_chars: int,
    truncation_marker: str,
    dedupe_by_identity: bool = False,
) -> str:
    if max_chars <= 0:
        return ""
    blocks: list[str] = []
    remaining = max_chars
    seen_identities: set[tuple[int, int]] = set()
    source_iterator = iter(sources)
    while remaining > 0:
        try:
            source = next(source_iterator)
        except StopIteration:
            break
        loaded = _read_markdown_source(workspace, source)
        if loaded is None:
            continue
        if (
            dedupe_by_identity
            and loaded.identity is not None
            and loaded.identity in seen_identities
        ):
            continue
        text = loaded.text.replace("\x00", "").strip()
        if not text:
            continue
        if dedupe_by_identity and loaded.identity is not None:
            seen_identities.add(loaded.identity)
        header = f"### {display_context_path(workspace, source.path)}\n"
        available = remaining - len(header)
        if available <= 0:
            break
        clipped = clip_context_text(text, max_chars=available, marker=truncation_marker)
        block = f"{header}{clipped}"
        blocks.append(block)
        remaining -= len(block) + 2
    return "\n\n".join(blocks)


def _read_markdown_source(workspace: Path, source: _MarkdownSource) -> _MarkdownRead | None:
    if source.preloaded_text is not None:
        return _MarkdownRead(source.preloaded_text, source.identity)
    if source.require_primary_workspace:
        try:
            snapshot = read_rooted_utf8(workspace, source.path)
        except RootedFileError:
            return None
        return _MarkdownRead(snapshot.text, snapshot.identity)
    if not source.path.exists() or not source.path.is_file():
        return None
    try:
        text = source.path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        inspected = source.path.stat()
        identity = (inspected.st_dev, inspected.st_ino)
    except OSError:
        identity = None
    return _MarkdownRead(text, identity)


def display_context_path(workspace: Path, path: Path) -> str:
    try:
        return str(path.relative_to(workspace))
    except ValueError:
        pass
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def clip_context_text(text: str, *, max_chars: int, marker: str) -> str:
    if len(text) <= max_chars:
        return text
    keep = max(0, max_chars - len(marker))
    if keep == 0:
        return marker[:max_chars]
    return marker + text[-keep:].lstrip()


def load_authored_skills(
    workspace: Path,
    *,
    max_chars: int,
    max_authored_skills: int,
    max_skill_description_chars: int,
) -> str:
    skills_dir = workspace / ".local-agent" / "skills"
    if max_chars <= 0 or not skills_dir.exists() or not skills_dir.is_dir():
        return ""
    lines: list[str] = []
    remaining = max_chars
    for skill_file in iter_authored_skill_files(skills_dir):
        metadata = read_skill_metadata(workspace, skill_file, max_description_chars=max_skill_description_chars)
        if metadata is None or metadata.get("hide"):
            continue
        rendered = f"- {metadata['name']}: {metadata['description']} Source: {skill_file.relative_to(workspace)}"
        if len(rendered) + 1 > remaining:
            break
        lines.append(rendered)
        remaining -= len(rendered) + 1
        if len(lines) >= max_authored_skills:
            break
    return "\n".join(lines)


def iter_authored_skill_files(skills_dir: Path) -> list[Path]:
    if not skills_dir.exists() or not skills_dir.is_dir():
        return []
    return [
        child / "SKILL.md"
        for child in sorted(skills_dir.iterdir(), key=lambda item: item.name.lower())
        if child.is_dir() and (child / "SKILL.md").is_file()
    ]


def read_skill_metadata(
    workspace: Path,
    skill_file: Path,
    *,
    max_description_chars: int,
) -> dict[str, str | bool] | None:
    try:
        skill_file.resolve().relative_to(workspace.resolve())
    except (OSError, ValueError):
        return None
    try:
        raw = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    frontmatter = _parse_frontmatter(raw)
    name = _clean_skill_name(str(frontmatter.get("name") or skill_file.parent.name))
    if not name:
        return None
    description = _clean_skill_description(
        str(frontmatter.get("description") or _fallback_skill_description(raw)),
        max_chars=max_description_chars,
    )
    if not description:
        return None
    return {"name": name, "description": description, "hide": _parse_bool(frontmatter.get("hide"))}


def _parse_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    data: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        if key in {"name", "description", "hide"}:
            data[key] = _strip_wrapping_quotes(value.strip())
    return data


def _fallback_skill_description(text: str) -> str:
    in_frontmatter = False
    frontmatter_done = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "---" and not frontmatter_done:
            in_frontmatter = not in_frontmatter
            if not in_frontmatter:
                frontmatter_done = True
            continue
        if in_frontmatter or not line or line.startswith("#"):
            continue
        return line
    return ""


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _clean_skill_name(name: str) -> str:
    cleaned = name.strip()[:64]
    if not cleaned or not all(char.isalnum() or char in {"-", "_"} for char in cleaned):
        return ""
    return cleaned


def _clean_skill_description(description: str, *, max_chars: int) -> str:
    cleaned = " ".join(description.replace("\x00", "").split())
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 14].rstrip() + "...<truncated>"
    return cleaned


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


__all__ = [
    "build_system_prompt",
    "iter_authored_skill_files",
    "load_sticky_rules",
    "read_skill_metadata",
    "workspace_root_markers",
    "workspace_roots_context",
]

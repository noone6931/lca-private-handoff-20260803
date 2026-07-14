from __future__ import annotations

import fnmatch
import glob
import json
import subprocess
from pathlib import Path, PurePath
from typing import Any

from local_agent.patch.anchored import PatchError
from local_agent.patch.anchored import display_workspace_path
from local_agent.patch.anchored import resolve_workspace_path

from .base import Tool, ToolContext, ToolResult

SKIPPED_DIRS = {".git", ".local-agent", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__"}
DEFAULT_GLOB_LIMIT = 200
MAX_GLOB_LIMIT = 1000
MAX_GLOB_RESULT_CHARS = 30000


def search_tools() -> list[Tool]:
    return [
        Tool(
            name="list_files",
            description=(
                "Browse files near a workspace or explicitly allowed directory. Results may be truncated and do not "
                "prove that omitted paths are absent; use glob_files for filename, extension, or directory discovery."
            ),
            tier="read",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                "additionalProperties": False,
            },
            handler=list_files,
        ),
        Tool(
            name="glob_files",
            description=(
                "Find filenames and paths under the primary workspace or explicitly allowed directories. Accepts exact "
                "files, directories, or glob patterns such as src/**/*.java. Returns structured scope, match, limit, "
                "and completeness metadata. This searches paths, not file contents; use search_code for text."
            ),
            tier="read",
            input_schema={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 32,
                    },
                    "hidden": {"type": "boolean"},
                    "gitignore": {"type": "boolean"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_GLOB_LIMIT},
                },
                "required": ["paths"],
                "additionalProperties": False,
            },
            handler=glob_files,
        ),
        Tool(
            name="search_code",
            description=(
                "Search text inside workspace or explicitly allowed directory file contents with ripgrep. It does not "
                "search filenames or prove that a path exists; use glob_files for filename, extension, and directory "
                "discovery. Returns workspace-relative paths for the main workspace and absolute paths for allowed directories."
            ),
            tier="read",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            handler=search_code,
        )
    ]


def list_files(args: dict[str, Any], context: ToolContext) -> ToolResult:
    max_results = min(max(int(args.get("max_results") or 200), 1), 1000)
    raw_path = args.get("path") or "."
    try:
        root = resolve_workspace_path(context.workspace, raw_path, context.allowed_dirs)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
    if not root.exists():
        return ToolResult(
            _with_workspace_roots_hint(f"Path not found: {raw_path}", context),
            is_error=True,
            metadata={
                "negative_evidence_type": "exact_path_missing",
                "path": str(raw_path),
                "complete": True,
            },
        )
    if root.is_file():
        file_name = display_workspace_path(context.workspace, root, context.allowed_dirs)
        return ToolResult(
            file_name,
            metadata={
                "listed_root": str(root),
                "files": [file_name],
                "entry_count": 1,
                "truncated": False,
                "result_limit_reached": False,
                "complete": True,
            },
        )

    results: list[str] = []
    truncated = False
    for path in _walk_files(root):
        if len(results) >= max_results:
            truncated = True
            break
        results.append(display_workspace_path(context.workspace, path, context.allowed_dirs))
    content = "\n".join(results) if results else "(no files)"
    if truncated:
        content += f"\n... truncated after {max_results} files"
    if _is_primary_root_listing(raw_path) and context.allowed_dirs:
        content = f"{_workspace_roots_hint(context)}\n\nFiles under primary workspace:\n{content}"
    return ToolResult(
        content,
        metadata={
            "listed_root": str(root),
            "files": list(results),
            "entry_count": len(results),
            "truncated": truncated,
            "result_limit_reached": truncated,
            "complete": not truncated,
            "negative_evidence_type": "incomplete" if truncated else "directory_listing",
        },
    )


def glob_files(args: dict[str, Any], context: ToolContext) -> ToolResult:
    """Discover filenames within explicitly authorized roots without invoking the shell tool."""

    limit = min(max(int(args.get("limit") or DEFAULT_GLOB_LIMIT), 1), MAX_GLOB_LIMIT)
    include_hidden = bool(args.get("hidden", False))
    respect_gitignore = bool(args.get("gitignore", True))
    raw_paths = tuple(str(path) for path in args["paths"])
    discovered: set[Path] = set()
    searched_scopes: list[str] = []
    missing_paths: list[str] = []
    result_limit_reached = False

    for raw_path in raw_paths:
        try:
            candidates, scope, missing = _glob_candidate_paths(raw_path, context)
        except PatchError as exc:
            return ToolResult(str(exc), is_error=True)
        searched_scopes.append(str(scope))
        if missing is not None:
            missing_paths.append(missing)
            continue
        for raw_candidate in candidates:
            candidate = Path(raw_candidate).resolve()
            if not candidate.is_file() or not _glob_candidate_is_visible(
                candidate,
                context,
                include_hidden=include_hidden,
                respect_gitignore=respect_gitignore,
            ):
                continue
            if candidate in discovered:
                continue
            if len(discovered) >= limit:
                result_limit_reached = True
                break
            discovered.add(candidate)
        if result_limit_reached:
            break

    files = sorted(
        (display_workspace_path(context.workspace, path, context.allowed_dirs) for path in discovered),
        key=str.casefold,
    )
    payload = {
        "files": files,
        "file_count": len(files),
        "match_count": len(files),
        "match_count_is_lower_bound": result_limit_reached,
        "searched_scopes": _unique_strings(searched_scopes),
        "searched_roots": _unique_strings(_scope_root(scope, context) for scope in searched_scopes),
        "patterns": list(raw_paths),
        "hidden": include_hidden,
        "gitignore": respect_gitignore,
        "limit": limit,
        "truncated": result_limit_reached,
        "result_limit_reached": result_limit_reached,
        "missing_paths": _unique_strings(missing_paths),
        "complete": not result_limit_reached,
        "negative_evidence_type": (
            "incomplete"
            if result_limit_reached
            else "exact_path_missing"
            if missing_paths and len(raw_paths) == 1 and not _contains_glob_magic(raw_paths[0])
            else "path_no_match"
            if not files and not missing_paths
            else "path_match"
        ),
        "structured_output": True,
    }
    payload = _bound_glob_payload(payload)
    rendered = _render_glob_result(payload)
    if missing_paths and len(raw_paths) == 1 and not _contains_glob_magic(raw_paths[0]):
        return ToolResult(rendered, is_error=True, metadata=payload)
    return ToolResult(rendered, useless=not files and not missing_paths, metadata=payload)


def _glob_candidate_paths(raw_path: str, context: ToolContext) -> tuple[Any, Path, str | None]:
    if not raw_path.strip():
        raise PatchError("glob_files paths entries must not be empty.")
    if not _contains_glob_magic(raw_path):
        target = resolve_workspace_path(context.workspace, raw_path, context.allowed_dirs)
        scope = target if target.exists() and target.is_dir() else target.parent
        if not target.exists():
            return (), scope, raw_path
        if target.is_file():
            return (target,), target.parent, None
        return glob.iglob(str(target / "**" / "*"), recursive=True, include_hidden=True), target, None

    literal_prefix, pattern = _glob_literal_prefix(raw_path)
    if any(part == ".." for part in PurePath(pattern).parts):
        raise PatchError(f"Glob pattern escapes its authorized scope: {raw_path}")
    scope = resolve_workspace_path(context.workspace, literal_prefix, context.allowed_dirs)
    if not scope.exists():
        return (), scope, display_workspace_path(context.workspace, scope, context.allowed_dirs)
    if not scope.is_dir():
        return (), scope.parent, None
    return glob.iglob(str(scope / pattern), recursive=True, include_hidden=True), scope, None


def _glob_literal_prefix(raw_path: str) -> tuple[str, str]:
    expanded = str(Path(raw_path).expanduser())
    parts = PurePath(expanded).parts
    for index, part in enumerate(parts):
        if _contains_glob_magic(part):
            prefix_parts = parts[:index]
            prefix = str(PurePath(*prefix_parts)) if prefix_parts else "."
            return prefix, str(PurePath(*parts[index:]))
    return expanded, ""


def _contains_glob_magic(value: str) -> bool:
    return glob.has_magic(value)


def _glob_candidate_is_visible(
    candidate: Path,
    context: ToolContext,
    *,
    include_hidden: bool,
    respect_gitignore: bool,
) -> bool:
    try:
        resolved = resolve_workspace_path(context.workspace, str(candidate), context.allowed_dirs)
    except PatchError:
        return False
    root = _authorized_root(resolved, context)
    relative = resolved.relative_to(root)
    if any(part in SKIPPED_DIRS for part in relative.parts):
        return False
    if not include_hidden and any(part.startswith(".") for part in relative.parts):
        return False
    return not respect_gitignore or not _is_gitignored(resolved, root)


def _authorized_root(path: Path, context: ToolContext) -> Path:
    roots = sorted(
        (context.workspace.resolve(), *(root.resolve() for root in context.allowed_dirs)),
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for root in roots:
        try:
            path.relative_to(root)
            return root
        except ValueError:
            continue
    raise PatchError(f"Path escapes workspace and allowed directories: {path}")


def _is_gitignored(path: Path, root: Path) -> bool:
    ignored = False
    directories = [root]
    parent = path.parent
    while parent != root:
        directories.append(parent)
        parent = parent.parent
    for directory in reversed(directories):
        ignore_file = directory / ".gitignore"
        if not ignore_file.is_file():
            continue
        try:
            rules = ignore_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        try:
            relative = path.relative_to(directory).as_posix()
        except ValueError:
            continue
        for raw_rule in rules:
            matched, negated = _gitignore_rule_matches(relative, raw_rule)
            if matched:
                ignored = not negated
    return ignored


def _gitignore_rule_matches(relative_path: str, raw_rule: str) -> tuple[bool, bool]:
    rule = raw_rule.strip()
    if not rule or rule.startswith("#"):
        return False, False
    negated = rule.startswith("!")
    if negated:
        rule = rule[1:]
    directory_only = rule.endswith("/")
    rule = rule.rstrip("/")
    anchored = rule.startswith("/")
    rule = rule.lstrip("/")
    if not rule:
        return False, negated
    path_parts = PurePath(relative_path).parts
    if directory_only:
        candidates = ("/".join(path_parts[:index]) for index in range(1, len(path_parts)))
    elif anchored or "/" in rule:
        candidates = (relative_path,)
    else:
        candidates = path_parts
    return any(fnmatch.fnmatchcase(candidate, rule) for candidate in candidates), negated


def _render_glob_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _bound_glob_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep structured discovery results small enough for a model turn.

    ``match_count`` still records every visible match observed before the output
    bound; the files array is only a representative prefix when it is shortened.
    """

    files = payload.get("files")
    if not isinstance(files, list) or len(_render_glob_result(payload)) <= MAX_GLOB_RESULT_CHARS:
        return payload
    rendered_files: list[str] = []
    estimated_chars = 0
    for file_name in files:
        value = str(file_name)
        if estimated_chars + len(value) + 4 > MAX_GLOB_RESULT_CHARS - 1800:
            break
        rendered_files.append(value)
        estimated_chars += len(value) + 4
    bounded = dict(payload)
    bounded["files"] = rendered_files
    bounded["file_count"] = len(rendered_files)
    bounded["returned_file_count"] = len(rendered_files)
    bounded["observed_match_count"] = payload.get("match_count", len(files))
    bounded["output_char_limit_reached"] = True
    bounded["truncated"] = True
    bounded["complete"] = False
    bounded["negative_evidence_type"] = "incomplete"
    return bounded


def _scope_root(scope: str, context: ToolContext) -> str:
    path = Path(scope)
    try:
        return str(_authorized_root(path.resolve(), context))
    except (PatchError, OSError):
        return str(path)


def _unique_strings(values: Any) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values))


def search_code(args: dict[str, Any], context: ToolContext) -> ToolResult:
    max_results = min(max(int(args.get("max_results") or 80), 1), 200)
    raw_path = args.get("path") or "."
    try:
        path = resolve_workspace_path(context.workspace, raw_path, context.allowed_dirs)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
    if not path.exists():
        return ToolResult(
            f"Path not found: {raw_path}",
            is_error=True,
            metadata={
                "negative_evidence_type": "exact_path_missing",
                "path": str(raw_path),
                "complete": True,
            },
        )
    search_path = _rg_search_path(path, context)
    command = [
        "rg",
        "--line-number",
        "--column",
        "--color",
        "never",
        "--",
        args["pattern"],
        search_path,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=context.workspace,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except FileNotFoundError:
        return ToolResult("ripgrep is not installed. Please install rg in the VM image.", is_error=True)
    if completed.returncode not in {0, 1}:
        output = completed.stderr or completed.stdout or f"rg failed with exit code {completed.returncode}."
        return ToolResult(output[:20000], is_error=True)
    if not completed.stdout:
        content = "No matches."
        if _is_primary_root_listing(raw_path) and context.allowed_dirs:
            content = _with_workspace_roots_hint(content, context)
        return ToolResult(
            content,
            useless=True,
            metadata={
                "negative_evidence_type": "content_no_match",
                "pattern": str(args["pattern"]),
                "path": str(path),
                "complete": True,
                "truncated": False,
            },
        )
    output = _normalize_search_output_paths(completed.stdout, context.workspace)
    truncated_output = _truncate_search_output(output, max_results)
    truncated = "\n... truncated after " in truncated_output
    return ToolResult(
        truncated_output[:20000],
        metadata={
            "pattern": str(args["pattern"]),
            "path": str(path),
            "complete": not truncated,
            "truncated": truncated,
            "negative_evidence_type": "incomplete" if truncated else "content_match",
        },
    )


def _is_primary_root_listing(raw_path: Any) -> bool:
    return str(raw_path or ".").strip() in {"", "."}


def _workspace_roots_hint(context: ToolContext) -> str:
    lines = [
        "Workspace roots:",
        f"- Primary workspace (--cwd): {context.workspace}",
    ]
    if context.allowed_dirs:
        lines.append("- Additional allowed directories; use these exact absolute paths for external docs/specs/code:")
        lines.extend(f"  - {path}" for path in context.allowed_dirs)
    return "\n".join(lines)


def _with_workspace_roots_hint(content: str, context: ToolContext) -> str:
    if not context.allowed_dirs:
        return content
    return f"{content}\n\n{_workspace_roots_hint(context)}"


def _walk_files(root: Path):
    for child in sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if child.is_dir():
            if child.name in SKIPPED_DIRS:
                continue
            yield from _walk_files(child)
        elif child.is_file():
            yield child


def _truncate_search_output(output: str, max_results: int) -> str:
    if output == "No matches.":
        return output
    lines = output.splitlines()
    if len(lines) <= max_results:
        return output
    kept = lines[:max_results]
    kept.append(f"... truncated after {max_results} matches")
    return "\n".join(kept)


def _normalize_search_output_paths(output: str, workspace: Path) -> str:
    workspace_prefix = str(workspace) + "/"
    normalized: list[str] = []
    for line in output.splitlines():
        if line.startswith(workspace_prefix):
            line = line[len(workspace_prefix) :]
        if line.startswith("./"):
            line = line[2:]
        normalized.append(line)
    return "\n".join(normalized)


def _rg_search_path(path: Path, context: ToolContext) -> str:
    if path == context.workspace:
        return "."
    try:
        return str(path.relative_to(context.workspace))
    except ValueError:
        return str(path)

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path, PurePosixPath
from typing import Any

from .base import Tool, ToolContext, ToolResult
from .files import session_patch_records
from .relevance import is_code_implementation_request
from .relevance import is_low_relevance_patch_path
from .relevance import is_source_code_path
from .relevance import request_mentions_config_or_path

MAX_DIFF_SUMMARY_FILES = 12
MAX_DIFF_SUMMARY_HUNKS_PER_FILE = 4
MAX_DIFF_SUMMARY_LINES_PER_KIND = 3
MAX_DIFF_SUMMARY_LINE_CHARS = 120


@dataclass
class DiffHunkSummary:
    header: str
    additions: list[str] = field(default_factory=list)
    removals: list[str] = field(default_factory=list)


@dataclass
class DiffFileSummary:
    path: str
    additions: int = 0
    removals: int = 0
    hunks: list[DiffHunkSummary] = field(default_factory=list)
    binary: bool = False


def git_tools() -> list[Tool]:
    return [
        Tool(
            name="git_status",
            description="Show local git status.",
            tier="read",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=git_status,
        ),
        Tool(
            name="git_diff",
            description="Show local git diff.",
            tier="read",
            input_schema={
                "type": "object",
                "properties": {"staged": {"type": "boolean"}},
                "additionalProperties": False,
            },
            handler=git_diff,
        ),
    ]


def git_status(args: dict[str, Any], context: ToolContext) -> ToolResult:
    return _git(context, ["status", "--short"])


def git_diff(args: dict[str, Any], context: ToolContext) -> ToolResult:
    command = ["diff", "--staged"] if args.get("staged") else ["diff"]
    result = _git(context, command)
    content = result.content
    if not result.is_error and result.content == "(empty)":
        status = _git(context, ["status", "--short"])
        if not status.is_error and status.content != "(empty)":
            content = (
                "(empty diff)\n\n"
                "[git status --short]\n"
                f"{status.content}\n"
                "Note: git diff does not show untracked files. Create an initial commit or stage files to see diffs."
            )
    if result.is_error:
        return result
    raw_diff = content
    content = _with_diff_summary(content)
    content = _with_attribution_note(content, args, context)
    return ToolResult(_with_diff_reviewer_note(content, context, raw_diff=raw_diff))


def capture_git_baseline(workspace: str | PathLike[str]) -> dict[str, Any]:
    workspace_path = Path(workspace)
    try:
        rev_parse = _git_raw(workspace_path, ["rev-parse", "--is-inside-work-tree"])
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "is_git_repo": False,
            "error": f"{type(exc).__name__}: {exc}",
            "status_short": "",
            "diff_name_status": "",
            "staged_name_status": "",
        }
    if rev_parse.returncode != 0:
        return {
            "is_git_repo": False,
            "status_short": "",
            "diff_name_status": "",
            "staged_name_status": "",
        }
    try:
        status_short = _git_raw(workspace_path, ["status", "--short"]).stdout.rstrip()
        diff_name_status = _git_raw(workspace_path, ["diff", "--name-status"]).stdout.rstrip()
        staged_name_status = _git_raw(workspace_path, ["diff", "--staged", "--name-status"]).stdout.rstrip()
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "is_git_repo": False,
            "error": f"{type(exc).__name__}: {exc}",
            "status_short": "",
            "diff_name_status": "",
            "staged_name_status": "",
        }
    return {
        "is_git_repo": True,
        "status_short": status_short,
        "diff_name_status": diff_name_status,
        "staged_name_status": staged_name_status,
    }


def _git(context: ToolContext, args: list[str]) -> ToolResult:
    completed = _git_raw(context.workspace, args)
    output = completed.stdout or completed.stderr or "(empty)"
    is_error = completed.returncode != 0
    is_not_repository = is_error and "not a git repository" in output.lower()
    metadata = {
        "git_probe_root": str(context.workspace),
        "git_repository": False if is_not_repository else not is_error if not is_error else None,
    }
    if is_not_repository:
        output = (
            f"{output.rstrip()}\n\n"
            f"git_status checks the primary workspace only: {context.workspace}. "
            "This does not determine whether additional roots are Git repositories. "
            "Use /move /path/to/project before Git operations for that project."
        )
    return ToolResult(output[:30000], is_error=is_error, metadata=metadata)


def _git_raw(workspace: str | PathLike[str], args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def _with_diff_summary(content: str) -> str:
    summaries = _parse_diff_summary(content)
    if not summaries:
        return (
            content.rstrip()
            + "\n\n[diff summary]\n"
            + "- No tracked file hunks in git diff output."
        )
    lines = ["", "[diff summary]"]
    total_additions = sum(summary.additions for summary in summaries)
    total_removals = sum(summary.removals for summary in summaries)
    total_hunks = sum(len(summary.hunks) for summary in summaries)
    lines.append(
        f"- Total: {len(summaries)} file(s), +{total_additions} -{total_removals}, {total_hunks} hunk(s)."
    )
    for summary in summaries[:MAX_DIFF_SUMMARY_FILES]:
        binary_note = " (binary)" if summary.binary else ""
        lines.append(
            f"- {summary.path}: +{summary.additions} -{summary.removals}, {len(summary.hunks)} hunk(s){binary_note}."
        )
        for hunk in summary.hunks[:MAX_DIFF_SUMMARY_HUNKS_PER_FILE]:
            lines.append(f"  - {hunk.header}")
            if hunk.removals:
                lines.append(f"    removed: {_join_snippets(hunk.removals)}")
            if hunk.additions:
                lines.append(f"    added: {_join_snippets(hunk.additions)}")
        if len(summary.hunks) > MAX_DIFF_SUMMARY_HUNKS_PER_FILE:
            lines.append(f"  - ... {len(summary.hunks) - MAX_DIFF_SUMMARY_HUNKS_PER_FILE} more hunk(s)")
    if len(summaries) > MAX_DIFF_SUMMARY_FILES:
        lines.append(f"- ... {len(summaries) - MAX_DIFF_SUMMARY_FILES} more file(s)")
    lines.append("- Final answer hint: use these counts and hunk snippets when summarizing git_diff.")
    return content.rstrip() + "\n\n" + "\n".join(lines)


def _parse_diff_summary(content: str) -> list[DiffFileSummary]:
    summaries: list[DiffFileSummary] = []
    current_file: DiffFileSummary | None = None
    current_hunk: DiffHunkSummary | None = None
    for line in content.splitlines():
        if line.startswith("diff --git "):
            current_file = DiffFileSummary(path=_path_from_diff_git_line(line))
            summaries.append(current_file)
            current_hunk = None
            continue
        if current_file is None:
            continue
        if line.startswith("+++ "):
            path = _path_from_file_marker(line)
            if path and path != "/dev/null":
                current_file.path = path
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current_file.binary = True
            continue
        if line.startswith("@@ "):
            current_hunk = DiffHunkSummary(header=line)
            current_file.hunks.append(current_hunk)
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            current_file.additions += 1
            if current_hunk is not None and len(current_hunk.additions) < MAX_DIFF_SUMMARY_LINES_PER_KIND:
                current_hunk.additions.append(_snippet(line[1:]))
            continue
        if line.startswith("-"):
            current_file.removals += 1
            if current_hunk is not None and len(current_hunk.removals) < MAX_DIFF_SUMMARY_LINES_PER_KIND:
                current_hunk.removals.append(_snippet(line[1:]))
            continue
    return [summary for summary in summaries if summary.hunks or summary.binary or summary.additions or summary.removals]


def _path_from_diff_git_line(line: str) -> str:
    parts = line.split()
    if len(parts) >= 4:
        return _strip_diff_prefix(parts[3])
    return "unknown"


def _path_from_file_marker(line: str) -> str:
    value = line[4:].strip()
    if "\t" in value:
        value = value.split("\t", 1)[0]
    return _strip_diff_prefix(value)


def _strip_diff_prefix(path: str) -> str:
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _join_snippets(lines: list[str]) -> str:
    return " | ".join(lines)


def _snippet(line: str) -> str:
    compact = line.strip()
    if not compact:
        compact = "<blank line>"
    if len(compact) > MAX_DIFF_SUMMARY_LINE_CHARS:
        return compact[: MAX_DIFF_SUMMARY_LINE_CHARS - 3] + "..."
    return compact


def _with_attribution_note(content: str, args: dict[str, Any], context: ToolContext) -> str:
    baseline = context.git_baseline
    if not baseline or baseline.get("is_git_repo") is False:
        return content

    staged = bool(args.get("staged"))
    current_name_status = _git(context, ["diff", "--staged", "--name-status"] if staged else ["diff", "--name-status"])
    current_paths = _name_status_paths(current_name_status.content if not current_name_status.is_error else "")
    baseline_paths = _baseline_paths(baseline, staged=staged)
    patch_paths = _session_patch_paths(context)

    pre_existing = sorted(path for path in current_paths if path in baseline_paths)
    this_session = sorted(path for path in current_paths if path in patch_paths)
    mixed = sorted(set(pre_existing).intersection(this_session))
    new_unattributed = sorted(path for path in current_paths if path not in baseline_paths and path not in patch_paths)

    lines = [
        "",
        "[diff attribution]",
        "- Baseline: captured at this agent run start.",
    ]
    if baseline_paths:
        lines.append(f"- Pre-existing dirty files at run start: {_join_paths(baseline_paths)}")
    else:
        lines.append("- Pre-existing dirty files at run start: none")
    if patch_paths:
        lines.append(f"- This-session apply_patch files: {_join_paths(patch_paths)}")
    else:
        lines.append("- This-session apply_patch files: none recorded")
    if mixed:
        lines.append(f"- Files with both pre-existing and this-session changes: {_join_paths(mixed)}")
    if new_unattributed:
        lines.append(
            "- Current diff files not present at baseline and not recorded by apply_patch: "
            f"{_join_paths(new_unattributed)}"
        )
    lines.append(
        "- Attribution hint: summarize pre-existing and this-session changes separately; "
        "a file can contain both if it was already dirty before this run."
    )
    return content.rstrip() + "\n\n" + "\n".join(lines)


def _with_diff_reviewer_note(content: str, context: ToolContext, *, raw_diff: str) -> str:
    request = context.current_user_request
    if not is_code_implementation_request(request):
        return content
    lines = []
    suspicious_paths = sorted(
        path
        for path in _session_patch_paths(context)
        if is_low_relevance_patch_path(path) and not request_mentions_config_or_path(request, path)
    )
    if suspicious_paths:
        lines.extend(
            [
                "- Potential relevance warning: this-session apply_patch touched deployment/config-like path(s): "
                f"{_join_paths(suspicious_paths)}",
                "- Before final answer, explain the exact source-code evidence connecting these files to the requested "
                "implementation, or use rollback_patch/re-target the edit.",
            ]
        )
    comment_only_paths = _comment_only_code_patch_paths(raw_diff, _session_patch_paths(context))
    if comment_only_paths:
        lines.extend(
            [
                "- Potential implementation-quality warning: this-session code diff appears comment/documentation-only "
                f"for path(s): {_join_paths(comment_only_paths)}",
                "- For implementation tasks, do not claim behavior, validation, parsing, or test coverage changed unless "
                "the diff includes non-comment code or tests. Re-target the edit, rollback it, or explicitly report "
                "that only documentation/comments changed.",
            ]
        )
    if not lines:
        return content
    return content.rstrip() + "\n\n[diff reviewer]\n" + "\n".join(lines)


def _comment_only_code_patch_paths(raw_diff: str, patch_paths: set[str]) -> list[str]:
    if not patch_paths:
        return []
    files = _changed_code_lines_by_file(raw_diff)
    comment_only: list[str] = []
    for path, changed_lines in files.items():
        if path not in patch_paths or not is_source_code_path(path):
            continue
        meaningful = [line for line in changed_lines if line.strip()]
        if meaningful and all(_looks_like_comment_line(path, line) for line in meaningful):
            comment_only.append(path)
    return sorted(comment_only)


def _changed_code_lines_by_file(raw_diff: str) -> dict[str, list[str]]:
    files: dict[str, list[str]] = {}
    current_path = ""
    in_hunk = False
    for line in raw_diff.splitlines():
        if line.startswith("diff --git "):
            current_path = _path_from_diff_git_line(line)
            files.setdefault(current_path, [])
            in_hunk = False
            continue
        if line.startswith("+++ "):
            path = _path_from_file_marker(line)
            if path and path != "/dev/null":
                current_path = path
                files.setdefault(current_path, [])
            continue
        if line.startswith("@@ "):
            in_hunk = True
            continue
        if not in_hunk or not current_path:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            files.setdefault(current_path, []).append(line[1:])
    return files


def _looks_like_comment_line(path: str, line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if stripped in {"*/", "/*", "/**"}:
        return True
    if stripped.startswith(("//", "#", "*", "/*", "<!--", "-->", "{/*", "*/")):
        return True
    if _is_javadoc_markup_line(path, stripped):
        return True
    if stripped.endswith("-->"):
        return True
    return False


def _is_javadoc_markup_line(path: str, stripped: str) -> bool:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix != ".java":
        return False
    return stripped.startswith(("<p>", "</p>", "<ul>", "</ul>", "<li>", "</li>"))


def _baseline_paths(baseline: dict[str, Any], *, staged: bool) -> set[str]:
    name_status = str(baseline.get("staged_name_status" if staged else "diff_name_status") or "")
    paths = _name_status_paths(name_status)
    if not staged:
        paths.update(_status_short_paths(str(baseline.get("status_short") or "")))
    return paths


def _session_patch_paths(context: ToolContext) -> set[str]:
    paths: set[str] = set()
    for record in session_patch_records(context):
        if record.get("event") == "apply" and isinstance(record.get("path"), str):
            paths.add(record["path"])
    return paths


def _name_status_paths(content: str) -> set[str]:
    paths: set[str] = set()
    for line in content.splitlines():
        if not line.strip() or line.startswith("["):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        for path in parts[1:]:
            path = path.strip()
            if path:
                paths.add(path)
    return paths


def _status_short_paths(content: str) -> set[str]:
    paths: set[str] = set()
    for line in content.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        if path:
            paths.add(path)
    return paths


def _join_paths(paths: set[str] | list[str]) -> str:
    rendered = sorted(paths)
    if not rendered:
        return "none"
    limit = 8
    shown = rendered[:limit]
    suffix = f", ... (+{len(rendered) - limit} more)" if len(rendered) > limit else ""
    return ", ".join(shown) + suffix

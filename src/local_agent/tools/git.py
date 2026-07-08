from __future__ import annotations

import subprocess
from os import PathLike
from pathlib import Path
from typing import Any

from .base import Tool, ToolContext, ToolResult
from .files import session_patch_records


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
    return ToolResult(_with_attribution_note(content, args, context))


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
    return ToolResult(output[:30000], is_error=completed.returncode != 0)


def _git_raw(workspace: str | PathLike[str], args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


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

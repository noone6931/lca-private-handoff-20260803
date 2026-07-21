from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..patch.anchored import display_workspace_path
from ..patch.anchored import PatchError
from ..patch.anchored import resolve_workspace_path
from .requirements import is_requirement_source_path
from .requirements import RequirementEvidence
from .requirements import update_requirement_evidence
from ..steering.final_answer import SourceEvidence
from ..tools.argument_normalization import normalize_compatibility_arguments
from ..tools.base import ToolResult
from ..tools.relevance import is_low_relevance_patch_path
from ..tools.relevance import path_matches_any
from .timeline import WRITE_TOOL_NAMES
from .timeline import result_changed_workspace
from .timeline import result_workspace_write_paths


MAX_RECORDS = 30
CONTEXT_RECORDS = 18
CONTEXT_CHAR_LIMIT = 6000
MAX_READ_FILE_PATHS = 20
MAX_SOURCE_EVIDENCE = 40
MAX_DESIGN_READ_PATHS = 40
MAX_STRONG_RELEVANCE_PATHS = 30


@dataclass(frozen=True)
class EvidenceRecord:
    tool: str
    subject: str
    summary: str
    status: str = "ok"
    details: Mapping[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        origin = str(self.details.get("evidence_origin") or "current_run")
        return f"- [{self.status}; {origin}] {self.tool} {self.subject}: {self.summary}"


@dataclass
class EvidenceLedger:
    """Run-scoped, provider-safe observations derived from tool results."""

    read_file_paths: list[str] = field(default_factory=list)
    design_read_paths: list[str] = field(default_factory=list)
    source_evidence: list[SourceEvidence] = field(default_factory=list)
    pinned_requirement_evidence: list[RequirementEvidence] = field(default_factory=list)
    successful_patch_preview_signatures: set[str] = field(default_factory=set)
    strong_relevance_paths: list[str] = field(default_factory=list)
    records: list[EvidenceRecord] = field(default_factory=list)
    workspace_root_recorded: bool = False

    def reset(self) -> None:
        self.read_file_paths.clear()
        self.design_read_paths.clear()
        self.source_evidence.clear()
        self.pinned_requirement_evidence.clear()
        self.successful_patch_preview_signatures.clear()
        self.strong_relevance_paths.clear()
        self.records.clear()
        self.workspace_root_recorded = False

    def record_read_file(
        self,
        *,
        arguments: str | dict[str, Any],
        result: ToolResult,
        workspace: Path,
        allowed_dirs: tuple[Path, ...],
        requirement_candidates: tuple[Path, ...] = (),
    ) -> None:
        if result.is_error:
            return
        parsed = parse_tool_arguments(arguments)
        raw_path = parsed.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return
        try:
            resolved_path = resolve_workspace_path(workspace, raw_path.strip(), allowed_dirs)
        except PatchError:
            return
        root_path = evidence_root_for_path(resolved_path, workspace, allowed_dirs)
        root_label = evidence_root_label(root_path, workspace, allowed_dirs)
        canonical_path = str(resolved_path)
        if canonical_path not in self.design_read_paths:
            self.design_read_paths.append(canonical_path)
            self.design_read_paths = self.design_read_paths[-MAX_DESIGN_READ_PATHS:]
        display_path = display_read_file_path(workspace, raw_path.strip(), allowed_dirs)
        if display_path not in self.read_file_paths:
            self.read_file_paths.append(display_path)
            self.read_file_paths = self.read_file_paths[-MAX_READ_FILE_PATHS:]
        self.source_evidence.append(
            SourceEvidence(
                display_path,
                result.content,
                root=str(root_path),
                scope="root_local",
            )
        )
        self.source_evidence = self.source_evidence[-MAX_SOURCE_EVIDENCE:]
        if _is_requirement_candidate(resolved_path, requirement_candidates) or is_requirement_source_path(display_path):
            self.pinned_requirement_evidence = update_requirement_evidence(
                self.pinned_requirement_evidence,
                path=display_path,
                content=result.content,
                root=str(root_path),
                scope="root_local",
            )

    def record_tool(
        self,
        *,
        name: str,
        arguments: str | dict[str, Any],
        result: ToolResult,
        workspace: Path,
        allowed_dirs: tuple[Path, ...],
    ) -> EvidenceRecord | None:
        self._record_strong_relevance_paths(name, result)
        return build_tool_evidence_record(name, arguments, result, workspace, allowed_dirs)

    def record_workspace_root(self, workspace: Path) -> EvidenceRecord | None:
        if self.workspace_root_recorded:
            return None
        self.workspace_root_recorded = True
        markers = workspace_root_markers(workspace)
        if not markers:
            return None
        record = EvidenceRecord(
            tool="workspace",
            subject="root",
            summary="Primary workspace contains: " + ", ".join(markers) + ".",
            details={
                "evidence_root": str(workspace.resolve()),
                "evidence_root_label": "primary",
                "evidence_scope": "workspace_root",
            },
        )
        return record

    def invalidate_source_after_write(
        self,
        *,
        name: str,
        arguments: str | dict[str, Any],
        result: ToolResult,
        workspace: Path,
        allowed_dirs: tuple[Path, ...],
    ) -> None:
        if name not in WRITE_TOOL_NAMES or not result_changed_workspace(result):
            return
        parsed = parse_tool_arguments(arguments)
        if name == "apply_patch" and parsed.get("dry_run"):
            return
        raw_paths = result_workspace_write_paths(result)
        if not raw_paths:
            raw_path = parsed.get("path")
            raw_paths = (raw_path,) if isinstance(raw_path, str) and raw_path.strip() else ()
        display_paths = {display_read_file_path(workspace, path, allowed_dirs) for path in raw_paths}
        self.source_evidence = [item for item in self.source_evidence if item.path not in display_paths]

    def record_successful_patch_preview(
        self,
        *,
        name: str,
        arguments: str | dict[str, Any],
        result: ToolResult,
        workspace: Path,
        allowed_dirs: tuple[Path, ...],
    ) -> None:
        if name != "apply_patch" or result.is_error:
            return
        try:
            normalized, _ = normalize_compatibility_arguments(name, parse_tool_arguments(arguments))
        except ValueError:
            return
        if not normalized.get("dry_run"):
            return
        raw_path = normalized.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return
        try:
            resolved_path = resolve_workspace_path(workspace, raw_path, allowed_dirs)
        except PatchError:
            return
        self.successful_patch_preview_signatures.add(patch_preview_signature(normalized, resolved_path))

    def patch_relevance_denial_reason(
        self,
        raw_path: str,
        resolved_path: Path,
        *,
        workspace: Path,
        allowed_dirs: tuple[Path, ...],
        is_code_implementation_request: bool,
        request_mentions_config_or_path: bool,
    ) -> str | None:
        display_path = display_workspace_path(workspace, resolved_path, allowed_dirs)
        if not path_matches_any(display_path, tuple(self.read_file_paths)):
            return (
                f"Patch relevance gate: refusing real apply_patch for {display_path!r} because that file "
                "has not been read with read_file in this run. Call read_file on the exact target first; "
                "apply_patch dry_run=true previews are still allowed."
            )
        if (
            is_code_implementation_request
            and is_low_relevance_patch_path(display_path)
            and not request_mentions_config_or_path
            and not path_matches_any(display_path, tuple(self.strong_relevance_paths))
        ):
            return (
                f"Patch relevance gate: refusing real apply_patch for {display_path!r} because the current "
                "request looks like a code implementation task, while the target looks like deployment/config "
                "material. Before editing this path, establish direct relevance from source-code evidence or "
                "ask the user to confirm a configuration/deployment edit. apply_patch dry_run=true previews are "
                "still allowed."
            )
        return None

    def patch_preview_denial_reason(
        self,
        args: dict[str, Any],
        resolved_path: Path,
        *,
        preview_required: bool,
    ) -> str | None:
        if not preview_required or patch_preview_signature(args, resolved_path) in self.successful_patch_preview_signatures:
            return None
        return (
            "Preview contract: this task explicitly requires a patch preview before a real write. "
            "Call apply_patch first with the identical path, tag, line range, old_text, new_text, and mode plus "
            "dry_run=true. The preview must succeed before applying this patch."
        )

    def read_file_summary(self) -> str:
        if not self.read_file_paths:
            return ""
        return "\n".join(
            [
                "",
                "",
                "Already read these files in this run; do not claim they were unread:",
                *[f"- {path}" for path in self.read_file_paths[-12:]],
                "If one of these files still needs deeper implementation review, say it was already read and specify the missing detail.",
            ]
        )

    def evidence_for_read_file_range(self, subject: str) -> str:
        matches = [record.render() for record in reversed(self.records) if record.tool == "read_file" and record.subject == subject]
        return "\n".join(reversed(matches[:3]))

    def summary(self) -> str:
        if not self.records:
            return ""
        lines = [
            "[Evidence ledger]",
            "Runtime-collected tool evidence for this run. Use it to distinguish evidence-backed facts from inference.",
            "In final answers, cite exact paths only when they appear here or in tool results; label guessed files/classes as unverified.",
            "Do not claim workspace root files are missing when workspace evidence lists them.",
            "Read-file and rule/document evidence is root-local by default: it applies only to the workspace root it came from unless the user explicitly asked for cross-root synthesis.",
            *(record.render() for record in self.records[-CONTEXT_RECORDS:]),
        ]
        return one_line_block("\n".join(lines), max_chars=CONTEXT_CHAR_LIMIT)

    def append(self, record: EvidenceRecord) -> bool:
        if self.records and self.records[-1].render() == record.render():
            return False
        self.records.append(record)
        self.records = self.records[-MAX_RECORDS:]
        return True

    def hydrate_session_cached(
        self,
        *,
        record: EvidenceRecord,
        source_evidence: SourceEvidence | None,
        requirement_evidence: RequirementEvidence | None,
        canonical_paths: tuple[str, ...] = (),
    ) -> bool:
        """Project fresh session evidence into this run without claiming a new tool call."""

        changed = self.append(record)
        if source_evidence is not None:
            if source_evidence.path not in self.read_file_paths:
                self.read_file_paths.append(source_evidence.path)
                self.read_file_paths = self.read_file_paths[-MAX_READ_FILE_PATHS:]
            for design_path in canonical_paths or (source_evidence.path,):
                if design_path not in self.design_read_paths:
                    self.design_read_paths.append(design_path)
            self.design_read_paths = self.design_read_paths[-MAX_DESIGN_READ_PATHS:]
            if source_evidence not in self.source_evidence:
                self.source_evidence.append(source_evidence)
                self.source_evidence = self.source_evidence[-MAX_SOURCE_EVIDENCE:]
                changed = True
        if requirement_evidence is not None and requirement_evidence not in self.pinned_requirement_evidence:
            self.pinned_requirement_evidence = [
                *self.pinned_requirement_evidence,
                requirement_evidence,
            ][-2:]
            changed = True
        return changed

    def _record_strong_relevance_paths(self, name: str, result: ToolResult) -> None:
        if result.is_error:
            return
        paths: list[str] = []
        if name == "search_code":
            paths = [path for path in first_search_result_paths(result.content, limit=8) if not is_low_relevance_patch_path(path)]
        elif name.startswith("lsp_"):
            paths = first_result_line_paths(result.content, limit=8)
        for path in paths:
            if path and path not in self.strong_relevance_paths:
                self.strong_relevance_paths.append(path)
        self.strong_relevance_paths = self.strong_relevance_paths[-MAX_STRONG_RELEVANCE_PATHS:]


def display_read_file_path(workspace: Path, raw_path: str, allowed_dirs: tuple[Path, ...]) -> str:
    try:
        path = resolve_workspace_path(workspace, raw_path, allowed_dirs)
    except PatchError:
        return raw_path
    try:
        return str(path.relative_to(workspace))
    except ValueError:
        return str(path)


def _is_requirement_candidate(path: Path, candidates: tuple[Path, ...]) -> bool:
    for candidate in candidates:
        try:
            if path.resolve() == candidate.resolve():
                return True
        except OSError:
            continue
    return False


def build_tool_evidence_record(
    name: str,
    arguments: str | dict[str, Any],
    result: ToolResult,
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
) -> EvidenceRecord | None:
    parsed = parse_tool_arguments(arguments)
    metadata = dict(result.metadata)
    negative_type = str(metadata.get("negative_evidence_type") or "")
    if result.is_error and negative_type != "exact_path_missing" and name not in {
        "apply_patch",
        "git_diff",
        "git_status",
        "rollback_patch",
        "run_tests",
        "shell",
        "write_file",
    }:
        return None
    if name == "glob_files":
        patterns = metadata.get("patterns")
        pattern_text = ", ".join(str(pattern) for pattern in patterns) if isinstance(patterns, list) else "(unknown)"
        files = metadata.get("files")
        file_count = len(files) if isinstance(files, list) else metadata.get("file_count", 0)
        scopes = metadata.get("searched_scopes")
        scope_text = ", ".join(str(scope) for scope in scopes) if isinstance(scopes, list) else "(unknown)"
        searched_roots = metadata.get("searched_roots")
        root_text = ", ".join(
            evidence_root_label(Path(str(root)), workspace, allowed_dirs)
            for root in searched_roots
            if isinstance(root, str) and root.strip()
        ) if isinstance(searched_roots, list) else "(unknown)"
        summary = f"{file_count} file(s) for {pattern_text}; roots: {root_text}; scopes: {scope_text}."
        if metadata.get("truncated"):
            summary += " Result limit reached; scan is incomplete."
        if metadata.get("missing_paths"):
            summary += " Missing paths: " + ", ".join(str(path) for path in metadata["missing_paths"]) + "."
        return EvidenceRecord(
            "glob_files",
            f"patterns={pattern_text}",
            summary,
            status=negative_type or "ok",
            details=metadata,
        )
    if negative_type == "exact_path_missing":
        raw_path = str(metadata.get("path") or parsed.get("path") or "(unknown)")
        return EvidenceRecord(
            name,
            raw_path,
            "Exact path was not found.",
            status="exact_path_missing",
            details=metadata,
        )
    if name == "read_file" and not result.is_error:
        raw_path = parsed.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        try:
            resolved_path = resolve_workspace_path(workspace, raw_path.strip(), allowed_dirs)
            root_path = evidence_root_for_path(resolved_path, workspace, allowed_dirs)
            root_label = evidence_root_label(root_path, workspace, allowed_dirs)
        except PatchError:
            resolved_path = None
            root_path = None
            root_label = "(unknown)"
        start_line = parsed.get("start_line") or 1
        end_line = parsed.get("end_line")
        line_range = f"lines {start_line}-{end_line}" if end_line else f"from line {start_line}"
        return EvidenceRecord(
            tool="read_file",
            subject=display_read_file_path(workspace, raw_path.strip(), allowed_dirs),
            summary=f"{one_line(first_nonempty_line(result.content), max_chars=180)}; read {line_range}; root: {root_label}.",
            details={
                **metadata,
                "evidence_root": str(root_path) if root_path is not None else "",
                "evidence_root_label": root_label,
                "evidence_scope": "root_local",
                "resolved_path": str(resolved_path) if resolved_path is not None else "",
            },
        )
    if name == "inspect_image" and not result.is_error:
        raw_path = parsed.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        try:
            resolved_path = resolve_workspace_path(workspace, raw_path.strip(), allowed_dirs)
            root_path = evidence_root_for_path(resolved_path, workspace, allowed_dirs)
            root_label = evidence_root_label(root_path, workspace, allowed_dirs)
        except PatchError:
            resolved_path = None
            root_path = None
            root_label = "(unknown)"
        return EvidenceRecord(
            tool="inspect_image",
            subject=display_read_file_path(workspace, raw_path.strip(), allowed_dirs),
            summary=f"Image observation: {one_line(first_nonempty_line(result.content), max_chars=220)}; root: {root_label}.",
            details={
                **metadata,
                "evidence_root": str(root_path) if root_path is not None else "",
                "evidence_root_label": root_label,
                "evidence_scope": "root_local",
                "resolved_path": str(resolved_path) if resolved_path is not None else "",
            },
        )
    if name == "search_code" and not result.is_error:
        pattern = str(parsed.get("pattern") or "").strip()
        if not pattern:
            return None
        raw_path = str(parsed.get("path") or ".").strip() or "."
        try:
            resolved_path = resolve_workspace_path(workspace, raw_path, allowed_dirs)
            root_path = evidence_root_for_path(resolved_path, workspace, allowed_dirs)
            root_label = evidence_root_label(root_path, workspace, allowed_dirs)
        except PatchError:
            root_path = None
            root_label = "(unknown)"
        subject = f"pattern={pattern!r} path={raw_path!r}"
        if result.useless or result.content.strip().startswith("No matches."):
            return EvidenceRecord(
                "search_code",
                subject,
                f"No matches returned; root: {root_label}.",
                status=negative_type or "content_no_match",
                details={
                    **metadata,
                    "evidence_root": str(root_path) if root_path is not None else "",
                    "evidence_root_label": root_label,
                    "evidence_scope": "path_or_root_local",
                },
            )
        paths = first_search_result_paths(result.content, limit=5)
        summary = "Matched files: " + ", ".join(paths) if paths else "Returned matches; inspect the search_code tool result for exact lines."
        return EvidenceRecord(
            "search_code",
            subject,
            f"{summary}; root: {root_label}.",
            details={
                **metadata,
                "evidence_root": str(root_path) if root_path is not None else "",
                "evidence_root_label": root_label,
                "evidence_scope": "path_or_root_local",
            },
        )
    if name.startswith("lsp_") and not result.is_error:
        subject = lsp_subject(parsed) or "query"
        provenance = tool_evidence_provenance(parsed, workspace, allowed_dirs)
        if result.useless or result.content.strip().startswith("No "):
            return EvidenceRecord(
                name,
                subject,
                one_line(result.content, max_chars=220),
                status="no_match",
                details={**metadata, **provenance},
            )
        lines = first_content_lines(result.content, limit=4)
        summary = " | ".join(one_line(line, max_chars=160) for line in lines) or "Returned lightweight code navigation results."
        return EvidenceRecord(name, subject, summary, details={**metadata, **provenance})
    if name in {"apply_patch", "rollback_patch", "write_file"}:
        raw_path = parsed.get("path") if isinstance(parsed.get("path"), str) else ""
        status = "error" if result.is_error else "preview" if name == "apply_patch" and parsed.get("dry_run") else "ok"
        summary = one_line(first_nonempty_line(result.content) or result.content, max_chars=260)
        changed_files = diff_changed_files(result.content, limit=4)
        if changed_files:
            summary = f"{summary}; diff files: {', '.join(changed_files)}"
        return EvidenceRecord(name, raw_path or name, summary, status=status)
    if name in {"git_diff", "git_status"}:
        status = "error" if result.is_error else "ok"
        if result.content.strip() in {"(empty)", "(empty diff)"}:
            status, summary = "empty", "No output."
        else:
            changed_files = diff_changed_files(result.content, limit=6)
            summary = "Changed files: " + ", ".join(changed_files) if changed_files else one_line(result.content, max_chars=260)
        return EvidenceRecord(name, "workspace", summary, status=status)
    if name in {"run_tests", "shell"}:
        command = str(parsed.get("command") or ("default test command" if name == "run_tests" else "command"))
        parts = [one_line(first_nonempty_line(result.content), max_chars=180)] if first_nonempty_line(result.content) else []
        exit_code = last_exit_code_line(result.content)
        if exit_code:
            parts.append(exit_code)
        return EvidenceRecord(name, one_line(command, max_chars=140), "; ".join(parts) or "Command executed.", status="error" if result.is_error else "ok")
    return None


def parse_tool_arguments(arguments: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def patch_preview_signature(args: dict[str, Any], resolved_path: Path) -> str:
    payload = {
        "path": str(resolved_path),
        "tag": args.get("tag"),
        "start_line": args.get("start_line"),
        "end_line": args.get("end_line"),
        "old_text": args.get("old_text"),
        "new_text": args.get("new_text"),
        "mode": args.get("mode") or "replace",
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


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


def evidence_root_for_path(path: Path, workspace: Path, allowed_dirs: tuple[Path, ...]) -> Path:
    roots = sorted(
        (workspace.resolve(), *(root.resolve() for root in allowed_dirs)),
        key=lambda item: len(item.parts),
        reverse=True,
    )
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root)
            return root
        except ValueError:
            continue
    return workspace.resolve()


def evidence_root_label(root: Path, workspace: Path, allowed_dirs: tuple[Path, ...]) -> str:
    try:
        if root.resolve() == workspace.resolve():
            return "primary"
    except OSError:
        return "(unknown)"
    return display_workspace_path(workspace, root, allowed_dirs)


def tool_evidence_provenance(
    parsed: Mapping[str, Any],
    workspace: Path,
    allowed_dirs: tuple[Path, ...],
) -> dict[str, str]:
    """Describe the authorized root a path-oriented tool result can speak for."""

    raw_path = parsed.get("path")
    path_text = raw_path.strip() if isinstance(raw_path, str) and raw_path.strip() else "."
    try:
        resolved = resolve_workspace_path(workspace, path_text, allowed_dirs)
    except PatchError:
        return {
            "evidence_root": "",
            "evidence_root_label": "(unknown)",
            "evidence_scope": "incomplete",
        }
    root = evidence_root_for_path(resolved, workspace, allowed_dirs)
    return {
        "evidence_root": str(root),
        "evidence_root_label": evidence_root_label(root, workspace, allowed_dirs),
        "evidence_scope": "root_local",
    }


def first_search_result_paths(content: str, *, limit: int) -> list[str]:
    paths: list[str] = []
    for line in content.splitlines():
        if not line or line.startswith("...") or line.startswith("Workspace roots:") or line.startswith("- "):
            continue
        path = line.split(":", 1)[0].strip()
        if path and path not in paths:
            paths.append(path)
        if len(paths) >= limit:
            break
    return paths


def first_result_line_paths(content: str, *, limit: int) -> list[str]:
    paths: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("No ") or stripped.startswith("["):
            continue
        path = stripped.split(":", 1)[0].strip()
        if path and path not in paths:
            paths.append(path)
        if len(paths) >= limit:
            break
    return paths


def diff_changed_files(content: str, *, limit: int) -> list[str]:
    files: list[str] = []
    for line in content.splitlines():
        path = ""
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3]
        elif line.startswith("+++ b/") or line.startswith("--- a/"):
            path = line[4:]
        if path.startswith(("a/", "b/")):
            path = path[2:]
        if path and path != "/dev/null" and path not in files:
            files.append(path)
        if len(files) >= limit:
            break
    return files


def lsp_subject(parsed: dict[str, Any]) -> str:
    parts = []
    for key in ("query", "symbol", "path"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}={value.strip()!r}")
    return " ".join(parts)


def first_content_lines(content: str, *, limit: int) -> list[str]:
    lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            lines.append(stripped)
        if len(lines) >= limit:
            break
    return lines


def first_nonempty_line(content: str) -> str:
    lines = first_content_lines(content, limit=1)
    return lines[0] if lines else ""


def last_exit_code_line(content: str) -> str:
    for line in reversed(content.splitlines()):
        if line.strip().startswith("[exit_code]"):
            return line.strip()
    return ""


def one_line(content: str, *, max_chars: int) -> str:
    normalized = " ".join(content.split())
    return normalized if len(normalized) <= max_chars else normalized[: max_chars - 14] + "...<truncated>"


def one_line_block(content: str, *, max_chars: int) -> str:
    if len(content) <= max_chars:
        return content
    marker = "\n...<evidence ledger truncated>"
    return content[: max(0, max_chars - len(marker))].rstrip() + marker


__all__ = [
    "EvidenceLedger",
    "EvidenceRecord",
    "display_read_file_path",
    "parse_tool_arguments",
]

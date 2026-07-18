from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ...tool_observation import ToolResultSummary


DEFAULT_TOOL_NAMES = frozenset(
    {
        "apply_patch",
        "ask_user",
        "git_diff",
        "git_status",
        "glob_files",
        "inspect_image",
        "learn",
        "list_files",
        "lsp_definition",
        "lsp_diagnostics",
        "lsp_document_symbols",
        "lsp_references",
        "lsp_status",
        "lsp_symbols",
        "lsp_workspace_symbols",
        "memory_read",
        "memory_write",
        "read_file",
        "rollback_patch",
        "run_tests",
        "search_code",
        "shell",
        "todo_add",
        "todo_read",
        "todo_update",
        "write_file",
    }
)

LSP_EVIDENCE_TOOL_NAMES = frozenset(
    {
        "lsp_definition",
        "lsp_diagnostics",
        "lsp_document_symbols",
        "lsp_references",
        "lsp_symbols",
        "lsp_workspace_symbols",
    }
)
CODE_EVIDENCE_TOOL_NAMES = frozenset({"read_file", "search_code", *LSP_EVIDENCE_TOOL_NAMES})
CODE_EVIDENCE_ALLOWED_TOOL_NAMES = frozenset({"glob_files", "list_files", *CODE_EVIDENCE_TOOL_NAMES})
# A document-only contract is narrower than a requirement document used as an
# input to a code investigation. The former must not quietly widen into source
# discovery after the first Markdown read.
DOCUMENT_ONLY_TOOL_NAMES = frozenset({"ask_user", "list_files", "read_file", "inspect_image"})
REQUIREMENT_DOC_TOOL_NAMES = frozenset({"ask_user", "inspect_image", "list_files", "read_file", "search_code"})
WORKSPACE_INVENTORY_TOOL_NAMES = frozenset({"glob_files", "list_files", "read_file"})
WORKSPACE_INVENTORY_DISCOVERY_TOOL_NAMES = frozenset({"glob_files"})
MAX_WORKSPACE_INVENTORY_DISCOVERY_CALLS_PER_ROOT = 2
MAX_WORKSPACE_INVENTORY_DISCOVERY_CALLS = 8
PLANNER_EXPLORE_TOOL_NAMES = frozenset(
    {
        "ask_user",
        "git_diff",
        "git_status",
        "glob_files",
        "list_files",
        "lsp_definition",
        "lsp_diagnostics",
        "lsp_document_symbols",
        "lsp_references",
        "lsp_status",
        "lsp_symbols",
        "lsp_workspace_symbols",
        "read_file",
        "search_code",
        "todo_add",
        "todo_read",
        "todo_update",
    }
)

READ_ONLY_FORBIDDEN_TOOL_NAMES = frozenset(
    {
        "apply_patch",
        "learn",
        "memory_write",
        "rollback_patch",
        "run_tests",
        "shell",
        "write_file",
    }
)


def _one_line(content: str, *, max_chars: int = 240) -> str:
    normalized = " ".join(content.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 14] + "...<truncated>"


@dataclass(frozen=True)
class ToolChoiceDecision:
    steering_required: bool
    allowed_tool_names: frozenset[str]
    reason: str
    rule_id: str | None = None
    # Keep the rule category stable for telemetry.  A scoped workflow may
    # still advance to a different concrete requirement within that category.
    requirement_identity: str = ""
    missing_requirements: tuple[str, ...] = ()
    preferred_tool_names: tuple[str, ...] = ()
    tool_call_hints: tuple[str, ...] = ()
    required_glob_roots: tuple[str, ...] = ()
    required_tool_arguments_json: str = ""
    scoped_read_paths: tuple[str, ...] = ()
    scoped_read_budget: int | None = None
    read_only_unlocated_on_exhaustion: bool = False
    stop_message: str | None = None
    force_final_answer_without_tools: bool = False

    @property
    def needs_steering(self) -> bool:
        return self.steering_required

    @property
    def allowed_tools(self) -> frozenset[str]:
        return self.allowed_tool_names

    @property
    def should_stop(self) -> bool:
        return self.stop_message is not None


def tool_choice_steering_signature(decision: ToolChoiceDecision, result_count: int) -> str:
    payload = {
        "rule_id": decision.rule_id,
        "missing": decision.missing_requirements,
        "results": result_count,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def tool_choice_steering_identity(decision: ToolChoiceDecision) -> str:
    """Return the stable lifecycle identity for a non-final queue reminder."""

    payload = {
        "rule_id": decision.rule_id,
        "requirement_identity": decision.requirement_identity,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def tool_choice_signature_count(
    signatures: set[str],
    rule_id: str | None,
    requirement_identity: str = "",
) -> int:
    prefix = f'"rule_id": "{rule_id}"' if rule_id else '"rule_id": null'
    identity = f'"requirement_identity": {json.dumps(requirement_identity, ensure_ascii=False)}'
    return sum(1 for signature in signatures if prefix in signature and identity in signature)


def tool_choice_steering_message(decision: ToolChoiceDecision, current_user_request: str | None) -> str:
    allowed = ", ".join(sorted(decision.allowed_tool_names)) or "(no tools currently allowed)"
    preferred = ", ".join(decision.preferred_tool_names) or "(none)"
    missing = ", ".join(decision.missing_requirements) or "(none)"
    hints = "\n".join(f"- call hint: {hint}" for hint in decision.tool_call_hints)
    request = _one_line(current_user_request or "", max_chars=800)
    if decision.force_final_answer_without_tools:
        if decision.rule_id == "document_artifacts_synthesis":
            return (
                "[Runtime tool choice queue]\n"
                "The explicitly requested document/image artifacts are now covered by successful observations. "
                "Your next response must be the final synthesis without tool calls: use only the collected evidence, "
                "cite the observed artifacts, preserve unresolved document discrepancies, and do not inspect paths "
                "outside the requested material set.\n"
                f"- rule: {decision.rule_id or 'unknown'}\n"
                f"- reason: {decision.reason}\n"
                f"- original request: {request}"
            )
        if decision.rule_id == "document_artifacts_limited_synthesis":
            return (
                "[Runtime tool choice queue]\n"
                "At least one explicitly requested document/image artifact is typed unavailable, and no missing "
                "artifact remains to retry. Your next response must be the final limited synthesis without tool calls: "
                "use only collected evidence, state the unavailable artifact as a limitation, and do not infer its contents.\n"
                f"- rule: {decision.rule_id or 'unknown'}\n"
                f"- reason: {decision.reason}\n"
                f"- original request: {request}"
            )
        return (
            "[Runtime tool choice queue]\n"
            "The bounded exploration budget is exhausted. Your next response must be the final answer without tool calls. "
            "Use only collected evidence, include searched scope and incomplete/truncated limits, and do not infer absence "
            "from omitted results.\n"
            f"- rule: {decision.rule_id or 'unknown'}\n"
            f"- reason: {decision.reason}\n"
            f"- original request: {request}"
        )
    return (
        "[Runtime tool choice queue]\n"
        "A required workflow gate is not satisfied yet. Use the allowed tool set for the next step; "
        "do not answer as final until the missing requirement is satisfied or you can explicitly explain why it cannot be satisfied.\n"
        f"- rule: {decision.rule_id or 'unknown'}\n"
        f"- missing: {missing}\n"
        f"- preferred next tools: {preferred}\n"
        f"- allowed tools now: {allowed}\n"
        f"- reason: {decision.reason}\n"
        f"{hints + chr(10) if hints else ''}"
        f"- original request: {request}"
    )


@dataclass(frozen=True)
class SoftToolDirective:
    """A bounded turn reminder which never changes the active tool schema."""

    kind: str
    message: str
    paths: tuple[str, ...] = ()


def session_evidence_reuse_directive(
    tool_results: Iterable[ToolResultSummary],
) -> SoftToolDirective | None:
    """Remind a follow-up turn that fresh cached evidence is already available.

    This follows the OMP soft-tool-choice shape: it is an advisory turn
    directive, not a schema restriction or a synthetic tool result. The model
    remains free to re-read a file when it needs a fresher observation.
    """

    cached = [
        result
        for result in tool_results
        if result.metadata.get("evidence_origin") == "session_cached"
    ]
    if not cached:
        return None
    paths: list[str] = []
    descriptions: list[str] = []
    for result in cached:
        path = result.path or str(result.metadata.get("display_path") or "")
        if path and path not in paths:
            paths.append(path)
        description = f"- {result.name}: {path or 'previous verified result'}"
        if description not in descriptions:
            descriptions.append(description)
    return SoftToolDirective(
        kind="session_evidence_reuse",
        paths=tuple(paths),
        message=(
            "[Runtime session evidence reminder]\n"
            "Fresh evidence from the immediately relevant earlier turn was revalidated for this request. "
            "Reuse it before repeating the same read/search; call a tool again only when the current question needs "
            "different scope or a new freshness check. This is advisory and does not restrict available tools.\n"
            + "\n".join(descriptions[:6])
        ),
    )

def _available_tool_names(available_tool_names: Iterable[str] | None) -> frozenset[str]:
    if available_tool_names is None:
        return DEFAULT_TOOL_NAMES
    return frozenset(str(name) for name in available_tool_names if str(name).strip())

def _allowed_subset(candidates: Iterable[str], allowed_tools: frozenset[str]) -> frozenset[str]:
    return frozenset(name for name in candidates if name in allowed_tools)

def _tool_name_set(tool_names: Iterable[str] | None, results: tuple[ToolResultSummary, ...]) -> set[str]:
    names = {str(name) for name in (tool_names or ()) if str(name).strip()}
    names.update(result.name for result in results if result.name)
    return names


def _normalize_tool_result(result: ToolResultSummary | Mapping[str, Any] | str) -> ToolResultSummary:
    if isinstance(result, ToolResultSummary):
        return result
    if isinstance(result, str):
        return _normalize_string_tool_result(result)
    name = str(result.get("tool_name") or result.get("name") or result.get("_lca_tool_name") or "")
    content = str(result.get("content") or result.get("summary") or result.get("result") or "")
    arguments = result.get("arguments") or result.get("args") or {}
    path = result.get("path")
    if path is None and isinstance(arguments, Mapping):
        path = arguments.get("path")
    error_value = result.get("is_error", result.get("error", False))
    changed = result.get("changed", result.get("workspace_changed"))
    metadata = result.get("metadata")
    return ToolResultSummary(
        name=name,
        content=content,
        is_error=bool(error_value),
        useless=bool(result.get("useless", False)),
        path=str(path) if path is not None else None,
        changed=changed if isinstance(changed, bool) else None,
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )


def _normalize_string_tool_result(result: str) -> ToolResultSummary:
    stripped = result.strip()
    name = ""
    content = stripped
    prefix = stripped.split(":", 1)[0].strip()
    if prefix in DEFAULT_TOOL_NAMES:
        name = prefix
        content = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
    return ToolResultSummary(name=name, content=content)

def _successful_tool_result(result: ToolResultSummary) -> bool:
    return bool(result.name) and not result.is_error


def has_code_evidence(
    seen_tool_names: set[str],
    results: tuple[ToolResultSummary, ...],
) -> bool:
    if any(_successful_tool_result(result) and result.name in CODE_EVIDENCE_TOOL_NAMES for result in results):
        return True
    return bool(seen_tool_names.intersection(CODE_EVIDENCE_TOOL_NAMES))


def _compact(value: str) -> str:
    return re.sub(r"[\s-]+", "_", (value or "").strip().lower())


def _lower_text(value: str) -> str:
    return (value or "").lower()

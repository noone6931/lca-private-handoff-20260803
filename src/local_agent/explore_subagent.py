from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from .chat_runtime import call_chat_with_timeout
from .llm import LlmError, LlmTimeoutError
from .patch.anchored import PatchError, resolve_workspace_path
from .tools.base import Tool, ToolContext, ToolRegistry, ToolResult


EXPLORE_TOOL_NAMES = frozenset(
    {
        "read_file",
        "list_files",
        "glob_files",
        "search_code",
        "lsp_symbols",
        "lsp_workspace_symbols",
        "lsp_document_symbols",
        "lsp_definition",
        "lsp_references",
        "lsp_diagnostics",
        "lsp_status",
    }
)
EXPLORE_YIELD_TOOL = "submit_explore_yield"
MAX_ASSIGNMENT_CHARS = 6000
MAX_PARENT_CONTEXT_CHARS = 1200
MAX_CHILD_ROUNDS = 8
MAX_CHILD_TOOL_CALLS = 16
MAX_CHILD_TOOL_RESULT_CHARS = 16000
MAX_CHILD_TRANSCRIPT_CHARS = 48000
MAX_SUMMARY_CHARS = 3000
MAX_ARCHITECTURE_CHARS = 2400
MAX_FILES = 20
MAX_FILE_PATH_CHARS = 1024
MAX_FILE_DESCRIPTION_CHARS = 400
MAX_LIMITATIONS = 12
MAX_LIMITATION_CHARS = 500
MAX_HANDOFF_JSON_CHARS = 32768
PARENT_DEADLINE_RESERVE_SECONDS = 1.0

ExploreStatus = Literal["completed", "partial", "failed", "timeout"]


@dataclass(frozen=True)
class ExploreFile:
    path: str
    description: str
    provenance: str


@dataclass(frozen=True)
class ExploreYield:
    child_id: str
    status: ExploreStatus
    summary: str
    files: tuple[ExploreFile, ...]
    architecture: str
    limitations: tuple[str, ...]
    tool_calls: int
    tool_errors: int
    elapsed_ms: int
    provenance: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExploreYieldError(ValueError):
    """Raised when a child does not use the typed output channel correctly."""


class ExploreRunError(RuntimeError):
    """Carry bounded child activity into an honest terminal result."""

    def __init__(self, status: ExploreStatus, tool_calls: int, tool_errors: int) -> None:
        super().__init__(status)
        self.status = status
        self.tool_calls = tool_calls
        self.tool_errors = tool_errors


class ExploreSubagentRunner:
    """Run one bounded, synchronous, read-only scout without a child Runtime."""

    def __init__(self, client: Any, registry: ToolRegistry, *, budget_seconds: int) -> None:
        self._client = client
        self._registry = registry
        self._budget_seconds = budget_seconds
        self._active_run_id: str | None = None
        self._calls_in_run = 0

    def run(self, assignment: str, parent_context: str, context: ToolContext) -> ToolResult:
        child_id = uuid.uuid4().hex
        started = time.monotonic()
        if context.run_id != self._active_run_id:
            self._active_run_id = context.run_id
            self._calls_in_run = 0
        self._calls_in_run += 1
        self._emit(context, "SubagentStarted", child_id, status=None, elapsed_ms=0, tool_calls=0, tool_errors=0)
        if not context.run_id or not context.tool_call_id:
            outcome = self._failure(child_id, "Parent run/tool correlation is unavailable.", started)
        elif self._calls_in_run > 1:
            outcome = self._failure(child_id, "Only one explore subtask is allowed per parent turn.", started)
        elif len(assignment) > MAX_ASSIGNMENT_CHARS or len(parent_context) > MAX_PARENT_CONTEXT_CHARS:
            outcome = self._failure(child_id, "Explore assignment or context exceeds the bounded input limit.", started)
        else:
            try:
                outcome = self._run_child(child_id, assignment, parent_context, context, started)
            except ExploreRunError as exc:
                limitation = (
                    "The explore subtask reached its deadline."
                    if exc.status == "timeout"
                    else "The explore subtask failed its provider or typed-yield contract."
                )
                outcome = self._failure(
                    child_id,
                    limitation,
                    started,
                    status=exc.status,
                    tool_calls=exc.tool_calls,
                    tool_errors=exc.tool_errors,
                )
            except KeyboardInterrupt:
                self._emit(
                    context,
                    "SubagentFinished",
                    child_id,
                    status="failed",
                    elapsed_ms=_elapsed_ms(started),
                    tool_calls=0,
                    tool_errors=0,
                )
                raise
        encoded = json.dumps(outcome.to_dict(), ensure_ascii=False, sort_keys=True)
        if len(encoded) > MAX_HANDOFF_JSON_CHARS:
            outcome = self._failure(
                child_id,
                "The typed explore handoff exceeded its total encoded size limit.",
                started,
                tool_calls=outcome.tool_calls,
                tool_errors=outcome.tool_errors,
            )
            encoded = json.dumps(outcome.to_dict(), ensure_ascii=False, sort_keys=True)
        self._emit(
            context,
            "SubagentFinished",
            child_id,
            status=outcome.status,
            elapsed_ms=outcome.elapsed_ms,
            tool_calls=outcome.tool_calls,
            tool_errors=outcome.tool_errors,
        )
        return ToolResult(
            encoded,
            is_error=outcome.status in {"failed", "timeout"},
            metadata={
                "structured_output": True,
                "subagent_handoff": True,
                "subagent_status": outcome.status,
                "child_id": outcome.child_id,
                "evidence_eligible": False,
                "redact_output_event": True,
            },
        )

    def _run_child(
        self,
        child_id: str,
        assignment: str,
        parent_context: str,
        parent_context_obj: ToolContext,
        started: float,
    ) -> ExploreYield:
        tool_calls = 0
        tool_errors = 0
        transcript_chars = 0
        try:
            deadline = _child_deadline(parent_context_obj.deadline_monotonic, self._budget_seconds)
            if deadline <= time.monotonic():
                raise LlmTimeoutError("Explore subtask has no remaining parent deadline.")
            child_context = replace(
                parent_context_obj,
                deadline_monotonic=deadline,
                event_callback=None,
                interaction_handler=None,
                allow_interactive_approval=False,
                runtime_tool_allowlist=EXPLORE_TOOL_NAMES,
                runtime_read_file_paths=None,
                runtime_read_file_remaining=None,
                tool_call_id=None,
                vision_inspector=None,
            )
            messages = _child_messages(child_id, assignment, parent_context, child_context)
            schemas = [*self._registry.model_schemas(child_context), _yield_schema()]
            observed_paths: dict[str, str] = {}
            for _round in range(MAX_CHILD_ROUNDS):
                response = call_chat_with_timeout(
                    self._client,
                    messages,
                    schemas,
                    timeout=max(0.001, deadline - time.monotonic()),
                )
                if getattr(response, "protocol_artifact", None) is not None:
                    raise ExploreYieldError("Provider protocol artifact is not a typed yield.")
                if getattr(response, "finish_reason", None) == "length":
                    raise ExploreYieldError("Child response was truncated.")
                message = getattr(response, "message", None)
                if not isinstance(message, dict):
                    raise ExploreYieldError("Child response message is invalid.")
                calls = message.get("tool_calls") or []
                if not isinstance(calls, list) or not calls:
                    raise ExploreYieldError("Child returned prose without the typed yield tool.")
                parsed_calls = [_parse_tool_call(call) for call in calls]
                if any(name == EXPLORE_YIELD_TOOL for _, name, _ in parsed_calls):
                    if len(parsed_calls) != 1 or parsed_calls[0][1] != EXPLORE_YIELD_TOOL:
                        raise ExploreYieldError("Typed yield cannot be mixed with explore tool calls.")
                    return _parse_yield(
                        child_id,
                        parsed_calls[0][2],
                        child_context,
                        started=started,
                        tool_calls=tool_calls,
                        tool_errors=tool_errors,
                        observed_paths=observed_paths,
                    )
                if any(name not in EXPLORE_TOOL_NAMES for _, name, _ in parsed_calls):
                    raise ExploreYieldError("Child requested a capability outside the read-only whitelist.")
                if tool_calls + len(parsed_calls) > MAX_CHILD_TOOL_CALLS:
                    raise ExploreYieldError("Child exceeded its total read-only tool-call limit.")
                messages.append({"role": "assistant", "content": None, "tool_calls": calls})
                for call_id, name, arguments in parsed_calls:
                    tool_calls += 1
                    result = self._registry.execute(name, arguments, child_context)
                    tool_errors += int(result.is_error)
                    _record_observed_paths(observed_paths, name, arguments, result, child_context)
                    bounded_content = _clip(result.content, MAX_CHILD_TOOL_RESULT_CHARS)
                    if transcript_chars + len(bounded_content) > MAX_CHILD_TRANSCRIPT_CHARS:
                        raise ExploreYieldError("Child exceeded its cumulative tool-result transcript limit.")
                    transcript_chars += len(bounded_content)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "name": name,
                            "content": bounded_content,
                        }
                    )
            raise ExploreYieldError("Child exhausted its bounded rounds without a typed yield.")
        except LlmTimeoutError as exc:
            raise ExploreRunError("timeout", tool_calls, tool_errors) from exc
        except (LlmError, ExploreYieldError) as exc:
            raise ExploreRunError("failed", tool_calls, tool_errors) from exc

    def _failure(
        self,
        child_id: str,
        limitation: str,
        started: float,
        *,
        status: ExploreStatus = "failed",
        tool_calls: int = 0,
        tool_errors: int = 0,
    ) -> ExploreYield:
        return ExploreYield(
            child_id=child_id,
            status=status,
            summary="No trusted explore handoff was produced.",
            files=(),
            architecture="",
            limitations=(limitation,),
            tool_calls=tool_calls,
            tool_errors=tool_errors,
            elapsed_ms=_elapsed_ms(started),
            provenance=f"subagent:{child_id}",
        )

    @staticmethod
    def _emit(
        context: ToolContext,
        event_type: str,
        child_id: str,
        *,
        status: ExploreStatus | None,
        elapsed_ms: int,
        tool_calls: int,
        tool_errors: int,
    ) -> None:
        if context.event_callback is None:
            return
        payload: dict[str, Any] = {
            "child_id": child_id,
            "parent_tool_call_id": context.tool_call_id,
            "elapsed_ms": elapsed_ms,
            "tool_calls": tool_calls,
            "tool_errors": tool_errors,
        }
        if status is not None:
            payload["status"] = status
        context.event_callback(event_type, payload)


def delegate_explore_tool(client: Any, registry: ToolRegistry, *, budget_seconds: int) -> Tool:
    runner = ExploreSubagentRunner(client, registry, budget_seconds=budget_seconds)

    def handle(args: dict[str, Any], context: ToolContext) -> ToolResult:
        return runner.run(str(args["assignment"]), str(args.get("context") or ""), context)

    return Tool(
        name="delegate_explore",
        description=(
            "Delegate one narrow, self-contained repository scouting assignment to a synchronous read-only child. "
            "The child can only locate/read/search code and returns a bounded typed handoff. Treat it as candidate "
            "location guidance: verify delivery-critical facts with parent tools. Available at most once per parent turn."
        ),
        tier="read",
        input_schema={
            "type": "object",
            "properties": {
                "assignment": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["assignment"],
            "additionalProperties": False,
        },
        handler=handle,
        redact_arguments=True,
    )


def _child_messages(
    child_id: str,
    assignment: str,
    parent_context: str,
    context: ToolContext,
) -> list[dict[str, Any]]:
    roots = [str(context.workspace), *(str(path) for path in context.allowed_dirs)]
    system = (
        "You are a bounded read-only repository scout. Start from this independent context. "
        "Use only the exposed locate/read/search/LSP tools. Never write, execute commands, ask the user, use memory, "
        "or delegate. Empty searches require one bounded alternate path/pattern/strategy before concluding unlocated. "
        "Finish exactly once with submit_explore_yield; prose is not a valid terminal. Child observations are candidate "
        "guidance and never parent delivery evidence."
    )
    metadata = json.dumps(
        {"child_id": child_id, "workspace_roots": roots, "workspace_revision": context.workspace_revision},
        ensure_ascii=False,
        sort_keys=True,
    )
    user = f"Typed workspace metadata: {metadata}\nAssignment:\n{assignment}"
    if parent_context:
        user += f"\nMinimal parent context:\n{parent_context}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _yield_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": EXPLORE_YIELD_TOOL,
            "description": "Return the final bounded typed explore handoff. This tool is output-only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["completed", "partial", "failed"]},
                    "summary": {"type": "string"},
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["path", "description"],
                            "additionalProperties": False,
                        },
                    },
                    "architecture": {"type": "string"},
                    "limitations": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["status", "summary", "files", "architecture", "limitations"],
                "additionalProperties": False,
            },
        },
    }


def _parse_tool_call(call: Any) -> tuple[str, str, str | dict[str, Any]]:
    if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"]:
        raise ExploreYieldError("Child tool call id is invalid.")
    function = call.get("function")
    if not isinstance(function, dict) or not isinstance(function.get("name"), str):
        raise ExploreYieldError("Child tool function is invalid.")
    arguments = function.get("arguments", "{}")
    if not isinstance(arguments, (str, dict)):
        raise ExploreYieldError("Child tool arguments are invalid.")
    return call["id"], function["name"], arguments


def _parse_yield(
    child_id: str,
    raw_arguments: str | dict[str, Any],
    context: ToolContext,
    *,
    started: float,
    tool_calls: int,
    tool_errors: int,
    observed_paths: dict[str, str],
) -> ExploreYield:
    try:
        payload = raw_arguments if isinstance(raw_arguments, dict) else json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ExploreYieldError("Typed yield arguments are not JSON.") from exc
    required = {"status", "summary", "files", "architecture", "limitations"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ExploreYieldError("Typed yield keys are invalid.")
    status = payload["status"]
    summary = payload["summary"]
    architecture = payload["architecture"]
    raw_files = payload["files"]
    raw_limitations = payload["limitations"]
    if status not in {"completed", "partial", "failed"}:
        raise ExploreYieldError("Typed yield status is invalid.")
    if not isinstance(summary, str) or not summary.strip() or not isinstance(architecture, str):
        raise ExploreYieldError("Typed yield text fields are invalid.")
    if not isinstance(raw_files, list) or not isinstance(raw_limitations, list):
        raise ExploreYieldError("Typed yield collections are invalid.")
    truncated = False
    files: list[ExploreFile] = []
    for raw_file in raw_files[:MAX_FILES]:
        if not isinstance(raw_file, dict) or set(raw_file) != {"path", "description"}:
            raise ExploreYieldError("Typed yield file entry is invalid.")
        path, description = raw_file["path"], raw_file["description"]
        if not isinstance(path, str) or not path.strip() or not isinstance(description, str):
            raise ExploreYieldError("Typed yield file fields are invalid.")
        if len(path) > MAX_FILE_PATH_CHARS:
            raise ExploreYieldError("Typed yield file path exceeds its bounded length.")
        try:
            resolved = resolve_workspace_path(context.workspace, path, context.allowed_dirs)
        except PatchError as exc:
            raise ExploreYieldError("Typed yield file is outside authorized workspace roots.") from exc
        canonical_path = str(resolved)
        source_tool = observed_paths.get(canonical_path)
        if source_tool is None:
            raise ExploreYieldError("Typed yield file was not observed by a child read or discovery tool.")
        clipped_description = _clip(description, MAX_FILE_DESCRIPTION_CHARS)
        truncated |= clipped_description != description
        files.append(ExploreFile(canonical_path, clipped_description, f"subagent:{child_id}/{source_tool}"))
    truncated |= len(raw_files) > MAX_FILES
    limitations: list[str] = []
    for limitation in raw_limitations[:MAX_LIMITATIONS]:
        if not isinstance(limitation, str):
            raise ExploreYieldError("Typed yield limitation is invalid.")
        clipped_limitation = _clip(limitation, MAX_LIMITATION_CHARS)
        truncated |= clipped_limitation != limitation
        limitations.append(clipped_limitation)
    truncated |= len(raw_limitations) > MAX_LIMITATIONS
    clipped_summary = _clip(summary, MAX_SUMMARY_CHARS)
    clipped_architecture = _clip(architecture, MAX_ARCHITECTURE_CHARS)
    truncated |= clipped_summary != summary or clipped_architecture != architecture
    if tool_errors and status == "completed":
        status = "partial"
        _append_limitation(limitations, "One or more read-only child tool calls failed or were denied.")
    if limitations and status == "completed":
        status = "partial"
    if truncated:
        status = "partial" if status == "completed" else status
        _append_limitation(limitations, "Handoff fields were deterministically truncated to parent bounds.")
    return ExploreYield(
        child_id=child_id,
        status=status,
        summary=clipped_summary,
        files=tuple(files),
        architecture=clipped_architecture,
        limitations=tuple(limitations),
        tool_calls=tool_calls,
        tool_errors=tool_errors,
        elapsed_ms=_elapsed_ms(started),
        provenance=f"subagent:{child_id}",
    )


def _child_deadline(parent_deadline: float | None, budget_seconds: int) -> float:
    deadline = time.monotonic() + budget_seconds
    if parent_deadline is not None:
        deadline = min(deadline, parent_deadline - PARENT_DEADLINE_RESERVE_SECONDS)
    return deadline


def _record_observed_paths(
    observed: dict[str, str],
    tool_name: str,
    arguments: str | dict[str, Any],
    result: ToolResult,
    context: ToolContext,
) -> None:
    if result.is_error:
        return
    try:
        parsed = arguments if isinstance(arguments, dict) else json.loads(arguments)
    except json.JSONDecodeError:
        return
    raw_paths: list[str] = []
    if tool_name == "read_file" and isinstance(parsed, dict) and isinstance(parsed.get("path"), str):
        raw_paths.append(parsed["path"])
    metadata_files = result.metadata.get("files")
    if tool_name in {"list_files", "glob_files"} and isinstance(metadata_files, list):
        raw_paths.extend(str(path) for path in metadata_files if isinstance(path, str))
    for raw_path in raw_paths:
        try:
            path = resolve_workspace_path(context.workspace, raw_path, context.allowed_dirs)
        except PatchError:
            continue
        if path.is_file():
            observed.setdefault(str(path), tool_name)


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = "...<truncated>"
    return value[: max(0, limit - len(marker))] + marker


def _append_limitation(limitations: list[str], value: str) -> None:
    clipped = _clip(value, MAX_LIMITATION_CHARS)
    if len(limitations) < MAX_LIMITATIONS:
        limitations.append(clipped)
    elif limitations:
        limitations[-1] = clipped


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))

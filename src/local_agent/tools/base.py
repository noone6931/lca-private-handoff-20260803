from __future__ import annotations

import difflib
import json
import select
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..inventory_contract import glob_inventory_denial_reason
from ..protocol.interactions import InteractionHandler
from ..protocol.interactions import InteractionRequest
from ..terminal_io import terminal_input_prompt
from .argument_normalization import normalize_compatibility_arguments


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False
    useless: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolContext:
    workspace: Path
    approval_mode: str
    state_dir: Path | None = None
    allowed_dirs: tuple[Path, ...] = ()
    session_id: str | None = None
    auto_approve_tools: tuple[str, ...] = ()
    tool_approval: dict[str, str] | None = None
    session_tool_approval: dict[str, str] | None = None
    deadline_monotonic: float | None = None
    git_baseline: dict[str, Any] | None = None
    current_user_request: str | None = None
    patch_relevance_checker: Callable[[str, Path], str | None] | None = None
    patch_preview_checker: Callable[[dict[str, Any], Path], str | None] | None = None
    event_callback: Callable[[str, dict[str, Any]], None] | None = None
    interaction_handler: InteractionHandler | None = None
    runtime_tool_allowlist: frozenset[str] | None = None
    runtime_read_file_paths: frozenset[str] | None = None
    runtime_read_file_remaining: int | None = None
    runtime_glob_required_roots: frozenset[str] | None = None
    vision_inspector: Callable[[Path, str, bytes, str], str] | None = None


def tool_state_dir(context: ToolContext) -> Path:
    return context.state_dir or context.workspace / ".local-agent"


class ToolValidationError(RuntimeError):
    """Raised when tool arguments do not match the tool schema."""


class VisionInspectionUnavailableError(RuntimeError):
    """Image bytes are authorized, but no explicit vision capability is configured."""


ToolHandler = Callable[[dict[str, Any], ToolContext], ToolResult]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    tier: str
    handler: ToolHandler

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self._tools = {tool.name: tool for tool in tools}

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.openai_schema() for tool in self._tools.values()]

    def model_schemas(self, context: ToolContext) -> list[dict[str, Any]]:
        exposed = set(self.exposed_tool_names(context))
        return [tool.openai_schema() for tool in self._tools.values() if tool.name in exposed]

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def tool_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def exposed_tool_names(self, context: ToolContext) -> tuple[str, ...]:
        return tuple(self._exposed_tool_names(context))

    def is_preapproved(self, name: str, context: ToolContext) -> bool:
        """Return whether a tool can run without an interactive approval read."""

        tool = self._tools.get(name)
        return tool is not None and tool_is_preapproved(tool, context)

    def execute(self, name: str, raw_arguments: str | dict[str, Any], context: ToolContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            suggested = tuple(difflib.get_close_matches(name, self._exposed_tool_names(context), n=3, cutoff=0.55))
            hint = f" Available related tools: {', '.join(suggested)}." if suggested else ""
            return ToolResult(
                f"Unknown tool: {name}.{hint}",
                is_error=True,
                metadata={
                    "unknown_tool": True,
                    "requested_tool": name,
                    "suggested_tools": list(suggested),
                },
            )
        if context.runtime_tool_allowlist is not None and name not in context.runtime_tool_allowlist:
            allowed = ", ".join(sorted(context.runtime_tool_allowlist)) or "(no tools)"
            return ToolResult(
                f"Runtime tool choice restriction: '{name}' is not allowed at this step. "
                f"Allowed tools: {allowed}. Follow the current workflow before retrying.",
                is_error=True,
                metadata={
                    "provider_schema_violation": True,
                    "requested_tool": name,
                    "allowed_tools": sorted(context.runtime_tool_allowlist),
                },
            )
        try:
            denial_reason = _approval_denial_reason(tool, context)
            if denial_reason:
                return ToolResult(
                    denial_reason,
                    is_error=True,
                    metadata={
                        "execution_status": "denied",
                        "denial_kind": "approval",
                        "tool": tool.name,
                    },
                )
            arguments = raw_arguments if isinstance(raw_arguments, dict) else json.loads(raw_arguments or "{}")
            if not isinstance(arguments, dict):
                return ToolResult("Tool arguments must be a JSON object.", is_error=True)
            arguments, compatibility_notes = normalize_compatibility_arguments(name, arguments)
            arguments = validate_tool_arguments(tool.input_schema, arguments)
            scope_denial = _runtime_read_file_scope_denial_reason(name, arguments, context)
            if scope_denial:
                return ToolResult(scope_denial, is_error=True)
            glob_scope_denial = _runtime_glob_scope_denial_reason(name, arguments, context)
            if glob_scope_denial:
                return ToolResult(
                    glob_scope_denial,
                    is_error=True,
                    metadata={
                        "active_tool_rejection": True,
                        "inventory_contract_rejection": True,
                        "tool": name,
                    },
                )
            result = tool.handler(arguments, context)
            if compatibility_notes:
                metadata = {**dict(result.metadata), "compatibility_normalized": list(compatibility_notes)}
                content = result.content
                if result.metadata.get("structured_output"):
                    try:
                        payload = json.loads(content)
                        if isinstance(payload, dict):
                            payload["compatibility_normalized"] = list(compatibility_notes)
                            content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                    except json.JSONDecodeError:
                        pass
                else:
                    content = (
                        f"{content}\n\n[compatibility normalized] {'; '.join(compatibility_notes)}. "
                        "Use canonical tool arguments on the next call."
                    )
                return ToolResult(
                    content,
                    is_error=result.is_error,
                    useless=result.useless,
                    metadata=metadata,
                )
            return result
        except Exception as exc:  # noqa: BLE001 - tool errors must be returned to the model.
            return ToolResult(f"{type(exc).__name__}: {exc}", is_error=True)

    def _exposed_tool_names(self, context: ToolContext) -> list[str]:
        """Return only tools this runtime could expose to the current model turn."""

        names = set(self._tools)
        if context.runtime_tool_allowlist is not None:
            names.intersection_update(context.runtime_tool_allowlist)
        names.difference_update(
            name for name, policy in (context.tool_approval or {}).items() if policy == "deny"
        )
        names.difference_update(
            name
            for name, policy in (context.session_tool_approval or {}).items()
            if policy == "reject_always"
        )
        if not _interaction_tool_can_prompt(context):
            names.discard("ask_user")
        return sorted(names)


def _runtime_read_file_scope_denial_reason(
    name: str,
    arguments: dict[str, Any],
    context: ToolContext,
) -> str | None:
    if name != "read_file" or context.runtime_read_file_paths is None:
        return None
    if context.runtime_read_file_remaining is not None and context.runtime_read_file_remaining <= 0:
        return (
            "Runtime candidate read budget exhausted: use the source/test evidence and hash tags already collected, "
            "then answer from the evidence already collected. Do not continue splitting the candidate files into more reads."
        )
    raw_path = arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = context.workspace / path
    if str(path.resolve()) in context.runtime_read_file_paths:
        return None
    allowed = ", ".join(sorted(context.runtime_read_file_paths)) or "(none)"
    return (
        "Runtime candidate read restriction: read_file may only revisit the selected candidate paths at this step. "
        f"Allowed paths: {allowed}. Retry a listed path with a narrower range, or answer from existing evidence."
    )


def _runtime_glob_scope_denial_reason(
    name: str,
    arguments: dict[str, Any],
    context: ToolContext,
) -> str | None:
    """Require inventory discovery to cover each root before broader exploration."""

    if name != "glob_files" or context.runtime_glob_required_roots is None:
        return None
    return glob_inventory_denial_reason(
        arguments,
        required_roots=context.runtime_glob_required_roots,
        workspace=context.workspace,
    )


def _approval_denial_reason(tool: Tool, context: ToolContext) -> str | None:
    config_policy = (context.tool_approval or {}).get(tool.name)
    session_policy = (context.session_tool_approval or {}).get(tool.name)
    if config_policy == "deny":
        return f"Tool '{tool.name}' is denied by tool_approval policy."
    if session_policy == "reject_always":
        return f"Tool '{tool.name}' is denied by session approval policy."
    if config_policy == "prompt":
        return _interactive_approval_denial_reason(tool, context, allow_session_cache=False)
    if session_policy == "prompt":
        return _interactive_approval_denial_reason(tool, context)
    if session_policy == "allow_always":
        return None
    if config_policy == "allow":
        return None
    if _approval_mode(context.approval_mode) == "yolo":
        return None
    if config_policy is None and tool.name in context.auto_approve_tools:
        return None
    mode = _approval_mode(context.approval_mode)
    if mode == "write" and tool.tier in {"read", "state", "interaction", "write"}:
        return None
    if mode == "always-ask" and tool.tier in {"read", "state", "interaction"}:
        return None
    return _interactive_approval_denial_reason(tool, context)


def _interaction_tool_can_prompt(context: ToolContext) -> bool:
    return context.interaction_handler is not None or sys.stdin.isatty()


def tool_is_preapproved(tool: Tool, context: ToolContext) -> bool:
    """Pure approval projection for background lifecycle owners.

    Background restore paths must never open an approval prompt.  This mirrors
    the non-interactive branches of ``_approval_denial_reason`` and returns
    false whenever policy would require user input.
    """

    config_policy = (context.tool_approval or {}).get(tool.name)
    session_policy = (context.session_tool_approval or {}).get(tool.name)
    if config_policy in {"deny", "prompt"} or session_policy in {"reject_always", "prompt"}:
        return False
    if session_policy == "allow_always" or config_policy == "allow":
        return True
    if _approval_mode(context.approval_mode) == "yolo":
        return True
    if config_policy is None and tool.name in context.auto_approve_tools:
        return True
    mode = _approval_mode(context.approval_mode)
    if mode == "write":
        return tool.tier in {"read", "state", "interaction", "write"}
    return mode == "always-ask" and tool.tier in {"read", "state", "interaction"}


def _interactive_approval_denial_reason(
    tool: Tool,
    context: ToolContext,
    *,
    allow_session_cache: bool = True,
) -> str | None:
    if context.interaction_handler is not None:
        return _interactive_approval_with_handler(tool, context, allow_session_cache=allow_session_cache)
    if not sys.stdin.isatty():
        _emit_context_event(
            context,
            "ApprovalResult",
            {
                "tool": tool.name,
                "tier": tool.tier,
                "decision": "non_interactive",
                "allowed": False,
            },
        )
        return (
            f"Tool '{tool.name}' requires approval, but stdin is not interactive. "
            "Run with an interactive terminal, use --approval-mode write for write-safe tasks, "
            "or use --approval-mode yolo only in a trusted workspace."
        )
    prompt = _approval_prompt(tool, allow_session_cache=allow_session_cache)
    _emit_context_event(
        context,
        "ApprovalRequested",
        {
            "tool": tool.name,
            "tier": tool.tier,
            "allow_session_cache": allow_session_cache,
        },
    )
    try:
        answer = _read_approval_answer(prompt, context)
    except EOFError:
        _emit_approval_result(tool, context, "eof", allowed=False)
        return f"Tool '{tool.name}' requires approval, but stdin closed before a decision."
    if answer is None:
        _emit_approval_result(tool, context, "cancelled", allowed=False)
        return f"Tool '{tool.name}' approval cancelled because budget_seconds is exhausted."
    if answer in {"y", "yes"}:
        _emit_approval_result(tool, context, "allow_once", allowed=True)
        return None
    if allow_session_cache and answer in {"s", "session", "always"}:
        if context.session_tool_approval is not None:
            context.session_tool_approval[tool.name] = "allow_always"
        _emit_approval_result(tool, context, "allow_session", allowed=True)
        return None
    if allow_session_cache and answer in {"d", "deny", "reject_always"}:
        if context.session_tool_approval is not None:
            context.session_tool_approval[tool.name] = "reject_always"
        _emit_approval_result(tool, context, "reject_session", allowed=False)
        return f"User denied tool execution for this session: {tool.name}"
    _emit_approval_result(tool, context, "reject_once", allowed=False)
    return f"User denied tool execution: {tool.name}"


def _interactive_approval_with_handler(
    tool: Tool,
    context: ToolContext,
    *,
    allow_session_cache: bool,
) -> str | None:
    prompt = _approval_prompt(tool, allow_session_cache=allow_session_cache)
    _emit_context_event(
        context,
        "ApprovalRequested",
        {
            "tool": tool.name,
            "tier": tool.tier,
            "allow_session_cache": allow_session_cache,
        },
    )
    _emit_context_event(
        context,
        "InteractionRequested",
        {"kind": "approval", "tool": tool.name, "tier": tool.tier},
    )
    result = context.interaction_handler.request_interaction(
        InteractionRequest(
            kind="approval",
            prompt=prompt,
            timeout_seconds=_interaction_timeout_seconds(context),
        )
    )
    if result.status == "cancelled":
        _emit_context_event(context, "InteractionCancelled", {"kind": "approval", "tool": tool.name})
        _emit_approval_result(tool, context, "cancelled", allowed=False)
        return f"Tool '{tool.name}' approval cancelled by user."
    if result.status != "answered":
        _emit_context_event(
            context,
            "InteractionCancelled",
            {"kind": "approval", "tool": tool.name, "reason": result.status},
        )
        _emit_approval_result(tool, context, "cancelled", allowed=False)
        if result.status == "eof":
            return f"Tool '{tool.name}' requires approval, but stdin closed before a decision."
        return f"Tool '{tool.name}' approval cancelled because budget_seconds is exhausted."
    answer = (result.value or "").strip().lower()
    _emit_context_event(
        context,
        "InteractionResolved",
        {"kind": "approval", "tool": tool.name, "answer": answer},
    )
    if answer in {"y", "yes"}:
        _emit_approval_result(tool, context, "allow_once", allowed=True)
        return None
    if allow_session_cache and answer in {"s", "session", "always"}:
        if context.session_tool_approval is not None:
            context.session_tool_approval[tool.name] = "allow_always"
        _emit_approval_result(tool, context, "allow_session", allowed=True)
        return None
    if allow_session_cache and answer in {"d", "deny", "reject_always"}:
        if context.session_tool_approval is not None:
            context.session_tool_approval[tool.name] = "reject_always"
        _emit_approval_result(tool, context, "reject_session", allowed=False)
        return f"User denied tool execution for this session: {tool.name}"
    _emit_approval_result(tool, context, "reject_once", allowed=False)
    return f"User denied tool execution: {tool.name}"


def _approval_prompt(tool: Tool, *, allow_session_cache: bool) -> str:
    if allow_session_cache:
        return (
            f"Allow {tool.tier} tool '{tool.name}'?\n"
            "[y] once / [s] always this session / [n] reject / [d] reject this session: "
        )
    return f"Allow {tool.tier} tool '{tool.name}'? [y/N] "


def _read_approval_answer(prompt: str, context: ToolContext) -> str | None:
    with terminal_input_prompt(sys.stdin):
        if context.deadline_monotonic is None:
            return input(prompt).strip().lower()
        remaining = context.deadline_monotonic - time.monotonic()
        if remaining <= 0:
            return None
        print(prompt, end="", flush=True)
        try:
            ready, _, _ = select.select([sys.stdin], [], [], remaining)
        except (OSError, ValueError):
            return None
        if not ready:
            return None
        line = sys.stdin.readline()
        if line == "":
            raise EOFError
        return line.strip().lower()


def _interaction_timeout_seconds(context: ToolContext) -> float | None:
    if context.deadline_monotonic is None:
        return None
    remaining = context.deadline_monotonic - time.monotonic()
    return max(0.0, remaining)


def _emit_approval_result(tool: Tool, context: ToolContext, decision: str, *, allowed: bool) -> None:
    _emit_context_event(
        context,
        "ApprovalResult",
        {
            "tool": tool.name,
            "tier": tool.tier,
            "decision": decision,
            "allowed": allowed,
        },
    )


def _emit_context_event(context: ToolContext, event_type: str, payload: dict[str, Any]) -> None:
    if context.event_callback is not None:
        context.event_callback(event_type, payload)


def _approval_mode(raw_mode: str) -> str:
    aliases = {"ask": "always-ask", "auto-read": "always-ask", "always_ask": "always-ask"}
    return aliases.get(raw_mode, raw_mode)


def validate_tool_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    validated = _validate_value(schema, arguments, "args", required=True)
    if not isinstance(validated, dict):
        raise ToolValidationError("Tool arguments must be an object.")
    return validated


def _validate_value(schema: dict[str, Any], value: Any, path: str, *, required: bool) -> Any:
    if value is None and not required:
        return None

    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, dict):
            raise ToolValidationError(f"{path} must be an object.")
        return _validate_object(schema, value, path)
    if expected_type == "string":
        if not isinstance(value, str):
            raise ToolValidationError(f"{path} must be a string.")
        _validate_enum(schema, value, path)
        return value
    if expected_type == "integer":
        normalized = _normalize_integer(value, path)
        _validate_number_bounds(schema, normalized, path)
        _validate_enum(schema, normalized, path)
        return normalized
    if expected_type == "boolean":
        if not isinstance(value, bool):
            raise ToolValidationError(f"{path} must be a boolean.")
        _validate_enum(schema, value, path)
        return value

    _validate_enum(schema, value, path)
    return value


def _validate_object(schema: dict[str, Any], value: dict[str, Any], path: str) -> dict[str, Any]:
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        raise ToolValidationError(f"{path}.properties must be an object in the tool schema.")
    required_fields = set(schema.get("required") or [])
    missing = sorted(field for field in required_fields if field not in value or value[field] is None)
    if missing:
        raise ToolValidationError(f"Missing required argument(s): {', '.join(missing)}.")

    if schema.get("additionalProperties") is False:
        extra = sorted(key for key in value if key not in properties)
        if extra:
            raise ToolValidationError(f"Unexpected argument(s): {', '.join(extra)}.")

    validated: dict[str, Any] = {}
    for key, raw in value.items():
        property_schema = properties.get(key)
        if property_schema is None:
            if schema.get("additionalProperties") is False:
                continue
            validated[key] = raw
            continue
        if raw is None and key not in required_fields:
            continue
        validated[key] = _validate_value(property_schema, raw, f"{path}.{key}", required=key in required_fields)
    return validated


def _normalize_integer(value: Any, path: str) -> int:
    if isinstance(value, bool):
        raise ToolValidationError(f"{path} must be an integer.")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    raise ToolValidationError(f"{path} must be an integer.")


def _validate_enum(schema: dict[str, Any], value: Any, path: str) -> None:
    choices = schema.get("enum")
    if choices is not None and value not in choices:
        rendered = ", ".join(repr(choice) for choice in choices)
        raise ToolValidationError(f"{path} must be one of: {rendered}.")


def _validate_number_bounds(schema: dict[str, Any], value: int, path: str) -> None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if minimum is not None and value < minimum:
        raise ToolValidationError(f"{path} must be >= {minimum}.")
    if maximum is not None and value > maximum:
        raise ToolValidationError(f"{path} must be <= {maximum}.")

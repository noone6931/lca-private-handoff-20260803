from __future__ import annotations

import difflib
import json
import select
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..protocol.cancellation import CancellationSignal, raise_if_cancelled
from .policy import ExecutionPolicyDecision
from .policy import evaluate_execution_policy
from .policy import execution_action
from ..protocol.interactions import InteractionHandler
from ..protocol.interactions import InteractionRequest
from ..platform.terminal import terminal_input_prompt
from ..workspace.context import WorkspaceRootIdentity
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
    run_id: str | None = None
    tool_call_id: str | None = None
    workspace_revision: int = 0
    workspace_identity: WorkspaceRootIdentity | None = None
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
    allow_interactive_approval: bool = True
    runtime_tool_allowlist: frozenset[str] | None = None
    runtime_read_file_paths: frozenset[str] | None = None
    runtime_read_file_remaining: int | None = None
    vision_inspector: Callable[[Path, str, bytes, str], str] | None = None
    cancel_event: CancellationSignal | None = None


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
    redact_arguments: bool = False

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

    def extended(self, extra_tools: tuple[Tool, ...]) -> "ToolRegistry":
        return ToolRegistry([*self._tools.values(), *extra_tools])

    def telemetry_arguments(self, name: str, arguments: Any) -> Any:
        tool = self._tools.get(name)
        return "[redacted by tool owner]" if tool is not None and tool.redact_arguments else arguments

    def session_safe_assistant_message(self, message: dict[str, Any]) -> dict[str, Any]:
        safe = dict(message)
        safe_calls: list[Any] = []
        for raw_call in message.get("tool_calls") or []:
            if not isinstance(raw_call, dict):
                safe_calls.append(raw_call)
                continue
            call = dict(raw_call)
            function = dict(call.get("function") or {})
            tool = self._tools.get(str(function.get("name") or ""))
            if tool is not None and tool.redact_arguments:
                function["arguments"] = "{}"
            call["function"] = function
            safe_calls.append(call)
        if "tool_calls" in message:
            safe["tool_calls"] = safe_calls
        return safe

    def exposed_tool_names(self, context: ToolContext) -> tuple[str, ...]:
        return tuple(self._exposed_tool_names(context))

    def is_preapproved(self, name: str, context: ToolContext) -> bool:
        """Return whether a tool can run without an interactive approval read."""

        tool = self._tools.get(name)
        return tool is not None and tool_is_preapproved(tool, context)

    def execute(self, name: str, raw_arguments: str | dict[str, Any], context: ToolContext) -> ToolResult:
        raise_if_cancelled(context.cancel_event)
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
        pending_session_grant = False
        try:
            policy_decision = _execution_policy_decision(
                tool,
                context,
                interactive_available=_interaction_tool_can_prompt(context),
            )
            _emit_context_event(context, "ExecutionPolicyEvaluated", policy_decision.event_payload())
            if policy_decision.outcome == "deny":
                return _approval_denied_result(tool, _execution_policy_denial_reason(tool, context, policy_decision))
            arguments = raw_arguments if isinstance(raw_arguments, dict) else json.loads(raw_arguments or "{}")
            if not isinstance(arguments, dict):
                return ToolResult("Tool arguments must be a JSON object.", is_error=True)
            arguments, compatibility_notes = normalize_compatibility_arguments(name, arguments)
            arguments = validate_tool_arguments(tool.input_schema, arguments)
            scope_denial = _runtime_read_file_scope_denial_reason(name, arguments, context)
            if scope_denial:
                return ToolResult(scope_denial, is_error=True)
            if policy_decision.outcome == "prompt":
                denial_reason, pending_session_grant = _interactive_approval_denial_reason(
                    tool, context, allow_session_cache=policy_decision.session_cache_allowed
                )
                if denial_reason:
                    return _approval_denied_result(tool, denial_reason)
            raise_if_cancelled(context.cancel_event)
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
                result = ToolResult(
                    content,
                    is_error=result.is_error,
                    useless=result.useless,
                    metadata=metadata,
                )
            if pending_session_grant:
                pending_session_grant = False
                _settle_pending_session_grant(tool, context, commit=not result.is_error)
            return result
        except Exception as exc:  # noqa: BLE001 - tool errors must be returned to the model.
            if pending_session_grant:
                pending_session_grant = False
                _settle_pending_session_grant(tool, context, commit=False)
            return ToolResult(f"{type(exc).__name__}: {exc}", is_error=True)
        except BaseException:
            if pending_session_grant:
                _settle_pending_session_grant(tool, context, commit=False)
            raise

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


def registry_schemas_for_context(registry: Any, context: ToolContext) -> list[dict[str, Any]]:
    """Return context-aware schemas while tolerating narrow characterization registries."""

    model_schemas = getattr(registry, "model_schemas", None)
    if callable(model_schemas):
        return model_schemas(context)
    return registry.schemas()


def session_safe_assistant_message(registry: Any, message: dict[str, Any]) -> dict[str, Any]:
    projector = getattr(registry, "session_safe_assistant_message", None)
    if callable(projector):
        return projector(message)
    return message


def telemetry_tool_arguments(registry: Any, name: str, arguments: Any) -> Any:
    projector = getattr(registry, "telemetry_arguments", None)
    if callable(projector):
        return projector(name, arguments)
    return arguments


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


def _interaction_tool_can_prompt(context: ToolContext) -> bool:
    return context.allow_interactive_approval and (context.interaction_handler is not None or sys.stdin.isatty())


def tool_is_preapproved(tool: Tool, context: ToolContext) -> bool:
    """Pure approval projection for background lifecycle owners.

    Background restore paths never open an approval prompt. The same typed
    evaluator used by execution returns false for every prompt or denial.
    """

    return _execution_policy_decision(tool, context, interactive_available=True).outcome == "allow"


def _execution_policy_decision(
    tool: Tool,
    context: ToolContext,
    *,
    interactive_available: bool,
) -> ExecutionPolicyDecision:
    return evaluate_execution_policy(
        execution_action(tool.name, tool.tier),
        approval_mode=context.approval_mode,
        config_policy=(context.tool_approval or {}).get(tool.name),
        session_policy=(context.session_tool_approval or {}).get(tool.name),
        auto_approved=tool.name in context.auto_approve_tools,
        interactive_available=interactive_available,
    )


def _execution_policy_denial_reason(tool: Tool, context: ToolContext, decision: ExecutionPolicyDecision) -> str:
    if decision.source == "config_per_tool":
        return f"Tool '{tool.name}' is denied by tool_approval policy."
    if decision.source == "session_per_tool":
        return f"Tool '{tool.name}' is denied by session approval policy."
    _emit_context_event(context, "ApprovalResult", {
        "tool": tool.name, "tier": tool.tier, "decision": "non_interactive", "allowed": False,
    })
    return (
        f"Tool '{tool.name}' requires approval, but stdin is not interactive. "
        "Run with an interactive terminal, use --approval-mode write for write-safe tasks, "
        "or use --approval-mode yolo only in a trusted workspace."
    )


def _approval_denied_result(tool: Tool, reason: str) -> ToolResult:
    return ToolResult(
        reason,
        is_error=True,
        metadata={"execution_status": "denied", "denial_kind": "approval", "tool": tool.name},
    )


def _interactive_approval_denial_reason(
    tool: Tool, context: ToolContext, *, allow_session_cache: bool = True
) -> tuple[str | None, bool]:
    if context.interaction_handler is not None:
        return _interactive_approval_with_handler(tool, context, allow_session_cache=allow_session_cache)
    prompt = _approval_prompt(tool, allow_session_cache=allow_session_cache)
    _emit_context_event(context, "ApprovalRequested", {
        "tool": tool.name, "tier": tool.tier, "allow_session_cache": allow_session_cache,
    })
    try:
        answer = _read_approval_answer(prompt, context)
    except EOFError:
        _emit_approval_result(tool, context, "eof", allowed=False)
        return f"Tool '{tool.name}' requires approval, but stdin closed before a decision.", False
    if answer is None:
        _emit_approval_result(tool, context, "cancelled", allowed=False)
        return f"Tool '{tool.name}' approval cancelled because budget_seconds is exhausted.", False
    return _approval_answer_denial_reason(tool, context, answer, allow_session_cache=allow_session_cache)


def _interactive_approval_with_handler(
    tool: Tool, context: ToolContext, *, allow_session_cache: bool
) -> tuple[str | None, bool]:
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
        return f"Tool '{tool.name}' approval cancelled by user.", False
    if result.status != "answered":
        _emit_context_event(
            context,
            "InteractionCancelled",
            {"kind": "approval", "tool": tool.name, "reason": result.status},
        )
        _emit_approval_result(tool, context, "cancelled", allowed=False)
        if result.status == "eof":
            return f"Tool '{tool.name}' requires approval, but stdin closed before a decision.", False
        return f"Tool '{tool.name}' approval cancelled because budget_seconds is exhausted.", False
    answer = (result.value or "").strip().lower()
    _emit_context_event(
        context,
        "InteractionResolved",
        {"kind": "approval", "tool": tool.name, "answer": answer},
    )
    return _approval_answer_denial_reason(tool, context, answer, allow_session_cache=allow_session_cache)


def _approval_answer_denial_reason(
    tool: Tool, context: ToolContext, answer: str, *, allow_session_cache: bool
) -> tuple[str | None, bool]:
    if answer in {"y", "yes"}:
        _emit_approval_result(tool, context, "allow_once", allowed=True)
        return None, False
    if allow_session_cache and answer in {"s", "session", "always"}:
        return None, True
    if allow_session_cache and answer in {"d", "deny", "reject_always"}:
        if context.session_tool_approval is not None:
            context.session_tool_approval[tool.name] = "reject_always"
        _emit_approval_result(tool, context, "reject_session", allowed=False)
        return f"User denied tool execution for this session: {tool.name}", False
    _emit_approval_result(tool, context, "reject_once", allowed=False)
    return f"User denied tool execution: {tool.name}", False


def _settle_pending_session_grant(tool: Tool, context: ToolContext, *, commit: bool) -> None:
    committed = commit and context.session_tool_approval is not None
    if committed:
        context.session_tool_approval[tool.name] = "allow_always"
    status = "committed" if committed else "discarded"
    _emit_approval_result(
        tool,
        context,
        "allow_session",
        allowed=True,
        session_grant_status=status,
    )


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


def _emit_approval_result(
    tool: Tool, context: ToolContext, decision: str, *, allowed: bool, session_grant_status: str | None = None
) -> None:
    payload: dict[str, Any] = {
        "tool": tool.name,
        "tier": tool.tier,
        "decision": decision,
        "allowed": allowed,
    }
    if session_grant_status is not None:
        payload.update(
            session_grant_requested=True,
            session_grant_was_pending=True,
            session_grant_status=session_grant_status,
        )
    _emit_context_event(
        context,
        "ApprovalResult",
        payload,
    )


def _emit_context_event(context: ToolContext, event_type: str, payload: dict[str, Any]) -> None:
    if context.event_callback is not None:
        context.event_callback(event_type, payload)


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

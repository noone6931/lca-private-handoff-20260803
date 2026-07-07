from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class ToolContext:
    workspace: Path
    approval_mode: str


class ToolValidationError(RuntimeError):
    """Raised when tool arguments do not match the tool schema."""


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

    def execute(self, name: str, raw_arguments: str | dict[str, Any], context: ToolContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"Unknown tool: {name}", is_error=True)
        try:
            denial_reason = _approval_denial_reason(tool, context)
            if denial_reason:
                return ToolResult(denial_reason, is_error=True)
            arguments = raw_arguments if isinstance(raw_arguments, dict) else json.loads(raw_arguments or "{}")
            if not isinstance(arguments, dict):
                return ToolResult("Tool arguments must be a JSON object.", is_error=True)
            arguments = validate_tool_arguments(tool.input_schema, arguments)
            return tool.handler(arguments, context)
        except Exception as exc:  # noqa: BLE001 - tool errors must be returned to the model.
            return ToolResult(f"{type(exc).__name__}: {exc}", is_error=True)


def _approval_denial_reason(tool: Tool, context: ToolContext) -> str | None:
    if context.approval_mode == "yolo":
        return None
    if context.approval_mode == "auto-read" and tool.tier == "read":
        return None
    if tool.tier == "read":
        return None
    if not sys.stdin.isatty():
        return (
            f"Tool '{tool.name}' requires approval, but stdin is not interactive. "
            "Run with an interactive terminal, use --approval-mode auto-read for read-only tasks, "
            "or use --approval-mode yolo only in a trusted workspace."
        )
    try:
        answer = input(f"Allow {tool.tier} tool '{tool.name}'? [y/N] ").strip().lower()
    except EOFError:
        return f"Tool '{tool.name}' requires approval, but stdin closed before a decision."
    if answer in {"y", "yes"}:
        return None
    return f"User denied tool execution: {tool.name}"


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

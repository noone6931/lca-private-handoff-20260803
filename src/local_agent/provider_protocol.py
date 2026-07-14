from __future__ import annotations

import re
import json
from html import unescape
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


_BAILIAN_PROVIDER_NAMES = frozenset({"bailian", "bailian-intl", "dashscope", "aliyun"})
_TOOL_ENVELOPE = re.compile(r"<tool_call>\s*(?P<body>.*?)\s*</tool_call>\s*$", re.DOTALL)
_FUNCTION_ENVELOPE = re.compile(
    r"\A\s*<function=(?P<name>[A-Za-z_][A-Za-z0-9_]*)>\s*(?P<parameters>.*?)\s*</function>\s*\Z",
    re.DOTALL,
)
_PARAMETER = re.compile(
    r"<parameter=(?P<name>[A-Za-z_][A-Za-z0-9_]*)>(?P<value>.*?)</parameter>",
    re.DOTALL,
)
INVALID_TOOL_CALL_NAME = "__invalid_tool_call"
MAX_BAILIAN_XML_ARGUMENT_CHARS = 20_000


@dataclass(frozen=True)
class ProviderProtocolArtifact:
    """A narrowly recognized provider tool envelope that arrived as text."""

    kind: str
    tool_name: str
    parameter_names: tuple[str, ...]
    preview: str


def classify_provider_content_artifact(
    provider: str,
    content: object,
) -> ProviderProtocolArtifact | None:
    """Classify only a complete known Bailian XML tool envelope.

    This deliberately does not strip or rewrite content. The Runtime owns the
    phase policy: a classified envelope is a provider protocol violation rather
    than ordinary user-visible text, while fenced/quoted XML stays untouched.
    """
    if provider.strip().lower() not in _BAILIAN_PROVIDER_NAMES or not isinstance(content, str):
        return None
    envelope = _TOOL_ENVELOPE.search(content)
    if envelope is None or _inside_fenced_code(content, envelope.start()):
        return None
    function = _FUNCTION_ENVELOPE.fullmatch(envelope.group("body"))
    if function is None:
        return None
    parameter_block = function.group("parameters")
    parameters = tuple(_PARAMETER.finditer(parameter_block))
    cursor = 0
    for parameter in parameters:
        if parameter_block[cursor : parameter.start()].strip():
            return None
        cursor = parameter.end()
    if parameter_block[cursor:].strip():
        return None
    tool_name = function.group("name")
    parameter_names = tuple(parameter.group("name") for parameter in parameters)
    return ProviderProtocolArtifact(
        kind="bailian_xml_tool_envelope",
        tool_name=tool_name,
        parameter_names=parameter_names,
        preview=_structural_preview(tool_name, parameter_names),
    )


def bounded_tool_call_names(tool_calls: list[object], *, limit: int = 8) -> list[str]:
    """Return only structural names; never retain provider argument values."""

    names: list[str] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, Mapping):
            continue
        function = tool_call.get("function")
        name = function.get("name") if isinstance(function, Mapping) else None
        if isinstance(name, str) and name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names


def protocol_violation_payload(
    *,
    phase: str,
    artifact_kind: str,
    steering_kind: str | None = None,
    tool_calls: list[object] = (),
    artifact: ProviderProtocolArtifact | None = None,
) -> dict[str, Any]:
    """Build redacted telemetry for an adapter-recognized protocol violation."""

    payload: dict[str, Any] = {
        "phase": phase,
        "kind": artifact_kind,
        "suppressed_tool_calls": len(tool_calls),
    }
    if steering_kind:
        payload["steering_kind"] = steering_kind
    if artifact is not None:
        payload.update(
            {
                "tool_name": artifact.tool_name,
                "parameter_names": list(artifact.parameter_names),
                "preview": artifact.preview,
            }
        )
    elif tool_calls:
        payload["tool_names"] = bounded_tool_call_names(tool_calls)
    return payload


def protocol_violation_message(*, phase: str) -> str:
    if phase == "forced_final":
        return (
            "未完成/未验证：模型在无工具的最终收束阶段返回了工具协议内容。"
            "Runtime 未执行其中的工具调用，也未将该协议内容作为最终答复展示；"
            "请重试或检查 provider 的 tool-calling 兼容性。"
        )
    return (
        "未完成/未验证：provider 将已识别的工具协议内容放进普通文本响应。"
        "Runtime 未执行其中的工具调用，也未将该协议内容作为最终答复展示；"
        "请重试或检查 provider 的 tool-calling 兼容性。"
    )


def normalize_provider_dialect_message(
    message: dict[str, Any],
    *,
    provider: str,
) -> tuple[dict[str, Any], tuple[ProviderProtocolArtifact, ...]]:
    """Normalize only recognized provider dialects; preserve all other wire shapes."""

    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return message, ()
    normalized_calls: list[Any] = []
    artifacts: list[ProviderProtocolArtifact] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            normalized_calls.append(tool_call)
            continue
        function = tool_call.get("function")
        if not isinstance(function, dict):
            normalized_calls.append(tool_call)
            continue
        tool_name = function.get("name")
        arguments = function.get("arguments")
        if not isinstance(tool_name, str) or not isinstance(arguments, str):
            normalized_calls.append(tool_call)
            continue
        normalized = _parse_bailian_structured_tool_arguments(
            provider=provider,
            outer_tool_name=tool_name,
            arguments=arguments.strip(),
        )
        if normalized is None:
            normalized_calls.append(tool_call)
            continue
        parsed_arguments, artifact = normalized
        normalized_calls.append(
            {
                **tool_call,
                "function": {
                    **function,
                    "arguments": json.dumps(parsed_arguments, ensure_ascii=False, sort_keys=True),
                },
            }
        )
        artifacts.append(artifact)
    return {**message, "tool_calls": normalized_calls}, tuple(artifacts)


def provider_safe_assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    """Return the generic provider-safe projection without dialect parsing."""

    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return message
    safe_tool_calls = [_provider_safe_tool_call(tool_call, index) for index, tool_call in enumerate(tool_calls)]
    return {**message, "tool_calls": safe_tool_calls}


def _provider_safe_tool_call(
    tool_call: Any,
    index: int,
) -> dict[str, Any]:
    if not isinstance(tool_call, dict):
        return {
            "id": f"invalid_tool_call_{index}",
            "type": "function",
            "function": {"name": INVALID_TOOL_CALL_NAME, "arguments": "{}"},
        }
    function = tool_call.get("function")
    function = function if isinstance(function, dict) else {}
    tool_call_id = tool_call.get("id")
    if not isinstance(tool_call_id, str) or not tool_call_id.strip():
        tool_call_id = f"invalid_tool_call_{index}"
    tool_name = _provider_safe_tool_name(function.get("name"))
    return {
        **tool_call,
        "id": tool_call_id,
        "type": tool_call.get("type") or "function",
        "function": {
            **function,
            "name": tool_name,
            "arguments": _provider_safe_tool_arguments(function.get("arguments")),
        },
    }


def _provider_safe_tool_name(name: Any) -> str:
    if not isinstance(name, str):
        return INVALID_TOOL_CALL_NAME
    normalized = name.strip()
    if not normalized or not all(char.isalnum() or char == "_" for char in normalized):
        return INVALID_TOOL_CALL_NAME
    return normalized


def _provider_safe_tool_arguments(arguments: Any) -> str:
    if isinstance(arguments, dict):
        return json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    if not isinstance(arguments, str):
        return "{}"
    stripped = arguments.strip()
    if not stripped:
        return "{}"
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return json.dumps({"_invalid_arguments": stripped[:500]}, ensure_ascii=False, sort_keys=True)
    if not isinstance(parsed, dict):
        return json.dumps({"_invalid_arguments": parsed}, ensure_ascii=False, sort_keys=True, default=str)
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True)


def _parse_bailian_structured_tool_arguments(
    *,
    provider: str,
    outer_tool_name: str,
    arguments: str,
) -> tuple[dict[str, Any], ProviderProtocolArtifact] | None:
    """Parse one complete Bailian XML envelope already inside a typed tool call."""

    if provider.strip().lower() not in _BAILIAN_PROVIDER_NAMES:
        return None
    if not arguments or len(arguments) > MAX_BAILIAN_XML_ARGUMENT_CHARS:
        return None
    envelope = _TOOL_ENVELOPE.fullmatch(arguments)
    if envelope is None:
        return None
    function = _FUNCTION_ENVELOPE.fullmatch(envelope.group("body"))
    if function is None or function.group("name") != outer_tool_name:
        return None
    parameter_block = function.group("parameters")
    parameters = tuple(_PARAMETER.finditer(parameter_block))
    cursor = 0
    parsed: dict[str, Any] = {}
    for parameter in parameters:
        if parameter_block[cursor : parameter.start()].strip():
            return None
        cursor = parameter.end()
        name = parameter.group("name")
        if name in parsed:
            return None
        parsed[name] = _parse_bailian_parameter_value(parameter.group("value"))
    if parameter_block[cursor:].strip():
        return None
    parameter_names = tuple(parsed)
    return parsed, ProviderProtocolArtifact(
        kind="bailian_xml_structured_arguments",
        tool_name=outer_tool_name,
        parameter_names=parameter_names,
        preview=_structural_preview(outer_tool_name, parameter_names),
    )


def _parse_bailian_parameter_value(value: str) -> Any:
    stripped = unescape(value.strip())
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        if stripped == "True":
            return True
        if stripped == "False":
            return False
        if stripped == "None":
            return None
        return stripped


def _inside_fenced_code(content: str, position: int) -> bool:
    return content[:position].count("```") % 2 == 1


def _structural_preview(tool_name: str, parameter_names: tuple[str, ...]) -> str:
    parameters = ",".join(parameter_names) if parameter_names else "(none)"
    return f"tool={tool_name}; parameters={parameters}"


__all__ = [
    "ProviderProtocolArtifact",
    "INVALID_TOOL_CALL_NAME",
    "bounded_tool_call_names",
    "classify_provider_content_artifact",
    "protocol_violation_message",
    "protocol_violation_payload",
    "normalize_provider_dialect_message",
    "provider_safe_assistant_message",
]

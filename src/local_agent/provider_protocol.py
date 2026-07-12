from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


_BAILIAN_PROVIDER_NAMES = frozenset({"bailian", "bailian-intl", "dashscope", "aliyun"})
_TOOL_ENVELOPE = re.compile(r"<tool_call>\s*(?P<body>.*?)\s*</tool_call>\s*$", re.DOTALL)
_FUNCTION_ENVELOPE = re.compile(
    r"\A\s*<function=(?P<name>[A-Za-z_][A-Za-z0-9_]*)>\s*(?P<parameters>.*?)\s*</function>\s*\Z",
    re.DOTALL,
)
_PARAMETER = re.compile(r"<parameter=(?P<name>[A-Za-z_][A-Za-z0-9_]*)>.*?</parameter>", re.DOTALL)


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


def _inside_fenced_code(content: str, position: int) -> bool:
    return content[:position].count("```") % 2 == 1


def _structural_preview(tool_name: str, parameter_names: tuple[str, ...]) -> str:
    parameters = "".join(f"<parameter={name}>…</parameter>" for name in parameter_names)
    return f"<tool_call><function={tool_name}>{parameters}</function></tool_call>"


__all__ = [
    "ProviderProtocolArtifact",
    "bounded_tool_call_names",
    "classify_provider_content_artifact",
    "protocol_violation_message",
    "protocol_violation_payload",
]

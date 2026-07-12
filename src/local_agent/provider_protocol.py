from __future__ import annotations

import re
from dataclasses import dataclass


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

    This deliberately does not strip or rewrite content. Callers decide whether
    the current phase treats a classified envelope as a protocol violation or
    leaves it as ordinary user-visible text.
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


def _inside_fenced_code(content: str, position: int) -> bool:
    return content[:position].count("```") % 2 == 1


def _structural_preview(tool_name: str, parameter_names: tuple[str, ...]) -> str:
    parameters = "".join(f"<parameter={name}>…</parameter>" for name in parameter_names)
    return f"<tool_call><function={tool_name}>{parameters}</function></tool_call>"


__all__ = ["ProviderProtocolArtifact", "classify_provider_content_artifact"]

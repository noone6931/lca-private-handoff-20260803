from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


ExtensionTransport = Literal["mcp-stdio", "connector"]
ExtensionTier = Literal["read", "network", "write", "exec", "state", "interaction"]

EXTENSION_TRANSPORTS = frozenset({"mcp-stdio", "connector"})
EXTENSION_TIERS = frozenset({"read", "network", "write", "exec", "state", "interaction"})
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}")


@dataclass(frozen=True)
class ExtensionToolDeclaration:
    name: str
    tier: ExtensionTier
    input_schema: dict[str, object]
    output_bytes: int
    redact_arguments: bool = False

    def __post_init__(self) -> None:
        _require_identifier("tool name", self.name)
        if self.tier not in EXTENSION_TIERS:
            raise ValueError(f"tier must be one of: {', '.join(sorted(EXTENSION_TIERS))}")
        if self.input_schema.get("type") != "object":
            raise ValueError("extension tool input_schema must describe an object")
        if not 1 <= self.output_bytes <= 1_048_576:
            raise ValueError("output_bytes must be between 1 and 1048576")


@dataclass(frozen=True)
class ExtensionManifest:
    plugin_id: str
    version: str
    transport: ExtensionTransport
    tools: tuple[ExtensionToolDeclaration, ...] = ()
    server_command: tuple[str, ...] = ()
    connector_id: str | None = None

    def __post_init__(self) -> None:
        _require_identifier("plugin_id", self.plugin_id)
        if not self.version.strip():
            raise ValueError("version must not be empty")
        if self.transport not in EXTENSION_TRANSPORTS:
            raise ValueError(
                f"transport must be one of: {', '.join(sorted(EXTENSION_TRANSPORTS))}"
            )
        tool_names = [tool.name for tool in self.tools]
        if len(set(tool_names)) != len(tool_names):
            raise ValueError("extension tool names must be unique")
        if self.transport == "mcp-stdio":
            if not self.server_command or not self.server_command[0].strip():
                raise ValueError("mcp-stdio extension requires server_command")
            if self.connector_id is not None:
                raise ValueError("mcp-stdio extension cannot declare connector_id")
        if self.transport == "connector":
            if not self.connector_id or not self.connector_id.strip():
                raise ValueError("connector extension requires connector_id")
            if self.server_command:
                raise ValueError("connector extension cannot declare server_command")

    def namespaced_tool_name(self, tool: ExtensionToolDeclaration) -> str:
        if tool not in self.tools:
            raise ValueError("tool is not declared by this extension")
        return f"plugin__{self.plugin_id}__{tool.name}"


def _require_identifier(name: str, value: str) -> None:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must match {_IDENTIFIER.pattern}")

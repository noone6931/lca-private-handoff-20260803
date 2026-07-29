from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ProviderTransport = Literal[
    "openai-chat",
    "openai-responses",
    "anthropic-messages",
    "gemini",
    "openai-compatible-local",
]
ProviderToolProtocol = Literal["openai-tools", "responses-tools", "anthropic-tools", "gemini-tools"]
ProviderAuthScheme = Literal["api-key", "bearer", "none"]

PROVIDER_TRANSPORTS = frozenset(
    {
        "openai-chat",
        "openai-responses",
        "anthropic-messages",
        "gemini",
        "openai-compatible-local",
    }
)
PROVIDER_TOOL_PROTOCOLS = frozenset(
    {"openai-tools", "responses-tools", "anthropic-tools", "gemini-tools"}
)
PROVIDER_AUTH_SCHEMES = frozenset({"api-key", "bearer", "none"})


@dataclass(frozen=True)
class ProviderCapabilities:
    streaming: bool
    tool_calls: bool
    vision: bool
    structured_output: bool
    web_search: bool
    prompt_cache: bool
    reasoning: bool


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    transport: ProviderTransport
    auth_scheme: ProviderAuthScheme
    tool_protocol: ProviderToolProtocol
    context_window: int
    capabilities: ProviderCapabilities

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("provider_id must not be empty")
        if self.transport not in PROVIDER_TRANSPORTS:
            raise ValueError(
                f"transport must be one of: {', '.join(sorted(PROVIDER_TRANSPORTS))}"
            )
        if self.auth_scheme not in PROVIDER_AUTH_SCHEMES:
            raise ValueError(
                f"auth_scheme must be one of: {', '.join(sorted(PROVIDER_AUTH_SCHEMES))}"
            )
        if self.tool_protocol not in PROVIDER_TOOL_PROTOCOLS:
            raise ValueError(
                f"tool_protocol must be one of: {', '.join(sorted(PROVIDER_TOOL_PROTOCOLS))}"
            )
        if self.context_window < 1:
            raise ValueError("context_window must be positive")

    def require_capability(self, name: str) -> None:
        if name not in ProviderCapabilities.__dataclass_fields__:
            raise ValueError(f"unknown provider capability: {name}")
        if not bool(getattr(self.capabilities, name)):
            raise ValueError(f"provider {self.provider_id} does not support {name}")

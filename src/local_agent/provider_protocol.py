"""Compatibility facade for provider protocol normalization."""

from .providers.protocol import (
    INVALID_TOOL_CALL_NAME,
    ProviderProtocolArtifact,
    bounded_tool_call_names,
    classify_provider_content_artifact,
    normalize_provider_dialect_message,
    protocol_violation_message,
    protocol_violation_payload,
    provider_allows_provisional_text,
    provider_safe_assistant_message,
)

__all__ = [
    "INVALID_TOOL_CALL_NAME",
    "ProviderProtocolArtifact",
    "bounded_tool_call_names",
    "classify_provider_content_artifact",
    "normalize_provider_dialect_message",
    "protocol_violation_message",
    "protocol_violation_payload",
    "provider_allows_provisional_text",
    "provider_safe_assistant_message",
]

"""Compatibility imports for completion auditing."""

from .evidence.completion import (
    CompletionAuditItem,
    CompletionAuditResult,
    audit_completion,
    render_completion_audit_message,
)

__all__ = [
    "CompletionAuditItem",
    "CompletionAuditResult",
    "audit_completion",
    "render_completion_audit_message",
]

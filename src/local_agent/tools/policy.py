from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ExecutionPolicyOutcome = Literal["allow", "prompt", "deny"]
ExecutionPolicySource = Literal[
    "config_per_tool",
    "session_per_tool",
    "approval_mode",
    "auto_approve",
    "non_interactive",
]
SandboxState = Literal["none", "unsandboxed"]

EXECUTION_POLICY_OUTCOMES = frozenset({"allow", "prompt", "deny"})
EXECUTION_POLICY_SOURCES = frozenset(
    {"config_per_tool", "session_per_tool", "approval_mode", "auto_approve", "non_interactive"}
)
EXECUTION_SANDBOX_STATES = frozenset({"none", "unsandboxed"})

_APPROVAL_MODE_ALLOWED_TIERS = {
    "always-ask": frozenset({"read", "state", "interaction"}),
    "write": frozenset({"read", "state", "interaction", "write"}),
    "yolo": frozenset({"read", "state", "interaction", "write", "exec"}),
}
_APPROVAL_MODE_ALIASES = {"ask": "always-ask", "auto-read": "always-ask", "always_ask": "always-ask"}
_CAPABILITY_CLASS_BY_TIER = {
    "read": "workspace_read",
    "write": "workspace_write",
    "exec": "process_exec",
    "state": "session_state",
    "interaction": "user_interaction",
}


@dataclass(frozen=True)
class ExecutionAction:
    tool_name: str
    tier: str
    capability_class: str


@dataclass(frozen=True)
class ExecutionPolicyDecision:
    action: ExecutionAction
    outcome: ExecutionPolicyOutcome
    source: ExecutionPolicySource
    approval_mode: str
    session_cache_allowed: bool
    sandbox_state: SandboxState

    def event_payload(self) -> dict[str, object]:
        return {
            "tool": self.action.tool_name,
            "tier": self.action.tier,
            "capability_class": self.action.capability_class,
            "outcome": self.outcome,
            "source": self.source,
            "approval_mode": self.approval_mode,
            "session_cache_allowed": self.session_cache_allowed,
            "sandbox_state": self.sandbox_state,
        }


def execution_action(tool_name: str, tier: str) -> ExecutionAction:
    return ExecutionAction(
        tool_name=tool_name,
        tier=tier,
        capability_class=_CAPABILITY_CLASS_BY_TIER.get(tier, "unknown"),
    )


def evaluate_execution_policy(
    action: ExecutionAction,
    *,
    approval_mode: str,
    config_policy: str | None = None,
    session_policy: str | None = None,
    auto_approved: bool = False,
    interactive_available: bool = True,
) -> ExecutionPolicyDecision:
    """Resolve the existing LCA approval precedence without performing I/O."""

    mode = normalize_approval_mode(approval_mode)
    if config_policy == "deny":
        return _decision(action, "deny", "config_per_tool", mode)
    if session_policy == "reject_always":
        return _decision(action, "deny", "session_per_tool", mode)
    if config_policy == "prompt":
        return _prompt_decision(
            action,
            source="config_per_tool",
            mode=mode,
            session_cache_allowed=False,
            interactive_available=interactive_available,
        )
    if session_policy == "prompt":
        return _prompt_decision(
            action,
            source="session_per_tool",
            mode=mode,
            session_cache_allowed=True,
            interactive_available=interactive_available,
        )
    if session_policy == "allow_always":
        return _decision(action, "allow", "session_per_tool", mode)
    if config_policy == "allow":
        return _decision(action, "allow", "config_per_tool", mode)
    if mode == "yolo":
        return _decision(action, "allow", "approval_mode", mode)
    if config_policy is None and auto_approved:
        return _decision(action, "allow", "auto_approve", mode)
    if action.tier in _APPROVAL_MODE_ALLOWED_TIERS.get(mode, frozenset()):
        return _decision(action, "allow", "approval_mode", mode)
    return _prompt_decision(
        action,
        source="approval_mode",
        mode=mode,
        session_cache_allowed=True,
        interactive_available=interactive_available,
    )


def normalize_approval_mode(raw_mode: str) -> str:
    return _APPROVAL_MODE_ALIASES.get(raw_mode, raw_mode)


def _prompt_decision(
    action: ExecutionAction,
    *,
    source: ExecutionPolicySource,
    mode: str,
    session_cache_allowed: bool,
    interactive_available: bool,
) -> ExecutionPolicyDecision:
    if not interactive_available:
        return _decision(
            action,
            "deny",
            "non_interactive",
            mode,
            session_cache_allowed=session_cache_allowed,
        )
    return _decision(action, "prompt", source, mode, session_cache_allowed=session_cache_allowed)


def _decision(
    action: ExecutionAction,
    outcome: ExecutionPolicyOutcome,
    source: ExecutionPolicySource,
    mode: str,
    *,
    session_cache_allowed: bool = False,
) -> ExecutionPolicyDecision:
    return ExecutionPolicyDecision(
        action=action,
        outcome=outcome,
        source=source,
        approval_mode=mode,
        session_cache_allowed=session_cache_allowed,
        sandbox_state=_sandbox_state(action),
    )


def _sandbox_state(action: ExecutionAction) -> SandboxState:
    if action.tier == "exec":
        return "unsandboxed"
    return "none"

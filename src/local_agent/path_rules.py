"""Compatibility imports for path-scoped workspace rules."""

from .workspace.path_rules import (
    PathRule,
    PathRuleDiagnostic,
    PathRuleIndex,
    candidate_paths_for_path_rules,
    discover_path_scoped_rules,
    matching_path_rule_context,
    render_path_rule_metadata,
)

__all__ = [
    "PathRule",
    "PathRuleDiagnostic",
    "PathRuleIndex",
    "candidate_paths_for_path_rules",
    "discover_path_scoped_rules",
    "matching_path_rule_context",
    "render_path_rule_metadata",
]

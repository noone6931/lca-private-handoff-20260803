"""Compatibility imports for requirement evidence."""

from .evidence.requirements import (
    DocumentLocator,
    RequirementEvidence,
    document_locator_excerpt,
    is_requirement_source_path,
    parse_document_line_range,
    parse_document_locators,
    render_pinned_requirement_evidence,
    requirement_citation_examples,
    requirement_fact_citation_issues,
    update_requirement_evidence,
)

__all__ = [
    "DocumentLocator",
    "RequirementEvidence",
    "document_locator_excerpt",
    "is_requirement_source_path",
    "parse_document_line_range",
    "parse_document_locators",
    "render_pinned_requirement_evidence",
    "requirement_citation_examples",
    "requirement_fact_citation_issues",
    "update_requirement_evidence",
]

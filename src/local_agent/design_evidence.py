"""Compatibility imports for design evidence coverage."""

from .evidence.design import (
    DesignEvidenceCoverageDecision,
    DesignEvidenceCoverageSteerer,
    WorkspaceEvidenceRootProjection,
    cross_root_design_evidence_roots,
    missing_design_evidence_roots,
    project_workspace_evidence_roots,
)

__all__ = [
    "DesignEvidenceCoverageDecision",
    "DesignEvidenceCoverageSteerer",
    "WorkspaceEvidenceRootProjection",
    "cross_root_design_evidence_roots",
    "missing_design_evidence_roots",
    "project_workspace_evidence_roots",
]

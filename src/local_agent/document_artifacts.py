"""Compatibility imports for document artifact evidence."""

from .evidence.documents import (
    DocumentArtifactCoverage,
    DocumentArtifactRequirement,
    document_artifact_coverage,
    document_material_targets,
    extract_document_artifact_requirements,
    local_artifact_references,
    missing_document_artifacts,
    unavailable_document_artifacts,
)

__all__ = [name for name in globals() if not name.startswith("_")]

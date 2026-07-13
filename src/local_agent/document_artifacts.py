"""Typed artifact coverage for document-only requirement analysis."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from .tool_observation import ToolResultSummary


ArtifactKind = Literal["markdown", "html", "image"]


@dataclass(frozen=True)
class DocumentArtifactRequirement:
    """One user-requested document artifact, exact when a filename was supplied."""

    kind: ArtifactKind
    reference: str
    exact: bool = False

    @property
    def label(self) -> str:
        return self.reference if self.exact else self.kind


@dataclass(frozen=True)
class DocumentArtifactCoverage:
    requirement: DocumentArtifactRequirement
    status: Literal["observed", "unavailable", "missing"]
    path: str = ""


_EXTENSION_KIND: dict[str, ArtifactKind] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".html": "html",
    ".htm": "html",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
}
_ARTIFACT_SUFFIX = r"(?:md|markdown|html?|png|jpe?g|gif|webp)"
_QUOTED_ARTIFACT = re.compile(
    rf"(?:`([^`\n]+?\.{_ARTIFACT_SUFFIX})`|[\"'“]([^\"'”\n]+?\.{_ARTIFACT_SUFFIX})[\"'”])",
    re.IGNORECASE,
)
_PATHLIKE_ARTIFACT = re.compile(
    rf"((?:~?/|\.?\.?/)[^\s`'\"，,；;()（）]+?\.{_ARTIFACT_SUFFIX})",
    re.IGNORECASE,
)


def extract_document_artifact_requirements(prompt: str) -> tuple[DocumentArtifactRequirement, ...]:
    """Extract only explicit filenames or document modalities from a request."""

    lower = (prompt or "").lower()
    requirements: list[DocumentArtifactRequirement] = []
    seen: set[tuple[str, str, bool]] = set()

    def add(kind: ArtifactKind, reference: str, *, exact: bool) -> None:
        key = (kind, reference.lower(), exact)
        if key not in seen:
            seen.add(key)
            requirements.append(DocumentArtifactRequirement(kind, reference, exact))

    exact_matches = [
        (match.start(), next((group for group in match.groups() if group), ""))
        for match in _QUOTED_ARTIFACT.finditer(prompt or "")
    ]
    exact_matches.extend((match.start(), match.group(1)) for match in _PATHLIKE_ARTIFACT.finditer(prompt or ""))
    for _, raw_reference in sorted(exact_matches):
        reference = Path(raw_reference).name
        kind = _EXTENSION_KIND.get(Path(reference).suffix.lower())
        if kind is not None:
            add(kind, reference, exact=True)

    # A modality is only required when no exact file of that modality was
    # named. This retains the user's specificity without duplicating work.
    exact_kinds = {item.kind for item in requirements if item.exact}
    if "markdown" not in exact_kinds and _modality_is_requested(lower, "markdown"):
        add("markdown", "markdown", exact=False)
    if "html" not in exact_kinds and _modality_is_requested(lower, "html"):
        add("html", "html", exact=False)
    if "image" not in exact_kinds and _modality_is_requested(lower, "image"):
        add("image", "image", exact=False)
    return tuple(requirements)


_MODALITY_MARKERS: dict[ArtifactKind, tuple[str, ...]] = {
    "markdown": ("markdown", "md 文档", ".md"),
    "html": ("html", "网页原型", "原型页", ".html", ".htm"),
    "image": ("示例图", "图片", "图像", "image", "png", "jpeg", "jpg", "gif", "webp"),
}
_ARTIFACT_ACTION_MARKERS = (
    "根据", "基于", "读取", "查看", "检查", "分析", "观察", "实际", "read", "inspect", "check", "analy", "review",
)
_ARTIFACT_NEGATION = r"(?:未读取|未读|不(?:需|要)?(?:读取|查看|检查|分析|观察)|无需|不能(?:读取|查看|检查|分析|观察)|无法(?:读取|查看|检查|分析|观察)|not\s+(?:read|inspected|checked|analyzed)|without\s+(?:reading|inspection)|cannot\s+(?:read|inspect|check|analy))"


def _modality_is_requested(prompt: str, kind: ArtifactKind) -> bool:
    """Require an affirmative inspection request, not a boundary mention.

    A sentence such as "图片未读取" reports a limitation.  It must not
    silently become a request to invoke image inspection on an otherwise text
    only analysis.
    """

    markers = _MODALITY_MARKERS[kind]
    # Semicolons introduce an independent boundary/limitation often enough to
    # be meaningful here.  Do not let a later "image was not read" clause
    # erase an earlier affirmative Markdown/HTML request in the same sentence.
    for sentence in re.split(r"[。！？!?\n；;]", prompt):
        lowered = sentence.lower()
        if not any(marker in lowered for marker in markers):
            continue
        for marker in markers:
            for match in re.finditer(re.escape(marker), lowered, re.IGNORECASE):
                if _artifact_marker_is_negated(lowered, match.start(), match.end()):
                    continue
                if any(action in lowered for action in _ARTIFACT_ACTION_MARKERS):
                    return True
    return False


def _artifact_marker_is_negated(clause: str, start: int, end: int) -> bool:
    """Limit a negation to the artifact it grammatically neighbours.

    In particular, "不要检查代码" must not cancel "根据 Markdown、HTML 和
    示例图分析" earlier in the same clause.
    """

    before = clause[max(0, start - 18) : start]
    after = clause[end : end + 18]
    return bool(
        re.search(_ARTIFACT_NEGATION + r"\s*$", before, re.IGNORECASE)
        or re.match(r"\s*(?:未读|未读取|不可用|无需|不(?:需|要)?(?:读取|查看|检查|分析|观察)|无法(?:读取|查看|检查|分析|观察)|not\s+(?:read|inspected|checked)|cannot\s+(?:read|inspect|check))", after, re.IGNORECASE)
    )


def document_artifact_coverage(
    requirements: Iterable[DocumentArtifactRequirement],
    tool_results: Iterable[ToolResultSummary],
) -> tuple[DocumentArtifactCoverage, ...]:
    """Match each requested artifact to a real observation or typed inability."""

    results = tuple(tool_results)
    coverage: list[DocumentArtifactCoverage] = []
    for requirement in requirements:
        observed = next((result for result in results if _result_observes(requirement, result)), None)
        if observed is not None:
            coverage.append(DocumentArtifactCoverage(requirement, "observed", str(observed.path or "")))
            continue
        unavailable = next((result for result in results if _result_marks_unavailable(requirement, result)), None)
        if unavailable is not None:
            coverage.append(DocumentArtifactCoverage(requirement, "unavailable", str(unavailable.path or "")))
        else:
            coverage.append(DocumentArtifactCoverage(requirement, "missing"))
    return tuple(coverage)


def missing_document_artifacts(coverage: Iterable[DocumentArtifactCoverage]) -> tuple[DocumentArtifactRequirement, ...]:
    return tuple(item.requirement for item in coverage if item.status == "missing")


def unavailable_document_artifacts(coverage: Iterable[DocumentArtifactCoverage]) -> tuple[DocumentArtifactCoverage, ...]:
    return tuple(item for item in coverage if item.status == "unavailable")


def _result_observes(requirement: DocumentArtifactRequirement, result: ToolResultSummary) -> bool:
    if result.is_error:
        return False
    path = str(result.path or "")
    if requirement.kind == "image":
        if result.name != "inspect_image" or not result.metadata.get("image_observation"):
            return False
    elif result.name != "read_file" or not _path_has_kind(path, requirement.kind):
        return False
    return not requirement.exact or Path(path).name.lower() == requirement.reference.lower()


def _result_marks_unavailable(requirement: DocumentArtifactRequirement, result: ToolResultSummary) -> bool:
    path = str(result.path or "")
    if requirement.kind == "image":
        if result.name == "read_file" and result.metadata.get("image_metadata") and not result.metadata.get("inspect_image_available", True):
            return not requirement.exact or Path(path).name.lower() == requirement.reference.lower()
        if result.name == "inspect_image" and result.metadata.get("image_inspection_unavailable"):
            return not requirement.exact or Path(path).name.lower() == requirement.reference.lower()
    return False


def _path_has_kind(path: str, kind: ArtifactKind) -> bool:
    return _EXTENSION_KIND.get(Path(path).suffix.lower()) == kind

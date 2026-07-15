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
        return Path(self.reference).name if self.exact else self.kind


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
_REMOTE_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s`'\"，,；;()（）<>]+", re.IGNORECASE)
_LOCAL_ARTIFACT_REFERENCE = re.compile(
    rf"(?P<reference>[^\s`'\"，,；;()（）<>?#\[\]]+\.{_ARTIFACT_SUFFIX})(?:\?[^\s`'\"，,；;()（）<>]*)?",
    re.IGNORECASE,
)
MAX_LINKED_DOCUMENT_MATERIAL_TARGETS = 4


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

    remote_spans = tuple(match.span() for match in _REMOTE_URL.finditer(prompt or ""))

    def is_within_remote_url(start: int, end: int) -> bool:
        return any(start >= url_start and end <= url_end for url_start, url_end in remote_spans)

    exact_matches = [
        (match.start(), next((group for group in match.groups() if group), ""))
        for match in _QUOTED_ARTIFACT.finditer(prompt or "")
    ]
    exact_matches.extend(
        (match.start(), match.group(1))
        for match in _PATHLIKE_ARTIFACT.finditer(prompt or "")
        if not is_within_remote_url(*match.span(1))
    )
    for _, raw_reference in sorted(exact_matches):
        # The label may be a basename, but an exact material directive must
        # retain the executable user reference.  In particular, an allowed
        # additional-root absolute path must not be rebound to the primary
        # workspace merely because its basename is common.
        reference = raw_reference.strip()
        if "://" in reference:
            # T-199 only follows locally executable material targets.  A URL
            # can end in a familiar suffix but has no workspace authority.
            continue
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
    "markdown": ("markdown", "md 文档", "需求文档", ".md"),
    "html": ("html", "网页原型", "原型页", "原型", ".html", ".htm"),
    "image": ("示例图", "图片", "图像", "image", "png", "jpeg", "jpg", "gif", "webp"),
}
_ARTIFACT_ACTION_MARKERS = (
    "根据", "基于", "阅读", "读取", "查看", "检查", "分析", "观察", "实际", "read", "inspect", "check", "analy", "review",
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
        versioned_requirement = kind == "markdown" and re.search(
            r"(?:v\d+(?:\.\d+)*\s*)需求|需求\s*(?:v\d+(?:\.\d+)*)",
            lowered,
            re.IGNORECASE,
        )
        if not any(marker in lowered for marker in markers) and not versioned_requirement:
            continue
        for marker in markers:
            for match in re.finditer(re.escape(marker), lowered, re.IGNORECASE):
                if _artifact_marker_is_negated(lowered, match.start(), match.end()):
                    continue
                if any(action in lowered for action in _ARTIFACT_ACTION_MARKERS):
                    return True
        if kind == "markdown":
            for match in re.finditer(r"(?:v\d+(?:\.\d+)*\s*)需求|需求\s*(?:v\d+(?:\.\d+)*)", lowered, re.IGNORECASE):
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
            coverage.append(DocumentArtifactCoverage(requirement, "observed", _observation_path(observed)))
            continue
        unavailable = next((result for result in results if _result_marks_unavailable(requirement, result)), None)
        if unavailable is not None:
            coverage.append(DocumentArtifactCoverage(requirement, "unavailable", _observation_path(unavailable)))
        else:
            coverage.append(DocumentArtifactCoverage(requirement, "missing"))
    return tuple(coverage)


def document_material_targets(
    requirements: Iterable[DocumentArtifactRequirement],
    tool_results: Iterable[ToolResultSummary],
) -> tuple[DocumentArtifactRequirement, ...]:
    """Resolve bounded local material targets without promoting sibling inventory.

    Exact task references stay exact.  A modality reference becomes exact only
    when a successfully read local document links it, or when one listed local
    entry is the sole candidate for that modality.  A directory listing alone
    therefore never expands into a read-everything policy.
    """

    requested = tuple(requirements)
    results = tuple(tool_results)
    listed_paths = _listed_document_paths(results)
    linked_paths = _linked_local_artifact_paths(results, listed_paths)
    targets: list[DocumentArtifactRequirement] = []
    seen: set[tuple[ArtifactKind, str, bool]] = set()

    def add(requirement: DocumentArtifactRequirement) -> None:
        key = (requirement.kind, requirement.reference.lower(), requirement.exact)
        if key not in seen:
            seen.add(key)
            targets.append(requirement)

    for requirement in requested:
        if requirement.exact:
            add(requirement)
            continue
        linked = tuple(path for path in linked_paths if _path_has_kind(path, requirement.kind))
        if linked:
            for path in linked:
                add(DocumentArtifactRequirement(requirement.kind, path, exact=True))
            continue
        listed = tuple(path for path in listed_paths if _path_has_kind(path, requirement.kind))
        if len(listed) == 1:
            add(DocumentArtifactRequirement(requirement.kind, listed[0], exact=True))
            continue
        add(requirement)

    # A successfully read local material may explicitly link a companion
    # document or image that was not named as a modality in the prompt.  The
    # link, rather than a directory sibling, is the bounded provenance for
    # following it.  This is intentionally capped so one document cannot
    # turn the material lifecycle into an open-ended crawl.
    for path in linked_paths[:MAX_LINKED_DOCUMENT_MATERIAL_TARGETS]:
        kind = _EXTENSION_KIND.get(Path(path).suffix.lower())
        if kind is not None:
            add(DocumentArtifactRequirement(kind, path, exact=True))
    return tuple(targets)


def missing_document_artifacts(coverage: Iterable[DocumentArtifactCoverage]) -> tuple[DocumentArtifactRequirement, ...]:
    return tuple(item.requirement for item in coverage if item.status == "missing")


def unavailable_document_artifacts(coverage: Iterable[DocumentArtifactCoverage]) -> tuple[DocumentArtifactCoverage, ...]:
    return tuple(item for item in coverage if item.status == "unavailable")


def _result_observes(requirement: DocumentArtifactRequirement, result: ToolResultSummary) -> bool:
    if result.is_error:
        return False
    path = _observation_path(result)
    if requirement.kind == "image":
        if result.name != "inspect_image" or not result.metadata.get("image_observation"):
            return False
    elif result.name != "read_file" or not _path_has_kind(path, requirement.kind):
        return False
    return not requirement.exact or _exact_reference_matches_path(requirement.reference, path)


def _result_marks_unavailable(requirement: DocumentArtifactRequirement, result: ToolResultSummary) -> bool:
    path = _observation_path(result)
    if requirement.kind == "image":
        if result.name == "read_file" and result.metadata.get("image_metadata") and not result.metadata.get("inspect_image_available", True):
            return not requirement.exact or _exact_reference_matches_path(requirement.reference, path)
        if result.name == "inspect_image" and result.metadata.get("image_inspection_unavailable"):
            return not requirement.exact or _exact_reference_matches_path(requirement.reference, path)
    return False


def _path_has_kind(path: str, kind: ArtifactKind) -> bool:
    return _EXTENSION_KIND.get(Path(path).suffix.lower()) == kind


def _exact_reference_matches_path(reference: str, path: str) -> bool:
    target = Path(reference)
    observed = Path(path)
    if target.is_absolute():
        return str(target) == str(observed)
    normalized = reference.replace("\\", "/").lstrip("./")
    observed_normalized = str(observed).replace("\\", "/")
    return observed_normalized == normalized or observed_normalized.endswith("/" + normalized)


def _listed_document_paths(results: Iterable[ToolResultSummary]) -> tuple[str, ...]:
    paths: list[str] = []
    seen: set[str] = set()
    for result in results:
        if result.name != "list_files" or result.is_error:
            continue
        files = result.metadata.get("files")
        values = files if isinstance(files, (list, tuple)) else result.content.splitlines()
        # list_files displays entries relative to the evidence root.  Its
        # listed_root may itself be a nested directory, so use it only for
        # older results which do not carry the canonical evidence root.
        root = str(result.metadata.get("evidence_root") or result.metadata.get("listed_root") or "").strip()
        for raw in values:
            if not isinstance(raw, str) or not raw.strip():
                continue
            path = Path(raw.strip())
            if not path.is_absolute() and root:
                path = Path(root) / path
            if not path.is_absolute() or Path(path).suffix.lower() not in _EXTENSION_KIND:
                continue
            rendered = str(path)
            if rendered not in seen:
                seen.add(rendered)
                paths.append(rendered)
    return tuple(paths)


def _linked_local_artifact_paths(
    results: Iterable[ToolResultSummary],
    listed_paths: tuple[str, ...],
) -> tuple[str, ...]:
    listed_normalized = {
        str(Path(path).resolve(strict=False)): str(Path(path))
        for path in listed_paths
    }
    listed_by_name: dict[str, list[str]] = {}
    for path in listed_paths:
        listed_by_name.setdefault(Path(path).name.casefold(), []).append(path)
    paths: list[str] = []
    seen: set[str] = set()
    for result in results:
        if result.name != "read_file" or result.is_error:
            continue
        resolved_path = _result_canonical_path(result)
        if resolved_path is None:
            continue
        parent = resolved_path.parent
        for match in _LOCAL_ARTIFACT_REFERENCE.finditer(result.content or ""):
            reference = match.group("reference").strip()
            if "://" in reference or reference.startswith(("/", "../", "~")):
                continue
            local_candidate = str((parent / reference).resolve(strict=False))
            listed = listed_normalized.get(local_candidate)
            if listed is None:
                same_name = listed_by_name.get(Path(reference).name.casefold(), ())
                listed = same_name[0] if len(same_name) == 1 else None
            candidate = listed or local_candidate
            if Path(candidate).suffix.lower() not in _EXTENSION_KIND or candidate in seen:
                continue
            seen.add(candidate)
            paths.append(candidate)
    return tuple(paths)


def _result_canonical_path(result: ToolResultSummary) -> Path | None:
    """Resolve a tool observation without treating a raw display path as cwd-relative."""

    resolved = result.metadata.get("resolved_path")
    if isinstance(resolved, str) and resolved.strip():
        return Path(resolved)
    raw_path = str(result.path or "").strip()
    if not raw_path:
        return None
    path = Path(raw_path)
    if path.is_absolute():
        return path
    evidence_root = result.metadata.get("evidence_root")
    if isinstance(evidence_root, str) and evidence_root.strip():
        return Path(evidence_root) / path
    return None


def _observation_path(result: ToolResultSummary) -> str:
    """Return the canonical path when a tool result supplies one."""

    canonical = _result_canonical_path(result)
    return str(canonical) if canonical is not None else str(result.path or "")

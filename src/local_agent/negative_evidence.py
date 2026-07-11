from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .tool_choice_queue import ToolResultSummary


@dataclass(frozen=True)
class NegativeExistenceClaim:
    """A finite, path-oriented negative claim that needs matching tool evidence."""

    kind: str
    subject: str
    claim: str


_JAVA_CLAIM = re.compile(
    r"(?:没有|未发现|未找到|不存在|无)\s*(?:任何)?\s*(?:\.?java|java)\s*(?:文件|源码|source|files?)"
    r"|\bno\s+(?:\.java|java)\s+(?:files?|source)\b",
    re.IGNORECASE,
)
_SOURCE_CLAIM = re.compile(
    r"(?:没有|未发现|未找到|不存在|无)\s*(?:源码|源代码|代码文件)"
    r"|\bno\s+(?:source(?:\s+files?)?|codebase)\b",
    re.IGNORECASE,
)
_EXACT_PATH_CLAIM = re.compile(
    r"(?P<path>src(?:/main/java)?)(?:目录|文件夹|文件)?\s*(?:不存在|未发现|未找到|没有)"
    r"|(?:不存在|未发现|未找到|没有)\s*(?P<reverse>src(?:/main/java)?)",
    re.IGNORECASE,
)
_GIT_CLAIM = re.compile(
    r"(?:不是|非)\s*git\s*(?:仓库|项目)|not\s+(?:a\s+)?git\s+(?:repo|repository)|"
    r"is\s+not\s+(?:a\s+)?git\s+(?:repo|repository)",
    re.IGNORECASE,
)


def unsupported_negative_existence_claims(
    content: str,
    tool_results: Iterable[ToolResultSummary],
) -> tuple[NegativeExistenceClaim, ...]:
    """Return definite path/source/Git absences that lack type-matched evidence.

    Text or symbol search misses intentionally remain outside this function: those
    are content claims and continue to be supported by ``search_code``.
    """

    results = tuple(tool_results)
    unsupported: list[NegativeExistenceClaim] = []
    for claim in negative_existence_claims(content):
        if not _claim_has_matching_evidence(claim, results):
            unsupported.append(claim)
    return tuple(unsupported)


def negative_existence_claims(content: str) -> tuple[NegativeExistenceClaim, ...]:
    claims: list[NegativeExistenceClaim] = []
    if _JAVA_CLAIM.search(content):
        claims.append(NegativeExistenceClaim("extension", "java", _first_match(_JAVA_CLAIM, content)))
    if _SOURCE_CLAIM.search(content):
        claims.append(NegativeExistenceClaim("source_tree", "source", _first_match(_SOURCE_CLAIM, content)))
    for match in _EXACT_PATH_CLAIM.finditer(content):
        subject = (match.group("path") or match.group("reverse") or "").lower()
        if subject:
            claims.append(NegativeExistenceClaim("exact_path", subject, match.group(0)))
    if _GIT_CLAIM.search(content):
        claims.append(NegativeExistenceClaim("git_repository", "git", _first_match(_GIT_CLAIM, content)))
    return tuple(claims)


def allowed_tools_for_negative_claims(claims: Iterable[NegativeExistenceClaim]) -> tuple[str, ...]:
    allowed: set[str] = set()
    for claim in claims:
        if claim.kind in {"extension", "source_tree"}:
            allowed.add("glob_files")
        elif claim.kind == "exact_path":
            allowed.update({"glob_files", "list_files", "read_file"})
    return tuple(sorted(allowed))


def render_negative_existence_issues(claims: Iterable[NegativeExistenceClaim]) -> list[str]:
    return [
        f"{claim.claim!r} is a {claim.kind} absence claim without matching discovery evidence"
        for claim in claims
    ]


def _claim_has_matching_evidence(claim: NegativeExistenceClaim, results: tuple[ToolResultSummary, ...]) -> bool:
    if claim.kind == "exact_path":
        return any(_is_exact_path_missing(result, claim.subject) for result in results)
    if claim.kind == "extension":
        return any(_is_complete_path_no_match(result, claim.subject) for result in results)
    if claim.kind == "source_tree":
        return any(_is_complete_path_no_match(result, "source") for result in results)
    if claim.kind == "git_repository":
        return any(
            not result.is_error
            and result.name == "git_status"
            and bool(result.metadata.get("git_probe_root"))
            and result.metadata.get("git_repository") is False
            for result in results
        )
    return False


def _is_exact_path_missing(result: ToolResultSummary, subject: str) -> bool:
    metadata = result.metadata
    if str(metadata.get("negative_evidence_type") or "") != "exact_path_missing":
        return False
    path = str(metadata.get("path") or "").replace("\\", "/").rstrip("/").lower()
    return path.endswith(subject.rstrip("/"))


def _is_complete_path_no_match(result: ToolResultSummary, subject: str) -> bool:
    metadata = result.metadata
    if result.name != "glob_files" or result.is_error:
        return False
    if str(metadata.get("negative_evidence_type") or "") != "path_no_match":
        return False
    if not metadata.get("complete") or metadata.get("truncated") or metadata.get("result_limit_reached"):
        return False
    patterns = " ".join(str(value).lower() for value in metadata.get("patterns", ()))
    if subject == "java":
        return ".java" in patterns or "java" in patterns
    return patterns.strip() in {"**/*", "**", "*"}


def _first_match(pattern: re.Pattern[str], content: str) -> str:
    match = pattern.search(content)
    return match.group(0) if match is not None else ""


__all__ = [
    "NegativeExistenceClaim",
    "allowed_tools_for_negative_claims",
    "negative_existence_claims",
    "render_negative_existence_issues",
    "unsupported_negative_existence_claims",
]

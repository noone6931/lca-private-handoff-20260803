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

_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`]*`")
_QUOTED_EXAMPLE = re.compile(r"(?:\"[^\"]*\"|'[^']*'|“[^”]*”|‘[^’]*’)")
_CLAUSE_BOUNDARY = re.compile(r"(?<=[。！？!?；;])|\n+")
_NON_ASSERTIVE_PREFIX = re.compile(
    r"(?:不能|无法|不应|不要|不可|未验证|尚未验证|不足以|并非|仅引用|只是引用|避免|禁止).{0,28}$"
    r"|(?:cannot\s+(?:conclude|state|claim)|can't\s+(?:conclude|state|claim)|do\s+not\s+(?:conclude|state|claim)|"
    r"don't\s+(?:conclude|state|claim)|should\s+not\s+(?:conclude|state|claim)|does\s+not\s+prove|"
    r"doesn't\s+prove|not\s+enough\s+to\s+conclude).{0,28}$",
    re.IGNORECASE,
)
_NON_ASSERTIVE_SUFFIX = re.compile(
    r"^\s*(?:[（(]\s*)?(?:未验证|尚未验证|不成立|仅作示例|只是引用|not\s+verified|unverified|"
    r"only\s+(?:an\s+)?example|quoted\s+example)(?:\s*[）)])?",
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
    sanitized = _non_code_text(content)
    for match in _JAVA_CLAIM.finditer(sanitized):
        if _is_asserted_claim(sanitized, match):
            claims.append(NegativeExistenceClaim("extension", "java", match.group(0)))
    for match in _SOURCE_CLAIM.finditer(sanitized):
        if _is_asserted_claim(sanitized, match):
            claims.append(NegativeExistenceClaim("source_tree", "source", match.group(0)))
    for match in _EXACT_PATH_CLAIM.finditer(sanitized):
        subject = (match.group("path") or match.group("reverse") or "").lower()
        if subject and _is_asserted_claim(sanitized, match):
            claims.append(NegativeExistenceClaim("exact_path", subject, match.group(0)))
    for match in _GIT_CLAIM.finditer(sanitized):
        if _is_asserted_claim(sanitized, match):
            claims.append(NegativeExistenceClaim("git_repository", "git", match.group(0)))
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


def _non_code_text(content: str) -> str:
    """Preserve offsets while preventing quoted/code examples from becoming claims."""

    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    return _QUOTED_EXAMPLE.sub(blank, _INLINE_CODE.sub(blank, _FENCED_CODE.sub(blank, content or "")))


def _is_asserted_claim(content: str, match: re.Match[str]) -> bool:
    start = max(
        (boundary.end() for boundary in _CLAUSE_BOUNDARY.finditer(content, 0, match.start())),
        default=0,
    )
    next_boundary = _CLAUSE_BOUNDARY.search(content, match.end())
    end = next_boundary.start() if next_boundary is not None else len(content)
    prefix = content[start : match.start()]
    suffix = content[match.end() : end]
    # Only the governing prefix or an immediate qualifier can negate a claim.
    # A later unrelated phrase such as "but that does not mean no docs" must
    # not erase an earlier real assertion that this root has no source code.
    return not bool(_NON_ASSERTIVE_PREFIX.search(prefix) or _NON_ASSERTIVE_SUFFIX.search(suffix))


__all__ = [
    "NegativeExistenceClaim",
    "allowed_tools_for_negative_claims",
    "negative_existence_claims",
    "render_negative_existence_issues",
    "unsupported_negative_existence_claims",
]

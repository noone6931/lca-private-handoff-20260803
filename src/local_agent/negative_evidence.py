from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from .tool_choice_queue import ToolResultSummary


ASSERTED_ABSENCE = "asserted_absence"
OBSERVED_NO_MATCH = "observed_no_match"
EPISTEMICALLY_QUALIFIED = "epistemically_qualified"
QUOTED_OR_HYPOTHETICAL = "quoted_or_hypothetical"


@dataclass(frozen=True)
class NegativeExistenceClaim:
    """A clause-local negative claim with its epistemic stance and provenance."""

    kind: str
    subject: str
    claim: str
    stance: str = ASSERTED_ABSENCE
    scope: str = "unspecified"
    root: str | None = None
    support_requirement: str = "complete_discovery"
    span_start: int = 0
    span_end: int = 0


_JAVA_CLAIM = re.compile(
    r"(?:没有|未发现|未找到|不存在|无)\s*(?:任何)?\s*(?:\.?java|java)(?:\s*(?:文件|源码|source|files?))?"
    r"|\b(?:no\s+(?:\.java|java)\s+(?:files?|source)\s+(?:were|was)\s+found|no\s+(?:\.java|java)\s+(?:files?|source))\b",
    re.IGNORECASE,
)
_SOURCE_CLAIM = re.compile(
    r"(?:没有|未发现|未找到|不存在|无)\s*(?:源码|源代码|代码文件)"
    r"|\b(?:no\s+(?:source(?:\s+files?)?|codebase)\s+(?:were|was)\s+found|no\s+(?:source(?:\s+files?)?|codebase))\b",
    re.IGNORECASE,
)
_EXACT_PATH_CLAIM = re.compile(
    r"(?P<path>src(?:/main/java)?)(?:目录|文件夹|文件)?\s*(?P<verb>不存在|未发现|未找到|没有)"
    r"|(?P<reverse_verb>不存在|未发现|未找到|没有)\s*(?P<reverse>src(?:/main/java)?)",
    re.IGNORECASE,
)
_GIT_CLAIM = re.compile(
    r"(?:不是|非)\s*git\s*(?:仓库|项目)|not\s+(?:a\s+)?git\s+(?:repo|repository)|"
    r"is\s+not\s+(?:a\s+)?git\s+(?:repo|repository)",
    re.IGNORECASE,
)
_ENTITY_CLAIM = re.compile(
    r"(?:不存在|没有|无)\s*(?P<subject>[A-Za-z_][A-Za-z0-9_.-]{1,})"
    r"|\bno\s+(?P<english>[A-Za-z_][A-Za-z0-9_.-]{1,})\s+exists\b",
    re.IGNORECASE,
)

_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`]*`")
_QUOTED_EXAMPLE = re.compile(r"(?:\"[^\"]*\"|'[^']*'|“[^”]*”|‘[^’]*’)")
_CLAUSE_BREAK = re.compile(r"(?<=[。！？!?；;])|\n+|\b(?:but|however)\b|(?:但是|然而|但)", re.IGNORECASE)
_QUALIFYING_PREFIX = re.compile(
    r"(?:不能|无法|不可|不要)\s*(?:据此\s*)?(?:推导|断言|声称|陈述|认定|证明|说明|得出|判断)|"
    r"(?:未验证|尚未验证).{0,18}(?:推导|断言|声称|陈述|认定|证明|说明|得出|判断)|"
    r"不足以证明|不等于|并非|仅引用|只是引用|"
    r"cannot\s+(?:conclude|state|claim)|can't\s+(?:conclude|state|claim)|do\s+not\s+(?:conclude|state|claim)|"
    r"don't\s+(?:conclude|state|claim)|should\s+not\s+(?:conclude|state|claim)|does\s+not\s+(?:prove|establish)|"
    r"doesn't\s+(?:prove|establish)|not\s+enough\s+to\s+(?:conclude|establish)",
    re.IGNORECASE,
)
_QUALIFYING_SUFFIX = re.compile(
    r"(?:不等于证明|不能据此(?:推导|断言|声称|陈述)|不足以证明|未验证|尚未验证|not\s+(?:proof|verified)|"
    r"does\s+not\s+(?:prove|establish)|cannot\s+(?:conclude|state|claim)|unverified)",
    re.IGNORECASE,
)
_OBSERVED_MARKER = re.compile(r"(?:未发现|未找到|no\s+.+?\s+(?:was|were)\s+found|no\s+matches?)", re.IGNORECASE)
_QUESTION_OR_HYPOTHETICAL = re.compile(
    r"[?？]|(?:假设|如果|若|例如|比如)|\b(?:suppose|if|example)\b",
    re.IGNORECASE,
)
_NEXT_CLAUSE_QUALIFIER = re.compile(
    r"^\s*(?:(?:但|但是|然而)\s*)?(?:这\s*)?(?:不等于证明|不能据此(?:推导|断言|声称|陈述)|不足以证明)|"
    r"^\s*(?:(?:but|however)\s+)?(?:this\s+)?(?:does\s+not\s+(?:prove|establish)|is\s+not\s+proof)",
    re.IGNORECASE,
)


def parse_negative_evidence_claims(content: str) -> tuple[NegativeExistenceClaim, ...]:
    """Parse finite path/source/Git negative claims without making evidence decisions."""
    sanitized = _non_code_text(content)
    claims: list[NegativeExistenceClaim] = []
    spans = tuple(_clause_spans(sanitized))
    for index, (start, end) in enumerate(spans):
        clause = sanitized[start:end]
        following_clause = sanitized[spans[index + 1][0] : spans[index + 1][1]] if index + 1 < len(spans) else ""
        for kind, subject, matcher in (
            ("extension", "java", _JAVA_CLAIM),
            ("source_tree", "source", _SOURCE_CLAIM),
            ("git_repository", "git", _GIT_CLAIM),
        ):
            for match in matcher.finditer(clause):
                claims.append(_claim_from_match(kind, subject, match, clause, following_clause, start))
        for match in _EXACT_PATH_CLAIM.finditer(clause):
            subject = (match.group("path") or match.group("reverse") or "").lower()
            if subject:
                claims.append(_claim_from_match("exact_path", subject, match, clause, following_clause, start))
        for match in _ENTITY_CLAIM.finditer(clause):
            subject = match.group("subject") or match.group("english") or ""
            if subject.lower() not in {"java", "source", "code", "git"}:
                claims.append(_claim_from_match("entity", subject, match, clause, following_clause, start))
    return tuple(_dedupe_claims(claims))


def negative_existence_claims(content: str) -> tuple[NegativeExistenceClaim, ...]:
    """Compatibility view: only asserted absences require exhaustive evidence."""
    return tuple(claim for claim in parse_negative_evidence_claims(content) if claim.stance == ASSERTED_ABSENCE)


def unsupported_negative_existence_claims(
    content: str,
    tool_results: Iterable[ToolResultSummary],
) -> tuple[NegativeExistenceClaim, ...]:
    results = tuple(tool_results)
    return tuple(claim for claim in negative_existence_claims(content) if not _claim_has_matching_evidence(claim, results))


def negative_claim_metrics(content: str, tool_results: Iterable[ToolResultSummary]) -> dict[str, int]:
    claims = parse_negative_evidence_claims(content)
    results = tuple(tool_results)
    asserted = [claim for claim in claims if claim.stance == ASSERTED_ABSENCE]
    return {
        "asserted_absence": len(asserted),
        "observed_no_match": sum(claim.stance == OBSERVED_NO_MATCH for claim in claims),
        "epistemically_qualified": sum(claim.stance == EPISTEMICALLY_QUALIFIED for claim in claims),
        "quoted_or_hypothetical": sum(claim.stance == QUOTED_OR_HYPOTHETICAL for claim in claims),
        "blocked_assertions": sum(not _claim_has_matching_evidence(claim, results) for claim in asserted),
        "qualified_skips": sum(claim.stance == EPISTEMICALLY_QUALIFIED for claim in claims),
    }


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
        f"{claim.claim!r} is a {claim.kind} asserted absence without matching {claim.support_requirement} evidence"
        for claim in claims
    ]


def _claim_from_match(
    kind: str,
    subject: str,
    match: re.Match[str],
    clause: str,
    following_clause: str,
    clause_start: int,
) -> NegativeExistenceClaim:
    claim_text = match.group(0)
    stance = _claim_stance(clause, match, following_clause)
    support = "complete_git_probe" if kind == "git_repository" else "complete_discovery"
    return NegativeExistenceClaim(
        kind=kind,
        subject=subject,
        claim=claim_text,
        stance=stance,
        scope=_claim_scope(clause),
        support_requirement=support,
        span_start=clause_start + match.start(),
        span_end=clause_start + match.end(),
    )


def _claim_stance(clause: str, match: re.Match[str], following_clause: str) -> str:
    if _QUESTION_OR_HYPOTHETICAL.search(clause):
        return QUOTED_OR_HYPOTHETICAL
    before = clause[: match.start()]
    after = clause[match.end() :]
    if (
        _QUALIFYING_PREFIX.search(before)
        or _QUALIFYING_SUFFIX.search(after)
        or _NEXT_CLAUSE_QUALIFIER.search(following_clause)
    ):
        return EPISTEMICALLY_QUALIFIED
    if _OBSERVED_MARKER.search(match.group(0)):
        return OBSERVED_NO_MATCH
    return ASSERTED_ABSENCE


def _claim_scope(clause: str) -> str:
    lowered = clause.lower()
    if any(value in lowered for value in ("all roots", "所有 root", "所有目录", "全部目录")):
        return "multi_root"
    if any(value in lowered for value in ("primary", "主工作区", "当前工作区")):
        return "primary"
    if any(value in lowered for value in ("root", "当前目录", "该目录", "这里", "this directory", "this root")):
        return "root_local"
    return "unspecified"


def _clause_spans(content: str) -> Iterable[tuple[int, int]]:
    start = 0
    for match in _CLAUSE_BREAK.finditer(content):
        end = match.start()
        if content[start:end].strip():
            yield start, end
        start = match.end()
    if content[start:].strip():
        yield start, len(content)


def _dedupe_claims(claims: Iterable[NegativeExistenceClaim]) -> list[NegativeExistenceClaim]:
    seen: set[tuple[str, str, int, int]] = set()
    output: list[NegativeExistenceClaim] = []
    for claim in claims:
        key = (claim.kind, claim.subject, claim.span_start, claim.span_end)
        if key not in seen:
            seen.add(key)
            output.append(claim)
    return output


def _claim_has_matching_evidence(claim: NegativeExistenceClaim, results: tuple[ToolResultSummary, ...]) -> bool:
    if claim.kind == "exact_path":
        return any(_result_supports_scope(claim, result) and _is_exact_path_missing(result, claim.subject) for result in results)
    if claim.kind == "extension":
        return any(_result_supports_scope(claim, result) and _is_complete_path_no_match(result, claim.subject) for result in results)
    if claim.kind == "source_tree":
        return any(_result_supports_scope(claim, result) and _is_complete_path_no_match(result, "source") for result in results)
    if claim.kind == "git_repository":
        return any(
            not result.is_error
            and result.name == "git_status"
            and bool(result.metadata.get("git_probe_root"))
            and result.metadata.get("git_repository") is False
            for result in results
        )
    return False


def _result_supports_scope(claim: NegativeExistenceClaim, result: ToolResultSummary) -> bool:
    """Keep root-local observations from silently becoming cross-root facts."""
    metadata = result.metadata
    label = str(metadata.get("evidence_root_label") or "")
    if claim.scope == "primary" and label and label != "primary":
        return False
    if claim.scope != "multi_root":
        return True
    roots = metadata.get("searched_roots")
    return isinstance(roots, (list, tuple)) and len({str(root) for root in roots if str(root).strip()}) >= 2


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
    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    return _QUOTED_EXAMPLE.sub(blank, _INLINE_CODE.sub(blank, _FENCED_CODE.sub(blank, content or "")))


__all__ = [
    "ASSERTED_ABSENCE",
    "EPISTEMICALLY_QUALIFIED",
    "NegativeExistenceClaim",
    "OBSERVED_NO_MATCH",
    "QUOTED_OR_HYPOTHETICAL",
    "allowed_tools_for_negative_claims",
    "negative_claim_metrics",
    "negative_existence_claims",
    "parse_negative_evidence_claims",
    "render_negative_existence_issues",
    "unsupported_negative_existence_claims",
]

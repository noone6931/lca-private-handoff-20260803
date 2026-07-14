from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .tool_observation import ToolResultSummary


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
    r"(?:没有|未发现|未找到|不存在|无)\s*(?:任何)?\s*(?:\.?java|java)\s*(?:文件|源码|source|files?)"
    r"|\b(?:no\s+(?:\.java|java)\s+(?:files?|source)\s+(?:were|was)\s+found|no\s+(?:\.java|java)\s+(?:files?|source))\b",
    re.IGNORECASE,
)
_BARE_JAVA_CLAIM = re.compile(
    r"(?:没有|未发现|未找到|不存在|无)\s*(?:任何)?\s*(?:\.?java|java)\b"
    r"|\bno\s+(?:\.java|java)\b",
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
    r"不存在\s*(?P<subject>[A-Za-z_][A-Za-z0-9_.-]{1,})"
    r"|\bno\s+(?P<english>[A-Za-z_][A-Za-z0-9_.-]{1,})\s+exists\b",
    re.IGNORECASE,
)
_GLOBAL_ENTITY_ABSENCE = re.compile(
    r"\b(?:the\s+)?(?:repository|repo|codebase|source(?:\s+tree)?|current\s+(?:code|source))\s+"
    r"(?:(?:conclusively|definitively|clearly|verifiedly)\s+)?"
    r"(?:lacks|contains\s+no|has\s+no(?!\s+(?:need|obligation|requirement)\s+to\b)|does\s+not\s+have(?!\s+to\b))\s+"
    r"(?P<english>[^.。；;\n]{2,180})"
    r"|\b(?:the\s+)?(?:repository|repo|codebase|source(?:\s+tree)?|current\s+(?:code|source))\s+"
    r"(?:(?:conclusively|definitively|clearly)\s+)?(?:proves|confirms|shows|establishes)\s+"
    r"(?:that\s+)?no\s+(?P<proven_english>[^.。；;\n]{2,160})"
    r"|(?:仓库|代码库|源码|当前(?:代码|源码|仓库))(?:中|里)?\s*"
    r"(?:(?:已(?:经)?)?(?:证明|证实|确认)|确定|明确)?\s*"
    r"(?:缺少|没有(?!\s*(?:必要|义务|要求|需要))|不存在|未包含)\s*(?P<subject>[^。；;\n]{2,120})",
    re.IGNORECASE,
)
_UNLOCATED_MARKER = re.compile(
    r"(?:未定位|未验证|尚未验证|未确认|尚未确认|证据不足)|"
    r"\b(?:unlocated|unverified|not\s+located|not\s+verified|not\s+confirmed|insufficient\s+evidence)\b",
    re.IGNORECASE,
)
_UNLOCATED_CERTAINTY_ESCALATION = re.compile(
    r"(?:需要|需|必须|应当|应该)\s*(?:完全)?\s*从零(?:开始)?(?:开发|实现|构建)|"
    r"(?:结论\s*[:：]?\s*)?(?:无|没有)\s*(?:任何)?\s*直接影响|"
    r"\b(?:must|need(?:s)?\s+to|require(?:s|d)?)\s+(?:be\s+)?(?:built|implemented)\s+from\s+scratch\b|"
    r"\b(?:build|implement)\s+(?:it\s+)?from\s+scratch\b|"
    r"\bno\s+direct\s+impact\b",
    re.IGNORECASE,
)
_CONDITIONAL_OR_PROPOSAL = re.compile(
    r"(?:如果|若|假如|前提是|待确认|建议|可考虑|候选方案)|"
    r"\b(?:if|assuming|provided\s+that|proposal|recommend(?:ed|ation)?|option)\b",
    re.IGNORECASE,
)
_EPISTEMIC_NEGATION = re.compile(
    r"(?:不能|无法|不足以|尚不能|并非|不应)\s*(?:据此\s*)?(?:确认|断言|认定|证明|得出)?|"
    r"\b(?:cannot|can't|do\s+not|don't|not\s+enough\s+to)\s+(?:conclude|claim|establish|prove)\b",
    re.IGNORECASE,
)

_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`]*`")
_QUOTED_EXAMPLE = re.compile(r"(?:\"[^\"]*\"|'[^']*'|“[^”]*”|‘[^’]*’)")
_CLAUSE_BREAK = re.compile(
    r"(?<=[。！？!?；;])|\n+|\b(?:but|however|and|also|separately)\b|(?:但是|然而|但|同时|另外|此外|而且|并且)",
    re.IGNORECASE,
)
_QUALIFYING_PREFIX = re.compile(
    r"(?:(?:不能|无法|不可|不要)\s*(?:据此\s*)?(?:推导|断言|声称|陈述|认定|证明|说明|得出|判断)(?:出|为)?|"
    r"(?:未验证|尚未验证).{0,18}(?:推导|断言|声称|陈述|认定|证明|说明|得出|判断)|"
    r"不足以证明|不等于|并非|仅引用|只是引用|"
    r"cannot\s+(?:conclude|state|claim)|can't\s+(?:conclude|state|claim)|do\s+not\s+(?:conclude|state|claim)|"
    r"don't\s+(?:conclude|state|claim)|should\s+not\s+(?:conclude|state|claim)|does\s+not\s+(?:prove|establish)|"
    r"doesn't\s+(?:prove|establish)|not\s+enough\s+to\s+(?:conclude|establish))(?:[\sA-Za-z0-9_.-]|[\u4e00-\u9fff]){0,24}$",
    re.IGNORECASE,
)
_QUALIFYING_SUFFIX = re.compile(
    r"^\s*(?:[，,:;]\s*)?(?:不等于证明|不能据此(?:推导|断言|声称|陈述)|不足以证明|未验证|尚未验证|not\s+(?:proof|verified)|"
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
_ROOT_REFERENCE = re.compile(
    r"(?P<multi>\b(?:all|every)\s+roots?\b|所有(?:\s*root|目录)|全部目录)|"
    r"(?P<primary>\bprimary(?:\s+(?:workspace|root))?\b|主工作区|当前工作区)|"
    r"(?P<additional>\b(?:additional|sibling)(?:\s+root)?\b|附加(?:\s*(?:root|目录))?|"
    r"额外(?:\s*(?:root|目录))?|同级(?:\s*(?:root|目录))?|另一个\s*root)|"
    r"(?P<local>\b(?:this\s+)?(?:root|workspace|directory|folder)\b|当前目录|该目录|这里|文件夹)",
    re.IGNORECASE,
)
_ROOT_SCOPE_MARKER = _ROOT_REFERENCE
_BARE_JAVA_TAIL = re.compile(r"^\s*(?:$|[。！？!?；;,，.].*)")
_OBSERVED_BARE_JAVA_TAIL = re.compile(r"^\s*(?:was|were)\s+found\s*(?:[。！？!?；;,，.]|$)", re.IGNORECASE)
_BARE_JAVA_SOURCE_TAIL = re.compile(
    r"^\s*(?:相关(?:的)?\s*)?(?:文件|源码|源代码|代码|source|files?|code)"
    r"(?:\s*(?:或|and|or)\s*(?:文件|源码|源代码|代码|source|files?|code))?",
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
        for match in _BARE_JAVA_CLAIM.finditer(clause):
            if _bare_java_has_file_scope(clause, match) and not _overlaps_existing_match(match, _JAVA_CLAIM, clause):
                claims.append(_claim_from_match("extension", "java", match, clause, following_clause, start))
        for match in _EXACT_PATH_CLAIM.finditer(clause):
            subject = (match.group("path") or match.group("reverse") or "").lower()
            if subject:
                claims.append(_claim_from_match("exact_path", subject, match, clause, following_clause, start))
        for match in _GLOBAL_ENTITY_ABSENCE.finditer(clause):
            subject = (match.group("subject") or match.group("english") or match.group("proven_english") or "").strip()
            subject = re.sub(r"\s+exists?\s*$", "", subject, flags=re.IGNORECASE).strip()
            if subject:
                claims.append(_claim_from_match("repository_entity", subject, match, clause, following_clause, start))
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
    claims = parse_negative_evidence_claims(content)
    return tuple(
        claim
        for claim in claims
        if claim.stance in {ASSERTED_ABSENCE, OBSERVED_NO_MATCH}
        and not _claim_has_matching_evidence(claim, results)
    )


def unsupported_unlocated_escalations(content: str) -> tuple[str, ...]:
    """Return certainty claims that contradict an answer's unlocated stance."""

    sanitized = _non_code_text(content)
    if not _UNLOCATED_MARKER.search(sanitized):
        return ()
    issues: list[str] = []
    for start, end in _clause_spans(sanitized):
        clause = sanitized[start:end].strip()
        if not clause or _CONDITIONAL_OR_PROPOSAL.search(clause) or _EPISTEMIC_NEGATION.search(clause):
            continue
        for match in _UNLOCATED_CERTAINTY_ESCALATION.finditer(clause):
            issues.append(match.group(0).strip())
    return tuple(dict.fromkeys(issues))


def negative_claim_metrics(content: str, tool_results: Iterable[ToolResultSummary]) -> dict[str, int]:
    claims = parse_negative_evidence_claims(content)
    results = tuple(tool_results)
    asserted = [claim for claim in claims if claim.stance == ASSERTED_ABSENCE]
    observed = [claim for claim in claims if claim.stance == OBSERVED_NO_MATCH]
    return {
        "asserted_absence": len(asserted),
        "observed_no_match": sum(claim.stance == OBSERVED_NO_MATCH for claim in claims),
        "epistemically_qualified": sum(claim.stance == EPISTEMICALLY_QUALIFIED for claim in claims),
        "quoted_or_hypothetical": sum(claim.stance == QUOTED_OR_HYPOTHETICAL for claim in claims),
        "blocked_assertions": sum(not _claim_has_matching_evidence(claim, results) for claim in asserted),
        "blocked_observations": sum(not _claim_has_matching_evidence(claim, results) for claim in observed),
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
        f"{claim.claim!r} is a {claim.kind} {claim.stance} claim without matching {claim.support_requirement} evidence"
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
    scope, root = _claim_scope_and_root(clause, match)
    return NegativeExistenceClaim(
        kind=kind,
        subject=subject,
        claim=claim_text,
        stance=stance,
        scope=scope,
        root=root,
        support_requirement=support,
        span_start=clause_start + match.start(),
        span_end=clause_start + match.end(),
    )


def _claim_stance(clause: str, match: re.Match[str], following_clause: str) -> str:
    if _match_is_quoted(clause, match) or _QUESTION_OR_HYPOTHETICAL.search(clause):
        return QUOTED_OR_HYPOTHETICAL
    before = clause[max(0, match.start() - 56) : match.start()]
    after = clause[match.end() : match.end() + 72]
    observed = bool(_OBSERVED_MARKER.search(match.group(0) + after[:48]))
    if _QUALIFYING_PREFIX.search(before) or _QUALIFYING_SUFFIX.search(after):
        return EPISTEMICALLY_QUALIFIED
    if observed and _NEXT_CLAUSE_QUALIFIER.search(following_clause):
        return EPISTEMICALLY_QUALIFIED
    if observed:
        return OBSERVED_NO_MATCH
    return ASSERTED_ABSENCE


def _claim_scope_and_root(clause: str, claim_match: re.Match[str]) -> tuple[str, str | None]:
    """Bind scope to the root reference governing this particular claim.

    A single clause can describe several roots.  Selecting a root from the whole
    clause lets an earlier primary reference incorrectly authorize a later
    additional-root absence claim, so prefer the closest preceding typed marker.
    """
    references = tuple(_ROOT_REFERENCE.finditer(clause))
    preceding = [reference for reference in references if reference.end() <= claim_match.start()]
    reference = preceding[-1] if preceding else next(
        (candidate for candidate in references if candidate.start() >= claim_match.end()),
        None,
    )
    if reference is None:
        return "unspecified", None
    if reference.group("multi"):
        return "multi_root", "multi_root"
    if reference.group("primary"):
        return "primary", "primary"
    if reference.group("additional"):
        return "root_local", "additional"
    return "root_local", None


def _bare_java_has_file_scope(clause: str, match: re.Match[str]) -> bool:
    """Avoid treating experience, dependency, or version language as file discovery facts."""
    nearby = clause[max(0, match.start() - 48) : match.end() + 48]
    tail = clause[match.end() :]
    observed = bool(_OBSERVED_MARKER.search(match.group(0) + tail[:48]))
    has_source_noun = bool(_BARE_JAVA_SOURCE_TAIL.match(tail))
    has_bounded_tail = bool(_BARE_JAVA_TAIL.match(tail))
    # "I checked and found no Java." is an observation claim even without a
    # root label. An absolute "no Java" remains a file/root-scoped claim.
    return has_source_noun or (
        observed and (has_bounded_tail or bool(_OBSERVED_BARE_JAVA_TAIL.match(tail)))
    ) or (has_bounded_tail and bool(_ROOT_SCOPE_MARKER.search(nearby)))


def _overlaps_existing_match(match: re.Match[str], matcher: re.Pattern[str], clause: str) -> bool:
    return any(candidate.start() <= match.start() < candidate.end() for candidate in matcher.finditer(clause))


def _match_is_quoted(clause: str, match: re.Match[str]) -> bool:
    return any(quoted.start() <= match.start() and match.end() <= quoted.end() for quoted in _QUOTED_EXAMPLE.finditer(clause))


def _clause_spans(content: str) -> Iterable[tuple[int, int]]:
    quote_spans = tuple((quoted.start(), quoted.end()) for quoted in _QUOTED_EXAMPLE.finditer(content))
    start = 0
    for match in _CLAUSE_BREAK.finditer(content):
        if any(quote_start <= match.start() < quote_end for quote_start, quote_end in quote_spans):
            continue
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
        return any(
            _result_supports_scope(claim, result) and _is_extension_observation(result, claim.subject, claim.stance)
            for result in results
        )
    if claim.kind == "source_tree":
        return any(
            _result_supports_scope(claim, result) and _is_source_observation(result, claim.stance)
            for result in results
        )
    if claim.kind == "git_repository":
        if claim.root == "additional":
            # Git tools are deliberately primary-workspace only; an additional
            # root must be promoted with /move before making this conclusion.
            return False
        return any(
            _result_supports_scope(claim, result)
            and
            result.name == "git_status"
            and bool(result.metadata.get("git_probe_root"))
            and result.metadata.get("git_repository") is False
            for result in results
        )
    return False


def _result_supports_scope(claim: NegativeExistenceClaim, result: ToolResultSummary) -> bool:
    """Keep root-local observations from silently becoming cross-root facts."""
    metadata = result.metadata
    label = str(metadata.get("evidence_root_label") or "")
    if claim.root == "primary":
        return label == "primary"
    if claim.root == "additional":
        return bool(label and label not in {"primary", "(unknown)"})
    if claim.scope == "root_local":
        return bool(label and label != "(unknown)")
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
    if str(metadata.get("evidence_origin") or "current_run") != "current_run":
        return False
    if str(metadata.get("negative_evidence_type") or "") != "path_no_match":
        return False
    if not metadata.get("complete") or metadata.get("truncated") or metadata.get("result_limit_reached"):
        return False
    patterns = " ".join(str(value).lower() for value in metadata.get("patterns", ()))
    if subject == "java":
        return ".java" in patterns or "java" in patterns
    return patterns.strip() in {"**/*", "**", "*"}


def _is_extension_observation(result: ToolResultSummary, subject: str, stance: str) -> bool:
    return _is_complete_path_no_match(result, subject)


def _is_source_observation(result: ToolResultSummary, stance: str) -> bool:
    return _is_complete_path_no_match(result, "source")


def _non_code_text(content: str) -> str:
    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    # Quoted claims remain visible to the parser so they can be reported as
    # quoted_or_hypothetical rather than silently disappearing from telemetry.
    return _INLINE_CODE.sub(blank, _FENCED_CODE.sub(blank, content or ""))


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
    "unsupported_unlocated_escalations",
]

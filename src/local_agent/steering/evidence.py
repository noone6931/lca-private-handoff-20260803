from __future__ import annotations

import re

from ..design_evidence import missing_design_evidence_roots
from ..negative_evidence import allowed_tools_for_negative_claims
from ..negative_evidence import negative_claim_metrics
from ..negative_evidence import render_negative_existence_issues
from ..negative_evidence import unsupported_negative_existence_claims
from ..requirement_evidence import requirement_fact_citation_issues
from ..task_contract import is_inspection_forbidden
from .models import *  # noqa: F403
from .models import _EXPLICIT_TOOL_NON_EXECUTION

class ReadOnlyEvidenceSteerer:
    kind = "read_only_evidence"

    def __init__(self, *, max_steers: int) -> None:
        self._max_steers = max_steers

    def decide(self, context: FinalAnswerContext) -> SteeringDecision | None:
        if context.steer_counts.get(self.kind, 0) >= self._max_steers:
            return None
        if not request_needs_read_only_code_evidence(context.request):
            return None
        if has_successful_read_file_since(context.messages, context.run_start_index):
            return None
        if content_reports_no_source_evidence(context.content) and has_negative_source_evidence_since(
            context.messages,
            context.run_start_index,
        ):
            return None
        content_summary = one_line(context.content, max_chars=800)
        steering = (
            "Runtime steering: the user asked for code/source evidence, but the previous answer was not grounded "
            "in a successful read_file result from this run. Do not give an industry-practice or filename-based "
            "guess.\n"
            "- Use search_code or lsp_* to locate candidate files, then read_file the relevant implementation file.\n"
            "- If searches return no matches, state that as a verified negative result and include the search terms.\n"
            "- After collecting code evidence, answer the original question directly and separate verified facts from inference.\n"
            f"- Draft final answer that triggered this check: {content_summary}"
            f"{final_answer_request_summary(context.request)}"
        )
        return SteeringDecision(
            kind=self.kind,
            message=steering,
            payload={},
            force_final_answer_without_tools=False,
            temporary_tool_allowlist=set(READ_ONLY_EVIDENCE_TOOLS),
        )


class RequirementEvidenceSteerer:
    kind = "requirement_evidence"

    def __init__(self, *, max_steers: int) -> None:
        self._max_steers = max_steers

    def decide(self, context: FinalAnswerContext) -> SteeringDecision | None:
        if context.steer_counts.get(self.kind, 0) >= self._max_steers:
            return None
        if context.requirement_contract is not None and context.requirement_contract.inspection_forbidden:
            return None
        if context.requirement_contract is not None and context.requirement_contract.task_kind == "code-implementation":
            return None
        issues = requirement_fact_citation_issues(context.content, context.requirement_evidence)
        if not issues:
            return None
        sources = ", ".join(
            f"{item.path} [root={item.root or '(unknown)'}; scope={item.scope}]"
            for item in context.requirement_evidence
        )
        steering = (
            "Runtime steering: the previous read-only design answer states requirement facts without an exact "
            "requirement-file path and line citation. Do not call tools. Rewrite the final answer using the pinned "
            "requirement evidence as the authority. Remove any workflow that is not in the requirement source, and "
            "label new classes, fields, routes, or integration choices as 推断/建议 rather than verified facts.\n"
            f"- Requirement sources: {sources}\n"
            "- A root_local source constrains only that source root; it does not require changes in sibling roots unless "
            "the user explicitly requested cross-root synthesis.\n"
            "- Cite every requirement fact as `path:line`; do not cite a made-up section or line.\n"
            f"- Missing condition: {', '.join(issues)}"
            f"{final_answer_request_summary(context.request)}"
        )
        return SteeringDecision(
            kind=self.kind,
            message=steering,
            payload={"issues": issues, "sources": [item.path for item in context.requirement_evidence]},
        )


class DesignEvidenceSteerer:
    kind = "design_evidence"

    def __init__(self, *, max_steers: int) -> None:
        self._max_steers = max_steers

    def decide(self, context: FinalAnswerContext) -> SteeringDecision | None:
        if context.steer_counts.get(self.kind, 0) >= self._max_steers:
            return None
        if context.requirement_contract is not None and context.requirement_contract.inspection_forbidden:
            return None
        missing_roots = missing_design_evidence_roots(
            context.required_design_evidence_roots,
            context.design_evidence_read_paths,
        )
        if not missing_roots:
            return None
        steering = (
            "Runtime steering: this cross-root design task cannot finalize yet because the required source evidence "
            "coverage is incomplete. Use read/search/LSP tools only until each missing code root has at least one "
            "successful source-file read. Do not infer front-end or back-end reuse from a different root.\n"
            + "\n".join(f"- Missing source read under: {root}" for root in missing_roots)
            + f"{final_answer_request_summary(context.request)}"
        )
        return SteeringDecision(
            kind=self.kind,
            message=steering,
            payload={"missing_roots": list(missing_roots)},
            force_final_answer_without_tools=False,
            temporary_tool_allowlist=set(READ_ONLY_EVIDENCE_TOOLS),
        )



class SourceGroundedNumericSteerer:
    kind = "source_grounded_numeric"

    def __init__(self, *, max_steers: int) -> None:
        self._max_steers = max_steers

    def decide(self, context: FinalAnswerContext) -> SteeringDecision | None:
        if context.steer_counts.get(self.kind, 0) >= self._max_steers:
            return None
        if context.requirement_contract is not None and context.requirement_contract.task_kind != "read-only":
            return None
        if not context.source_evidence:
            return None
        if not request_needs_source_grounded_numeric_facts(context.request, context.content):
            return None
        issues = source_numeric_issues(context.content, context.source_evidence)
        if not issues:
            return None
        issue_lines: list[str] = []
        for issue in issues[:5]:
            issue_lines.append(f"- Unsupported numeric/source claim: {issue['claim']}")
            issue_lines.append(f"  Evidence file: {issue['path']}")
            for snippet in issue["snippets"][:8]:
                issue_lines.append(f"  {snippet}")
        steering = (
            "Runtime steering: the previous final answer contains numeric/status/interface facts that do not match "
            "the source snippets read in this run. Do not call tools. Rewrite the final answer using only exact "
            "values from the evidence below; if a value is not present, mark it as 未找到/未确认 instead of inventing it.\n"
            + "\n".join(issue_lines)
            + f"{final_answer_request_summary(context.request)}"
        )
        return SteeringDecision(
            kind=self.kind,
            message=steering,
            payload={"issues": [issue["summary"] for issue in issues[:5]]},
        )


class SourceEvidenceFalseNegativeSteerer:
    kind = "source_evidence_false_negative"

    def __init__(self, *, max_steers: int) -> None:
        self._max_steers = max_steers

    def decide(self, context: FinalAnswerContext) -> SteeringDecision | None:
        if context.steer_counts.get(self.kind, 0) >= self._max_steers:
            return None
        if not context.source_evidence:
            return None
        if not request_needs_read_only_code_evidence(context.request):
            return None
        if not content_claims_source_missing_or_incomplete(context.content):
            return None
        issues = source_false_negative_issues(context.request or "", context.content, context.source_evidence)
        if not issues:
            return None
        issue_lines: list[str] = []
        for issue in issues[:5]:
            issue_lines.append(f"- Claimed missing/incomplete, but evidence contains: {', '.join(issue['terms'])}")
            issue_lines.append(f"  Evidence file: {issue['path']}")
            for snippet in issue["snippets"][:8]:
                issue_lines.append(f"  {snippet}")
        steering = (
            "Runtime steering: the previous final answer said source evidence was missing or incomplete, but "
            "the already-read source snippets below contain requested symbols/facts. Do not call tools. Rewrite "
            "the final answer from these snippets; only mark a specific item 未找到/未确认 when it is absent from "
            "the evidence below.\n"
            + "\n".join(issue_lines)
            + final_answer_request_summary(context.request)
        )
        return SteeringDecision(
            kind=self.kind,
            message=steering,
            payload={"issues": [issue["summary"] for issue in issues[:5]]},
        )


class ToolUsageEvidenceSteerer:
    """Keep final claims about tool evidence aligned with observed tool results."""

    kind = "tool_usage_evidence"

    def __init__(self, *, max_steers: int) -> None:
        self._max_steers = max_steers

    def decide(self, context: FinalAnswerContext) -> SteeringDecision | None:
        if context.steer_counts.get(self.kind, 0) >= self._max_steers:
            return None
        claimed_missing_tools = phantom_tool_evidence_claims(context.content, context.tool_results)
        if not claimed_missing_tools:
            return None
        tools = ", ".join(claimed_missing_tools)
        steering = (
            "Runtime steering: the previous final answer claimed evidence, invocation, or an empty result from tools "
            "that did not run in this task. Do not call tools. Rewrite the answer using only tool results actually "
            "observed in this run; say an item is unverified rather than attributing it to an uncalled tool.\n"
            f"- Unsupported tool-evidence claims: {tools}\n"
            f"- Tools actually observed: {', '.join(_observed_tool_names(context.tool_results)) or 'none'}"
            f"{final_answer_request_summary(context.request)}"
        )
        return SteeringDecision(
            kind=self.kind,
            message=steering,
            payload={"unobserved_tools": list(claimed_missing_tools)},
        )


class NegativeExistenceSteerer:
    """Reject path/source/Git absence claims that lack matching discovery evidence."""

    kind = "negative_existence"

    def __init__(self, *, max_steers: int) -> None:
        self._max_steers = max_steers

    def decide(self, context: FinalAnswerContext) -> SteeringDecision | None:
        if context.steer_counts.get(self.kind, 0) >= self._max_steers:
            return None
        issues = unsupported_negative_existence_claims(context.content, context.tool_results)
        if not issues:
            return None
        claim_metrics = negative_claim_metrics(context.content, context.tool_results)
        allowed_tools = set(allowed_tools_for_negative_claims(issues))
        issue_lines = "\n".join(f"- {issue}" for issue in render_negative_existence_issues(issues))
        if allowed_tools:
            action = (
                "Use glob_files for file/extension/source-tree absence, or read_file/list_files for an exact path. "
                "A content search no-match and a truncated directory listing cannot prove that a path is absent."
            )
        else:
            action = (
                "Do not call tools: rewrite this as unconfirmed. Git checks apply only to the primary workspace; "
                "tell the user to use /move before making a Git-repository conclusion about an additional root."
            )
        steering = (
            "Runtime steering: the previous final answer made a path/source/Git absence claim without matching "
            "evidence. Do not present it as verified.\n"
            f"{issue_lines}\n{action}"
            f"{final_answer_request_summary(context.request)}"
        )
        return SteeringDecision(
            kind=self.kind,
            message=steering,
            payload={
                "issues": render_negative_existence_issues(issues),
                "claim_metrics": claim_metrics,
                "blocked_assertion_count": len(issues),
            },
            force_final_answer_without_tools=not allowed_tools,
            temporary_tool_allowlist=allowed_tools or None,
        )



def request_needs_read_only_code_evidence(request: str | None) -> bool:
    if is_inspection_forbidden(request or ""):
        return False
    lowered = (request or "").lower()
    if not lowered.strip():
        return False
    if any(keyword.lower() in lowered for keyword in NO_SPECULATION_REQUEST_KEYWORDS):
        return True
    if any(keyword.lower() in lowered for keyword in EVIDENCE_REQUEST_KEYWORDS):
        return True
    return any(keyword.lower() in lowered for keyword in IMPLEMENTATION_EVIDENCE_REQUEST_KEYWORDS)


def has_successful_read_file_since(messages: list[dict[str, Any]], start_index: int) -> bool:
    for message in messages[start_index:]:
        if message.get("role") != "tool":
            continue
        if message.get("_lca_tool_name") == "read_file" and not message.get("_lca_is_error"):
            return True
    return False


def has_negative_source_evidence_since(messages: list[dict[str, Any]], start_index: int) -> bool:
    for message in messages[start_index:]:
        if message.get("role") != "tool" or message.get("_lca_is_error"):
            continue
        name = str(message.get("_lca_tool_name") or "")
        if (name == "search_code" or name.startswith("lsp_")) and message.get("_lca_useless"):
            return True
    return False


def content_reports_no_source_evidence(content: str) -> bool:
    lowered = content.lower()
    return any(marker.lower() in lowered for marker in SOURCE_NOT_FOUND_MARKERS)


def content_claims_source_missing_or_incomplete(content: str) -> bool:
    lowered = content.lower()
    return any(marker.lower() in lowered for marker in SOURCE_EVIDENCE_ABSENCE_MARKERS)


def source_false_negative_issues(
    request: str,
    content: str,
    evidence: list[SourceEvidence],
) -> list[dict[str, Any]]:
    terms = request_source_terms(request)
    if not terms:
        return []
    lowered_content = content.lower()
    issues: list[dict[str, Any]] = []
    for item in evidence:
        lowered_source = item.content.lower()
        matched_terms = [
            term
            for term in terms
            if term.lower() in lowered_source
            and (term.lower() in lowered_content or content_claims_source_missing_or_incomplete(content))
        ]
        if not matched_terms:
            continue
        snippets = _source_snippets_for_terms(item.content, matched_terms)
        if not snippets:
            continue
        issues.append(
            {
                "path": item.path,
                "terms": matched_terms[:8],
                "snippets": snippets,
                "summary": f"{item.path}: evidence contains {', '.join(matched_terms[:5])}",
            }
        )
    return issues


def request_source_terms(request: str) -> list[str]:
    # Absolute paths are evidence locations, not source identifiers. Keeping
    # their segments (for example Users/chengming/src) lets a later false-
    # negative check "find" the path header injected by read_file itself.
    pathless_request = re.sub(
        r"(?<![A-Za-z0-9_])(?:~|/)[^\s`\"'，。；;、()（）]*",
        " ",
        request or "",
    )
    raw_terms = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", pathless_request)
    seen: set[str] = set()
    terms: list[str] = []
    for term in raw_terms:
        lowered = term.lower()
        if lowered in seen or lowered in SOURCE_FALSE_NEGATIVE_STOPWORDS:
            continue
        seen.add(lowered)
        terms.append(term)
    return terms[:24]


def request_mentions_todo(content: str | None) -> bool:
    lowered = (content or "").lower()
    return any(keyword in lowered for keyword in TODO_REQUEST_KEYWORDS)


def tool_names_since(messages: list[dict[str, Any]], start_index: int) -> set[str]:
    names: set[str] = set()
    for message in messages[start_index:]:
        if message.get("role") == "tool":
            name = message.get("_lca_tool_name")
            if isinstance(name, str) and name:
                names.add(name)
        elif message.get("role") == "assistant":
            names.update(assistant_tool_call_names(message))
    return names


def phantom_tool_evidence_claims(
    content: str,
    tool_results: list[ToolResultSummary],
) -> tuple[str, ...]:
    """Return unobserved tools that the answer presents as run/result evidence.

    Merely recommending a tool is valid. A claim must occur in the same
    sentence-like segment as an execution/result/evidence marker, and explicit
    statements that the tool was *not called* are deliberately ignored.
    """

    observed = set(_observed_tool_names(tool_results))
    if not content.strip():
        return ()
    claimed: set[str] = set()
    for segment in re.split(r"[\n。！？!?;]+", content.lower()):
        if not segment.strip() or not _looks_like_tool_evidence_claim(segment):
            continue
        if _EXPLICIT_TOOL_NON_EXECUTION.search(segment):
            continue
        for tool in KNOWN_TOOL_EVIDENCE_NAMES - observed:
            if _tool_reference_is_recommendation(segment, tool):
                continue
            if re.search(rf"(?<![a-z0-9_]){re.escape(tool)}(?![a-z0-9_])", segment):
                claimed.add(tool)
        if (
            "lsp" not in observed
            and not _tool_reference_is_recommendation(segment, "lsp")
            and re.search(r"(?<![a-z0-9_])lsp(?:[_*][a-z0-9_*]+)?(?![a-z0-9_])", segment)
        ):
            claimed.add("lsp_*")
    return tuple(sorted(claimed))


def _looks_like_tool_evidence_claim(segment: str) -> bool:
    return any(marker in segment for marker in TOOL_EVIDENCE_CLAIM_MARKERS)


def _tool_reference_is_recommendation(segment: str, tool: str) -> bool:
    """Return whether a tool is only proposed as a future action in ``segment``.

    A final answer may recommend a verification step without falsely claiming
    that the Runtime already executed it. Keep this check local to the tool
    reference, so a separate factual claim in another sentence is still gated.
    """

    if tool == "lsp":
        tool_pattern = r"(?<![a-z0-9_])lsp(?:[_*][a-z0-9_*]+)?(?![a-z0-9_])"
    else:
        escaped_tool = re.escape(tool)
        tool_pattern = rf"(?<![a-z0-9_]){escaped_tool}(?![a-z0-9_])"
    matches = list(re.finditer(tool_pattern, segment))
    if not matches:
        return False
    saw_recommendation = False
    for match in matches:
        clause = _tool_reference_clause(segment, match.start(), match.end())
        if _clause_marks_tool_recommendation(clause, tool_pattern):
            saw_recommendation = True
            continue
        return False
    trailing = segment[matches[-1].end() :]
    if _looks_like_result_backreference(trailing):
        return False
    return saw_recommendation


def _tool_reference_clause(segment: str, start: int, end: int) -> str:
    left = max(
        segment.rfind(marker, 0, start)
        for marker in (",", "，", "。", ";", "；", "!", "！", "?", "？")
    )
    right_candidates = [
        index
        for marker in (",", "，", "。", ";", "；", "!", "！", "?", "？")
        if (index := segment.find(marker, end)) != -1
    ]
    right = min(right_candidates) if right_candidates else len(segment)
    return segment[left + 1 : right]


def _clause_marks_tool_recommendation(clause: str, tool_pattern: str) -> bool:
    chinese_recommendation = (
        r"(?:建议|推荐|下一步|后续|请|应当|应该|可以|可通过|可使用|需要)"
        r"(?:\s*(?:先|再|直接|使用|调用|运行|通过|借助|用))*\s*"
    )
    english_recommendation = (
        r"(?:recommend(?:ed|ation)?|suggest(?:ed|ion)?|should|can|could|may|"
        r"please|next(?:\s+step)?(?:\s+is)?)"
        r"(?:\s+(?:to|using|use|calling|call|run))*\s+"
    )
    return bool(
        re.search(chinese_recommendation + tool_pattern, clause)
        or re.search(english_recommendation + tool_pattern, clause)
    )


def _looks_like_result_backreference(trailing: str) -> bool:
    if _looks_like_future_tool_condition(trailing):
        return False
    return bool(
        re.search(
            r"(?:its|their|the)\s+results?\s+(?:show|shows|indicate|indicates|confirm|confirms)"
            r"|(?:根据|依照).{0,24}结果"
            r"|(?:结果|输出)\s*(?:显示|表明|说明|证明)"
            r"|all tests passed"
            r"|测试(?:全部)?通过",
            trailing,
        )
    )


def _looks_like_future_tool_condition(trailing: str) -> bool:
    return bool(
        re.search(
            r"(?:如果|若|待).{0,24}测试(?:全部)?通过"
            r"|测试(?:全部)?通过后(?:再|再行|再做|再去)"
            r"|(?:if|once|after)\s+(?:the\s+)?tests?\s+(?:pass|passed)\b",
            trailing,
        )
    )


def _observed_tool_names(tool_results: list[ToolResultSummary]) -> tuple[str, ...]:
    return tuple(sorted({result.name for result in tool_results if result.name}))


def assistant_tool_call_names(message: dict[str, Any]) -> list[str]:
    names: list[str] = []
    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        return names
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function") or {}
        if isinstance(function, dict) and function.get("name"):
            names.append(str(function["name"]))
    return names


def request_needs_source_grounded_numeric_facts(request: str | None, content: str) -> bool:
    lowered_request = (request or "").lower()
    if not any(char.isdigit() for char in content):
        return False
    # A status/date/line number in a generated answer is not a request for a
    # source-level numeric audit. Only activate this strict gate when the user
    # explicitly asks about codes, states, fields, or interfaces.
    return _contains_numeric_fact_marker(lowered_request)


def source_numeric_issues(content: str, evidence: list[SourceEvidence]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    evidence_by_key = _evidence_by_key(evidence)
    for claim in _numeric_claim_lines(content):
        if _is_tool_observation_numeric_claim(claim):
            continue
        # `path:line` and `V1.3:line` are citations, not business values to
        # prove against the cited implementation. A final answer can combine a
        # requirement citation with an implementation path on one table row.
        claim_numbers = _number_tokens(_strip_location_citations(claim))
        if not claim_numbers:
            continue
        matched = _matching_evidence(claim, evidence_by_key)
        if any(all(number in _number_tokens(item.content) for number in claim_numbers) for item in matched):
            continue
        for item in matched:
            missing = [number for number in claim_numbers if number not in _number_tokens(item.content)]
            snippets = _source_snippets_for_claim(item.content, claim)
            issues.append(
                {
                    "claim": one_line(claim, max_chars=220),
                    "path": item.path,
                    "snippets": snippets,
                    "summary": f"{item.path}: missing numbers {', '.join(missing)} for claim {one_line(claim, max_chars=120)}",
                }
            )
            break
    return issues


def _evidence_by_key(evidence: list[SourceEvidence]) -> dict[str, list[SourceEvidence]]:
    by_key: dict[str, list[SourceEvidence]] = {}
    for item in evidence:
        normalized_path = item.path.replace("\\", "/").lower()
        path_parts = normalized_path.split("/")
        filename = path_parts[-1] if path_parts else item.path
        stem = filename.rsplit(".", 1)[0]
        # Index the complete path as well as the basename. A codebase commonly
        # contains many `list.vue` files; preserving every candidate prevents a
        # later read from overwriting the source actually cited by the answer.
        for key in {normalized_path, filename, stem}:
            if key:
                by_key.setdefault(key.lower(), []).append(item)
    return by_key


def _matching_evidence(claim: str, evidence_by_key: dict[str, list[SourceEvidence]]) -> list[SourceEvidence]:
    lowered_claim = claim.lower()
    matched: list[SourceEvidence] = []
    for key, items in evidence_by_key.items():
        if key:
            for item in items:
                if key in lowered_claim and item not in matched:
                    matched.append(item)
    claim_identifiers = _claim_identifiers(claim)
    all_evidence = [item for items in evidence_by_key.values() for item in items]
    for item in all_evidence:
        if item in matched:
            continue
        lowered_source = item.content.lower()
        if any(identifier in lowered_source for identifier in claim_identifiers):
            matched.append(item)
    return matched


def _strip_location_citations(claim: str) -> str:
    without_version_citations = re.sub(r"\bV\d+(?:\.\d+)*:(?:\d+)(?:[-,]\d+)*", "", claim, flags=re.IGNORECASE)
    return re.sub(r"(?<=[A-Za-z0-9_.])\:(?:\d+)(?:[-,]\d+)*", "", without_version_citations)


def _numeric_claim_lines(content: str) -> list[str]:
    claims: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or not any(char.isdigit() for char in stripped):
            continue
        if (
            any(token in stripped for token in {"Enum", "Status", "状态", "枚举", "code", "接口", "字段", "Controller"})
            or _claim_identifiers(stripped)
            or _looks_like_numeric_table_row(stripped)
        ):
            claims.append(stripped)
    return claims


def _claim_identifiers(claim: str) -> set[str]:
    identifiers: set[str] = set()
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", claim):
        if len(token) < 3:
            continue
        if token.lower() in SOURCE_FALSE_NEGATIVE_STOPWORDS:
            continue
        if token.isupper() or "_" in token or any(char.isupper() for char in token[1:]):
            identifiers.add(token.lower())
    return identifiers


def _looks_like_numeric_table_row(line: str) -> bool:
    if not (line.startswith("|") and line.endswith("|")):
        return False
    cells = [cell.strip() for cell in line.strip("|").split("|")]
    return any(re.fullmatch(r"-?\d+", cell) for cell in cells)


def _is_tool_observation_numeric_claim(claim: str) -> bool:
    lowered = claim.lower()
    if any(
        marker in lowered
        for marker in {"git_diff", "run_tests", "apply_patch", "dry_run", "[exit_code]", "[diff summary]", "hunk", "tag:"}
    ):
        return True
    if "@@" in claim and re.search(r"@@\s*-?\d+(?:,\d+)?\s+\+?\d+(?:,\d+)?\s*@@", claim):
        return True
    if re.search(r"[+-]\d+\s+[+-]\d+", claim) and re.search(r"\.(?:java|py|ts|tsx|js|jsx|vue|xml|md)\b", lowered):
        return True
    if re.search(r"\bran\s+\d+\s+tests?\b", lowered) or re.search(r"\b\d+\s*(?:项)?测试\b", claim):
        return True
    return False


def _contains_numeric_fact_marker(text: str) -> bool:
    if any(marker in text for marker in {"状态码", "状态", "枚举", "接口", "字段", "常量", "enum", "status"}):
        return True
    return bool(re.search(r"\bcode\b", text))


def _number_tokens(content: str) -> set[str]:
    without_path_lines = re.sub(
        r"(?i)[^\s`]+\.(?:java|vue|ts|tsx|js|jsx|py|xml|md|yml|yaml|properties):\d+(?:-\d+)?",
        "",
        content,
    )
    without_read_file_line_numbers = re.sub(r"(?m)^\s*\d+:", "", without_path_lines)
    return set(re.findall(r"(?<![\w.])-?\d+(?![\w.])", without_read_file_line_numbers))


def _source_snippets_for_claim(source_content: str, claim: str) -> list[str]:
    claim_terms = {
        token.lower()
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]{2,}", claim)
        if len(token) >= 2
    }
    snippets: list[str] = []
    for line in source_content.splitlines():
        lowered = line.lower()
        if any(term in lowered for term in claim_terms) or any(char.isdigit() for char in line):
            snippets.append(line)
        if len(snippets) >= 12:
            break
    return snippets or source_content.splitlines()[:8]


def _source_snippets_for_terms(source_content: str, terms: list[str]) -> list[str]:
    lowered_terms = [term.lower() for term in terms if term]
    snippets: list[str] = []
    lines = source_content.splitlines()
    for index, line in enumerate(lines):
        lowered = line.lower()
        if not any(term in lowered for term in lowered_terms):
            continue
        start = max(0, index - 2)
        end = min(len(lines), index + 3)
        for snippet in lines[start:end]:
            if snippet not in snippets:
                snippets.append(snippet)
    return snippets[:12] or source_content.splitlines()[:8]


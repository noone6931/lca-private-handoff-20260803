from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .task_contract import RequirementContract
from .tool_choice_queue import ToolResultSummary
from .verification_timeline import results_after_last_write
from .verification_timeline import workspace_write_happened


ReviewSeverity = Literal["blocking", "warning"]

CALL_SITE_EVIDENCE_TOOLS = frozenset({"search_code", "lsp_references", "read_file"})
TEST_REMEDIATION_TOOLS = frozenset({"read_file", "search_code", "apply_patch", "write_file", "run_tests", "git_diff"})
QUALITY_REMEDIATION_TOOLS = frozenset(
    {"read_file", "search_code", "lsp_references", "apply_patch", "write_file", "rollback_patch", "run_tests", "git_diff"}
)

_DIFF_REVIEWER_HEADER = "[diff reviewer]"
_DIFF_SECTION_HEADERS = ("[diff summary]", "[diff attribution]", "[diff reviewer]")
_PUBLIC_METHOD_PATTERN = re.compile(
    r"^[+-]\s*(?:public|protected)\s+(?:static\s+)?[A-Za-z_$][\w$<>, ?\[\].]*\s+(?P<symbol>[A-Za-z_$][\w$]*)\s*\(",
)
_EXPORTED_JS_API_PATTERN = re.compile(
    r"^[+-]\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+(?P<symbol>[A-Za-z_$][\w$]*)",
)
_TEST_REQUEST_MARKERS = (
    "单元测试",
    "补测试",
    "补充测试",
    "新增测试",
    "加测试",
    "加一个测试",
    "添加测试",
    "编写测试",
    "测试用例",
    "test coverage",
    "unit test",
    "add test",
    "add tests",
    "write test",
    "write tests",
)


@dataclass(frozen=True)
class PatchReviewFinding:
    code: str
    severity: ReviewSeverity
    message: str
    allowed_tools: tuple[str, ...]


@dataclass(frozen=True)
class PatchReviewResult:
    findings: tuple[PatchReviewFinding, ...]

    @property
    def passed(self) -> bool:
        return not self.findings

    @property
    def blocking_findings(self) -> tuple[PatchReviewFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == "blocking")

    def allowed_tool_names(self) -> tuple[str, ...]:
        names: set[str] = set()
        for finding in self.findings:
            names.update(finding.allowed_tools)
        return tuple(sorted(names))

    def payload(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "finding_count": len(self.findings),
            "findings": [
                {
                    "code": finding.code,
                    "severity": finding.severity,
                    "message": finding.message,
                    "allowed_tools": list(finding.allowed_tools),
                }
                for finding in self.findings
            ],
        }


@dataclass(frozen=True)
class PatchReviewFacts:
    changed_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    public_api_symbols: tuple[str, ...]

    def metadata(self) -> dict[str, object]:
        return {
            "changed_paths": list(self.changed_paths),
            "test_paths": list(self.test_paths),
            "public_api_symbols": list(self.public_api_symbols),
        }


def review_patch(
    contract: RequirementContract | None,
    *,
    request: str | None,
    tool_results: list[ToolResultSummary],
) -> PatchReviewResult:
    """Review a completed implementation diff before the runtime accepts a final answer.

    This is intentionally deterministic. It turns facts already collected by the runtime
    (write results, git_diff output, and call-site searches) into a small second-stage
    review gate. It is not an LLM code review and it does not replace human review.
    """

    if contract is None or contract.task_kind != "code-implementation":
        return PatchReviewResult(())
    if not workspace_write_happened(tool_results):
        return PatchReviewResult(())

    latest_diff = _latest_successful_git_diff_after_last_write(tool_results)
    if latest_diff is None:
        return PatchReviewResult(
            (
                _finding(
                    "git_diff_missing",
                    "blocking",
                    "A workspace write happened, but no successful git_diff result is available for independent patch review.",
                    {"git_diff"},
                ),
            )
        )

    diff_content = latest_diff.content
    findings: list[PatchReviewFinding] = []
    if "Potential relevance warning" in diff_content:
        findings.append(
            _finding(
                "relevance_warning",
                "blocking",
                "git_diff flagged this-session edits in deployment/config-like paths without direct implementation relevance.",
                QUALITY_REMEDIATION_TOOLS,
            )
        )
    if "implementation-quality warning" in diff_content:
        findings.append(
            _finding(
                "comment_only_implementation",
                "blocking",
                "git_diff found a code patch that appears comment/documentation-only for an implementation task.",
                QUALITY_REMEDIATION_TOOLS,
            )
        )

    facts = _review_facts_for_diff(latest_diff)
    if _request_requires_test_change(request) and _has_changed_source_file(facts) and not facts.test_paths:
        findings.append(
            _finding(
                "requested_test_missing",
                "blocking",
                "The request explicitly asks for tests, but the reviewed diff contains source changes without a test-file change.",
                TEST_REMEDIATION_TOOLS,
            )
        )
    if facts.public_api_symbols and not _has_call_site_evidence_after_last_write(tool_results, facts):
        findings.append(
            _finding(
                "call_site_review_missing",
                "warning",
                "The diff changes a public/protected or exported API, but no post-write search/LSP reference evidence checks likely callers.",
                CALL_SITE_EVIDENCE_TOOLS | {"git_diff"},
            )
        )
    return PatchReviewResult(tuple(findings))


def render_patch_review_message(result: PatchReviewResult, *, request: str | None) -> str:
    lines = [
        "Runtime patch review found gaps after the implementation diff. Do not claim the RequirementContract is complete yet.",
        "Use only the allowed tools below to inspect callers, add/correct implementation or tests, re-check the diff, or roll back an unsuitable patch.",
    ]
    for finding in result.findings:
        allowed = ", ".join(finding.allowed_tools) or "(no tools)"
        lines.append(f"- [{finding.severity}] {finding.code}: {finding.message} Allowed tools: {allowed}.")
    lines.extend(
        [
            "- After any write, run the relevant tests/checks and git_diff again.",
            "- If the requested behavior cannot be completed safely, say it remains incomplete and explain the evidence; do not describe a comment-only or unrelated patch as an implementation.",
        ]
    )
    if request:
        lines.append(f"- Original request: {_one_line(request, max_chars=1000)}")
    return "\n".join(lines)


def review_input_summary(name: str, content: str, *, max_chars: int = 6000) -> str:
    """Keep enough diff evidence for the reviewer even when a raw diff is very large."""

    if name != "git_diff":
        # ToolChoiceQueue keeps a bounded run ledger. Only git_diff needs a larger
        # retained slice because its structured reviewer sections are consumed later.
        return _truncate(content, min(max_chars, 2000))
    if len(content) <= max_chars:
        return _truncate(content, max_chars)
    raw_diff = _raw_diff_content(content)
    sections = "\n\n".join(
        section
        for header in _DIFF_SECTION_HEADERS
        if (section := _section_from(content, header))
    )
    # Keep the semantic review sections even for deliberately tiny summaries.
    # The raw diff head is useful context, but it must yield space to the
    # `[diff reviewer]` warning that this module later evaluates.
    head_limit = max(0, max_chars - len(sections) - 2)
    summary = _truncate(raw_diff, head_limit)
    if sections:
        summary = f"{summary}\n\n{sections}"
    return _truncate(summary, max_chars)


def review_input_metadata(name: str, content: str) -> dict[str, object]:
    """Extract full-diff facts before the runtime stores a bounded diff summary."""

    if name != "git_diff":
        return {}
    return {"patch_review": _diff_review_facts(_raw_diff_content(content)).metadata()}


def _finding(code: str, severity: ReviewSeverity, message: str, allowed_tools: frozenset[str] | set[str]) -> PatchReviewFinding:
    return PatchReviewFinding(code, severity, message, tuple(sorted(allowed_tools)))


def _latest_successful_git_diff_after_last_write(results: list[ToolResultSummary]) -> ToolResultSummary | None:
    for result in reversed(results_after_last_write(results)):
        if result.name == "git_diff" and not result.is_error:
            return result
    return None


def _raw_diff_content(content: str) -> str:
    positions = [content.find(header) for header in _DIFF_SECTION_HEADERS if content.find(header) >= 0]
    return content[: min(positions)] if positions else content


def _section_from(content: str, header: str) -> str:
    start = content.find(header)
    if start < 0:
        return ""
    later_positions = [content.find(other, start + len(header)) for other in _DIFF_SECTION_HEADERS]
    later = [position for position in later_positions if position >= 0]
    end = min(later) if later else len(content)
    return content[start:end].strip()


def _request_requires_test_change(request: str | None) -> bool:
    lowered = (request or "").lower()
    return any(marker in lowered for marker in _TEST_REQUEST_MARKERS)


def _changed_paths(raw_diff: str) -> set[str]:
    paths: set[str] = set()
    for line in raw_diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) >= 4:
            path = parts[3]
            paths.add(path[2:] if path.startswith("b/") else path)
    return paths


def _diff_review_facts(raw_diff: str) -> PatchReviewFacts:
    changed_paths = tuple(sorted(_changed_paths(raw_diff)))
    test_paths = tuple(path for path in changed_paths if _is_test_path(path))
    public_api_symbols = tuple(sorted(_public_api_symbols(raw_diff)))
    return PatchReviewFacts(changed_paths, test_paths, public_api_symbols)


def _review_facts_for_diff(result: ToolResultSummary) -> PatchReviewFacts:
    metadata = result.metadata.get("patch_review") if isinstance(result.metadata, Mapping) else None
    if isinstance(metadata, Mapping):
        changed_paths = _metadata_strings(metadata.get("changed_paths"))
        test_paths = _metadata_strings(metadata.get("test_paths"))
        public_api_symbols = _metadata_strings(metadata.get("public_api_symbols"))
        return PatchReviewFacts(changed_paths, test_paths, public_api_symbols)
    return _diff_review_facts(_raw_diff_content(result.content))


def _has_changed_source_file(facts: PatchReviewFacts) -> bool:
    return any(path.lower().endswith((".java", ".py", ".js", ".jsx", ".ts", ".tsx", ".vue")) for path in facts.changed_paths)


def _is_test_path(path: str) -> bool:
    lowered = path.lower()
    filename = lowered.rsplit("/", 1)[-1]
    return (
        "/test/" in f"/{lowered}"
        or "/tests/" in f"/{lowered}"
        or "/spec/" in f"/{lowered}"
        or any(token in filename for token in ("test", "spec"))
    )


def _public_api_symbols(raw_diff: str) -> set[str]:
    symbols: set[str] = set()
    for line in raw_diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        match = _PUBLIC_METHOD_PATTERN.match(line) or _EXPORTED_JS_API_PATTERN.match(line)
        if match is not None:
            symbols.add(match.group("symbol"))
    return symbols


def _metadata_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(sorted(str(item) for item in value if isinstance(item, str) and item))


def _has_call_site_evidence_after_last_write(
    results: list[ToolResultSummary],
    facts: PatchReviewFacts,
) -> bool:
    evidence_tokens = {symbol.lower() for symbol in facts.public_api_symbols}
    changed_paths = {path.lower() for path in facts.changed_paths}
    evidence_tokens.update(_source_name_tokens(facts.changed_paths))
    for result in results_after_last_write(results):
        if result.is_error or result.useless or result.name not in CALL_SITE_EVIDENCE_TOOLS:
            continue
        if result.name == "read_file" and result.path and result.path.lower() in changed_paths:
            continue
        haystack = " ".join(part for part in (result.path, result.content) if part).lower()
        if any(token in haystack for token in evidence_tokens):
            return True
    return False


def _source_name_tokens(paths: tuple[str, ...]) -> set[str]:
    tokens: set[str] = set()
    for path in paths:
        filename = path.rsplit("/", 1)[-1]
        stem = filename.rsplit(".", 1)[0].lower()
        if len(stem) >= 4 and stem not in {"index", "main", "app"}:
            tokens.add(stem)
    return tokens


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 15)] + "...<truncated>"


def _one_line(value: str, *, max_chars: int) -> str:
    return _truncate(" ".join(value.split()), max_chars)

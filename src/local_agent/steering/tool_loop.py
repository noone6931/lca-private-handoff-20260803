from __future__ import annotations

from dataclasses import dataclass
import json
import re


READ_ONLY_EVIDENCE_TOOLS = frozenset(
    {
        "lsp_definition",
        "lsp_diagnostics",
        "lsp_document_symbols",
        "lsp_references",
        "lsp_symbols",
        "lsp_workspace_symbols",
        "read_file",
        "search_code",
    }
)

_FILENAME_PATTERN_SUFFIX = re.compile(
    r"\\\.(?:java|kt|kts|py|js|jsx|ts|tsx|vue|go|rs|rb|php|cs|cpp|c|h|xml|yml|yaml|json|md)\$?$",
    re.IGNORECASE,
)
_SOURCE_TREE_PATHS = frozenset(
    {
        "src",
        "src/main",
        "src/main/java",
        "src/main/resources",
        "src/test",
        "src/test/java",
    }
)


@dataclass(frozen=True)
class ToolLoopSignals:
    duplicate_skipped: bool
    duplicate_tool_name: str
    duplicate_guard_hits: int
    useless_search_skipped: bool
    useless_search_guard_hits: int
    useless_lsp_skipped: bool
    useless_lsp_guard_hits: int
    repeated_read_skipped: bool
    repeated_read_guard_hits: int
    semantic_exploration_skipped: bool
    semantic_exploration_guard_hits: int
    read_file_evidence: str
    request_summary: str


@dataclass(frozen=True)
class ToolLoopSteeringDecision:
    kind: str
    message: str
    payload: dict[str, object]
    force_final_answer_without_tools: bool
    temporary_tool_allowlist: set[str] | None = None


@dataclass(frozen=True)
class _SteererSpec:
    signal: str
    kind: str
    max_steers: int


@dataclass(frozen=True)
class _TerminationSpec:
    signal: str
    reason: str
    max_guard_hits: int


class ToolLoopSteeringRegistry:
    """Ordered runtime steerers for pathological tool-loop signals."""

    _SPECS = (
        _SteererSpec("repeated_read", "repeated_read_file_final_answer", 2),
        _SteererSpec("semantic_exploration", "semantic_exploration", 2),
        _SteererSpec("useless_search", "useless_search_pattern_final_answer", 2),
        _SteererSpec("useless_lsp", "useless_lsp_symbol_final_answer", 2),
        _SteererSpec("duplicate", "duplicate_tool_final_answer", 2),
    )
    _TERMINATION_SPECS = (
        _TerminationSpec("repeated_read", "repeated_read_file_guard", 4),
        _TerminationSpec("semantic_exploration", "semantic_exploration_guard", 4),
        _TerminationSpec("useless_search", "useless_search_pattern_guard", 4),
        _TerminationSpec("useless_lsp", "useless_lsp_symbol_guard", 4),
        _TerminationSpec("duplicate", "duplicate_tool_guard", 8),
    )

    def __init__(self) -> None:
        self._counts = {spec.kind: 0 for spec in self._SPECS}

    def reset(self) -> None:
        self._counts = {spec.kind: 0 for spec in self._SPECS}

    def count(self, kind: str) -> int:
        return self._counts.get(kind, 0)

    def decide(self, signals: ToolLoopSignals) -> ToolLoopSteeringDecision | None:
        for spec in self._SPECS:
            if not _signal_is_active(spec.signal, signals):
                continue
            if self.count(spec.kind) >= spec.max_steers:
                return None
            self._counts[spec.kind] = self.count(spec.kind) + 1
            return _decision_for(spec.kind, signals, self.count(spec.kind))
        return None

    def termination_reason(self, signals: ToolLoopSignals) -> str | None:
        """Return the first hard-stop reason in the same explicit priority order."""
        for spec in self._TERMINATION_SPECS:
            if _guard_hits(spec.signal, signals) >= spec.max_guard_hits:
                return spec.reason
        return None


def is_filename_search_misuse(name: str, arguments: str | dict[str, object]) -> bool:
    """Classify observable filename/path discovery mistakes for telemetry only.

    This intentionally does not block a call: a repository may legitimately contain a
    path-like literal. The structured `glob_files` boundary and ToolChoiceQueue decide
    what is allowed; telemetry lets pressure tests show whether the model still picks
    content search for filename, extension, or source-tree discovery.
    """

    if name != "search_code":
        return False
    parsed: object = arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return False
    if not isinstance(parsed, dict):
        return False
    pattern = parsed.get("pattern")
    if not isinstance(pattern, str):
        return False
    normalized = pattern.strip().replace("\\", "/").casefold()
    if normalized in _SOURCE_TREE_PATHS:
        return True
    return "/" in normalized or bool(_FILENAME_PATTERN_SUFFIX.search(normalized))


def _signal_is_active(signal: str, signals: ToolLoopSignals) -> bool:
    return {
        "duplicate": signals.duplicate_skipped,
        "useless_search": signals.useless_search_skipped,
        "useless_lsp": signals.useless_lsp_skipped,
        "repeated_read": signals.repeated_read_skipped,
        "semantic_exploration": signals.semantic_exploration_skipped,
    }[signal]


def _guard_hits(signal: str, signals: ToolLoopSignals) -> int:
    return {
        "duplicate": signals.duplicate_guard_hits,
        "useless_search": signals.useless_search_guard_hits,
        "useless_lsp": signals.useless_lsp_guard_hits,
        "repeated_read": signals.repeated_read_guard_hits,
        "semantic_exploration": signals.semantic_exploration_guard_hits,
    }[signal]


def _decision_for(kind: str, signals: ToolLoopSignals, steer_count: int) -> ToolLoopSteeringDecision:
    evidence = signals.read_file_evidence
    request_summary = signals.request_summary
    if kind == "duplicate_tool_final_answer":
        return ToolLoopSteeringDecision(
            kind=kind,
            message=(
                "Runtime steering: repeated identical tool calls are no longer useful. "
                "Your next response must be a final answer without tool calls. "
                "Use the evidence already collected, state uncertainty explicitly, and list exact next files or queries "
                "instead of repeating prior searches."
                f"{request_summary}{evidence}"
            ),
            payload={"tool": signals.duplicate_tool_name, "duplicate_hits": signals.duplicate_guard_hits},
            force_final_answer_without_tools=True,
        )
    if kind == "useless_search_pattern_final_answer":
        return ToolLoopSteeringDecision(
            kind=kind,
            message=(
                "Runtime steering: repeated search_code calls with the same no-match pattern are no longer useful. "
                "Your next response must be a final answer without tool calls. Return to the user's original requested "
                "output structure, use the evidence already collected, state uncertainty explicitly, and list exact next "
                "files or different business terms instead of continuing to search the same empty keyword across directories."
                f"{request_summary}{evidence}"
            ),
            payload={"guard_hits": signals.useless_search_guard_hits},
            force_final_answer_without_tools=True,
        )
    if kind == "useless_lsp_symbol_final_answer":
        return ToolLoopSteeringDecision(
            kind=kind,
            message=(
                "Runtime steering: repeated lsp symbol queries with no matches are no longer useful. "
                "Your next response must be a final answer without tool calls. Return to the user's original requested "
                "output structure, use the evidence already collected, state uncertainty explicitly, and list exact next "
                "files or truly different search terms instead of continuing to guess symbol names."
                f"{request_summary}{evidence}"
            ),
            payload={"guard_hits": signals.useless_lsp_guard_hits},
            force_final_answer_without_tools=True,
        )
    if kind == "repeated_read_file_final_answer":
        return ToolLoopSteeringDecision(
            kind=kind,
            message=(
                "Runtime steering: repeated read_file slices from the same file are no longer useful. "
                "Your next response must be a final answer without tool calls. Return to the user's original requested "
                "output structure, use the evidence already collected, state uncertainty explicitly, and list exact next "
                "files instead of continuing to read adjacent ranges."
                f"{request_summary}{evidence}"
            ),
            payload={"duplicate_hits": signals.repeated_read_guard_hits},
            force_final_answer_without_tools=True,
        )
    return ToolLoopSteeringDecision(
        kind=kind,
        message=(
            "Runtime steering: directory/path exploration is repeating under the same module or parent path. "
            "Do not keep calling list_files on sibling, parent, or child guesses. Use targeted evidence tools only: "
            "search_code with business terms, lsp_* navigation, or read_file on exact files already discovered. "
            "If enough evidence has been collected, answer the user's original question directly and label uncertainty explicitly."
            f"{request_summary}{evidence}"
        ),
        payload={"guard_hits": signals.semantic_exploration_guard_hits},
        force_final_answer_without_tools=False,
        temporary_tool_allowlist=set(READ_ONLY_EVIDENCE_TOOLS),
    )

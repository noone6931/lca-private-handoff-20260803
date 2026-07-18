from __future__ import annotations

from dataclasses import dataclass

from ..tools.base import ToolResult


MAX_IDENTICAL_TOOL_CALLS_IN_RECENT_WINDOW = 3
REPEAT_TOOL_CALL_WINDOW = 12
MAX_USELESS_SEARCHES_PER_PATTERN_IN_RECENT_WINDOW = 8
USELESS_SEARCH_PATTERN_WINDOW = 20
MAX_USELESS_LSP_SYMBOL_QUERIES_IN_RECENT_WINDOW = 12
USELESS_LSP_SYMBOL_QUERY_WINDOW = 24
MAX_READ_FILE_CALLS_PER_FILE_IN_RECENT_WINDOW = 8
READ_FILE_PATH_WINDOW = 14
MAX_SEMANTIC_EXPLORATIONS_PER_KEY_IN_RECENT_WINDOW = 4
SEMANTIC_EXPLORATION_WINDOW = 20
MAX_UNKNOWN_TOOL_CALLS_PER_NAME_IN_RECENT_WINDOW = 2
UNKNOWN_TOOL_WINDOW = 12


@dataclass(frozen=True)
class SessionGuardDecision:
    kind: str
    subject: str
    prior_count: int


class SessionGuardState:
    """Session-lived windows for repeated or unproductive tool exploration."""

    def __init__(self) -> None:
        self._recent_tool_call_signatures: list[str] = []
        self._recent_useless_search_pattern_keys: list[str] = []
        self._recent_useless_lsp_symbol_query_keys: list[str] = []
        self._recent_read_file_path_keys: list[str] = []
        self._recent_semantic_exploration_keys: list[str] = []
        self._recent_unknown_tool_name_keys: list[str] = []
        self._recent_complete_glob_signatures: list[str] = []
        self._guard_hits = {
            "duplicate_tool": 0,
            "useless_search_pattern": 0,
            "useless_lsp_symbol": 0,
            "repeated_read_file": 0,
            "semantic_exploration": 0,
            "unknown_tool": 0,
            "repeated_complete_glob": 0,
        }

    def before_tool(
        self,
        *,
        read_file_key: str | None,
        signature: str,
        search_pattern_key: str | None,
        lsp_symbol_query_key: str | None,
        semantic_exploration_key: str | None,
        unknown_tool_name: str | None = None,
        complete_glob_signature: str | None = None,
    ) -> SessionGuardDecision | None:
        if complete_glob_signature is not None and complete_glob_signature in self._recent_complete_glob_signatures:
            return self._hit("repeated_complete_glob", complete_glob_signature, 1)
        if read_file_key is not None:
            prior_count = self._recent_read_file_path_keys.count(read_file_key)
            self._remember("_recent_read_file_path_keys", read_file_key, READ_FILE_PATH_WINDOW)
            if prior_count >= MAX_READ_FILE_CALLS_PER_FILE_IN_RECENT_WINDOW:
                return self._hit("repeated_read_file", read_file_key, prior_count)

        prior_count = self._recent_tool_call_signatures.count(signature)
        self._remember("_recent_tool_call_signatures", signature, REPEAT_TOOL_CALL_WINDOW)
        if prior_count >= MAX_IDENTICAL_TOOL_CALLS_IN_RECENT_WINDOW:
            return self._hit("duplicate_tool", "", prior_count)

        if search_pattern_key is not None:
            prior_count = self._recent_useless_search_pattern_keys.count(search_pattern_key)
            if prior_count >= MAX_USELESS_SEARCHES_PER_PATTERN_IN_RECENT_WINDOW:
                return self._hit("useless_search_pattern", search_pattern_key, prior_count)

        if (
            lsp_symbol_query_key is not None
            and len(self._recent_useless_lsp_symbol_query_keys) >= MAX_USELESS_LSP_SYMBOL_QUERIES_IN_RECENT_WINDOW
        ):
            return self._hit("useless_lsp_symbol", lsp_symbol_query_key, len(self._recent_useless_lsp_symbol_query_keys))

        if semantic_exploration_key is not None:
            prior_count = self._recent_semantic_exploration_keys.count(semantic_exploration_key)
            self._remember("_recent_semantic_exploration_keys", semantic_exploration_key, SEMANTIC_EXPLORATION_WINDOW)
            if prior_count >= MAX_SEMANTIC_EXPLORATIONS_PER_KEY_IN_RECENT_WINDOW:
                return self._hit("semantic_exploration", semantic_exploration_key, prior_count)
        if unknown_tool_name is not None:
            prior_count = self._recent_unknown_tool_name_keys.count(unknown_tool_name)
            if prior_count >= MAX_UNKNOWN_TOOL_CALLS_PER_NAME_IN_RECENT_WINDOW:
                return self._hit("unknown_tool", unknown_tool_name, prior_count)
        return None

    def record_result(
        self,
        *,
        search_pattern_key: str | None,
        lsp_symbol_query_key: str | None,
        unknown_tool_name: str | None = None,
        complete_glob_signature: str | None = None,
        result: ToolResult,
    ) -> None:
        if search_pattern_key is not None and result.useless and not result.is_error:
            self._remember(
                "_recent_useless_search_pattern_keys",
                search_pattern_key,
                USELESS_SEARCH_PATTERN_WINDOW,
            )
        if lsp_symbol_query_key is None or result.is_error:
            pass
        elif result.useless:
            self._remember(
                "_recent_useless_lsp_symbol_query_keys",
                lsp_symbol_query_key,
                USELESS_LSP_SYMBOL_QUERY_WINDOW,
            )
        else:
            self._recent_useless_lsp_symbol_query_keys = []
        if unknown_tool_name is not None and result.metadata.get("unknown_tool"):
            self._remember("_recent_unknown_tool_name_keys", unknown_tool_name, UNKNOWN_TOOL_WINDOW)
        if (
            complete_glob_signature is not None
            and not result.is_error
            and bool(result.metadata.get("complete"))
            and not bool(result.metadata.get("truncated"))
        ):
            self._remember("_recent_complete_glob_signatures", complete_glob_signature, REPEAT_TOOL_CALL_WINDOW)

    def hit_count(self, kind: str) -> int:
        return self._guard_hits.get(kind, 0)

    def record_hit(self, kind: str) -> int:
        if kind not in self._guard_hits:
            raise ValueError(f"unknown session guard: {kind}")
        self._guard_hits[kind] += 1
        return self._guard_hits[kind]

    def counts(self) -> dict[str, int]:
        return dict(self._guard_hits)

    def _hit(self, kind: str, subject: str, prior_count: int) -> SessionGuardDecision:
        self._guard_hits[kind] += 1
        return SessionGuardDecision(kind=kind, subject=subject, prior_count=prior_count)

    def _remember(self, attribute: str, value: str, window: int) -> None:
        values = getattr(self, attribute)
        values.append(value)
        setattr(self, attribute, values[-window:])


__all__ = ["SessionGuardDecision", "SessionGuardState"]

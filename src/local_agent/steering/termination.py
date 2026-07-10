from __future__ import annotations


_SYNTHETIC_TOOL_MESSAGES = {
    "duplicate_tool_guard": "the assistant repeated identical tool calls too many times.",
    "repeated_read_file_guard": (
        "Tool call was not executed because repeated read_file slices from the same file were no longer useful. "
        "Use the already collected evidence and answer the user's original request."
    ),
    "useless_search_pattern_guard": (
        "Tool call was not executed because repeated search_code calls with the same no-match pattern "
        "were no longer useful. Use the already collected evidence and answer the user's original request."
    ),
    "useless_lsp_symbol_guard": (
        "Tool call was not executed because repeated lsp symbol queries with no matches "
        "were no longer useful. Use the already collected evidence and answer the user's original request."
    ),
    "semantic_exploration_guard": (
        "Tool call was not executed because repeated list_files exploration under the same module or parent path "
        "was no longer useful. Use targeted search_code/lsp/read_file evidence or answer from collected evidence."
    ),
}

_TERMINATION_MESSAGES = {
    "duplicate_tool_guard": (
        "Stopped because the assistant repeated identical tool calls too many times. "
        "Retry with a narrower request or ask it to answer from the evidence already collected."
    ),
    "repeated_read_file_guard": (
        "Stopped because the assistant kept reading adjacent ranges from the same file. "
        "Retry with a narrower request or ask it to answer from the evidence already collected."
    ),
    "useless_search_pattern_guard": (
        "Stopped because the assistant kept searching the same no-match pattern across paths. "
        "Retry with a narrower request or ask it to answer from the evidence already collected."
    ),
    "useless_lsp_symbol_guard": (
        "Stopped because the assistant kept guessing lsp symbol queries with no matches. "
        "Retry with a narrower request or ask it to answer from the evidence already collected."
    ),
    "semantic_exploration_guard": (
        "Stopped because the assistant kept exploring sibling, parent, or child directories in the same module. "
        "Retry with a narrower request or ask it to use search_code/LSP evidence instead of path guessing."
    ),
}


def synthetic_tool_stop_message(reason: str) -> str:
    return _SYNTHETIC_TOOL_MESSAGES[reason]


def termination_message(reason: str) -> str:
    return _TERMINATION_MESSAGES[reason]

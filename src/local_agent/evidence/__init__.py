"""Evidence ledger, observations, verification, and delivery boundaries."""

from importlib import import_module

__all__ = [
    "EvidenceLedger",
    "EvidenceRecord",
    "display_read_file_path",
    "evidence_root_for_path",
    "evidence_root_label",
    "first_result_line_paths",
    "first_search_result_paths",
    "parse_tool_arguments",
]


def __getattr__(name: str) -> object:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    ledger = import_module(".ledger", __name__)
    value = getattr(ledger, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})

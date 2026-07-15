from __future__ import annotations

import re


READ_ONLY_KEYWORDS = frozenset(
    {
        "do not edit",
        "do not modify",
        "don't edit",
        "don't modify",
        "no changes",
        "no edits",
        "read only",
        "read-only",
        "readonly",
        "不要改",
        "不要修改",
        "不做修改",
        "只分析",
        "只读",
        "禁止修改",
    }
)
IMPLEMENTATION_KEYWORDS = frozenset(
    {
        "bugfix",
        "change",
        "code edit",
        "edit",
        "fix",
        "implement",
        "implementation",
        "modify",
        "patch",
        "修复",
        "修改",
        "实现",
        "改造",
        "新增",
        "编码",
    }
)


def is_read_only_task(task_kind: str, prompt: str) -> bool:
    kind = _compact(task_kind)
    if kind in {"analysis", "question", "read_only", "readonly", "review"}:
        return True
    if kind in {"bugfix", "code_edit", "code_implementation", "edit", "feature", "fix", "implementation", "implement"}:
        return False
    text = (prompt or "").lower()
    return any(keyword in text for keyword in READ_ONLY_KEYWORDS)


def is_implementation_task(task_kind: str, prompt: str) -> bool:
    if is_read_only_task(task_kind, prompt):
        return False
    kind = _compact(task_kind)
    if kind in {"bugfix", "code_edit", "code_implementation", "edit", "feature", "fix", "implementation", "implement"}:
        return True
    if kind and kind not in {"coding", "general", "task", "unknown"}:
        return False
    text = (prompt or "").lower()
    return any(keyword in text for keyword in IMPLEMENTATION_KEYWORDS)


def _compact(value: str) -> str:
    return re.sub(r"[\s-]+", "_", (value or "").strip().lower())

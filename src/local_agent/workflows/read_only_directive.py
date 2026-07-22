"""Clause-scoped parsing for explicit read-only directives."""
from __future__ import annotations

import re


_DIRECTIVES = (
    "不要修改",
    "不得修改",
    "禁止修改",
    "不修改",
    "不改代码",
    "不写代码",
    "不用改",
    "无需修改",
    "不得写入",
    "禁止写入",
    "只分析",
    "只确认",
    "do not edit",
    "no changes",
)

_GLOBAL_TARGET_PREFIXES = (
    "anything",
    "any file",
    "any files",
    "all files",
    "code",
    "existing files",
    "files",
    "repository",
    "repo",
    "workspace",
    "任何",
    "全部",
    "所有",
    "代码",
    "源码",
    "现有文件",
    "文件",
    "仓库",
    "工作区",
)


def has_global_read_only_directive(prompt: str) -> bool:
    for directive in _DIRECTIVES:
        start = 0
        while True:
            index = prompt.find(directive, start)
            if index < 0:
                break
            target = _directive_target(prompt, index + len(directive))
            if not target or _is_global_target(target):
                return True
            start = index + len(directive)
    return False


def _directive_target(prompt: str, start: int) -> str:
    clause = re.split(r"[，。；;\n]", prompt[start : start + 96], maxsplit=1)[0]
    return clause.strip(" \t:：,.")


def _is_global_target(target: str) -> bool:
    target = re.sub(r"^(?:to\s+)?(?:the\s+)?", "", target, count=1)
    for prefix in _GLOBAL_TARGET_PREFIXES:
        if prefix.isascii():
            if re.match(rf"{re.escape(prefix)}(?:\s|$)", target):
                return True
        elif target.startswith(prefix):
            return True
    return False


__all__ = ["has_global_read_only_directive"]

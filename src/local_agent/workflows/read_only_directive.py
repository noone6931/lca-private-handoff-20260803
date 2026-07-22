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

_GLOBAL_TARGETS = frozenset({
    "anything",
    "any file",
    "any files",
    "any code",
    "all code",
    "all files",
    "codebase",
    "code",
    "entire codebase",
    "entire repository",
    "entire workspace",
    "existing files",
    "files",
    "repository",
    "repo",
    "source code",
    "workspace",
    "任何",
    "任何代码",
    "任何内容",
    "任何文件",
    "全部",
    "全部代码",
    "全部内容",
    "全部文件",
    "所有",
    "所有代码",
    "所有内容",
    "所有文件",
    "代码",
    "源码",
    "现有文件",
    "文件",
    "仓库",
    "工作区",
})


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
    clause = re.split(
        r"[，。；：！？,.;:!?\n]|\b(?:and|but|then)\b|(?:并且|然后|但是|以及|并|但)",
        prompt[start : start + 96],
        maxsplit=1,
    )[0]
    return clause.strip(" \t:：,.")


def _is_global_target(target: str) -> bool:
    target = re.sub(r"^(?:to\s+)?(?:the\s+)?", "", target, count=1)
    if target in _GLOBAL_TARGETS or target.split("、", 1)[0] in _GLOBAL_TARGETS:
        return True
    match = re.fullmatch(
        r"(?:any files?|all files|existing files|files|code|source code)\s+"
        r"(?:in|under|within)\s+(?:the\s+)?(?:workspace|repository|repo)",
        target,
    )
    return match is not None


__all__ = ["has_global_read_only_directive"]

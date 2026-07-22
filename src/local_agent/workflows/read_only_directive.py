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
    "repository directory",
    "repository path",
    "repo",
    "repo directory",
    "repo path",
    "source code",
    "workspace",
    "workspace directory",
    "workspace path",
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
    "仓库目录",
    "仓库路径",
    "工作区",
    "工作区目录",
    "工作区路径",
})

_PATH_SEPARATORS = ("/", "\\")
_CONCRETE_TARGET_LABELS = (" directory", " folder", " path", "目录", "文件夹", "路径")
_GLOBAL_SUBJECTS = "|".join(re.escape(value) for value in sorted(_GLOBAL_TARGETS, key=len, reverse=True))
_CLAUSE_CONNECTORS = r"and|but|then|并且|然后|但是|以及|并|但"
_IMPLEMENTATION_CONTINUATION = (
    rf"\s*(?:{_CLAUSE_CONNECTORS})\s*"
    r"(?:(?:要|需要|继续|仍需|must|should|need to|continue to)\s*)?"
    r"(?:实现|修复|修改|新增|增加|添加|接入|支持|调整|重构|删除|补充|编写|创建|更新|优化|迁移|"
    r"运行|执行|implement|build|fix|change|add|update|refactor|write|create|delete|support|run|execute)"
)
_GLOBAL_CONTAINER = (
    r"(?:(?:any files?|all files|existing files|files|code|source code)\s+"
    r"(?:in|under|within)\s+(?:the\s+)?(?:workspace|repository|repo)(?:\s+(?:directory|path))?"
    r"|(?:workspace|repository|repo)(?:\s+(?:directory|path))?"
    r"|(?:工作区|仓库)(?:目录|路径)?)"
)
_GLOBAL_ENUMERATION = (
    rf"(?:{_GLOBAL_SUBJECTS})\s*(?:{_CLAUSE_CONNECTORS})\s*"
    r"(?:paths?|directories|folders|路径|目录|文件夹)"
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
    clause = re.split(r"[，。、；：！？,.;:!?\n]", prompt[start : start + 96], maxsplit=1)[0]
    return clause.strip(" \t:：,.")


def _is_global_target(target: str) -> bool:
    target = re.sub(r"^(?:to\s+)?(?:the\s+)?", "", target, count=1)
    if target in _GLOBAL_TARGETS:
        return True
    if any(marker in target for marker in _PATH_SEPARATORS):
        return False
    if _matches_global_expression(_GLOBAL_CONTAINER, target):
        return True
    if _matches_global_expression(_GLOBAL_ENUMERATION, target):
        return True
    if any(marker in target for marker in _CONCRETE_TARGET_LABELS):
        return False
    return re.match(rf"(?:{_GLOBAL_SUBJECTS}){_IMPLEMENTATION_CONTINUATION}", target) is not None


def _matches_global_expression(expression: str, target: str) -> bool:
    return bool(
        re.fullmatch(expression, target)
        or re.match(rf"{expression}{_IMPLEMENTATION_CONTINUATION}", target)
    )


__all__ = ["has_global_read_only_directive"]

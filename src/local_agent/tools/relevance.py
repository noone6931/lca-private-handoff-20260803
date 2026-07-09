from __future__ import annotations

from pathlib import PurePosixPath

CODE_IMPLEMENTATION_KEYWORDS = {
    "bug",
    "change",
    "code",
    "compile",
    "dto",
    "fix",
    "implement",
    "java",
    "patch",
    "refactor",
    "test",
    "validate",
    "validator",
    "代码",
    "修复",
    "实现",
    "导入",
    "校验",
    "测试",
    "需求",
    "链路",
}

CONFIG_OR_DEPLOY_REQUEST_KEYWORDS = {
    "application.yml",
    "application.yaml",
    "application.properties",
    "config",
    "configuration",
    "deploy",
    "deployment",
    "nacos",
    "properties",
    "yaml",
    "yml",
    "上线配置",
    "部署",
    "配置",
}

LOW_RELEVANCE_PATH_PARTS = {
    "deploymessage",
    "nacos",
    "上线配置",
}

LOW_RELEVANCE_SUFFIXES = {
    ".conf",
    ".ini",
    ".properties",
    ".yaml",
    ".yml",
}

SOURCE_CODE_SUFFIXES = {
    ".java",
    ".js",
    ".jsx",
    ".py",
    ".ts",
    ".tsx",
    ".vue",
}


def is_code_implementation_request(text: str | None) -> bool:
    compact = (text or "").lower()
    return any(keyword.lower() in compact for keyword in CODE_IMPLEMENTATION_KEYWORDS)


def request_mentions_config_or_path(text: str | None, path: str) -> bool:
    compact = (text or "").lower()
    normalized_path = _normalize_path(path)
    basename = PurePosixPath(normalized_path).name.lower()
    if normalized_path and normalized_path.lower() in compact:
        return True
    if basename and basename in compact:
        return True
    return any(keyword.lower() in compact for keyword in CONFIG_OR_DEPLOY_REQUEST_KEYWORDS)


def is_low_relevance_patch_path(path: str) -> bool:
    normalized = _normalize_path(path)
    lowered = normalized.lower()
    parts = {part.lower() for part in PurePosixPath(normalized).parts}
    if parts.intersection(LOW_RELEVANCE_PATH_PARTS):
        return True
    return any(lowered.endswith(suffix) for suffix in LOW_RELEVANCE_SUFFIXES)


def path_matches_any(path: str, candidates: list[str] | tuple[str, ...]) -> bool:
    normalized = _normalize_path(path).lower()
    if not normalized:
        return False
    for candidate in candidates:
        other = _normalize_path(candidate).lower()
        if not other:
            continue
        if normalized == other or normalized.endswith("/" + other) or other.endswith("/" + normalized):
            return True
    return False


def is_source_code_path(path: str) -> bool:
    suffix = PurePosixPath(_normalize_path(path)).suffix.lower()
    return suffix in SOURCE_CODE_SUFFIXES


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").strip().strip("./")

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

ANALYSIS_ONLY_KEYWORDS = {
    "analysis only",
    "boundary",
    "boundary table",
    "candidate project",
    "candidate service",
    "do not scan source",
    "no source scan",
    "scope analysis",
    "范围分类",
    "范围分析",
    "服务边界",
    "候选服务",
    "候选项目",
    "哪些服务",
    "哪些项目",
    "项目范围",
    "禁止扫描源码",
    "不要扫描源码",
    "不扫源码",
    "纯业务范围",
    "纯分析",
}

ANALYSIS_SCOPE_KEYWORDS = {
    "boundary",
    "boundary table",
    "candidate project",
    "candidate service",
    "scope analysis",
    "范围分类",
    "范围分析",
    "服务边界",
    "候选服务",
    "候选项目",
    "哪些服务",
    "哪些项目",
    "项目范围",
    "项目清单",
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
    if is_analysis_only_request(text):
        return False
    compact = (text or "").lower()
    return any(keyword.lower() in compact for keyword in CODE_IMPLEMENTATION_KEYWORDS)


def is_analysis_only_request(text: str | None) -> bool:
    compact = (text or "").lower()
    if any(keyword.lower() in compact for keyword in ANALYSIS_ONLY_KEYWORDS):
        return True
    if "仅根据" in compact and any(keyword.lower() in compact for keyword in ANALYSIS_SCOPE_KEYWORDS):
        return True
    return False


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

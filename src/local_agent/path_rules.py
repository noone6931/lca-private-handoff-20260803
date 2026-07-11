from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
import re
from typing import Iterable


RULES_DIRECTORY = ".local-agent/rules"
MAX_RULE_FILES_PER_ROOT = 40
MAX_RULE_BODY_CHARS = 4000
MAX_RULE_DESCRIPTION_CHARS = 240
_PATH_TOKEN = re.compile(r"(?:~?/|[A-Za-z0-9_.-]+/)[^\s'\"<>|]*|[A-Za-z0-9_.-]+\.(?:py|java|js|jsx|ts|tsx|vue|json|yaml|yml|md|xml)")


@dataclass(frozen=True)
class PathRuleDiagnostic:
    source: Path
    message: str


@dataclass(frozen=True)
class PathRule:
    source: Path
    root: Path
    patterns: tuple[str, ...]
    priority: int
    description: str
    body: str

    @property
    def relative_source(self) -> str:
        return str(self.source.relative_to(self.root))


@dataclass(frozen=True)
class PathRuleIndex:
    roots: tuple[Path, ...]
    rules: tuple[PathRule, ...]
    diagnostics: tuple[PathRuleDiagnostic, ...]


def discover_path_scoped_rules(roots: Iterable[Path]) -> PathRuleIndex:
    normalized_roots = _normalized_roots(roots)
    rules: list[PathRule] = []
    diagnostics: list[PathRuleDiagnostic] = []
    for root in normalized_roots:
        rules_dir = root / RULES_DIRECTORY
        if not rules_dir.is_dir():
            continue
        candidates = sorted(
            (path for path in rules_dir.glob("*.md") if path.is_file()),
            key=lambda path: path.name.lower(),
        )
        if len(candidates) > MAX_RULE_FILES_PER_ROOT:
            diagnostics.append(
                PathRuleDiagnostic(
                    rules_dir,
                    f"Ignored {len(candidates) - MAX_RULE_FILES_PER_ROOT} rule files after limit {MAX_RULE_FILES_PER_ROOT}.",
                )
            )
            candidates = candidates[:MAX_RULE_FILES_PER_ROOT]
        for source in candidates:
            rule, rule_diagnostics = _load_rule(root, source)
            diagnostics.extend(rule_diagnostics)
            if rule is not None:
                rules.append(rule)
    return PathRuleIndex(
        roots=normalized_roots,
        rules=tuple(sorted(rules, key=lambda rule: (rule.priority, str(rule.source).lower()))),
        diagnostics=tuple(diagnostics),
    )


def render_path_rule_metadata(index: PathRuleIndex) -> str:
    if not index.rules and not index.diagnostics:
        return ""
    lines = [
        "[Path-scoped rules]",
        "These rules are advisory. Current user instructions and freshly read source evidence take precedence.",
        "Rule bodies load only when the current request or inspected file path matches their path patterns.",
    ]
    for rule in index.rules:
        description = f" - {rule.description}" if rule.description else ""
        patterns = ", ".join(rule.patterns)
        lines.append(
            f"- root={rule.root}; source={rule.relative_source}; priority={rule.priority}; paths={patterns}{description}"
        )
    if index.diagnostics:
        lines.append(f"- {len(index.diagnostics)} rule file(s) were skipped or degraded; rules remain available where valid.")
    return "\n".join(lines)


def candidate_paths_for_path_rules(
    request: str,
    result_paths: Iterable[str | Path] = (),
    *,
    primary_workspace: Path | None = None,
) -> tuple[str, ...]:
    candidates: list[str] = []
    for raw_value in [*result_paths, *_PATH_TOKEN.findall(request or "")]:
        value = str(raw_value or "").strip().strip("'\"")
        value = value.rstrip(".,:;)]}")
        path = Path(value).expanduser()
        if value and not path.is_absolute() and primary_workspace is not None:
            value = str(primary_workspace / path)
        if value and value not in candidates:
            candidates.append(value)
    return tuple(candidates)


def matching_path_rule_context(
    index: PathRuleIndex,
    candidate_paths: Iterable[str | Path],
) -> str:
    if not index.rules:
        return ""
    matched: list[PathRule] = []
    for rule in index.rules:
        if any(_rule_matches_candidate(rule, candidate) for candidate in candidate_paths):
            matched.append(rule)
    if not matched:
        return ""
    blocks = [
        "[Matched path-scoped rules]",
        "Apply these as advisory project guidance for the matching paths only. "
        "Current user instructions and direct source evidence take precedence.",
    ]
    for rule in matched:
        blocks.append(
            f"### {rule.relative_source} (root: {rule.root}; paths: {', '.join(rule.patterns)}; priority: {rule.priority})\n"
            f"{rule.body}"
        )
    return "\n\n".join(blocks)


def _load_rule(root: Path, source: Path) -> tuple[PathRule | None, list[PathRuleDiagnostic]]:
    try:
        raw = source.read_text(encoding="utf-8").replace("\x00", "")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [PathRuleDiagnostic(source, f"Could not read rule file: {type(exc).__name__}.")]
    metadata, body, error = _parse_frontmatter(raw)
    if error is not None:
        return None, [PathRuleDiagnostic(source, error)]
    patterns, error = _parse_patterns(metadata.get("paths", ""))
    if error is not None:
        return None, [PathRuleDiagnostic(source, error)]
    priority, error = _parse_priority(metadata.get("priority", "0"))
    if error is not None:
        return None, [PathRuleDiagnostic(source, error)]
    if not body.strip():
        return None, [PathRuleDiagnostic(source, "Rule body must not be empty.")]
    description = metadata.get("description", "").strip().replace("\n", " ")
    if len(description) > MAX_RULE_DESCRIPTION_CHARS:
        description = description[:MAX_RULE_DESCRIPTION_CHARS].rstrip() + "..."
    clipped_body = body.strip()
    if len(clipped_body) > MAX_RULE_BODY_CHARS:
        clipped_body = clipped_body[:MAX_RULE_BODY_CHARS].rstrip() + "\n...<rule body truncated>"
    return (
        PathRule(
            source=source,
            root=root,
            patterns=patterns,
            priority=priority,
            description=description,
            body=clipped_body,
        ),
        [],
    )


def _parse_frontmatter(raw: str) -> tuple[dict[str, str], str, str | None]:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, "", "Rule file must start with YAML-lite frontmatter delimited by ---."
    try:
        end_index = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        return {}, "", "Rule frontmatter is missing its closing --- delimiter."
    metadata: dict[str, str] = {}
    current_list_key: str | None = None
    for line in lines[1:end_index]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("-"):
            if current_list_key != "paths":
                return {}, "", f"Unexpected list item: {stripped}"
            metadata["paths"] = metadata.get("paths", "") + "\n" + stripped[1:].strip()
            continue
        if ":" not in stripped:
            return {}, "", f"Invalid frontmatter line: {stripped}"
        key, value = (part.strip() for part in stripped.split(":", 1))
        if key not in {"paths", "priority", "description"}:
            return {}, "", f"Unsupported frontmatter key: {key}"
        if key in metadata:
            return {}, "", f"Duplicate frontmatter key: {key}"
        metadata[key] = value
        current_list_key = key if not value else None
    return metadata, "\n".join(lines[end_index + 1:]), None


def _parse_patterns(raw_patterns: str) -> tuple[tuple[str, ...], str | None]:
    values = [value.strip() for value in raw_patterns.splitlines() if value.strip()]
    if not values:
        return (), "Rule frontmatter requires a non-empty paths list."
    patterns: list[str] = []
    for pattern in values:
        normalized = pattern.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized or normalized.startswith("/") or ".." in Path(normalized).parts:
            return (), f"Invalid root-relative rule path pattern: {pattern}"
        patterns.append(normalized)
    return tuple(patterns), None


def _parse_priority(raw_priority: str) -> tuple[int, str | None]:
    try:
        priority = int(raw_priority or "0")
    except ValueError:
        return 0, f"Rule priority must be an integer, got: {raw_priority}"
    if not -100 <= priority <= 100:
        return 0, "Rule priority must be between -100 and 100."
    return priority, None


def _normalized_roots(roots: Iterable[Path]) -> tuple[Path, ...]:
    seen: set[Path] = set()
    normalized: list[Path] = []
    for root in roots:
        resolved = Path(root).expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            normalized.append(resolved)
    return tuple(normalized)


def _rule_matches_candidate(rule: PathRule, candidate: str | Path) -> bool:
    relative = _candidate_relative_to_root(candidate, rule.root)
    if relative is None:
        return False
    return any(_glob_matches(relative, pattern) for pattern in rule.patterns)


def _candidate_relative_to_root(candidate: str | Path, root: Path) -> str | None:
    value = str(candidate).strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if path.is_absolute():
        try:
            return path.resolve().relative_to(root).as_posix()
        except ValueError:
            return None
    normalized = value.replace("\\", "/").lstrip("./")
    if not normalized or ".." in Path(normalized).parts:
        return None
    return normalized


def _glob_matches(relative_path: str, pattern: str) -> bool:
    if fnmatchcase(relative_path, pattern):
        return True
    if "/**/" in pattern and fnmatchcase(relative_path, pattern.replace("/**/", "/")):
        return True
    if pattern.endswith("/**") and relative_path.startswith(pattern[:-3]):
        return True
    return False


__all__ = [
    "PathRule",
    "PathRuleDiagnostic",
    "PathRuleIndex",
    "candidate_paths_for_path_rules",
    "discover_path_scoped_rules",
    "matching_path_rule_context",
    "render_path_rule_metadata",
]

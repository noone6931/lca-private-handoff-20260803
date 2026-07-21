from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..tools.observation import ToolResultSummary
from ..evidence.timeline import effective_workspace_write_paths


TestBreadth = Literal["module", "project", "blocked"]


@dataclass(frozen=True)
class TestPlan:
    command: str | None
    reason: str
    breadth: TestBreadth
    changed_paths: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.breadth == "blocked"

    def snapshot(self) -> dict[str, object]:
        return {
            "command": self.command,
            "reason": self.reason,
            "breadth": self.breadth,
            "changed_paths": list(self.changed_paths),
            "blocked": self.blocked,
        }


def plan_narrow_test(
    workspace: Path,
    results: list[ToolResultSummary],
    *,
    continuation_paths: tuple[str, ...] = (),
) -> TestPlan:
    """Select one conservative local test candidate without executing it.

    A project-level command is useful evidence, but is deliberately not described as
    the narrowest test unless Runtime can derive an exact module/test target.
    """

    changed_paths = _changed_paths(workspace, results) or continuation_paths
    if not changed_paths:
        return TestPlan(None, "no workspace write has been observed", "blocked")
    suffixes = {Path(path).suffix.lower() for path in changed_paths}
    if suffixes.intersection({".py"}) and (workspace / "tests").is_dir():
        prefix = "PYTHONPATH=src " if (workspace / "src").is_dir() else ""
        return _project_plan(
            f"{prefix}python3 -m unittest discover -s tests",
            "tests/ directory supports a local unittest project fallback; no exact test target was safely derived",
            changed_paths,
        )
    if suffixes.intersection({".java", ".kt"}):
        if (workspace / "pom.xml").is_file():
            return _project_plan("mvn test", "pom.xml is present; this is a Maven project fallback", changed_paths)
        if (workspace / "gradlew").is_file():
            return _project_plan("./gradlew test", "local Gradle wrapper is present; this is a project fallback", changed_paths)
        if any((workspace / name).is_file() for name in ("build.gradle", "build.gradle.kts")):
            return TestPlan(None, "Gradle build file exists but no local ./gradlew wrapper was found", "blocked", changed_paths)
    if suffixes.intersection({".js", ".jsx", ".ts", ".tsx", ".vue"}):
        package_json = workspace / "package.json"
        if package_json.is_file():
            if _package_test_script(package_json) is not None:
                return _project_plan("npm test", "package.json defines a usable test script; this is a project fallback", changed_paths)
            return TestPlan(None, "package.json has no usable test script", "blocked", changed_paths)
    if (workspace / "pom.xml").is_file():
        return _project_plan("mvn test", "pom.xml is the local project manifest; this is a project fallback", changed_paths)
    if (workspace / "gradlew").is_file():
        return _project_plan("./gradlew test", "local Gradle wrapper is present; this is a project fallback", changed_paths)
    if (workspace / "tests").is_dir():
        prefix = "PYTHONPATH=src " if (workspace / "src").is_dir() else ""
        return _project_plan(
            f"{prefix}python3 -m unittest discover -s tests",
            "tests/ directory supports a project fallback; no exact test target was safely derived",
            changed_paths,
        )
    return TestPlan(None, "no reliable local test manifest or test directory was found", "blocked", changed_paths)


def _project_plan(command: str, reason: str, changed_paths: tuple[str, ...]) -> TestPlan:
    return TestPlan(command, reason, "project", changed_paths)


def _changed_paths(workspace: Path, results: list[ToolResultSummary]) -> tuple[str, ...]:
    return tuple(_display_path(workspace, path) for path in effective_workspace_write_paths(results))


def _display_path(workspace: Path, raw_path: str) -> str:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        return raw_path.replace("\\", "/").lstrip("./")
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _package_test_script(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    scripts = payload.get("scripts") if isinstance(payload, dict) else None
    test = scripts.get("test") if isinstance(scripts, dict) else None
    if not isinstance(test, str) or not test.strip():
        return None
    normalized = " ".join(test.lower().split())
    if "no test specified" in normalized or normalized in {"echo no tests", "true"}:
        return None
    return test


__all__ = ["TestBreadth", "TestPlan", "plan_narrow_test"]

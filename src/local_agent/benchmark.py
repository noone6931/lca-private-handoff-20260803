from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Iterable, Mapping

from .agent import AgentRuntime
from .config import AgentConfig
from .patch.anchored import hash_text


DEFAULT_TASKS_DIR = Path(__file__).resolve().parents[2] / "benchmarks" / "tasks"
_TEMPLATE_TOKEN = re.compile(r"\{\{([^{}]+)\}\}")
_IGNORED_SNAPSHOT_PARTS = {".git", ".local-agent", "__pycache__"}


@dataclass(frozen=True)
class BenchmarkTask:
    identifier: str
    title: str
    prompt: str
    followup_prompt: str | None
    workspace_files: Mapping[str, str]
    additional_roots: Mapping[str, Mapping[str, str]]
    scripted_responses: tuple[Mapping[str, Any], ...]
    approval_mode: str
    tool_approval: Mapping[str, str]
    budget_seconds: int | None
    initialize_git: bool
    acceptance: Mapping[str, Any]
    residual_risk: str


@dataclass(frozen=True)
class BenchmarkResult:
    identifier: str
    title: str
    passed: bool
    mode: str
    answer: str
    elapsed_ms: int
    run_summary: Mapping[str, Any]
    acceptance: tuple[Mapping[str, Any], ...]
    changed_files: tuple[str, ...]
    test_evidence: tuple[str, ...]
    residual_risk: str
    session_id: str | None = None
    run_id: str | None = None
    tool_error_summaries: tuple[Mapping[str, Any], ...] = ()
    retained_session_path: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "title": self.title,
            "passed": self.passed,
            "mode": self.mode,
            "answer": self.answer,
            "elapsed_ms": self.elapsed_ms,
            "run_summary": dict(self.run_summary),
            "acceptance": [dict(item) for item in self.acceptance],
            "changed_files": list(self.changed_files),
            "test_evidence": list(self.test_evidence),
            "residual_risk": self.residual_risk,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "tool_error_summaries": [dict(item) for item in self.tool_error_summaries],
            "retained_session_path": self.retained_session_path,
            "error": self.error,
        }


class ScriptedBenchmarkClient:
    """Deterministic local provider used by the default offline benchmark path."""

    def __init__(
        self,
        responses: Iterable[Mapping[str, Any]],
        *,
        workspace: Path,
        named_roots: Mapping[str, Path],
    ) -> None:
        self._responses = [deepcopy(dict(response)) for response in responses]
        self._workspace = workspace
        self._named_roots = dict(named_roots)
        self._last_terminal_action: Mapping[str, Any] | None = None
        self.calls: list[dict[str, Any]] = []
        self.tool_schema_names: list[tuple[str, ...]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        timeout: float | None = None,
    ) -> Any:
        schema_names = tuple(
            str(schema.get("function", {}).get("name") or "")
            for schema in tools
            if isinstance(schema, Mapping)
        )
        self.tool_schema_names.append(schema_names)
        self.calls.append(
            {
                "message_count": len(messages),
                "tool_schema_names": list(schema_names),
                "timeout": timeout,
            }
        )
        if self._responses:
            action = _render_template(self._responses.pop(0), self._workspace, self._named_roots)
        elif self._last_terminal_action is not None:
            action = dict(self._last_terminal_action)
        else:
            return _BenchmarkResponse({"content": "Benchmark provider script exhausted."})
        tool_calls = action.get("tool_calls")
        if isinstance(tool_calls, list):
            rendered_calls: list[dict[str, Any]] = []
            for index, call in enumerate(tool_calls, start=1):
                if not isinstance(call, Mapping):
                    continue
                name = str(call.get("name") or "")
                arguments = call.get("arguments")
                if not isinstance(arguments, Mapping):
                    arguments = {}
                rendered_calls.append(
                    {
                        "id": f"benchmark-{len(self.calls)}-{index}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments, ensure_ascii=False),
                        },
                    }
                )
            return _BenchmarkResponse(
                {
                    "content": action.get("content"),
                    "tool_calls": rendered_calls,
                },
                finish_reason=str(action["finish_reason"]) if action.get("finish_reason") else None,
            )
        self._last_terminal_action = dict(action)
        return _BenchmarkResponse(
            {"content": str(action.get("content") or "")},
            finish_reason=str(action["finish_reason"]) if action.get("finish_reason") else None,
        )


@dataclass(frozen=True)
class _BenchmarkResponse:
    message: Mapping[str, Any]
    finish_reason: str | None = None


class SchemaRecordingClient:
    """Observe live-provider schema exposure without changing its request flow."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.calls: list[dict[str, Any]] = []
        self.tool_schema_names: list[tuple[str, ...]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        timeout: float | None = None,
    ) -> Any:
        schema_names = tuple(
            str(schema.get("function", {}).get("name") or "")
            for schema in tools
            if isinstance(schema, Mapping)
        )
        self.tool_schema_names.append(schema_names)
        self.calls.append(
            {
                "message_count": len(messages),
                "tool_schema_names": list(schema_names),
                "timeout": timeout,
            }
        )
        return self._delegate.chat(messages, tools, timeout=timeout)


def load_benchmark_tasks(
    tasks_dir: Path = DEFAULT_TASKS_DIR,
    *,
    selected_ids: Iterable[str] = (),
) -> tuple[BenchmarkTask, ...]:
    selected = {value.strip() for value in selected_ids if value.strip()}
    tasks: list[BenchmarkTask] = []
    for path in sorted(tasks_dir.glob("*.json"), key=lambda item: item.name):
        task = _load_benchmark_task(path)
        if selected and task.identifier not in selected:
            continue
        tasks.append(task)
    missing = selected - {task.identifier for task in tasks}
    if missing:
        raise ValueError("Benchmark task id not found: " + ", ".join(sorted(missing)))
    if not tasks:
        raise ValueError(f"No benchmark tasks found under {tasks_dir}.")
    return tuple(tasks)


def run_benchmark_suite(
    *,
    tasks_dir: Path = DEFAULT_TASKS_DIR,
    selected_ids: Iterable[str] = (),
    output_dir: Path | None = None,
    live_config: AgentConfig | None = None,
    preserve_failed_sessions: bool = False,
) -> tuple[BenchmarkResult, ...]:
    tasks = load_benchmark_tasks(tasks_dir, selected_ids=selected_ids)
    failed_session_dir = output_dir / "failed-sessions" if output_dir is not None and preserve_failed_sessions else None
    results = tuple(
        run_benchmark_task(task, live_config=live_config, failed_session_dir=failed_session_dir)
        for task in tasks
    )
    if output_dir is not None:
        write_benchmark_reports(results, output_dir)
    return results


def run_benchmark_task(
    task: BenchmarkTask,
    *,
    live_config: AgentConfig | None = None,
    failed_session_dir: Path | None = None,
) -> BenchmarkResult:
    mode = "live" if live_config is not None else "deterministic"
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix=f"lca-benchmark-{task.identifier}-") as temp:
            run_root = Path(temp).resolve()
            workspace = run_root / "workspace"
            _write_fixture_files(workspace, task.workspace_files)
            named_roots: dict[str, Path] = {}
            for name, files in task.additional_roots.items():
                root = run_root / "additional" / name
                _write_fixture_files(root, files)
                named_roots[name] = root
            if task.initialize_git:
                _initialize_git_repository(workspace)
            before = _snapshot_workspace_files(workspace)
            config = _benchmark_config(task, workspace, tuple(named_roots.values()), run_root, live_config)
            runtime = AgentRuntime(config, show_tool_logs=False)
            client: ScriptedBenchmarkClient | SchemaRecordingClient
            if live_config is None:
                client = ScriptedBenchmarkClient(
                    task.scripted_responses,
                    workspace=workspace,
                    named_roots=named_roots,
                )
                runtime._client = client
            else:
                client = SchemaRecordingClient(runtime._client)
                runtime._client = client
            answer = runtime.run(_render_prompt(task.prompt, workspace, named_roots))
            if task.followup_prompt:
                answer = runtime.run(_render_prompt(task.followup_prompt, workspace, named_roots))
            after = _snapshot_workspace_files(workspace)
            changed_files = _changed_files(before, after)
            run_summary = dict(runtime._last_run_summary or {})
            test_evidence = _test_evidence(runtime)
            acceptance = _evaluate_acceptance(
                task,
                mode=mode,
                answer=answer,
                runtime=runtime,
                client=client,
                workspace=workspace,
                named_roots=named_roots,
                changed_files=changed_files,
                test_evidence=test_evidence,
            )
            passed = all(bool(check["passed"]) for check in acceptance)
            elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
            retained_session_path = None
            if not passed and failed_session_dir is not None:
                retained_session_path = _preserve_failed_session(runtime, failed_session_dir, task.identifier)
            return BenchmarkResult(
                identifier=task.identifier,
                title=task.title,
                passed=passed,
                mode=mode,
                answer=answer,
                elapsed_ms=elapsed_ms,
                run_summary=run_summary,
                acceptance=tuple(acceptance),
                changed_files=tuple(changed_files),
                test_evidence=tuple(test_evidence),
                residual_risk=task.residual_risk,
                session_id=runtime._session.session_id,
                run_id=str(run_summary.get("run_id") or "") or None,
                tool_error_summaries=tuple(_tool_error_summaries(runtime, workspace, named_roots)),
                retained_session_path=retained_session_path,
            )
    except Exception as exc:  # noqa: BLE001 - benchmark reports failures rather than aborting the suite.
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        return BenchmarkResult(
            identifier=task.identifier,
            title=task.title,
            passed=False,
            mode=mode,
            answer="",
            elapsed_ms=elapsed_ms,
            run_summary={},
            acceptance=(),
            changed_files=(),
            test_evidence=(),
            residual_risk=task.residual_risk,
            error=f"{type(exc).__name__}: {exc}",
        )


def write_benchmark_reports(results: Iterable[BenchmarkResult], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    result_list = list(results)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "passed": sum(1 for result in result_list if result.passed),
        "failed": sum(1 for result in result_list if not result.passed),
        "results": [result.to_dict() for result in result_list],
    }
    json_path = output_dir / "benchmark-report.json"
    markdown_path = output_dir / "benchmark-report.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(_render_markdown_report(payload), encoding="utf-8")
    return json_path, markdown_path


def _load_benchmark_task(path: Path) -> BenchmarkTask:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not load benchmark task {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ValueError(f"Benchmark task {path} must be a JSON object.")
    identifier = _required_text(raw, "id", path)
    title = _required_text(raw, "title", path)
    prompt = _required_text(raw, "prompt", path)
    followup_prompt = raw.get("followup_prompt")
    if followup_prompt is not None and (not isinstance(followup_prompt, str) or not followup_prompt.strip()):
        raise ValueError(f"Benchmark task {path} followup_prompt must be a non-empty string when provided.")
    workspace_files = _string_mapping(raw.get("workspace_files"), "workspace_files", path)
    additional_roots = _nested_string_mapping(raw.get("additional_roots"), "additional_roots", path)
    responses = raw.get("scripted_responses") or []
    if not isinstance(responses, list) or not all(isinstance(item, Mapping) for item in responses):
        raise ValueError(f"Benchmark task {path} scripted_responses must be a list of objects.")
    tool_approval = _string_mapping(raw.get("tool_approval") or {}, "tool_approval", path)
    acceptance = raw.get("acceptance") or {}
    if not isinstance(acceptance, Mapping):
        raise ValueError(f"Benchmark task {path} acceptance must be an object.")
    budget_seconds = raw.get("budget_seconds", 120)
    if budget_seconds is not None and (not isinstance(budget_seconds, int) or budget_seconds < 0):
        raise ValueError(f"Benchmark task {path} budget_seconds must be a non-negative integer or null.")
    return BenchmarkTask(
        identifier=identifier,
        title=title,
        prompt=prompt,
        followup_prompt=followup_prompt.strip() if isinstance(followup_prompt, str) else None,
        workspace_files=workspace_files,
        additional_roots=additional_roots,
        scripted_responses=tuple(responses),
        approval_mode=str(raw.get("approval_mode") or "yolo"),
        tool_approval=tool_approval,
        budget_seconds=budget_seconds,
        initialize_git=bool(raw.get("initialize_git", False)),
        acceptance=acceptance,
        residual_risk=str(raw.get("residual_risk") or "Deterministic fixtures do not prove provider behavior on a real project."),
    )


def _benchmark_config(
    task: BenchmarkTask,
    workspace: Path,
    additional_roots: tuple[Path, ...],
    run_root: Path,
    live_config: AgentConfig | None,
) -> AgentConfig:
    state_root = run_root / "state"
    if live_config is not None:
        return replace(
            live_config,
            workspace=workspace,
            state_dir=state_root / "workspace",
            state_root=state_root,
            allowed_dirs=additional_roots,
            budget_seconds=task.budget_seconds,
            approval_mode=task.approval_mode,
            tool_approval=dict(task.tool_approval),
            memory_consolidation="off",
        )
    return AgentConfig(
        provider="benchmark-fake",
        api_base_url="https://benchmark.invalid/v1",
        api_key="benchmark-token",
        model="benchmark-script",
        workspace=workspace,
        state_dir=state_root / "workspace",
        state_root=state_root,
        allowed_dirs=additional_roots,
        max_steps=0,
        budget_seconds=task.budget_seconds,
        request_timeout=30,
        approval_mode=task.approval_mode,
        tool_approval=dict(task.tool_approval),
        context_char_budget=60000,
        context_token_budget=0,
        memory_consolidation="off",
    )


def _evaluate_acceptance(
    task: BenchmarkTask,
    *,
    mode: str,
    answer: str,
    runtime: AgentRuntime,
    client: ScriptedBenchmarkClient | SchemaRecordingClient,
    workspace: Path,
    named_roots: Mapping[str, Path],
    changed_files: list[str],
    test_evidence: list[str],
) -> list[dict[str, Any]]:
    acceptance = _acceptance_for_mode(task.acceptance, mode)
    checks: list[dict[str, Any]] = []
    required_tools = _string_list(acceptance.get("required_tools"))
    used_tools = [result.name for result in runtime._run.tool_choice_results]
    checks.append(_check("required_tools", all(name in used_tools for name in required_tools), required_tools, used_tools))
    forbidden_tools = _string_list(acceptance.get("forbidden_tools"))
    checks.append(_check("forbidden_tools", not any(name in used_tools for name in forbidden_tools), forbidden_tools, used_tools))
    expected_changed_files = sorted(_string_list(acceptance.get("changed_files")))
    if expected_changed_files:
        checks.append(_check("changed_files", sorted(changed_files) == expected_changed_files, expected_changed_files, changed_files))
    for path, expected_text in _string_mapping(acceptance.get("file_contains") or {}, "acceptance.file_contains", Path(task.identifier)).items():
        target = workspace / path
        actual = target.read_text(encoding="utf-8") if target.is_file() else ""
        checks.append(_check(f"file_contains:{path}", expected_text in actual, expected_text, actual[:1000]))
    for expected in _string_list(acceptance.get("answer_contains")):
        checks.append(_check(f"answer_contains:{expected}", expected in answer, expected, answer))
    normalized_answer = _normalize_answer(answer)
    for expected in _string_list(acceptance.get("answer_all_of")):
        checks.append(
            _check(
                f"answer_all_of:{expected}",
                _normalize_answer(expected) in normalized_answer,
                expected,
                answer,
            )
        )
    any_of = _string_list(acceptance.get("answer_any_of"))
    if any_of:
        checks.append(
            _check(
                "answer_any_of",
                any(_normalize_answer(expected) in normalized_answer for expected in any_of),
                any_of,
                answer,
            )
        )
    for pattern in _string_list(acceptance.get("answer_regex")):
        checks.append(
            _check(
                f"answer_regex:{pattern}",
                _matches_answer_regex(pattern, answer),
                pattern,
                answer,
            )
        )
    for pattern in _string_list(acceptance.get("answer_forbidden_regex")):
        checks.append(
            _check(
                f"answer_forbidden_regex:{pattern}",
                not _matches_answer_regex(pattern, answer),
                pattern,
                answer,
            )
        )
    coverage_roots = _string_list(acceptance.get("glob_coverage_roots"))
    if coverage_roots:
        actual_roots = _glob_coverage_roots(runtime)
        expected_roots = {
            str(workspace) if name == "primary" else str(named_roots[name])
            for name in coverage_roots
            if name == "primary" or name in named_roots
        }
        checks.append(
            _check(
                "glob_coverage_roots",
                expected_roots.issubset(actual_roots) and len(expected_roots) == len(coverage_roots),
                sorted(expected_roots),
                sorted(actual_roots),
            )
        )
    expected_reason = acceptance.get("termination_reason")
    if isinstance(expected_reason, str):
        actual_reason = str((runtime._last_run_summary or {}).get("termination_reason") or "")
        checks.append(_check("termination_reason", actual_reason == expected_reason, expected_reason, actual_reason))
    max_errors = acceptance.get("max_tool_errors")
    if isinstance(max_errors, int):
        actual_errors = int((runtime._last_run_summary or {}).get("tool_errors") or 0)
        checks.append(_check("max_tool_errors", actual_errors <= max_errors, max_errors, actual_errors))
    if acceptance.get("requires_test_evidence"):
        checks.append(_check("test_evidence", bool(test_evidence), "successful run_tests result", test_evidence))
    expected_delivery_checks = acceptance.get("delivery_checks")
    if isinstance(expected_delivery_checks, Mapping):
        actual_delivery_checks = (runtime._last_run_summary or {}).get("verification_plan") or {}
        expected_counts = _integer_mapping(expected_delivery_checks)
        checks.append(
            _check(
                "delivery_checks",
                _mapping_integer_values_match(actual_delivery_checks, expected_counts),
                expected_counts,
                actual_delivery_checks,
            )
        )
    expected_run_summary = acceptance.get("run_summary")
    if isinstance(expected_run_summary, Mapping):
        actual_run_summary = runtime._last_run_summary or {}
        expected_counts = _integer_mapping(expected_run_summary)
        checks.append(
            _check(
                "run_summary",
                _mapping_integer_values_match(actual_run_summary, expected_counts),
                expected_counts,
                actual_run_summary,
            )
        )
    expected_session_evidence = acceptance.get("session_evidence")
    if isinstance(expected_session_evidence, Mapping):
        actual_session_evidence = (runtime._last_run_summary or {}).get("session_evidence") or {}
        expected_counts = _integer_mapping(expected_session_evidence)
        checks.append(
            _check(
                "session_evidence",
                _mapping_integer_values_match(actual_session_evidence, expected_counts),
                expected_counts,
                actual_session_evidence,
            )
        )
    schema_excludes = _string_list(acceptance.get("schema_excludes"))
    if schema_excludes:
        schemas = client.tool_schema_names
        passed = bool(schemas) and all(name not in schema for schema in schemas for name in schema_excludes)
        checks.append(_check("schema_excludes", passed, schema_excludes, schemas))
    command = acceptance.get("command")
    if isinstance(command, str) and command.strip():
        completed = subprocess.run(
            command,
            cwd=workspace,
            shell=True,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        output = (completed.stdout + completed.stderr).strip()[:4000]
        checks.append(_check("acceptance_command", completed.returncode == 0, command, output))
    return checks


def _integer_mapping(values: Mapping[str, Any]) -> dict[str, int]:
    return {str(name): int(value) for name, value in values.items() if isinstance(value, int)}


def _mapping_integer_values_match(actual: Mapping[str, Any], expected: Mapping[str, int]) -> bool:
    """Require an explicitly present integer for every benchmarked metric."""
    for name, value in expected.items():
        actual_value = actual.get(name)
        if isinstance(actual_value, bool) or not isinstance(actual_value, int) or actual_value != value:
            return False
    return True


def _test_evidence(runtime: AgentRuntime) -> list[str]:
    evidence: list[str] = []
    for result in runtime._run.tool_choice_results:
        if result.name != "run_tests" or result.is_error:
            continue
        evidence.append(result.content[:1200])
    return evidence


def _acceptance_for_mode(raw_acceptance: Mapping[str, Any], mode: str) -> dict[str, Any]:
    """Keep deterministic wording strict while letting live providers vary phrasing."""

    acceptance = {key: value for key, value in raw_acceptance.items() if key not in {"deterministic", "live"}}
    mode_overrides = raw_acceptance.get(mode)
    if isinstance(mode_overrides, Mapping):
        acceptance.update(mode_overrides)
    return acceptance


def _normalize_answer(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _matches_answer_regex(pattern: str, answer: str) -> bool:
    try:
        return re.search(pattern, answer, flags=re.IGNORECASE | re.DOTALL) is not None
    except re.error:
        return False


def _glob_coverage_roots(runtime: AgentRuntime) -> set[str]:
    roots: set[str] = set()
    for result in runtime._run.tool_choice_results:
        if result.name != "glob_files" or result.is_error:
            continue
        searched_roots = result.metadata.get("searched_roots")
        if not isinstance(searched_roots, (list, tuple)):
            continue
        for root in searched_roots:
            if isinstance(root, str) and root:
                roots.add(str(Path(root).expanduser().resolve()))
    return roots


def _tool_error_summaries(
    runtime: AgentRuntime,
    workspace: Path,
    named_roots: Mapping[str, Path],
    *,
    max_items: int = 8,
    max_chars: int = 500,
) -> list[dict[str, str]]:
    summaries: list[dict[str, str]] = []
    redactions = [(str(workspace), "<workspace>")] + [
        (str(root), f"<additional:{name}>") for name, root in named_roots.items()
    ]
    for result in runtime._run.tool_choice_results:
        if not result.is_error:
            continue
        content = result.content
        for source, replacement in redactions:
            content = content.replace(source, replacement)
        content = re.sub(r"(?i)\b(?:sk|ak)-[a-z0-9_-]{12,}\b", "<redacted-token>", content)
        summaries.append(
            {
                "tool": result.name,
                "summary": re.sub(r"\s+", " ", content).strip()[:max_chars],
            }
        )
        if len(summaries) >= max_items:
            break
    return summaries


def _preserve_failed_session(runtime: AgentRuntime, output_dir: Path, task_id: str) -> str | None:
    source = runtime._session.path
    if not source.is_file():
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{task_id}-{runtime._session.session_id}.jsonl"
    shutil.copy2(source, target)
    return str(target)


def _write_fixture_files(root: Path, files: Mapping[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        path = root / relative
        resolved = path.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError(f"Fixture path escapes its root: {relative}") from exc
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")


def _initialize_git_repository(workspace: Path) -> None:
    commands = (
        ["git", "init", "-q"],
        ["git", "add", "."],
        ["git", "-c", "user.name=LCA Benchmark", "-c", "user.email=benchmark@local.invalid", "commit", "-qm", "baseline"],
    )
    for command in commands:
        completed = subprocess.run(command, cwd=workspace, text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            output = (completed.stdout + completed.stderr).strip()
            raise RuntimeError(f"Could not initialize benchmark Git fixture: {output}")


def _snapshot_workspace_files(workspace: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or any(part in _IGNORED_SNAPSHOT_PARTS for part in path.relative_to(workspace).parts):
            continue
        relative = path.relative_to(workspace).as_posix()
        snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _changed_files(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def _render_template(value: Any, workspace: Path, named_roots: Mapping[str, Path]) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _render_template(item, workspace, named_roots) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_template(item, workspace, named_roots) for item in value]
    if not isinstance(value, str):
        return value

    def replace_token(match: re.Match[str]) -> str:
        token = match.group(1).strip()
        if token.startswith("root:"):
            name = token.removeprefix("root:").strip()
            if name not in named_roots:
                raise ValueError(f"Benchmark template names unknown root: {name}")
            return str(named_roots[name])
        if token.startswith("hash:"):
            relative = token.removeprefix("hash:").strip()
            path = workspace / relative
            if not path.is_file():
                raise ValueError(f"Benchmark template hash target missing: {relative}")
            return hash_text(path.read_bytes().decode("utf-8"))
        if token == "workspace":
            return str(workspace)
        raise ValueError(f"Unknown benchmark template token: {token}")

    return _TEMPLATE_TOKEN.sub(replace_token, value)


def _render_prompt(prompt: str, workspace: Path, named_roots: Mapping[str, Path]) -> str:
    return str(_render_template(prompt, workspace, named_roots))


def _required_text(raw: Mapping[str, Any], key: str, path: Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Benchmark task {path} requires non-empty {key}.")
    return value.strip()


def _string_mapping(raw: Any, label: str, path: Path) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"Benchmark task {path} {label} must be an object.")
    values: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(f"Benchmark task {path} {label} keys and values must be strings.")
        values[key] = value
    return values


def _nested_string_mapping(raw: Any, label: str, path: Path) -> dict[str, dict[str, str]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"Benchmark task {path} {label} must be an object.")
    values: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValueError(f"Benchmark task {path} {label} names must be strings.")
        values[key] = _string_mapping(value, f"{label}.{key}", path)
    return values


def _string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [value for value in raw if isinstance(value, str)]


def _check(name: str, passed: bool, expected: Any, actual: Any) -> dict[str, Any]:
    return {
        "name": name,
        "passed": passed,
        "expected": expected,
        "actual": actual,
    }


def _render_markdown_report(payload: Mapping[str, Any]) -> str:
    lines = [
        "# LCA Benchmark Report",
        "",
        f"Generated: {payload.get('generated_at', '')}",
        f"Passed: {payload.get('passed', 0)}",
        f"Failed: {payload.get('failed', 0)}",
        "",
        "| Task | Mode | Result | Time | LLM | Tools | Errors | Termination |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for raw_result in payload.get("results", []):
        if not isinstance(raw_result, Mapping):
            continue
        summary = raw_result.get("run_summary") if isinstance(raw_result.get("run_summary"), Mapping) else {}
        status = "PASS" if raw_result.get("passed") else "FAIL"
        lines.append(
            "| {id} | {mode} | {status} | {elapsed}ms | {llm} | {tools} | {errors} | {reason} |".format(
                id=raw_result.get("id", ""),
                mode=raw_result.get("mode", ""),
                status=status,
                elapsed=raw_result.get("elapsed_ms", 0),
                llm=summary.get("llm_requests", 0),
                tools=summary.get("tool_calls", 0),
                errors=summary.get("tool_errors", 0),
                reason=summary.get("termination_reason", raw_result.get("error", "")),
            )
        )
        changed_files = raw_result.get("changed_files") or []
        if changed_files:
            lines.append("  Changed: " + ", ".join(str(path) for path in changed_files))
        evidence = raw_result.get("test_evidence") or []
        if evidence:
            lines.append("  Test evidence: " + str(evidence[0]).replace("\n", " ")[:300])
        session_id = raw_result.get("session_id")
        run_id = raw_result.get("run_id")
        if session_id or run_id:
            lines.append(f"  Session/run: {session_id or 'n/a'} / {run_id or 'n/a'}")
        error_summaries = raw_result.get("tool_error_summaries") or []
        for item in error_summaries:
            if isinstance(item, Mapping):
                lines.append(f"  Tool error [{item.get('tool', 'unknown')}]: {item.get('summary', '')}")
        retained_session = raw_result.get("retained_session_path")
        if retained_session:
            lines.append("  Retained failed session: " + str(retained_session))
        compactions = int(summary.get("compactions") or 0)
        if compactions:
            lines.append(
                "  Compaction effectiveness: "
                f"effective={summary.get('effective_compactions', 0)}, "
                f"zero_gain={summary.get('zero_gain_compactions', 0)}, "
                f"max_consecutive_zero_gain={summary.get('max_consecutive_zero_gain_compactions', 0)}, "
                f"estimated_token_reduction={summary.get('compaction_estimated_token_reduction', 0)}"
            )
        if summary.get("provider_schema_violations") or summary.get("finalization_attempts"):
            lines.append(
                "  Runtime reliability: "
                f"provider_schema_violations={summary.get('provider_schema_violations', 0)}, "
                f"finalization_attempts={summary.get('finalization_attempts', 0)}"
            )
        if raw_result.get("error"):
            lines.append("  Error: " + str(raw_result["error"]))
        lines.append("  Residual risk: " + str(raw_result.get("residual_risk") or "none"))
    return "\n".join(lines) + "\n"


__all__ = [
    "BenchmarkResult",
    "BenchmarkTask",
    "DEFAULT_TASKS_DIR",
    "ScriptedBenchmarkClient",
    "SchemaRecordingClient",
    "load_benchmark_tasks",
    "run_benchmark_suite",
    "run_benchmark_task",
    "write_benchmark_reports",
]

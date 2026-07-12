from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_agent.agent import AgentRuntime
from local_agent.config import AgentConfig
from local_agent.evidence import EvidenceRecord
from local_agent.session_evidence import SessionEvidenceCache
from local_agent.steering.final_answer import SourceEvidence
from local_agent.tool_choice_queue import ToolResultSummary
from local_agent.tools.base import ToolResult
from local_agent.user_facts import UserFactsLayer


class SessionEvidenceCacheTests(unittest.TestCase):
    def test_search_and_lsp_without_source_evidence_are_reused_when_matched_file_is_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "service-b" / "App.java"
            source.parent.mkdir()
            source.write_text("class App {}\n", encoding="utf-8")
            cache = SessionEvidenceCache()
            for tool in ("search_code", "lsp_symbols"):
                result = ToolResultSummary(
                    tool,
                    "service-b/App.java:1: class App {}",
                    path="service-b",
                    metadata={"evidence_root": str(root), "evidence_paths": ["service-b/App.java"]},
                )
                record = EvidenceRecord(
                    tool,
                    "App",
                    "Matched App.java",
                    details={"evidence_root": str(root), "evidence_scope": "root_local"},
                )
                self.assertTrue(
                    cache.capture(
                        tool_result=result,
                        record=record,
                        source_evidence=None,
                        requirement_evidence=None,
                        workspace_revision=0,
                        request="inspect service-b App",
                        run_id="run-1",
                    )
                )

            reuse = cache.reuse_for_request(
                prompt="service-b App 怎么样？",
                workspace_revision=0,
                authorized_roots=(root,),
            )

        self.assertEqual(reuse.hit_count, 2)
        self.assertEqual({entry.tool_result.name for entry in reuse.entries}, {"search_code", "lsp_symbols"})
        self.assertTrue(all(entry.tool_result.metadata["evidence_origin"] == "session_cached" for entry in reuse.entries))

    def test_negative_or_incomplete_results_are_never_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cache = SessionEvidenceCache()
            result = ToolResultSummary(
                "search_code",
                "No matches.",
                useless=True,
                metadata={"evidence_root": str(root), "negative_evidence_type": "content_no_match"},
            )
            record = EvidenceRecord(
                "search_code",
                "pattern='App'",
                "No matches returned.",
                status="content_no_match",
                details={"evidence_root": str(root)},
            )
            self.assertFalse(
                cache.capture(
                    tool_result=result,
                    record=record,
                    source_evidence=None,
                    requirement_evidence=None,
                    workspace_revision=0,
                    request="find App",
                    run_id="run-1",
                )
            )
            reuse = cache.reuse_for_request(prompt="find App", workspace_revision=0, authorized_roots=(root,))

        self.assertEqual(reuse.hit_count, 0)

    def test_external_file_change_invalidates_before_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "service-b" / "app.py"
            source.parent.mkdir()
            source.write_text("LANGUAGE = 'python'\n", encoding="utf-8")
            cache = _cache_read(cache_root=root, source=source)
            source.write_text("LANGUAGE = 'java'\n", encoding="utf-8")
            reuse = cache.reuse_for_request(
                prompt="service-b app.py 是什么语言？",
                workspace_revision=0,
                authorized_roots=(root,),
            )

        self.assertEqual(reuse.hit_count, 0)
        self.assertEqual(reuse.stale_count, 1)
        self.assertEqual(reuse.invalidation_count, 1)
        self.assertEqual(cache.snapshot()["entries"], 0)
        again = cache.reuse_for_request(
            prompt="service-b app.py 是什么语言？",
            workspace_revision=0,
            authorized_roots=(root,),
        )
        self.assertEqual(again.stale_count, 0)

    def test_multi_path_result_is_stale_when_the_ninth_referenced_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            paths: list[str] = []
            for index in range(9):
                path = root / "src" / f"File{index}.java"
                path.parent.mkdir(exist_ok=True)
                path.write_text(f"class File{index} {{}}\n", encoding="utf-8")
                paths.append(str(path.relative_to(root)))
            cache = SessionEvidenceCache()
            runtime = AgentRuntime(
                AgentConfig(
                    provider="openai-compatible",
                    api_base_url="https://example.invalid/v1",
                    api_key="token",
                    model="model",
                    workspace=root,
                    max_steps=0,
                    budget_seconds=None,
                    approval_mode="yolo",
                ),
                show_tool_logs=False,
            )
            output = "\n".join(f"{path}:1: class" for path in paths)
            metadata = runtime._tool_choice_result_metadata("search_code", {"path": "."}, ToolResult(output))
            self.assertEqual(metadata["evidence_paths"], paths)
            self.assertNotIn("evidence_paths_overflow", metadata)
            result = ToolResultSummary(
                "search_code",
                output,
                path="src",
                metadata=metadata,
            )
            record = EvidenceRecord(
                "search_code",
                "pattern='class'",
                "Matched Java files",
                details={"evidence_root": str(root), "evidence_scope": "root_local"},
            )
            self.assertTrue(
                cache.capture(
                    tool_result=result,
                    record=record,
                    source_evidence=None,
                    requirement_evidence=None,
                    workspace_revision=0,
                    request="inspect src Java files",
                    run_id="run-1",
                )
            )
            (root / paths[-1]).write_text("class File8 { int changed; }\n", encoding="utf-8")
            reuse = cache.reuse_for_request(
                prompt="inspect src Java files",
                workspace_revision=0,
                authorized_roots=(root,),
            )

        self.assertEqual(reuse.hit_count, 0)
        self.assertEqual(reuse.stale_count, 1)

    def test_overflowing_concrete_path_set_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            paths: list[str] = []
            for index in range(33):
                path = root / "src" / f"File{index}.java"
                path.parent.mkdir(exist_ok=True)
                path.write_text(f"class File{index} {{}}\n", encoding="utf-8")
                paths.append(str(path.relative_to(root)))
            cache = SessionEvidenceCache()
            runtime = AgentRuntime(
                AgentConfig(
                    provider="openai-compatible",
                    api_base_url="https://example.invalid/v1",
                    api_key="token",
                    model="model",
                    workspace=root,
                    max_steps=0,
                    budget_seconds=None,
                    approval_mode="yolo",
                ),
                show_tool_logs=False,
            )
            output = "\n".join(f"{path}:1: class" for path in paths)
            producer_metadata = runtime._tool_choice_result_metadata("search_code", {"path": "."}, ToolResult(output))
            self.assertTrue(producer_metadata["evidence_paths_overflow"])
            self.assertEqual(len(producer_metadata["evidence_paths"]), 32)
            result = ToolResultSummary(
                "search_code",
                output,
                metadata=producer_metadata,
            )
            record = EvidenceRecord("search_code", "pattern='class'", "Matched files", details={"evidence_root": str(root)})
            cached = cache.capture(
                tool_result=result,
                record=record,
                source_evidence=None,
                requirement_evidence=None,
                workspace_revision=0,
                request="inspect Java files",
                run_id="run-1",
            )

        self.assertFalse(cached)

    def test_unhashable_candidate_path_rejects_entire_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            existing = root / "src" / "Existing.java"
            existing.parent.mkdir()
            existing.write_text("class Existing {}\n", encoding="utf-8")
            cache = SessionEvidenceCache()
            result = ToolResultSummary(
                "search_code",
                "src/Existing.java:1: class\nsrc/Missing.java:1: class",
                metadata={
                    "evidence_root": str(root),
                    "evidence_paths": ["src/Existing.java", "src/Missing.java"],
                },
            )
            record = EvidenceRecord("search_code", "pattern='class'", "Matched files", details={"evidence_root": str(root)})
            cached = cache.capture(
                tool_result=result,
                record=record,
                source_evidence=None,
                requirement_evidence=None,
                workspace_revision=0,
                request="inspect Java files",
                run_id="run-1",
            )

        self.assertFalse(cached)

    def test_short_followup_compares_previous_request_before_replacing_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "service-b" / "app.py"
            source.parent.mkdir()
            source.write_text("LANGUAGE = 'python'\n", encoding="utf-8")
            cache = _cache_read(cache_root=root, source=source)
            cache.remember_request("请分析 service-b 的 app.py", "run-1")
            reuse = cache.reuse_for_request(
                prompt="呢？",
                workspace_revision=0,
                authorized_roots=(root,),
            )

        self.assertEqual(reuse.hit_count, 1)

    def test_short_followup_only_reuses_evidence_from_immediately_previous_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first = root / "service-a" / "app.py"
            second = root / "service-b" / "app.py"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_text("A = 1\n", encoding="utf-8")
            second.write_text("B = 2\n", encoding="utf-8")
            cache = _cache_read(cache_root=root, source=first, request="inspect service-a", run_id="run-a")
            _capture_read(cache, root, second, request="inspect service-b", run_id="run-b")
            cache.remember_request("inspect service-b", "run-b")
            reuse = cache.reuse_for_request(
                prompt="呢？",
                workspace_revision=0,
                authorized_roots=(root,),
            )

        self.assertEqual(reuse.hit_count, 1)
        self.assertEqual(reuse.reused_paths, (str(second.resolve()),))

    def test_user_facts_only_carry_relevant_prior_context(self) -> None:
        facts = UserFactsLayer()
        facts.begin_run("service-a 是 Java", "run-a")
        facts.begin_run("请分析 service-b 是 Python 的实现", "run-b")
        facts.begin_run("service-b 的接口在哪里？", "run-c")
        relevant = facts.render_for("service-b 的接口在哪里？")
        facts.begin_run("检查 payment 模块的安全问题", "run-d")
        unrelated = facts.render_for("检查 payment 模块的安全问题")

        self.assertIn("current_user_input", relevant)
        self.assertIn("relevant_prior_user_context_runs", relevant)
        self.assertIn("run-b", relevant)
        self.assertNotIn("service-a 是 Java", unrelated)
        self.assertNotIn("service-b 是 Python", unrelated)
        self.assertIn("run-d", unrelated)

    def test_unrelated_cached_module_is_not_reused_as_code_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "service-a" / "app.py"
            source.parent.mkdir()
            source.write_text("A = 1\n", encoding="utf-8")
            cache = _cache_read(cache_root=root, source=source, request="inspect service-a", run_id="run-a")
            cache.remember_request("inspect service-a", "run-a")
            reuse = cache.reuse_for_request(
                prompt="inspect payment module source",
                workspace_revision=0,
                authorized_roots=(root,),
            )

        self.assertEqual(reuse.hit_count, 0)


def _cache_read(
    *,
    cache_root: Path,
    source: Path,
    request: str = "请分析 service-b app.py",
    run_id: str = "run-1",
) -> SessionEvidenceCache:
    cache = SessionEvidenceCache()
    _capture_read(cache, cache_root, source, request=request, run_id=run_id)
    return cache


def _capture_read(cache: SessionEvidenceCache, cache_root: Path, source: Path, *, request: str, run_id: str) -> None:
    display = str(source.relative_to(cache_root))
    source_evidence = SourceEvidence(display, source.read_text(encoding="utf-8"), root=str(cache_root))
    result = ToolResultSummary(
        "read_file",
        source_evidence.content,
        path=display,
        metadata={"evidence_root": str(cache_root), "evidence_paths": [display]},
    )
    record = EvidenceRecord(
        "read_file",
        display,
        "read app.py",
        details={"evidence_root": str(cache_root), "resolved_path": str(source)},
    )
    assert cache.capture(
        tool_result=result,
        record=record,
        source_evidence=source_evidence,
        requirement_evidence=None,
        workspace_revision=0,
        request=request,
        run_id=run_id,
    )

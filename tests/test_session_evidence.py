from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from local_agent.agent import AgentRuntime
from local_agent.config import AgentConfig
from local_agent.evidence import EvidenceRecord
from local_agent.session_evidence import SessionEvidenceCache
from local_agent.session_evidence import serialize_cached_evidence_entry
from local_agent.steering.final_answer import SourceEvidence
from local_agent.tool_choice_queue import ToolResultSummary
from local_agent.tools.base import ToolResult
from local_agent.user_facts import UserFactsLayer


class SessionEvidenceCacheTests(unittest.TestCase):
    def test_image_observation_is_not_cached_or_serialized_as_session_source_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            image = root / "example.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nsecret-pixels")
            cache = SessionEvidenceCache()
            entry = cache.capture(
                tool_result=ToolResultSummary(
                    "inspect_image",
                    "[image observation: example.png#tag] visible fields",
                    path="example.png",
                    metadata={"image_observation": True},
                ),
                record=EvidenceRecord(
                    "inspect_image",
                    "example.png",
                    "Image observation: visible fields.",
                    details={"evidence_root": str(root), "resolved_path": str(image)},
                ),
                source_evidence=None,
                requirement_evidence=None,
                workspace_revision=0,
                request="inspect image",
                run_id="run-image",
            )

        self.assertIsNone(entry)
        self.assertEqual(cache.snapshot()["entries"], 0)

    def test_named_session_restores_fresh_requirement_evidence_across_runtime_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            requirement = root / "requirements.md"
            requirement.write_text("# Requirement\nsettlement requires rollback\n", encoding="utf-8")
            config = _config(root)
            first = AgentRuntime(config, show_tool_logs=False)
            first._run.run_id = "run-requirement"
            first._run.current_user_request = "读取 requirements.md"
            result = ToolResult(requirement.read_text(encoding="utf-8"))
            first._evidence_phase.record_tool_choice_result("read_file", {"path": "requirements.md"}, result)
            first._evidence_phase.record_read_file_evidence("read_file", {"path": "requirements.md"}, result)
            first._evidence_phase.record_tool_evidence("read_file", {"path": "requirements.md"}, result)

            resumed = AgentRuntime(config, show_tool_logs=False, session_id=first._session.session_id)
            self.assertEqual(resumed._session_evidence.snapshot()["entries"], 1)
            resumed._evidence_phase.hydrate_session_evidence("结合 requirements 的结算回退继续设计")

        pinned = resumed._run.evidence.pinned_requirement_evidence
        self.assertEqual(len(pinned), 1)
        self.assertTrue(pinned[0].path.endswith("requirements.md"))
        self.assertEqual(pinned[0].origin, "session_cached")

    def test_journal_restore_obeys_current_read_file_preapproval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            requirement = root / "requirements.md"
            requirement.write_text("requirement\n", encoding="utf-8")
            first = AgentRuntime(_config(root), show_tool_logs=False)
            first._run.run_id = "run-journal"
            first._run.current_user_request = "read requirement"
            result = ToolResult(requirement.read_text(encoding="utf-8"))
            first._evidence_phase.record_tool_choice_result("read_file", {"path": "requirements.md"}, result)
            first._evidence_phase.record_read_file_evidence("read_file", {"path": "requirements.md"}, result)
            first._evidence_phase.record_tool_evidence("read_file", {"path": "requirements.md"}, result)
            session_id = first._session.session_id

            for approval_mode, policy in (("always-ask", "deny"), ("always-ask", "prompt")):
                config = replace(_config(root), approval_mode=approval_mode, tool_approval={"read_file": policy})
                with patch("local_agent.session.evidence._read_journal_file_content") as read_file:
                    resumed = AgentRuntime(config, show_tool_logs=False, session_id=session_id)
                self.assertEqual(read_file.call_count, 0)
                self.assertEqual(resumed._session_evidence.snapshot()["entries"], 0)

            allowed = replace(_config(root), approval_mode="always-ask", tool_approval={"read_file": "allow"})
            resumed = AgentRuntime(allowed, show_tool_logs=False, session_id=session_id)
            self.assertEqual(resumed._session_evidence.snapshot()["entries"], 1)

    def test_named_session_restore_revalidates_latest_authorized_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            primary = base / "primary"
            extra = base / "extra"
            primary.mkdir()
            extra.mkdir()
            primary_source = primary / "requirements.md"
            extra_source = extra / "extra.md"
            primary_source.write_text("primary requirement continuity\n", encoding="utf-8")
            extra_source.write_text("secondaryrootxyz evidence should not revive\n", encoding="utf-8")
            first = AgentRuntime(_config(primary), show_tool_logs=False)

            def capture(runtime: AgentRuntime, source: Path, request: str) -> None:
                runtime._run.run_id = "run-journal-roots"
                runtime._run.current_user_request = request
                result = ToolResult(source.read_text(encoding="utf-8"))
                runtime._evidence_phase.record_tool_choice_result("read_file", {"path": str(source)}, result)
                runtime._evidence_phase.record_read_file_evidence("read_file", {"path": str(source)}, result)
                runtime._evidence_phase.record_tool_evidence("read_file", {"path": str(source)}, result)

            capture(first, primary_source, "primary continuity")
            first.add_workspace_root(str(extra))
            capture(first, extra_source, "secondaryrootxyz continuity")
            self.assertEqual(first._session_evidence.snapshot()["entries"], 2)
            first.remove_workspace_root(str(extra))
            self.assertEqual(first._session_evidence.snapshot()["entries"], 1)
            first.add_workspace_root(str(extra))
            capture(first, extra_source, "secondaryrootxyz continuity reset")
            self.assertEqual(first._session_evidence.snapshot()["entries"], 2)
            first.reset_workspace_roots()
            self.assertEqual(first._session_evidence.snapshot()["entries"], 1)

            resumed = AgentRuntime(_config(primary), show_tool_logs=False, session_id=first._session.session_id)
            primary_reuse = resumed._session_evidence.reuse_for_request(
                prompt="primary continuity",
                workspace_revision=resumed._workspace_context.revision,
                authorized_roots=resumed._workspace_context.all_roots,
            )
            extra_reuse = resumed._session_evidence.reuse_for_request(
                prompt="secondaryrootxyz",
                workspace_revision=resumed._workspace_context.revision,
                authorized_roots=resumed._workspace_context.all_roots,
            )

        self.assertEqual(resumed._session_evidence.snapshot()["entries"], 1)
        restored_paths = tuple(resumed._session_evidence.snapshot()["paths"])
        self.assertEqual(restored_paths, (str(primary_source),))
        self.assertTrue(all(str(primary) in path for path in primary_reuse.reused_paths))
        self.assertNotIn(str(extra_source), restored_paths)
        self.assertTrue(all(entry.root == str(primary) for entry in primary_reuse.entries))
        self.assertTrue(all(entry.root == str(primary) for entry in extra_reuse.entries))
        self.assertEqual(primary_reuse.hit_count, 1)

    def test_serialized_entry_is_revalidated_after_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "src" / "app.py"
            source.parent.mkdir()
            source.write_text("VALUE = 1\n", encoding="utf-8")
            cache = SessionEvidenceCache()
            display = str(source.relative_to(root))
            result = ToolResultSummary("read_file", source.read_text(encoding="utf-8"), path=display)
            record = EvidenceRecord(
                "read_file",
                display,
                "read app.py",
                details={"evidence_root": str(root), "resolved_path": str(source)},
            )
            entry = cache.capture(
                tool_result=result,
                record=record,
                source_evidence=SourceEvidence(display, result.content, root=str(root)),
                requirement_evidence=None,
                workspace_revision=0,
                request="inspect app VALUE",
                run_id="run-1",
            )
            self.assertIsNotNone(entry)
            restored = SessionEvidenceCache()
            self.assertEqual(restored.restore_entries([serialize_cached_evidence_entry(entry)]), 1)
            source.write_text("VALUE = 2\n", encoding="utf-8")

            reuse = restored.reuse_for_request(
                prompt="inspect app VALUE",
                workspace_revision=0,
                authorized_roots=(root,),
            )

        self.assertEqual(reuse.hit_count, 0)
        self.assertEqual(reuse.stale_count, 1)

    def test_serialized_entry_cannot_tag_a_path_outside_its_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "root"
            root.mkdir()
            source = root / "app.py"
            outside = base / "outside.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            outside.write_text("SECRET = 1\n", encoding="utf-8")
            cache = SessionEvidenceCache()
            display = source.name
            result = ToolResultSummary("read_file", source.read_text(encoding="utf-8"), path=display)
            record = EvidenceRecord(
                "read_file",
                display,
                "read app.py",
                details={"evidence_root": str(root), "resolved_path": str(source)},
            )
            entry = cache.capture(
                tool_result=result,
                record=record,
                source_evidence=SourceEvidence(display, result.content, root=str(root)),
                requirement_evidence=None,
                workspace_revision=0,
                request="inspect app",
                run_id="run-1",
            )
            self.assertIsNotNone(entry)
            payload = serialize_cached_evidence_entry(entry)
            payload["content_tags"] = {str(outside): "0" * 64}

            restored = SessionEvidenceCache()
            self.assertEqual(restored.restore_entries([payload]), 0)

    def test_journal_rebuilds_read_content_instead_of_trusting_serialized_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            requirement = root / "requirements.md"
            requirement.write_text("actual requirement\n", encoding="utf-8")
            cache = SessionEvidenceCache()
            result = ToolResultSummary("read_file", requirement.read_text(encoding="utf-8"), path="requirements.md")
            record = EvidenceRecord(
                "read_file",
                "requirements.md",
                "read requirement",
                details={"evidence_root": str(root), "resolved_path": str(requirement)},
            )
            entry = cache.capture(
                tool_result=result,
                record=record,
                source_evidence=SourceEvidence("requirements.md", result.content, root=str(root)),
                requirement_evidence=None,
                workspace_revision=0,
                request="read requirements",
                run_id="run-1",
            )
            self.assertIsNotNone(entry)
            payload = serialize_cached_evidence_entry(entry)
            payload["tool_result"]["content"] = "tampered text"
            payload["source_evidence"]["content"] = "tampered source"

            restored = SessionEvidenceCache()
            self.assertEqual(restored.restore_entries([payload]), 1)
            reuse = restored.reuse_for_request(
                prompt="requirements",
                workspace_revision=0,
                authorized_roots=(root,),
            )

        self.assertEqual(reuse.hit_count, 1)
        self.assertEqual(reuse.entries[0].tool_result.content, "actual requirement\n")
        self.assertEqual(reuse.entries[0].source_evidence.content, "actual requirement\n")

    def test_journal_refuses_cross_process_search_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "App.java"
            source.write_text("class App {}\n", encoding="utf-8")
            cache = SessionEvidenceCache()
            result = ToolResultSummary(
                "search_code",
                "App.java:1: class App {}",
                metadata={"evidence_root": str(root), "evidence_paths": ["App.java"]},
            )
            record = EvidenceRecord("search_code", "App", "matched App", details={"evidence_root": str(root)})
            entry = cache.capture(
                tool_result=result,
                record=record,
                source_evidence=None,
                requirement_evidence=None,
                workspace_revision=0,
                request="find App",
                run_id="run-1",
            )
            self.assertIsNotNone(entry)

            restored = SessionEvidenceCache()
            self.assertEqual(restored.restore_entries([serialize_cached_evidence_entry(entry)]), 0)

    def test_restored_chinese_requirement_matches_a_related_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            requirement = root / "requirements.md"
            requirement.write_text("拓展服务费结算需要支持回退。\n", encoding="utf-8")
            cache = SessionEvidenceCache()
            result = ToolResultSummary("read_file", requirement.read_text(encoding="utf-8"), path="requirements.md")
            record = EvidenceRecord(
                "read_file",
                "requirements.md",
                "read requirement",
                details={"evidence_root": str(root), "resolved_path": str(requirement)},
            )
            entry = cache.capture(
                tool_result=result,
                record=record,
                source_evidence=SourceEvidence("requirements.md", result.content, root=str(root)),
                requirement_evidence=None,
                workspace_revision=0,
                request="分析需求文档",
                run_id="run-1",
            )
            self.assertIsNotNone(entry)
            restored = SessionEvidenceCache()
            self.assertEqual(restored.restore_entries([serialize_cached_evidence_entry(entry)]), 1)
            reuse = restored.reuse_for_request(
                prompt="定位拓展服务费结算的实现范围",
                workspace_revision=0,
                authorized_roots=(root,),
            )

        self.assertEqual(reuse.hit_count, 1)

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
            metadata = runtime._evidence_phase.tool_choice_result_metadata("search_code", {"path": "."}, ToolResult(output))
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
            producer_metadata = runtime._evidence_phase.tool_choice_result_metadata(
                "search_code", {"path": "."}, ToolResult(output)
            )
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

    def test_prior_user_context_normalizes_workflow_nudge_and_stays_bounded(self) -> None:
        facts = UserFactsLayer()
        prior = "请记住 service-b 是 Python"
        facts.begin_run(prior, "run-a")
        facts.begin_run("service-b 呢？", "run-b")

        projected = facts.render_relevant_prior_user_context("service-b 呢？", retained_user_contents=())
        retained = facts.render_relevant_prior_user_context(
            "service-b 呢？",
            retained_user_contents=(prior + "\n\n[Runtime workflow reminder]\nworkflow",),
        )

        self.assertIn(prior, projected)
        self.assertEqual(retained, "")

    def test_semantic_dedupe_replaces_repeated_observation_and_keeps_distinct_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "a.py"
            source.write_text("one\ntwo\n", encoding="utf-8")
            runtime = AgentRuntime(_config(root), show_tool_logs=False)
            result = ToolResult(source.read_text(encoding="utf-8"))
            implicit = runtime._evidence_phase.tool_choice_result_metadata("read_file", {"path": "a.py"}, result)
            explicit_first = runtime._evidence_phase.tool_choice_result_metadata(
                "read_file", {"path": "a.py", "start_line": 1}, result
            )
            second_line = runtime._evidence_phase.tool_choice_result_metadata(
                "read_file", {"path": "a.py", "start_line": 2}, result
            )
            self.assertEqual(implicit["session_evidence_query_identity"], explicit_first["session_evidence_query_identity"])
            self.assertNotEqual(implicit["session_evidence_query_identity"], second_line["session_evidence_query_identity"])
            cache = SessionEvidenceCache()
            _capture_read(cache, root, source, request="inspect a.py", run_id="run-a", metadata=implicit)
            _capture_read(cache, root, source, request="inspect a.py", run_id="run-b", metadata=implicit)
            self.assertEqual(cache.snapshot()["entries"], 1)
            _capture_read(cache, root, source, request="inspect a.py line 2", run_id="run-c", metadata=second_line)
            self.assertEqual(cache.snapshot()["entries"], 2)
            cache.remember_request("inspect a.py", "run-c")
            reuse = cache.reuse_for_request(prompt="inspect a.py", workspace_revision=0, authorized_roots=(root,))

        self.assertEqual(reuse.hit_count, 2)

    def test_relative_and_absolute_reads_share_a_canonical_dedupe_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "a.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            runtime = AgentRuntime(_config(root), show_tool_logs=False)
            result = ToolResult(source.read_text(encoding="utf-8"))
            relative = runtime._evidence_phase.tool_choice_result_metadata("read_file", {"path": "a.py"}, result)
            absolute = runtime._evidence_phase.tool_choice_result_metadata(
                "read_file", {"path": str(source)}, result
            )
            self.assertEqual(relative["session_evidence_query_identity"], absolute["session_evidence_query_identity"])
            cache = SessionEvidenceCache()
            _capture_read(cache, root, source, request="inspect a.py", run_id="run-a", metadata=relative, path="a.py")
            _capture_read(cache, root, source, request="inspect a.py", run_id="run-b", metadata=absolute, path=str(source))

        self.assertEqual(cache.snapshot()["entries"], 1)

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

    def test_root_authorization_change_preserves_still_authorized_fresh_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            primary = base / "primary"
            secondary = base / "secondary"
            primary.mkdir()
            secondary.mkdir()
            primary_source = primary / "requirements.md"
            secondary_source = secondary / "service.py"
            primary_source.write_text("requirement says rollback\n", encoding="utf-8")
            secondary_source.write_text("class Service: pass\n", encoding="utf-8")
            cache = _cache_read(cache_root=primary, source=primary_source, request="rollback")
            _capture_read(cache, secondary, secondary_source, request="service", run_id="run-2")

            removed = cache.revalidate_authorized_roots(
                workspace_revision=1,
                authorized_roots=(primary, secondary),
            )
            self.assertEqual(removed, 0)
            reuse = cache.reuse_for_request(
                prompt="rollback",
                workspace_revision=1,
                authorized_roots=(primary, secondary),
            )
            self.assertEqual(reuse.hit_count, 1)
            self.assertTrue(reuse.entries[0].tool_result.path.endswith("requirements.md"))

            removed = cache.revalidate_authorized_roots(
                workspace_revision=2,
                authorized_roots=(secondary,),
            )
            self.assertEqual(removed, 1)
            reuse = cache.reuse_for_request(
                prompt="rollback",
                workspace_revision=2,
                authorized_roots=(secondary,),
            )

        self.assertEqual(reuse.hit_count, 0)

    def test_root_authorization_revalidation_evicts_changed_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "requirements.md"
            source.write_text("requirement v1\n", encoding="utf-8")
            cache = _cache_read(cache_root=root, source=source, request="requirement")
            source.write_text("requirement v2\n", encoding="utf-8")

            removed = cache.revalidate_authorized_roots(
                workspace_revision=1,
                authorized_roots=(root,),
            )

        self.assertEqual(removed, 1)
        self.assertEqual(cache.snapshot()["entries"], 0)


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


def _capture_read(
    cache: SessionEvidenceCache,
    cache_root: Path,
    source: Path,
    *,
    request: str,
    run_id: str,
    metadata: dict[str, object] | None = None,
    path: str | None = None,
) -> None:
    display = str(source.relative_to(cache_root))
    source_evidence = SourceEvidence(display, source.read_text(encoding="utf-8"), root=str(cache_root))
    result = ToolResultSummary(
        "read_file",
        source_evidence.content,
        path=path or display,
        metadata={"evidence_root": str(cache_root), "evidence_paths": [display], **(metadata or {})},
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


def _config(workspace: Path) -> AgentConfig:
    return AgentConfig(
        provider="openai-compatible",
        api_base_url="https://example.invalid/v1",
        api_key="token",
        model="model",
        workspace=workspace,
        max_steps=0,
        budget_seconds=None,
        approval_mode="yolo",
    )

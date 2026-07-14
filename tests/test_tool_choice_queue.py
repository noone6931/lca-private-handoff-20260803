from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_agent.tool_choice_queue import READ_ONLY_FORBIDDEN_TOOL_NAMES
from local_agent.tool_choice_queue import WORKSPACE_INVENTORY_TOOL_NAMES
from local_agent.tool_choice_queue import WORKSPACE_INVENTORY_DISCOVERY_TOOL_NAMES
from local_agent.tool_choice_queue import CANDIDATE_DELIVERY_TOOL_NAMES
from local_agent.tool_choice_queue import CANDIDATE_DIFF_TOOL_NAMES
from local_agent.tool_choice_queue import CANDIDATE_REMEDIATION_TOOL_NAMES
from local_agent.tool_choice_queue import CANDIDATE_TEST_TOOL_NAMES
from local_agent.tool_choice_queue import DOCUMENT_ONLY_TOOL_NAMES
from local_agent.tool_choice_queue import MAX_CANDIDATE_READ_REVISITS
from local_agent.tool_choice_queue import MAX_CANDIDATE_PATCH_PREVIEW_FAILURES
from local_agent.tool_choice_queue import PLANNER_EXPLORE_TOOL_NAMES
from local_agent.tool_choice_queue import POST_DIFF_REMEDIATION_TOOL_NAMES
from local_agent.tool_choice_queue import ToolChoiceQueue
from local_agent.tool_choice_queue import ToolChoiceDecision
from local_agent.tool_choice_queue import ToolResultSummary
from local_agent.tool_choice_queue import evaluate_tool_choice_state
from local_agent.tool_choice_queue import session_evidence_reuse_directive
from local_agent.tool_choice_queue import tool_choice_steering_message
from local_agent.read_only_explore import evaluate_read_only_explore
from local_agent.document_artifacts import DocumentArtifactRequirement
from local_agent.run_context import RunContext
from local_agent.task_contract import generate_requirement_contract
from local_agent.tool_choice_directive import ToolChoiceDirectiveOwner
from local_agent.tools.search import search_tools


class ToolChoiceQueueTests(unittest.TestCase):
    def test_tool_choice_steering_message_keeps_reason_after_owner_move(self) -> None:
        message = tool_choice_steering_message(
            ToolChoiceDecision(
                steering_required=True,
                allowed_tool_names=frozenset({"read_file"}),
                reason="collect exact source evidence",
                rule_id="read_only_explore",
                missing_requirements=("direct_read",),
                preferred_tool_names=("read_file",),
                tool_call_hints=("read_file path=src/Owner.java",),
            ),
            "只读分析 owner",
        )
        self.assertIn("- allowed tools now: read_file", message)
        self.assertIn("- reason: collect exact source evidence", message)
        self.assertIn("- call hint: read_file path=src/Owner.java", message)

    def test_owner_explore_prefers_direct_read_of_typed_search_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "src" / "Owner.java"
            source.parent.mkdir()
            source.write_text("class Owner {}\n", encoding="utf-8")
            result = ToolResultSummary(
                "search_code", f"{source}:1: class Owner", metadata={"evidence_paths": [str(source)]}
            )
            decision = evaluate_read_only_explore(
                profile="owner_impact", tool_results=(result,), code_roots=(str(root),)
            )
        self.assertEqual(decision.action, "precise")
        self.assertEqual(decision.read_candidates, (str(source),))

    def test_owner_explore_uses_read_search_subset_and_prioritizes_least_observed_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first = root / "service-a"
            second = root / "service-b"
            first.mkdir()
            second.mkdir()
            initial = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="请分析服务 owner 和影响范围。",
                tool_results=(),
                workspace_roots=(str(first), str(second)),
                read_only_review_profile="owner_impact",
            )
            after_first_root = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="请分析服务 owner 和影响范围。",
                tool_results=(
                    ToolResultSummary(
                        "search_code",
                        "No matches.",
                        useless=True,
                        metadata={
                            "evidence_root": str(first),
                            "negative_evidence_type": "content_no_match",
                        },
                    ),
                ),
                workspace_roots=(str(first), str(second)),
                read_only_review_profile="owner_impact",
            )
            source = second / "src" / "Owner.java"
            source.parent.mkdir()
            source.write_text("class Owner {}\n", encoding="utf-8")
            read_candidate = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="请分析服务 owner 和影响范围。",
                tool_results=(
                    ToolResultSummary(
                        "search_code",
                        f"{source}:1: class Owner",
                        metadata={
                            "evidence_root": str(second),
                            "evidence_paths": [str(source)],
                        },
                    ),
                ),
                workspace_roots=(str(first), str(second)),
                read_only_review_profile="owner_impact",
            )

        self.assertEqual(initial.allowed_tool_names, frozenset({"glob_files", "read_file", "search_code"}))
        self.assertFalse(any(name.startswith("lsp_") for name in initial.allowed_tool_names))
        self.assertIn(str(first), initial.tool_call_hints[0])
        self.assertIn(str(second), initial.tool_call_hints[0])
        self.assertIn(str(second), after_first_root.tool_call_hints[0])
        self.assertNotIn(str(first), after_first_root.tool_call_hints[0])
        self.assertEqual(read_candidate.rule_id, "read_only_profile_explore")
        self.assertEqual(read_candidate.allowed_tool_names, frozenset({"read_file", "search_code"}))
        self.assertEqual(read_candidate.scoped_read_paths, ())
        self.assertIn(str(source), read_candidate.tool_call_hints[0])

    def test_general_code_evidence_flow_still_exposes_lsp(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="read-only",
            prompt="只读分析当前代码并给出源码证据。",
            tool_results=(),
            workspace_roots=("/tmp/workspace",),
        )

        self.assertIn("lsp_symbols", decision.allowed_tool_names)
        self.assertIn("lsp_definition", decision.allowed_tool_names)

    def test_suppressed_or_rejected_observations_do_not_consume_explore_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            results = (
                ToolResultSummary(
                    "list_files",
                    "Tool call was rejected by active schema.",
                    is_error=True,
                    metadata={"provider_schema_violation": True, "evidence_root": str(root)},
                ),
                ToolResultSummary(
                    "search_code",
                    "Tool call was not executed",
                    is_error=True,
                    path=str(root / "A.java"),
                    metadata={"provider_schema_violation": True, "evidence_root": str(root)},
                ),
                ToolResultSummary(
                    "glob_files",
                    "Tool call was not executed",
                    is_error=True,
                    path=str(root),
                    metadata={"suppressed": True, "evidence_root": str(root)},
                ),
            )
            decision = evaluate_read_only_explore(
                profile="owner_impact", tool_results=results, code_roots=(str(root),)
            )

        self.assertEqual(decision.observation_calls, 0)
        self.assertEqual(decision.successful_observations, 0)
        self.assertEqual(decision.missing_roots, (str(root),))
        self.assertEqual(decision.discovery_roots, ())

    def test_legal_executed_tool_error_consumes_one_explore_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            result = ToolResultSummary(
                "read_file",
                "Permission denied.",
                is_error=True,
                path=str(root / "Owner.java"),
                metadata={"evidence_root": str(root)},
            )
            decision = evaluate_read_only_explore(
                profile="owner_impact", tool_results=(result,), code_roots=(str(root),)
            )

        self.assertEqual(decision.observation_calls, 1)
        self.assertEqual(decision.successful_observations, 0)
        self.assertEqual(decision.missing_roots, (str(root),))

    def test_executed_error_does_not_contribute_read_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "src" / "Owner.java"
            source.parent.mkdir()
            source.write_text("class Owner {}\n", encoding="utf-8")
            result = ToolResultSummary(
                "search_code",
                "Search backend failed.",
                is_error=True,
                metadata={"evidence_root": str(root), "evidence_paths": [str(source)]},
            )
            decision = evaluate_read_only_explore(
                profile="owner_impact", tool_results=(result,), code_roots=(str(root),)
            )

        self.assertEqual(decision.observation_calls, 1)
        self.assertEqual(decision.read_candidates, ())

    def test_typed_no_match_counts_as_progress_but_not_direct_root_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            results = (
                ToolResultSummary(
                    "search_code",
                    "No matches.",
                    useless=True,
                    metadata={"evidence_root": str(root), "negative_evidence_type": "content_no_match"},
                ),
                ToolResultSummary("lsp_symbols", "No symbols.", useless=True, metadata={"evidence_root": str(root)}),
                ToolResultSummary("read_file", "Tool call was not executed", is_error=True, path=str(root / "App.java")),
            )
            decision = evaluate_read_only_explore(
                profile="owner_impact", tool_results=results, code_roots=(str(root),)
            )

        self.assertEqual(decision.observation_calls, 2)
        self.assertEqual(decision.successful_observations, 1)
        self.assertEqual(decision.missing_roots, (str(root),))

    def test_owner_explore_fallback_discovery_is_once_and_hard_cap_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            no_match = ToolResultSummary(
                "search_code",
                "No matches.",
                useless=True,
                metadata={"evidence_root": str(root), "negative_evidence_type": "content_no_match"},
            )
            alternate = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="只读分析 owner 和设计影响。",
                tool_results=(no_match,),
                workspace_roots=(str(root),),
                read_only_review_profile="owner_impact",
            )
            self.assertEqual(alternate.allowed_tool_names, frozenset({"glob_files"}))
            self.assertEqual(alternate.required_glob_roots, (str(root),))
            required_arguments = json.loads(alternate.required_tool_arguments_json)
            self.assertEqual(required_arguments["limit"], 200)
            self.assertTrue(all(path.startswith(str(root) + "/") for path in required_arguments["paths"]))

            owner = ToolChoiceDirectiveOwner()
            owner.begin_decision(alternate, [])
            glob_schema = next(tool for tool in search_tools() if tool.name == "glob_files").openai_schema()
            projected = owner.project_schemas([glob_schema])[0]["function"]["parameters"]
            self.assertEqual(set(projected["required"]), {"paths", "limit", "hidden", "gitignore"})
            self.assertEqual(
                projected["properties"]["paths"]["items"]["enum"],
                required_arguments["paths"],
            )

            error_results: list[ToolResultSummary] = []
            for expected in ("force", "force", "exhausted"):
                error_results.append(ToolResultSummary("glob_files", "invalid arguments", is_error=True))
                action = owner.begin_decision(alternate, error_results)
                self.assertEqual(action.kind, expected)

            glob_no_match = ToolResultSummary(
                "glob_files",
                '{"files":[]}',
                useless=True,
                metadata={
                    "evidence_root": str(root),
                    "searched_roots": [str(root)],
                    "negative_evidence_type": "path_no_match",
                },
            )
            after_fallback = evaluate_read_only_explore(
                profile="owner_impact",
                tool_results=(no_match, glob_no_match),
                code_roots=(str(root),),
            )
            final = evaluate_read_only_explore(
                profile="owner_impact",
                tool_results=(no_match, glob_no_match, no_match, no_match),
                code_roots=(str(root),),
            )

        self.assertEqual(after_fallback.observation_calls, 2)
        self.assertEqual(after_fallback.action, "precise")
        self.assertEqual(after_fallback.discovery_roots, ())
        self.assertEqual(final.observation_calls, final.hard_budget)
        self.assertEqual(final.action, "finalize")
        self.assertEqual(final.discovery_roots, ())

    def test_owner_explore_fallback_glob_source_becomes_bounded_read_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "src" / "Owner.java"
            source.parent.mkdir()
            source.write_text("class Owner {}\n", encoding="utf-8")
            no_match = ToolResultSummary(
                "search_code",
                "No matches.",
                useless=True,
                metadata={"evidence_root": str(root), "negative_evidence_type": "content_no_match"},
            )
            glob_match = ToolResultSummary(
                "glob_files",
                str(source),
                metadata={
                    "evidence_root": str(root),
                    "searched_roots": [str(root)],
                    "files": [str(source)],
                    "negative_evidence_type": "path_match",
                },
            )
            decision = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="只读分析 owner 和设计影响。",
                tool_results=(no_match, no_match, glob_match),
                workspace_roots=(str(root),),
                read_only_review_profile="owner_impact",
            )

        self.assertEqual(decision.allowed_tool_names, frozenset({"read_file"}))
        self.assertEqual(decision.scoped_read_paths, ())
        self.assertEqual(decision.required_tool_arguments_json, "")
        self.assertIn(str(source), decision.tool_call_hints[0])

    def test_exact_source_filename_glob_becomes_a_bounded_read_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "src" / "PrepareOrderApplication.java"
            source.parent.mkdir()
            source.write_text("class PrepareOrderApplication {}\n", encoding="utf-8")
            glob_match = ToolResultSummary(
                "glob_files",
                str(source),
                metadata={
                    "evidence_root": str(root),
                    "searched_roots": [str(root)],
                    "files": [str(source)],
                    "patterns": [str(root / "**" / "PrepareOrderApplication.java")],
                    "negative_evidence_type": "path_match",
                },
            )
            decision = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="只读分析 owner 和设计影响。",
                tool_results=(glob_match,),
                workspace_roots=(str(root),),
                read_only_review_profile="owner_impact",
            )

        self.assertEqual(decision.allowed_tool_names, frozenset({"read_file"}))
        self.assertEqual(decision.scoped_read_paths, (str(source),))
        self.assertEqual(json.loads(decision.required_tool_arguments_json), {"path": str(source)})
        self.assertIn(str(source), decision.tool_call_hints[0])

    def test_cross_root_exact_candidates_keep_each_missing_root_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = Path(tmp, "backend").resolve()
            frontend = Path(tmp, "frontend").resolve()
            backend_source = backend / "src" / "PrepareOrderApplication.java"
            frontend_source = frontend / "src" / "views" / "preOrderManagement" / "list.vue"
            backend_source.parent.mkdir(parents=True)
            frontend_source.parent.mkdir(parents=True)
            backend_source.write_text("class PrepareOrderApplication {}\n", encoding="utf-8")
            frontend_source.write_text("<template />\n", encoding="utf-8")
            backend_glob = ToolResultSummary(
                "glob_files",
                str(backend_source),
                metadata={
                    "searched_roots": [str(backend)],
                    "files": [str(backend_source)],
                    "patterns": [str(backend_source)],
                    "negative_evidence_type": "path_match",
                },
            )
            frontend_glob = ToolResultSummary(
                "glob_files",
                str(frontend_source),
                metadata={
                    "searched_roots": [str(frontend)],
                    "files": [str(frontend_source)],
                    "patterns": [str(frontend_source)],
                    "negative_evidence_type": "path_match",
                },
            )
            decision = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="只读分析 owner 和设计影响。",
                tool_results=(backend_glob, frontend_glob),
                workspace_roots=(str(backend), str(frontend)),
                read_only_review_profile="design",
            )

        self.assertEqual(decision.allowed_tool_names, frozenset({"read_file"}))
        self.assertEqual(
            decision.scoped_read_paths,
            (str(backend_source), str(frontend_source)),
        )
        self.assertEqual(decision.scoped_read_budget, 1)
        self.assertEqual(decision.required_tool_arguments_json, "")

    def test_completed_source_only_glob_requires_one_model_selected_source_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            application = root / "src" / "OwnerApplication.java"
            controller = root / "src" / "OwnerController.java"
            application.parent.mkdir()
            application.write_text("class OwnerApplication {}\n", encoding="utf-8")
            controller.write_text("class OwnerController {}\n", encoding="utf-8")
            broad_glob = ToolResultSummary(
                "glob_files",
                f"{application}\n{controller}",
                metadata={
                    "evidence_root": str(root),
                    "searched_roots": [str(root)],
                    "files": [str(application), str(controller)],
                    "patterns": ["**/*Owner*.java"],
                    "negative_evidence_type": "path_match",
                    "truncated": False,
                },
            )
            decision = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="只读定位 owner 和调用链。",
                tool_results=(broad_glob,),
                workspace_roots=(str(root),),
                read_only_review_profile="owner_impact",
            )

        self.assertEqual(decision.rule_id, "read_only_profile_explore_inventory_read")
        self.assertEqual(decision.allowed_tool_names, frozenset({"read_file"}))
        self.assertEqual(decision.scoped_read_paths, ())
        self.assertIsNone(decision.scoped_read_budget)
        self.assertIn("OwnerApplication.java", decision.tool_call_hints[0])

    def test_model_selected_source_read_completes_root_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "src" / "OwnerApplication.java"
            source.parent.mkdir()
            source.write_text("class OwnerApplication {}\n", encoding="utf-8")
            broad_glob = ToolResultSummary(
                "glob_files",
                str(source),
                metadata={
                    "evidence_root": str(root),
                    "searched_roots": [str(root)],
                    "files": [str(source)],
                    "patterns": ["**/*Owner*.java"],
                    "negative_evidence_type": "path_match",
                    "truncated": False,
                },
            )
            source_read = ToolResultSummary(
                "read_file",
                "class OwnerApplication {}",
                path=str(source),
                metadata={"resolved_path": str(source)},
            )
            decision = evaluate_read_only_explore(
                profile="owner_impact",
                tool_results=(broad_glob, source_read),
                code_roots=(str(root),),
            )

        self.assertEqual(decision.action, "finalize")
        self.assertEqual(decision.missing_roots, ())

    def test_generic_mixed_inventory_exposes_only_source_read_choices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "src" / "Owner.java"
            manifest = root / "package.json"
            pom = root / "pom.xml"
            source.parent.mkdir()
            source.write_text("class Owner {}\n", encoding="utf-8")
            manifest.write_text("{}\n", encoding="utf-8")
            pom.write_text("<project />\n", encoding="utf-8")
            inventory = ToolResultSummary(
                "glob_files",
                f"{manifest}\n{pom}\n{source}",
                metadata={
                    "evidence_root": str(root),
                    "searched_roots": [str(root)],
                    "files": [str(manifest), str(pom), str(source)],
                    "patterns": ["**/package.json", "**/pom.xml", "**/src/**/*.*"],
                    "negative_evidence_type": "path_match",
                    "truncated": True,
                },
            )
            decision = evaluate_read_only_explore(
                profile="owner_impact",
                tool_results=(inventory,),
                code_roots=(str(root),),
            )

        self.assertEqual(decision.inventory_read_candidates, (str(source),))

    def test_hard_budget_keeps_one_typed_candidate_read_closure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "src" / "Owner.java"
            source.parent.mkdir()
            source.write_text("class Owner {}\n", encoding="utf-8")
            no_match = ToolResultSummary(
                "search_code",
                "No matches.",
                useless=True,
                metadata={"evidence_root": str(root), "negative_evidence_type": "content_no_match"},
            )
            candidate = ToolResultSummary(
                "search_code",
                f"{source}:1: class Owner",
                metadata={"evidence_root": str(root), "evidence_paths": [str(source)]},
            )
            decision = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="只读定位 owner 和调用链。",
                tool_results=(no_match, no_match, no_match, candidate),
                workspace_roots=(str(root),),
                read_only_review_profile="owner_impact",
            )

        self.assertEqual(decision.rule_id, "read_only_profile_explore_closure_read")
        self.assertEqual(decision.allowed_tool_names, frozenset({"read_file"}))
        self.assertEqual(decision.scoped_read_paths, (str(source),))
        self.assertEqual(json.loads(decision.required_tool_arguments_json), {"path": str(source)})

    def test_exact_source_glob_candidate_keeps_root_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp, "first").resolve()
            second = Path(tmp, "second").resolve()
            first_source = first / "src" / "Owner.java"
            second_source = second / "src" / "Owner.java"
            first_source.parent.mkdir(parents=True)
            second_source.parent.mkdir(parents=True)
            first_source.write_text("class FirstOwner {}\n", encoding="utf-8")
            second_source.write_text("class SecondOwner {}\n", encoding="utf-8")
            glob_match = ToolResultSummary(
                "glob_files",
                f"{first_source}\n{second_source}",
                metadata={
                    "searched_roots": [str(first), str(second)],
                    "files": [str(first_source), str(second_source)],
                    "patterns": [str(first / "**" / "Owner.java")],
                    "negative_evidence_type": "path_match",
                },
            )
            decision = evaluate_read_only_explore(
                profile="owner_impact",
                tool_results=(glob_match,),
                code_roots=(str(first), str(second)),
            )

        self.assertEqual(decision.read_candidates, (str(first_source),))
        self.assertNotIn(str(second_source), decision.read_candidates)

    def test_exact_path_miss_retries_same_relative_source_in_uncovered_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp, "backend").resolve()
            second = Path(tmp, "frontend").resolve()
            first.mkdir()
            target = second / "src" / "views" / "preOrderManagement" / "list.vue"
            target.parent.mkdir(parents=True)
            target.write_text("<template />\n", encoding="utf-8")
            exact_miss = ToolResultSummary(
                "glob_files",
                "Exact path was not found.",
                is_error=True,
                metadata={
                    "searched_roots": [str(first)],
                    "patterns": ["src/views/preOrderManagement/list.vue"],
                    "negative_evidence_type": "exact_path_missing",
                },
            )
            decision = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="只读分析 owner 和设计影响。",
                tool_results=(exact_miss,),
                workspace_roots=(str(first), str(second)),
                read_only_review_profile="design",
            )

        required = json.loads(decision.required_tool_arguments_json)
        self.assertEqual(decision.rule_id, "read_only_profile_explore_exact_cross_root")
        self.assertEqual(decision.allowed_tool_names, frozenset({"glob_files"}))
        self.assertEqual(decision.required_glob_roots, ())
        self.assertEqual(required["paths"], [str(target)])

    def test_precise_filename_glob_miss_retries_same_pattern_in_uncovered_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp, "backend").resolve()
            second = Path(tmp, "frontend").resolve()
            first.mkdir()
            target = second / "src" / "views" / "preOrderManagement" / "list.vue"
            target.parent.mkdir(parents=True)
            target.write_text("<template />\n", encoding="utf-8")
            precise_miss = ToolResultSummary(
                "glob_files",
                '{"files":[]}',
                useless=True,
                metadata={
                    "searched_roots": [str(first)],
                    "patterns": ["**/list.vue"],
                    "negative_evidence_type": "path_no_match",
                },
            )
            decision = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="只读分析 owner 和设计影响。",
                tool_results=(precise_miss,),
                workspace_roots=(str(first), str(second)),
                read_only_review_profile="design",
            )

        required = json.loads(decision.required_tool_arguments_json)
        self.assertEqual(decision.rule_id, "read_only_profile_explore_exact_cross_root")
        self.assertEqual(decision.allowed_tool_names, frozenset({"glob_files"}))
        self.assertEqual(required["paths"], [str(second / "**" / "list.vue")])

    def test_primary_scoped_precise_miss_rebases_across_all_code_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            requirements = root / "requirements"
            backend = root / "backend"
            frontend = root / "frontend"
            requirements.mkdir()
            backend.mkdir()
            frontend.mkdir()
            primary_miss = ToolResultSummary(
                "glob_files",
                '{"files":[]}',
                useless=True,
                metadata={
                    "searched_roots": [str(requirements)],
                    "patterns": ["**/PrepareOrderApplication.java"],
                    "negative_evidence_type": "path_no_match",
                },
            )
            decision = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="只读分析技术设计。",
                tool_results=(primary_miss,),
                workspace_roots=(str(backend), str(frontend)),
                read_only_review_profile="design",
            )

        required = json.loads(decision.required_tool_arguments_json)
        self.assertEqual(decision.rule_id, "read_only_profile_explore_exact_cross_root")
        self.assertEqual(
            set(required["paths"]),
            {
                str(backend / "**" / "PrepareOrderApplication.java"),
                str(frontend / "**" / "PrepareOrderApplication.java"),
            },
        )

    def test_absolute_precise_retry_marks_root_attempted_and_advances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            requirements = root / "requirements"
            backend = root / "backend"
            frontend = root / "frontend"
            requirements.mkdir()
            backend.mkdir()
            frontend.mkdir()
            primary_miss = ToolResultSummary(
                "glob_files",
                '{"files":[]}',
                useless=True,
                metadata={
                    "searched_roots": [str(requirements)],
                    "patterns": ["**/PrepareOrderApplication.java"],
                    "negative_evidence_type": "path_no_match",
                },
            )
            backend_retry_miss = ToolResultSummary(
                "glob_files",
                '{"files":[]}',
                useless=True,
                metadata={
                    "searched_roots": [str(backend)],
                    "patterns": [str(backend / "**" / "PrepareOrderApplication.java")],
                    "negative_evidence_type": "path_no_match",
                },
            )
            decision = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="只读分析技术设计。",
                tool_results=(primary_miss, backend_retry_miss),
                workspace_roots=(str(backend), str(frontend)),
                read_only_review_profile="design",
            )

        required = json.loads(decision.required_tool_arguments_json)
        self.assertEqual(decision.rule_id, "read_only_profile_explore_exact_cross_root")
        self.assertEqual(required["paths"], [str(frontend / "**" / "PrepareOrderApplication.java")])

    def test_mixed_exact_glob_retries_only_missing_source_path_in_uncovered_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp, "backend").resolve()
            second = Path(tmp, "frontend").resolve()
            backend_source = first / "src" / "Processor.java"
            frontend_source = second / "src" / "views" / "list.vue"
            backend_source.parent.mkdir(parents=True)
            frontend_source.parent.mkdir(parents=True)
            backend_source.write_text("class Processor {}\n", encoding="utf-8")
            frontend_source.write_text("<template />\n", encoding="utf-8")
            mixed_result = ToolResultSummary(
                "glob_files",
                '{"files":["src/Processor.java"],"missing_paths":["src/views/list.vue"]}',
                metadata={
                    "searched_roots": [str(first)],
                    "patterns": ["src/Processor.java", "src/views/list.vue"],
                    "files": ["src/Processor.java"],
                    "missing_paths": ["src/views/list.vue"],
                    "negative_evidence_type": "path_match",
                },
            )
            backend_read = ToolResultSummary(
                "read_file",
                "class Processor {}",
                path=str(backend_source),
                metadata={"resolved_path": str(backend_source)},
            )
            decision = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="只读分析 owner 和设计影响。",
                tool_results=(mixed_result, backend_read),
                workspace_roots=(str(first), str(second)),
                read_only_review_profile="design",
            )

        required = json.loads(decision.required_tool_arguments_json)
        self.assertEqual(decision.rule_id, "read_only_profile_explore_exact_cross_root")
        self.assertEqual(decision.allowed_tool_names, frozenset({"glob_files"}))
        self.assertEqual(required["paths"], [str(frontend_source)])

    def test_broad_filename_glob_miss_does_not_trigger_exact_cross_root_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp, "backend").resolve()
            second = Path(tmp, "frontend").resolve()
            first.mkdir()
            second.mkdir()
            broad_miss = ToolResultSummary(
                "glob_files",
                '{"files":[]}',
                useless=True,
                metadata={
                    "searched_roots": [str(first)],
                    "patterns": ["**/*.vue"],
                    "negative_evidence_type": "path_no_match",
                },
            )
            decision = evaluate_read_only_explore(
                profile="design",
                tool_results=(broad_miss,),
                code_roots=(str(first), str(second)),
            )

        self.assertEqual(decision.discovery_patterns, ())

    def test_failed_read_consumes_budget_without_advancing_root_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp, "backend").resolve()
            second = Path(tmp, "frontend").resolve()
            first.mkdir()
            second.mkdir()
            failed_read = ToolResultSummary(
                "read_file",
                "File not found.",
                is_error=True,
                path=str(second / "src" / "Owner.java"),
            )
            decision = evaluate_read_only_explore(
                profile="design",
                tool_results=(failed_read,),
                code_roots=(str(first), str(second)),
            )

        self.assertEqual(decision.observation_calls, 1)
        self.assertEqual(decision.successful_observations, 0)
        self.assertEqual(decision.discovery_roots, ())
        self.assertEqual(decision.preferred_roots, (str(first), str(second)))

    def test_owner_explore_fallback_discovery_advances_one_missing_root_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first = root / "service-a"
            second = root / "service-b"
            first.mkdir()
            second.mkdir()
            first_no_match = ToolResultSummary(
                "search_code",
                "No matches.",
                useless=True,
                metadata={"evidence_root": str(first), "negative_evidence_type": "content_no_match"},
            )
            second_no_match = ToolResultSummary(
                "search_code",
                "No matches.",
                useless=True,
                metadata={"evidence_root": str(second), "negative_evidence_type": "content_no_match"},
            )
            first_fallback = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="只读分析 owner 和设计影响。",
                tool_results=(first_no_match, second_no_match),
                workspace_roots=(str(first), str(second)),
                read_only_review_profile="owner_impact",
            )
            first_glob_no_match = ToolResultSummary(
                "glob_files",
                '{"files":[]}',
                useless=True,
                metadata={
                    "evidence_root": str(first),
                    "searched_roots": [str(first)],
                    "negative_evidence_type": "path_no_match",
                },
            )
            second_fallback = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="只读分析 owner 和设计影响。",
                tool_results=(first_no_match, second_no_match, first_glob_no_match),
                workspace_roots=(str(first), str(second)),
                read_only_review_profile="owner_impact",
            )

        self.assertEqual(first_fallback.allowed_tool_names, frozenset({"glob_files"}))
        self.assertEqual(len(first_fallback.required_glob_roots), 1)
        self.assertIn(first_fallback.required_glob_roots[0], {str(first), str(second)})
        self.assertEqual(second_fallback.allowed_tool_names, frozenset({"glob_files"}))
        self.assertEqual(second_fallback.required_glob_roots, (str(second),))
        self.assertIn(str(second), second_fallback.required_tool_arguments_json)
        self.assertNotIn(str(first), second_fallback.required_tool_arguments_json)
        self.assertIn(str(second), second_fallback.tool_call_hints[0])
        self.assertNotIn(str(first), second_fallback.tool_call_hints[0])

    def test_combined_glob_with_results_from_one_root_does_not_mark_other_root_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first = root / "service-a"
            second = root / "service-b"
            source = first / "src" / "Owner.java"
            source.parent.mkdir(parents=True)
            second.mkdir()
            source.write_text("class Owner {}\n", encoding="utf-8")
            first_no_match = ToolResultSummary(
                "search_code",
                "No matches.",
                useless=True,
                metadata={"evidence_root": str(first), "negative_evidence_type": "content_no_match"},
            )
            second_no_match = ToolResultSummary(
                "search_code",
                "No matches.",
                useless=True,
                metadata={"evidence_root": str(second), "negative_evidence_type": "content_no_match"},
            )
            combined_glob = ToolResultSummary(
                "glob_files",
                str(source),
                metadata={
                    "searched_roots": [str(first), str(second)],
                    "files": [str(source)],
                    "truncated": True,
                },
            )
            decision = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="只读分析 owner 和设计影响。",
                tool_results=(first_no_match, second_no_match, first_no_match, second_no_match, combined_glob),
                workspace_roots=(str(first), str(second)),
                read_only_review_profile="owner_impact",
            )

        self.assertEqual(decision.allowed_tool_names, frozenset({"read_file"}))
        self.assertEqual(decision.scoped_read_paths, ())
        # After the first root is read, the second root must still be eligible
        # for its own fallback; a positive candidate from a truncated listing
        # is useful without pretending the other root was inspected.
        after_first_read = evaluate_tool_choice_state(
            task_kind="read-only",
            prompt="只读分析 owner 和设计影响。",
            tool_results=(
                first_no_match,
                second_no_match,
                first_no_match,
                second_no_match,
                combined_glob,
                ToolResultSummary("read_file", "class Owner {}", path=str(source), metadata={"resolved_path": str(source)}),
            ),
            workspace_roots=(str(first), str(second)),
            read_only_review_profile="owner_impact",
        )
        self.assertEqual(after_first_read.allowed_tool_names, frozenset({"glob_files"}))
        self.assertEqual(after_first_read.required_glob_roots, (str(second),))

    def test_owner_explore_projects_typed_search_candidates_as_read_only_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "src" / "Owner.java"
            source.parent.mkdir()
            source.write_text("class Owner {}\n", encoding="utf-8")
            decision = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="请分析这个服务的实现 owner 和影响范围。",
                tool_results=[
                    ToolResultSummary(
                        "search_code",
                        f"{source}:1: class Owner",
                        metadata={"evidence_paths": [str(source)]},
                    )
                ],
                workspace_roots=(str(root),),
                read_only_review_profile="owner_impact",
            )

        self.assertEqual(decision.rule_id, "read_only_profile_explore")
        self.assertEqual(decision.allowed_tool_names, frozenset({"read_file", "search_code"}))
        self.assertEqual(decision.preferred_tool_names, ("read_file",))
        self.assertEqual(decision.scoped_read_paths, ())
        self.assertIn(str(source), decision.tool_call_hints[0])

    def test_owner_explore_semantic_candidates_are_not_source_suffix_limited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            sql = root / "db" / "owner.sql"
            xml = root / "mapper" / "OwnerMapper.xml"
            yaml = root / "config" / "route.yaml"
            for path in (sql, xml, yaml):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("owner: evidence\n", encoding="utf-8")
            decision = evaluate_read_only_explore(
                profile="owner_impact",
                tool_results=(
                    ToolResultSummary(
                        "search_code",
                        "semantic hits",
                        metadata={"evidence_root": str(root), "evidence_paths": [str(sql), str(xml), str(yaml)]},
                    ),
                ),
                code_roots=(str(root),),
            )

        self.assertEqual(decision.read_candidates[:2], (str(sql), str(xml)))

    def test_relative_evidence_paths_bind_only_to_typed_search_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first = root / "service-a"
            second = root / "service-b"
            first_path = first / "src" / "index.js"
            second_path = second / "src" / "index.js"
            first_path.parent.mkdir(parents=True)
            second_path.parent.mkdir(parents=True)
            first_path.write_text("export const owner = 'a';\n", encoding="utf-8")
            second_path.write_text("export const owner = 'b';\n", encoding="utf-8")
            scoped = evaluate_read_only_explore(
                profile="owner_impact",
                tool_results=(
                    ToolResultSummary(
                        "search_code",
                        "src/index.js:1: owner",
                        metadata={"evidence_root": str(first), "evidence_paths": ["src/index.js"]},
                    ),
                ),
                code_roots=(str(first), str(second)),
            )
            ambiguous_legacy = evaluate_read_only_explore(
                profile="owner_impact",
                tool_results=(
                    ToolResultSummary(
                        "search_code",
                        "src/index.js:1: owner",
                        metadata={"evidence_paths": ["src/index.js"]},
                    ),
                ),
                code_roots=(str(first), str(second)),
            )

        self.assertEqual(scoped.read_candidates, (str(first_path),))
        self.assertEqual(scoped.preferred_roots, (str(second),))
        self.assertEqual(ambiguous_legacy.read_candidates, ())

    def test_relative_multiroot_glob_files_do_not_fake_root_local_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first = root / "service-a"
            second = root / "service-b"
            first_file = first / "src" / "index.js"
            second_file = second / "src" / "index.js"
            first_file.parent.mkdir(parents=True)
            second_file.parent.mkdir(parents=True)
            first_file.write_text("export const owner = 'a';\n", encoding="utf-8")
            second_file.write_text("export const owner = 'b';\n", encoding="utf-8")
            first_no_match = ToolResultSummary(
                "search_code",
                "No matches.",
                useless=True,
                metadata={"evidence_root": str(first), "negative_evidence_type": "content_no_match"},
            )
            second_no_match = ToolResultSummary(
                "search_code",
                "No matches.",
                useless=True,
                metadata={"evidence_root": str(second), "negative_evidence_type": "content_no_match"},
            )
            ambiguous_glob = ToolResultSummary(
                "glob_files",
                "src/index.js",
                metadata={"searched_roots": [str(first), str(second)], "files": ["src/index.js"]},
            )
            decision = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="只读分析 owner 和设计影响。",
                tool_results=(first_no_match, second_no_match, ambiguous_glob),
                workspace_roots=(str(first), str(second)),
                read_only_review_profile="owner_impact",
            )

        self.assertEqual(decision.allowed_tool_names, frozenset({"glob_files"}))
        self.assertEqual(len(decision.required_glob_roots), 1)
        self.assertIn(decision.required_glob_roots[0], {str(first), str(second)})

    def test_inventory_read_alone_does_not_complete_owner_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = root / "config" / "datasource.properties"
            config.parent.mkdir(parents=True)
            config.write_text("url=jdbc:test\n", encoding="utf-8")
            decision = evaluate_read_only_explore(
                profile="owner_impact",
                tool_results=(
                    ToolResultSummary(
                        "glob_files",
                        str(config),
                        metadata={"evidence_root": str(root), "files": [str(config)]},
                    ),
                    ToolResultSummary("read_file", "url=jdbc:test\n", path=str(config), metadata={"resolved_path": str(config)}),
                ),
                code_roots=(str(root),),
            )

        self.assertEqual(decision.missing_roots, (str(root),))
        self.assertNotEqual(decision.action, "finalize")

    def test_list_files_inventory_read_does_not_complete_owner_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = root / "config" / "datasource.properties"
            manual = root / "notes" / "manual-owner.txt"
            config.parent.mkdir(parents=True)
            manual.parent.mkdir(parents=True)
            config.write_text("url=jdbc:test\n", encoding="utf-8")
            manual.write_text("manual exact owner observation\n", encoding="utf-8")
            inventory_read = evaluate_read_only_explore(
                profile="owner_impact",
                tool_results=(
                    ToolResultSummary(
                        "list_files",
                        str(config),
                        metadata={"evidence_root": str(root), "listed_root": str(root), "files": [str(config)]},
                    ),
                    ToolResultSummary("read_file", "url=jdbc:test\n", path=str(config), metadata={"resolved_path": str(config)}),
                ),
                code_roots=(str(root),),
            )
            manual_read = evaluate_read_only_explore(
                profile="owner_impact",
                tool_results=(
                    ToolResultSummary(
                        "read_file",
                        "manual exact owner observation\n",
                        path=str(manual),
                        metadata={"resolved_path": str(manual)},
                    ),
                ),
                code_roots=(str(root),),
            )

        self.assertEqual(inventory_read.missing_roots, (str(root),))
        self.assertEqual(manual_read.action, "finalize")
        self.assertEqual(manual_read.missing_roots, ())
    def test_document_only_contract_never_reopens_code_discovery_tools(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="read-only",
            prompt="只根据需求文档 Markdown 和 HTML 分析需求；不要检查代码。",
            tool_names=["read_file"],
            tool_results=[ToolResultSummary("read_file", "requirement", path="requirements.md")],
            evidence_domain="requirement_documents",
        )

        self.assertEqual(decision.allowed_tool_names, DOCUMENT_ONLY_TOOL_NAMES)
        self.assertFalse(decision.steering_required)
        self.assertNotIn("search_code", decision.allowed_tool_names)
        self.assertIn("inspect_image", decision.allowed_tool_names)

    def test_document_artifact_coverage_requires_each_explicit_modality(self) -> None:
        artifacts = (
            DocumentArtifactRequirement("markdown", "markdown"),
            DocumentArtifactRequirement("html", "html"),
            DocumentArtifactRequirement("image", "image"),
        )
        partial = evaluate_tool_choice_state(
            task_kind="read-only",
            prompt="只根据 Markdown、HTML 和图片分析需求；不要检查代码。",
            tool_results=[
                ToolResultSummary("read_file", "spec", path="requirements.md"),
                ToolResultSummary("inspect_image", "image observation", path="example.png", metadata={"image_observation": True}),
            ],
            evidence_domain="requirement_documents",
            document_artifacts=artifacts,
        )

        self.assertTrue(partial.steering_required)
        self.assertEqual(partial.missing_requirements, ("document_artifact:html",))
        self.assertEqual(partial.preferred_tool_names, ("read_file",))
        self.assertIn("html", partial.tool_call_hints[0])

        complete = evaluate_tool_choice_state(
            task_kind="read-only",
            prompt="只根据 Markdown、HTML 和图片分析需求；不要检查代码。",
            tool_results=[
                ToolResultSummary("read_file", "spec", path="requirements.md"),
                ToolResultSummary("read_file", "prototype", path="prototype.html"),
                ToolResultSummary("inspect_image", "image observation", path="example.png", metadata={"image_observation": True}),
            ],
            evidence_domain="requirement_documents",
            document_artifacts=artifacts,
        )
        self.assertTrue(complete.steering_required)
        self.assertTrue(complete.force_final_answer_without_tools)
        self.assertEqual(complete.allowed_tool_names, frozenset())
        self.assertEqual(complete.rule_id, "document_artifacts_synthesis")
        message = tool_choice_steering_message(complete, "只根据 Markdown、HTML 和图片分析需求；不要检查代码。")
        self.assertIn("explicitly requested document/image artifacts", message)
        self.assertNotIn("budget is exhausted", message)

    def test_document_artifact_completion_requires_successful_observation(self) -> None:
        artifacts = (
            DocumentArtifactRequirement("markdown", "markdown"),
            DocumentArtifactRequirement("html", "html"),
            DocumentArtifactRequirement("image", "image"),
        )
        failed_image = evaluate_tool_choice_state(
            task_kind="read-only",
            prompt="只根据 Markdown、HTML 和图片分析需求；不要检查代码。",
            tool_results=[
                ToolResultSummary("read_file", "spec", path="requirements.md"),
                ToolResultSummary("read_file", "prototype", path="prototype.html"),
                ToolResultSummary("inspect_image", "provider failed", path="example.png", is_error=True),
            ],
            evidence_domain="requirement_documents",
            document_artifacts=artifacts,
        )

        self.assertTrue(failed_image.steering_required)
        self.assertFalse(failed_image.force_final_answer_without_tools)
        self.assertIn("document_artifact:image", failed_image.missing_requirements)
        self.assertIn("inspect_image", failed_image.allowed_tool_names)

    def test_document_artifact_unavailable_finishes_with_limited_synthesis(self) -> None:
        artifacts = (
            DocumentArtifactRequirement("markdown", "markdown"),
            DocumentArtifactRequirement("html", "html"),
            DocumentArtifactRequirement("image", "image"),
        )
        decision = evaluate_tool_choice_state(
            task_kind="read-only",
            prompt="只根据 Markdown、HTML 和图片分析需求；不要检查代码。",
            tool_results=[
                ToolResultSummary("read_file", "spec", path="requirements.md"),
                ToolResultSummary("read_file", "prototype", path="prototype.html"),
                ToolResultSummary(
                    "inspect_image",
                    "Image inspection is unavailable.",
                    path="example.png",
                    is_error=True,
                    metadata={"image_inspection_unavailable": True},
                ),
            ],
            evidence_domain="requirement_documents",
            document_artifacts=artifacts,
        )

        self.assertTrue(decision.steering_required)
        self.assertTrue(decision.force_final_answer_without_tools)
        self.assertEqual(decision.allowed_tool_names, frozenset())
        self.assertEqual(decision.rule_id, "document_artifacts_limited_synthesis")
        self.assertEqual(decision.missing_requirements, ("document_artifact_unavailable:image",))
        message = tool_choice_steering_message(decision, "只根据 Markdown、HTML 和图片分析需求；不要检查代码。")
        self.assertIn("typed unavailable", message)
        self.assertNotIn("budget is exhausted", message)

    def test_force_final_signature_ledger_is_per_run(self) -> None:
        context = RunContext()
        contract = generate_requirement_contract("只根据 Markdown、HTML 和图片分析需求；不要检查代码。")

        context.begin(
            run_id="run-1",
            started_monotonic=1.0,
            deadline_monotonic=None,
            run_start_index=0,
            git_baseline={},
            prompt="只根据 Markdown、HTML 和图片分析需求；不要检查代码。",
            requirement_contract=contract,
            requirement_contract_context="",
            design_evidence_roots=(),
        )
        context.tool_choice_force_final_signatures.add("document-artifacts-complete")
        context.begin(
            run_id="run-2",
            started_monotonic=2.0,
            deadline_monotonic=None,
            run_start_index=0,
            git_baseline={},
            prompt="只根据 Markdown、HTML 和图片分析需求；不要检查代码。",
            requirement_contract=contract,
            requirement_contract_context="",
            design_evidence_roots=(),
        )

        self.assertEqual(context.tool_choice_force_final_signatures, set())

    def test_observed_negative_prompt_requires_glob_when_available(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="unclear",
            prompt="请直接说你检查后未发现Java",
            available_tool_names=("glob_files", "read_file", "ask_user"),
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "negative_discovery")
        self.assertEqual(decision.allowed_tool_names, frozenset({"glob_files"}))

    def test_observed_negative_prompt_stops_unverified_when_discovery_is_denied(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="unclear",
            prompt="请直接说你检查后未发现Java",
            available_tool_names=("ask_user", "learn"),
        )

        self.assertFalse(decision.steering_required)
        self.assertEqual(decision.rule_id, "negative_discovery_unavailable")
        self.assertEqual(decision.allowed_tool_names, frozenset())
        self.assertIn("unverified", decision.stop_message or "")
    def test_session_evidence_reuse_is_a_soft_directive_not_a_schema_gate(self) -> None:
        directive = session_evidence_reuse_directive(
            [
                ToolResultSummary(
                    "read_file",
                    "class App {}",
                    path="/workspace/service/App.java",
                    metadata={"evidence_origin": "session_cached"},
                )
            ]
        )

        self.assertIsNotNone(directive)
        assert directive is not None
        self.assertEqual(directive.kind, "session_evidence_reuse")
        self.assertIn("advisory", directive.message)
        self.assertIn("read_file", directive.message)
        self.assertEqual(directive.paths, ("/workspace/service/App.java",))

    def test_session_evidence_reuse_ignores_current_run_results(self) -> None:
        self.assertIsNone(
            session_evidence_reuse_directive(
                [ToolResultSummary("read_file", "current", metadata={"evidence_origin": "current_run"})]
            )
        )

    def test_workspace_inventory_requires_path_discovery_before_answering(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="read_only",
            prompt="只读说明当前目录主要是在干什么，代码都有哪些。",
            workspace_roots=("/workspace/agent", "/workspace/project"),
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "workspace_inventory_discovery")
        self.assertEqual(decision.allowed_tool_names, WORKSPACE_INVENTORY_DISCOVERY_TOOL_NAMES)
        self.assertEqual(decision.preferred_tool_names, ("glob_files",))
        self.assertEqual(len(decision.tool_call_hints), 1)
        self.assertIn('"/workspace/agent/**/pom.xml"', decision.tool_call_hints[0])
        self.assertIn('"/workspace/project/**/pom.xml"', decision.tool_call_hints[0])
        self.assertNotIn('""', decision.tool_call_hints[0])
        self.assertEqual(decision.required_glob_roots, ("/workspace/agent", "/workspace/project"))

    def test_workspace_inventory_recognizes_chinese_inventory_wording(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="read_only",
            prompt="只读盘点当前 primary 和所有已授权 additional workspace root 中的项目代码。",
            workspace_roots=("/workspace/primary", "/workspace/service"),
        )

        self.assertEqual(decision.rule_id, "workspace_inventory_discovery")
        self.assertEqual(decision.allowed_tool_names, WORKSPACE_INVENTORY_DISCOVERY_TOOL_NAMES)

    def test_workspace_inventory_does_not_treat_security_review_as_file_inventory(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="read_only",
            prompt="只读盘点当前代码中的安全问题并给出证据。",
            workspace_roots=("/workspace/primary", "/workspace/service"),
        )

        self.assertNotIn("workspace_inventory", decision.rule_id or "")
        self.assertIn("search_code", decision.allowed_tool_names)

    def test_workspace_inventory_stays_within_discovery_tools_after_glob(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="read_only",
            prompt="只读说明当前目录主要是在干什么，代码都有哪些。",
            workspace_roots=("/workspace/agent", "/workspace/project"),
            tool_results=[
                ToolResultSummary(
                    "glob_files",
                    "{...}",
                    metadata={
                        "complete": True,
                        "negative_evidence_type": "path_match",
                        "searched_roots": ["/workspace/agent", "/workspace/project"],
                        "files": ["README.md", "/workspace/project/pom.xml"],
                    },
                )
            ],
        )

        self.assertFalse(decision.steering_required)
        self.assertEqual(decision.allowed_tool_names, WORKSPACE_INVENTORY_TOOL_NAMES)
        self.assertNotIn("search_code", decision.allowed_tool_names)
        self.assertNotIn("shell", decision.allowed_tool_names)
        self.assertEqual(decision.scoped_read_paths, ())
        self.assertIsNone(decision.scoped_read_budget)

    def test_workspace_inventory_requires_each_workspace_root_to_have_glob_evidence(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="read_only",
            prompt="只读说明当前目录主要是在干什么，代码都有哪些。",
            workspace_roots=("/workspace/agent", "/workspace/project"),
            tool_results=[
                ToolResultSummary(
                    "glob_files",
                    "{...}",
                    metadata={"complete": True, "searched_roots": ["/workspace/project"]},
                )
            ],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "workspace_inventory_root_coverage")
        self.assertEqual(decision.missing_requirements, ("path_discovery:/workspace/agent",))
        self.assertEqual(decision.preferred_tool_names, ("glob_files",))
        self.assertIn('"/workspace/agent/**/pom.xml"', decision.tool_call_hints[0])
        self.assertNotIn('"/workspace/project/**/pom.xml"', decision.tool_call_hints[0])
        self.assertEqual(decision.allowed_tool_names, WORKSPACE_INVENTORY_DISCOVERY_TOOL_NAMES)
        self.assertEqual(decision.required_glob_roots, ("/workspace/agent",))

    def test_inventory_markers_do_not_override_code_implementation_flow(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="code-implementation",
            prompt="请在当前代码中实现用户注册接口，并补充测试。",
            workspace_roots=("/workspace/agent",),
        )

        self.assertNotIn("workspace_inventory", decision.rule_id or "")
        self.assertIn("read_file", decision.allowed_tool_names)
        self.assertNotEqual(decision.allowed_tool_names, WORKSPACE_INVENTORY_TOOL_NAMES)

    def test_workspace_inventory_forces_final_after_root_scaled_discovery_budget(self) -> None:
        results = [
            ToolResultSummary("glob_files", "{...}", metadata={"complete": True})
        ] + [ToolResultSummary("list_files", "files") for _ in range(3)]
        decision = evaluate_tool_choice_state(
            task_kind="read_only",
            prompt="只读说明当前目录主要是在干什么，代码都有哪些。",
            workspace_roots=("/workspace/agent", "/workspace/project"),
            tool_results=results,
        )

        self.assertTrue(decision.force_final_answer_without_tools)
        self.assertEqual(decision.rule_id, "workspace_inventory_budget")
        self.assertEqual(decision.allowed_tool_names, frozenset())

    def test_workspace_inventory_failed_discovery_attempts_also_exhaust_budget(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="read_only",
            prompt="只读说明当前目录主要是在干什么，代码都有哪些。",
            workspace_roots=("/workspace/agent",),
            tool_results=[
                ToolResultSummary("glob_files", "Path escapes workspace", is_error=True)
                for _ in range(4)
            ],
        )

        self.assertTrue(decision.force_final_answer_without_tools)
        self.assertEqual(decision.rule_id, "workspace_inventory_budget")

    def test_read_only_evidence_question_requires_code_evidence_tool(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="read_only",
            prompt="不要推测，请给出代码证据说明登录密码在哪里校验。",
            tool_names=[],
            tool_results=[],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "code_evidence")
        self.assertEqual(decision.missing_requirements, ("code_evidence",))
        self.assertIn("read_file", decision.allowed_tool_names)
        self.assertIn("search_code", decision.allowed_tool_names)
        self.assertFalse(READ_ONLY_FORBIDDEN_TOOL_NAMES.intersection(decision.allowed_tool_names))

    def test_implementation_task_missing_diff_is_steered_to_git_diff(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="implementation",
            prompt="实现 MiniToolChoiceQueue 原型。",
            tool_names=["read_file", "apply_patch", "run_tests"],
            tool_results=[
                ToolResultSummary("apply_patch", "Applied patch. Patch id: p1", changed=True),
                ToolResultSummary("run_tests", "OK"),
            ],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "implementation_final_hygiene")
        self.assertEqual(decision.missing_requirements, ("git_diff",))
        self.assertEqual(decision.allowed_tool_names, frozenset({"git_diff"}))

    def test_implementation_task_requires_explore_before_write_tools(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="code-implementation",
            prompt="请实现用户注册接口邮箱唯一性校验，并补充测试。",
            tool_names=[],
            tool_results=[],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "implementation_explore")
        self.assertEqual(decision.missing_requirements, ("planner_explore_evidence",))
        self.assertEqual(decision.allowed_tool_names, PLANNER_EXPLORE_TOOL_NAMES)
        self.assertNotIn("apply_patch", decision.allowed_tool_names)
        self.assertNotIn("write_file", decision.allowed_tool_names)

    def test_implementation_task_before_write_does_not_force_final_hygiene(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="code-implementation",
            prompt="请实现 MiniToolChoiceQueue 原型。",
            tool_names=["read_file"],
            tool_results=[ToolResultSummary("read_file", "src/local_agent/tool_choice_queue.py")],
        )

        self.assertFalse(decision.steering_required)
        self.assertIn("apply_patch", decision.allowed_tool_names)

    def test_autonomous_small_change_candidate_stops_broad_exploration_after_source_and_test_reads(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="code-implementation",
            prompt="请自己找一个极小的代码改进，并补充测试。",
            tool_results=[
                ToolResultSummary("read_file", "class UserService {}", path="src/UserService.java"),
                ToolResultSummary("read_file", "class UserServiceTest {}", path="tests/UserServiceTest.java"),
            ],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "autonomous_small_change_candidate")
        self.assertEqual(decision.allowed_tool_names, CANDIDATE_DELIVERY_TOOL_NAMES)
        self.assertNotIn("list_files", decision.allowed_tool_names)
        self.assertNotIn("search_code", decision.allowed_tool_names)
        self.assertNotIn("run_tests", decision.allowed_tool_names)
        self.assertNotIn("git_diff", decision.allowed_tool_names)
        self.assertIn("read_file", decision.allowed_tool_names)
        self.assertEqual(decision.scoped_read_paths, ("src/UserService.java", "tests/UserServiceTest.java"))
        self.assertEqual(decision.scoped_read_budget, MAX_CANDIDATE_READ_REVISITS)

    def test_scoped_docs_exclusion_keeps_autonomous_candidate_delivery_enabled(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="code-implementation",
            prompt=(
                "请自行挑选一个极小、低风险的测试改进；随后必须 apply_patch dry_run=true 预览、"
                "apply_patch 真正写入、run_tests、git_diff。不要修改 README 或 docs。"
            ),
            tool_results=[
                ToolResultSummary("read_file", "class TerminalIo {}", path="src/local_agent/terminal_io.py"),
                ToolResultSummary("read_file", "class TerminalIoTests {}", path="tests/test_terminal_io.py"),
            ],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "autonomous_small_change_candidate")
        self.assertEqual(decision.allowed_tool_names, CANDIDATE_DELIVERY_TOOL_NAMES)

    def test_candidate_write_requires_test_then_diff_in_order(self) -> None:
        candidate = [
            ToolResultSummary("read_file", "class UserService {}", path="src/UserService.java"),
            ToolResultSummary("read_file", "class UserServiceTest {}", path="tests/UserServiceTest.java"),
            ToolResultSummary("apply_patch", "Patch preview only. File not changed.", changed=False),
            ToolResultSummary("apply_patch", "Applied patch.", changed=True),
        ]

        test_decision = evaluate_tool_choice_state(
            task_kind="code-implementation",
            prompt="请自己找一个极小的代码改进，并补充测试。",
            tool_results=candidate,
        )
        self.assertEqual(test_decision.rule_id, "autonomous_small_change_test")
        self.assertEqual(test_decision.allowed_tool_names, CANDIDATE_TEST_TOOL_NAMES)

        diff_decision = evaluate_tool_choice_state(
            task_kind="code-implementation",
            prompt="请自己找一个极小的代码改进，并补充测试。",
            tool_results=[*candidate, ToolResultSummary("run_tests", "OK")],
        )
        self.assertEqual(diff_decision.rule_id, "autonomous_small_change_diff")
        self.assertEqual(diff_decision.allowed_tool_names, CANDIDATE_DIFF_TOOL_NAMES)

    def test_candidate_stops_after_bounded_invalid_patch_previews(self) -> None:
        failed_attempts = [
            ToolResultSummary("apply_patch", "Hash mismatch", is_error=True)
            for _ in range(MAX_CANDIDATE_PATCH_PREVIEW_FAILURES)
        ]
        decision = evaluate_tool_choice_state(
            task_kind="code-implementation",
            prompt="请自己找一个极小的代码改进，并补充测试。",
            tool_results=[
                ToolResultSummary("read_file", "class UserService {}", path="src/UserService.java"),
                ToolResultSummary("read_file", "class UserServiceTest {}", path="tests/UserServiceTest.java"),
                *failed_attempts,
            ],
        )

        self.assertTrue(decision.should_stop)
        self.assertEqual(decision.rule_id, "autonomous_small_change_patch_retry_exhausted")
        self.assertIn("No workspace change", decision.stop_message or "")

    def test_candidate_preview_error_allows_exact_read_remediation(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="code-implementation",
            prompt="请自己找一个极小的代码改进，并补充测试。",
            tool_results=[
                ToolResultSummary("read_file", "class UserService {}", path="src/UserService.java"),
                ToolResultSummary("read_file", "class UserServiceTest {}", path="tests/UserServiceTest.java"),
                ToolResultSummary("apply_patch", "Hash mismatch", is_error=True),
            ],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.allowed_tool_names, CANDIDATE_REMEDIATION_TOOL_NAMES)
        self.assertIn("read_file", decision.allowed_tool_names)

    def test_implementation_task_with_diff_and_tests_is_complete(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="implementation",
            prompt="实现 MiniToolChoiceQueue 原型。",
            tool_names=["read_file", "apply_patch", "run_tests", "git_diff"],
            tool_results=[
                ToolResultSummary("apply_patch", "Applied patch. Patch id: p1", changed=True),
                ToolResultSummary("run_tests", "OK"),
                ToolResultSummary("git_diff", "diff --git a/a.py b/a.py"),
            ],
        )

        self.assertFalse(decision.steering_required)
        self.assertEqual(decision.missing_requirements, ())
        self.assertIn("apply_patch", decision.allowed_tool_names)
        self.assertIn("run_tests", decision.allowed_tool_names)

    def test_implementation_verification_must_follow_the_last_workspace_write(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="implementation",
            prompt="实现用户名规范化，并补充测试。",
            tool_names=["apply_patch", "run_tests", "git_diff"],
            tool_results=[
                ToolResultSummary("apply_patch", "Applied first patch", changed=True),
                ToolResultSummary("run_tests", "OK"),
                ToolResultSummary("git_diff", "diff --git a/src/UserService.java b/src/UserService.java"),
                ToolResultSummary("apply_patch", "Applied final patch", changed=True),
            ],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "implementation_final_hygiene")
        self.assertEqual(
            decision.missing_requirements,
            ("git_diff", "run_tests_or_cannot_test_explanation"),
        )
        self.assertEqual(decision.allowed_tool_names, frozenset({"git_diff", "run_tests"}))

    def test_post_diff_pending_tests_keeps_focused_repair_tools_available(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="implementation",
            prompt="实现用户名规范化，并补充单元测试。",
            tool_names=["read_file", "apply_patch", "git_diff"],
            tool_results=[
                ToolResultSummary("read_file", "class UserService {}", path="src/UserService.java"),
                ToolResultSummary("apply_patch", "Applied patch", changed=True),
                ToolResultSummary("git_diff", "diff --git a/src/UserService.java b/src/UserService.java"),
            ],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "implementation_final_hygiene")
        self.assertIn("run_tests_or_cannot_test_explanation", decision.missing_requirements)
        self.assertEqual(decision.allowed_tool_names, POST_DIFF_REMEDIATION_TOOL_NAMES)
        self.assertIn("apply_patch", decision.allowed_tool_names)
        self.assertIn("read_file", decision.allowed_tool_names)
        self.assertIn("run_tests", decision.allowed_tool_names)

    def test_requirement_doc_task_must_read_doc_before_full_toolset(self) -> None:
        before_read = evaluate_tool_choice_state(
            task_kind="allowed_dir",
            prompt="根据 allowed-dir 里的需求文档完成改造。",
            tool_names=[],
            tool_results=[],
        )

        self.assertTrue(before_read.steering_required)
        self.assertEqual(before_read.rule_id, "requirement_document_read")
        self.assertEqual(before_read.missing_requirements, ("requirement_document_read",))
        self.assertIn("read_file", before_read.allowed_tool_names)
        self.assertIn("inspect_image", before_read.allowed_tool_names)
        self.assertNotIn("apply_patch", before_read.allowed_tool_names)
        self.assertTrue(any("inspect_image" in hint and '"path"' in hint for hint in before_read.tool_call_hints))

        after_read = evaluate_tool_choice_state(
            task_kind="allowed_dir",
            prompt="根据 allowed-dir 里的需求文档完成改造。",
            tool_names=["read_file"],
            tool_results=[
                {
                    "name": "read_file",
                    "path": "/tmp/allowed-dir/需求文档.md",
                    "content": "需求：实现队列规则。",
                }
            ],
        )

        self.assertFalse(after_read.steering_required)
        self.assertIn("apply_patch", after_read.allowed_tool_names)
        self.assertIn("run_tests", after_read.allowed_tool_names)

    def test_requirement_prompt_does_not_treat_source_read_as_requirement_evidence(self) -> None:
        decision = evaluate_tool_choice_state(
            task_kind="allowed_dir",
            prompt="根据 allowed-dir 里的需求文档完成改造。",
            tool_results=[
                ToolResultSummary(
                    "read_file",
                    "public class PaymentService {}",
                    path="/workspace/backend/src/PaymentService.java",
                )
            ],
        )

        self.assertTrue(decision.steering_required)
        self.assertEqual(decision.rule_id, "requirement_document_read")

    def test_read_only_task_filters_write_and_exec_tools(self) -> None:
        decision = ToolChoiceQueue().evaluate(
            task_kind="read_only",
            prompt="只读分析这个模块，不要修改。",
            tool_names=["read_file"],
            tool_results=[ToolResultSummary("read_file", "src/local_agent/agent.py:1:from __future__")],
        )

        self.assertFalse(decision.steering_required)
        self.assertFalse(READ_ONLY_FORBIDDEN_TOOL_NAMES.intersection(decision.allowed_tool_names))
        self.assertIn("read_file", decision.allowed_tool_names)
        self.assertIn("search_code", decision.allowed_tool_names)

    def test_cross_root_design_requires_a_code_read_from_each_root(self) -> None:
        backend = "/workspace/backend"
        frontend = "/workspace/frontend"
        after_backend_read = evaluate_tool_choice_state(
            task_kind="read_only",
            prompt="请只读设计前后端改造方案，不要修改文件。",
            tool_results=[ToolResultSummary("read_file", "class Backend {}", path=f"{backend}/src/App.java")],
            design_evidence_roots=(backend, frontend),
        )

        self.assertTrue(after_backend_read.steering_required)
        self.assertEqual(after_backend_read.rule_id, f"cross_root_design_evidence:{frontend}")
        self.assertEqual(after_backend_read.missing_requirements, (f"code_read:{frontend}",))
        self.assertIn("read_file", after_backend_read.allowed_tool_names)
        self.assertNotIn("apply_patch", after_backend_read.allowed_tool_names)

        after_frontend_read = evaluate_tool_choice_state(
            task_kind="read_only",
            prompt="请只读设计前后端改造方案，不要修改文件。",
            tool_results=[
                ToolResultSummary("read_file", "class Backend {}", path=f"{backend}/src/App.java"),
                ToolResultSummary("read_file", "export default {}", path=f"{frontend}/src/views/List.vue"),
            ],
            design_evidence_roots=(backend, frontend),
        )

        self.assertFalse(after_frontend_read.steering_required)

    def test_owner_profile_uses_precise_evidence_then_forces_bounded_candidate(self) -> None:
        backend = "/workspace/backend"
        frontend = "/workspace/frontend"
        initial = evaluate_tool_choice_state(
            task_kind="read-only",
            prompt="unrelated wording must not drive this typed policy",
            design_evidence_roots=(backend, frontend),
            read_only_review_profile="owner_impact",
        )
        self.assertEqual(initial.rule_id, "read_only_profile_explore")
        self.assertIn("read_file", initial.allowed_tool_names)
        self.assertIn("glob_files", initial.allowed_tool_names)

        noisy = [
            ToolResultSummary("glob_files", f"result {index}", path=f"{backend}/src")
            for index in range(8)
        ]
        exhausted = evaluate_tool_choice_state(
            task_kind="read-only",
            prompt="still unrelated",
            tool_results=noisy,
            design_evidence_roots=(backend, frontend),
            read_only_review_profile="owner_impact",
        )
        self.assertTrue(exhausted.force_final_answer_without_tools)
        self.assertEqual(exhausted.rule_id, "read_only_profile_explore_final")
        self.assertIn(f"code_read:{backend}", exhausted.missing_requirements)

    def test_design_profile_finalizes_after_one_read_per_required_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            backend = root / "backend"
            frontend = root / "frontend"
            backend_file = backend / "src" / "App.java"
            frontend_file = frontend / "src" / "Page.vue"
            backend_file.parent.mkdir(parents=True)
            frontend_file.parent.mkdir(parents=True)
            backend_file.write_text("class Service {}\n", encoding="utf-8")
            frontend_file.write_text("export default {}\n", encoding="utf-8")
            decision = evaluate_tool_choice_state(
                task_kind="read-only",
                prompt="arbitrary",
                tool_results=[
                    ToolResultSummary("search_code", "backend hit", metadata={"evidence_paths": [str(backend_file)]}),
                    ToolResultSummary("search_code", "frontend hit", metadata={"evidence_paths": [str(frontend_file)]}),
                    ToolResultSummary(
                        "read_file",
                        "class Service {}",
                        path=str(backend_file),
                        metadata={"resolved_path": str(backend_file)},
                    ),
                    ToolResultSummary(
                        "read_file",
                        "export default {}",
                        path=str(frontend_file),
                        metadata={"resolved_path": str(frontend_file)},
                    ),
                ],
                design_evidence_roots=(str(backend), str(frontend)),
                read_only_review_profile="design",
            )
        self.assertTrue(decision.force_final_answer_without_tools)
        self.assertEqual(decision.missing_requirements, ())


if __name__ == "__main__":
    unittest.main()

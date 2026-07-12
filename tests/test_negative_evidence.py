from __future__ import annotations

import unittest

from local_agent.negative_evidence import unsupported_negative_existence_claims
from local_agent.negative_evidence import ASSERTED_ABSENCE
from local_agent.negative_evidence import EPISTEMICALLY_QUALIFIED
from local_agent.negative_evidence import OBSERVED_NO_MATCH
from local_agent.negative_evidence import QUOTED_OR_HYPOTHETICAL
from local_agent.negative_evidence import negative_claim_metrics
from local_agent.negative_evidence import parse_negative_evidence_claims
from local_agent.steering.final_answer import FinalAnswerContext
from local_agent.steering.final_answer import NegativeExistenceSteerer
from local_agent.task_contract import generate_requirement_contract
from local_agent.tool_choice_queue import ToolResultSummary


class NegativeEvidenceTests(unittest.TestCase):
    def test_qualified_observation_is_not_an_asserted_absence(self) -> None:
        content = "我未发现任何 Java 源码，但这不等于证明 primary 无 Java。"

        claims = parse_negative_evidence_claims(content)

        self.assertTrue(claims)
        self.assertTrue(all(claim.stance == EPISTEMICALLY_QUALIFIED for claim in claims))
        self.assertEqual(claims[0].scope, "unspecified")
        self.assertEqual(unsupported_negative_existence_claims(content, []), ())
        self.assertGreaterEqual(negative_claim_metrics(content, [])["qualified_skips"], 1)

    def test_english_qualified_observation_is_not_an_asserted_absence(self) -> None:
        content = "No Java source was found; this does not establish that none exists."

        claims = parse_negative_evidence_claims(content)

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].stance, EPISTEMICALLY_QUALIFIED)
        self.assertEqual(unsupported_negative_existence_claims(content, []), ())

    def test_observed_no_match_is_distinct_from_absolute_absence(self) -> None:
        content = "在 primary 的 glob 中未发现 Java 文件。"
        claims = parse_negative_evidence_claims(content)

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].stance, OBSERVED_NO_MATCH)
        self.assertEqual(claims[0].scope, "primary")
        self.assertEqual(claims[0].root, "primary")
        self.assertEqual(len(unsupported_negative_existence_claims(content, [])), 1)

    def test_observed_no_match_requires_current_matching_tool_evidence(self) -> None:
        content = "我检查后未发现 Java 文件。"
        matching = ToolResultSummary(
            "glob_files",
            "{}",
            useless=True,
            metadata={
                "negative_evidence_type": "path_no_match",
                "patterns": ["**/*.java"],
                "complete": True,
                "truncated": False,
                "result_limit_reached": False,
                "evidence_root_label": "primary",
            },
        )
        wrong_query = ToolResultSummary(
            "search_code",
            "No matches.",
            useless=True,
            metadata={
                "negative_evidence_type": "content_no_match",
                "pattern": "python",
                "complete": True,
                "evidence_root_label": "primary",
            },
        )
        cached_no_match = ToolResultSummary(
            "glob_files",
            "{}",
            useless=True,
            metadata={**matching.metadata, "evidence_origin": "session_cached"},
        )

        self.assertEqual(len(unsupported_negative_existence_claims(content, [])), 1)
        self.assertEqual(len(unsupported_negative_existence_claims(content, [wrong_query])), 1)
        self.assertEqual(len(unsupported_negative_existence_claims(content, [cached_no_match])), 1)
        self.assertEqual(unsupported_negative_existence_claims(content, [matching]), ())

    def test_bare_observed_java_claim_is_gated_without_a_root_marker(self) -> None:
        for content in (
            "我检查后未发现 Java。",
            "未发现 Java 相关的文件或代码。",
            "No Java was found.",
        ):
            with self.subTest(content=content):
                claims = parse_negative_evidence_claims(content)
                self.assertEqual(len(claims), 1)
                self.assertEqual(claims[0].stance, OBSERVED_NO_MATCH)
                self.assertEqual(len(unsupported_negative_existence_claims(content, [])), 1)

    def test_bare_java_experience_dependency_and_version_are_not_file_claims(self) -> None:
        for content in (
            "未发现 Java 经验。",
            "workspace 没有 Java 依赖。",
            "This workspace has no Java version requirement.",
        ):
            with self.subTest(content=content):
                self.assertEqual(parse_negative_evidence_claims(content), ())

    def test_hypothetical_and_example_are_not_asserted_absence(self) -> None:
        claims = parse_negative_evidence_claims("建议调用 glob_files；如果没有 Java 源码再合并。")

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].stance, QUOTED_OR_HYPOTHETICAL)
        self.assertEqual(unsupported_negative_existence_claims("建议调用 glob_files；如果没有 Java 源码再合并。", []), ())

    def test_mixed_qualified_and_absolute_claim_only_blocks_absolute(self) -> None:
        content = "未发现 Java 源码，不等于证明没有 Java 源码，同时另一个 root 没有源码。"

        claims = parse_negative_evidence_claims(content)
        issues = unsupported_negative_existence_claims(content, [])

        self.assertEqual([claim.stance for claim in claims], [EPISTEMICALLY_QUALIFIED, EPISTEMICALLY_QUALIFIED, ASSERTED_ABSENCE])
        self.assertEqual([(issue.kind, issue.subject) for issue in issues], [("source_tree", "source")])

    def test_next_clause_qualifier_cannot_downgrade_an_asserted_absence(self) -> None:
        for content in (
            "该 root 没有 Java；这不等于证明没有项目价值。",
            "No Java source exists; that does not mean no docs exist.",
        ):
            with self.subTest(content=content):
                claims = parse_negative_evidence_claims(content)
                self.assertEqual(claims[0].stance, ASSERTED_ABSENCE)
                self.assertEqual(len(unsupported_negative_existence_claims(content, [])), 1)

    def test_unrelated_modal_does_not_hide_generic_entity_absence(self) -> None:
        claims = parse_negative_evidence_claims("无法读取 README，但这里不存在 Foo。")

        self.assertEqual(len(claims), 1)
        self.assertEqual((claims[0].kind, claims[0].subject, claims[0].stance), ("entity", "Foo", ASSERTED_ABSENCE))
        self.assertEqual(len(unsupported_negative_existence_claims("无法读取 README，但这里不存在 Foo。", [])), 1)

    def test_meta_negative_statements_do_not_become_existence_claims(self) -> None:
        issues = unsupported_negative_existence_claims(
            "不能推导出无 Java 源码；未验证，不能陈述无源码。",
            [],
        )

        self.assertEqual(issues, ())

    def test_english_meta_negative_statements_do_not_become_existence_claims(self) -> None:
        issues = unsupported_negative_existence_claims(
            "This does not prove no source code exists; I cannot conclude there are no Java files.",
            [],
        )

        self.assertEqual(issues, ())

    def test_quoted_and_inline_examples_do_not_become_existence_claims(self) -> None:
        issues = unsupported_negative_existence_claims(
            "The phrase `No Java source` is only an example. \"No source code\" is not a verified conclusion.",
            [],
        )

        self.assertEqual(issues, ())

    def test_quoted_claim_is_observed_in_taxonomy_without_triggering_a_gate(self) -> None:
        content = '"没有 Java 源码"只是示例。'

        claims = parse_negative_evidence_claims(content)
        metrics = negative_claim_metrics(content, [])

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].stance, QUOTED_OR_HYPOTHETICAL)
        self.assertGreater(claims[0].span_end, claims[0].span_start)
        self.assertEqual(metrics["quoted_or_hypothetical"], 1)
        self.assertEqual(unsupported_negative_existence_claims(content, []), ())

    def test_quoted_claim_with_connector_is_not_split_into_assertions(self) -> None:
        for content in (
            'The phrase "No Java source and no codebase" is quoted.',
            '“没有 Java 源码，但没有源码”只是引用。',
        ):
            with self.subTest(content=content):
                claims = parse_negative_evidence_claims(content)
                self.assertTrue(claims)
                self.assertTrue(all(claim.stance == QUOTED_OR_HYPOTHETICAL for claim in claims))
                self.assertEqual(unsupported_negative_existence_claims(content, []), ())

    def test_actual_java_absence_claim_still_requires_discovery_evidence(self) -> None:
        issues = unsupported_negative_existence_claims("该 root 没有 Java 源码。", [])

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].kind, "extension")

    def test_later_unrelated_negation_does_not_hide_real_chinese_assertion(self) -> None:
        issues = unsupported_negative_existence_claims(
            "该 root 没有 Java 源码，但这不等于没有项目价值。",
            [],
        )

        self.assertEqual(len(issues), 1)

    def test_later_unrelated_negation_does_not_hide_real_english_assertion(self) -> None:
        issues = unsupported_negative_existence_claims(
            "No source code exists; that does not mean no docs exist.",
            [],
        )

        self.assertEqual(len(issues), 1)

    def test_unrelated_chinese_modal_prefix_does_not_hide_real_assertion(self) -> None:
        for content in (
            "不能运行测试，但该 root 没有 Java 源码。",
            "无法读取 README，但该 root 没有 Java 源码。",
            "不要修改代码，但该 root 没有 Java 源码。",
        ):
            with self.subTest(content=content):
                self.assertEqual(len(unsupported_negative_existence_claims(content, [])), 1)

    def test_chinese_conclusion_negation_still_skips_claim(self) -> None:
        for content in (
            "不能据此推导出无 Java 源码。",
            "未验证，不能陈述无源码。",
        ):
            with self.subTest(content=content):
                self.assertEqual(unsupported_negative_existence_claims(content, []), ())

    def test_content_no_match_does_not_support_java_file_absence(self) -> None:
        issues = unsupported_negative_existence_claims(
            "已验证：search_code 未找到 \\.java$，因此没有 Java 文件。",
            [
                ToolResultSummary(
                    "search_code",
                    "No matches.",
                    useless=True,
                    metadata={
                        "negative_evidence_type": "content_no_match",
                        "pattern": r"\\.java$",
                        "complete": True,
                    },
                )
            ],
        )

        self.assertEqual([issue.kind for issue in issues], ["extension"])

    def test_complete_glob_no_match_supports_java_file_absence(self) -> None:
        issues = unsupported_negative_existence_claims(
            "已验证：glob_files 完整扫描未发现 Java 文件。",
            [
                ToolResultSummary(
                    "glob_files",
                    "{}",
                    useless=True,
                    metadata={
                        "negative_evidence_type": "path_no_match",
                        "patterns": ["**/*.java"],
                        "complete": True,
                        "truncated": False,
                        "result_limit_reached": False,
                    },
                )
            ],
        )

        self.assertEqual(issues, ())

    def test_truncated_or_cached_positive_result_cannot_support_absolute_absence(self) -> None:
        content = "该 root 没有 Java 源码。"
        for result in (
            ToolResultSummary(
                "glob_files",
                "{}",
                useless=True,
                metadata={
                    "negative_evidence_type": "path_no_match",
                    "patterns": ["**/*.java"],
                    "complete": False,
                    "truncated": True,
                    "result_limit_reached": True,
                },
            ),
            ToolResultSummary(
                "search_code",
                "src/App.java:1: class App {}",
                metadata={"evidence_origin": "session_cached", "negative_evidence_type": "content_match"},
            ),
        ):
            with self.subTest(result=result.name):
                self.assertEqual(len(unsupported_negative_existence_claims(content, [result])), 1)

    def test_multi_root_absence_requires_discovery_that_covers_multiple_roots(self) -> None:
        content = "所有 root 都没有 Java 源码。"
        one_root = ToolResultSummary(
            "glob_files",
            "{}",
            useless=True,
            metadata={
                "negative_evidence_type": "path_no_match",
                "patterns": ["**/*.java"],
                "complete": True,
                "truncated": False,
                "result_limit_reached": False,
                "searched_roots": ["/workspace/primary"],
            },
        )
        two_roots = ToolResultSummary(
            "glob_files",
            "{}",
            useless=True,
            metadata={**one_root.metadata, "searched_roots": ["/workspace/primary", "/workspace/additional"]},
        )

        self.assertEqual(len(unsupported_negative_existence_claims(content, [one_root])), 1)
        self.assertEqual(unsupported_negative_existence_claims(content, [two_roots]), ())

    def test_explicit_root_claim_requires_matching_root_provenance(self) -> None:
        content = "附加 root 没有 Java 源码。"
        base = {
            "negative_evidence_type": "path_no_match",
            "patterns": ["**/*.java"],
            "complete": True,
            "truncated": False,
            "result_limit_reached": False,
        }
        primary = ToolResultSummary("glob_files", "{}", useless=True, metadata={**base, "evidence_root_label": "primary"})
        additional = ToolResultSummary(
            "glob_files",
            "{}",
            useless=True,
            metadata={**base, "evidence_root_label": "/workspace/additional"},
        )
        missing_label = ToolResultSummary("glob_files", "{}", useless=True, metadata=base)

        self.assertEqual(len(unsupported_negative_existence_claims(content, [primary])), 1)
        self.assertEqual(len(unsupported_negative_existence_claims(content, [missing_label])), 1)
        self.assertEqual(unsupported_negative_existence_claims(content, [additional]), ())
        self.assertEqual(len(unsupported_negative_existence_claims("primary 没有 Java 源码。", [additional])), 1)

    def test_root_reference_binds_to_the_nearest_claim_in_a_multi_root_clause(self) -> None:
        base = {
            "negative_evidence_type": "path_no_match",
            "patterns": ["**/*.java"],
            "complete": True,
            "truncated": False,
            "result_limit_reached": False,
        }
        primary = ToolResultSummary("glob_files", "{}", useless=True, metadata={**base, "evidence_root_label": "primary"})
        additional = ToolResultSummary(
            "glob_files",
            "{}",
            useless=True,
            metadata={**base, "evidence_root_label": "/workspace/additional"},
        )
        for content, expected_root, supporting_result, non_supporting_result in (
            ("primary 有源码，附加 root 没有 Java 源码。", "additional", additional, primary),
            ("Primary has source, additional root has no Java source.", "additional", additional, primary),
            ("附加 root 有源码，primary 没有 Java 源码。", "primary", primary, additional),
            ("Additional root has source and primary has no Java source.", "primary", primary, additional),
        ):
            with self.subTest(content=content):
                claim = next(claim for claim in parse_negative_evidence_claims(content) if claim.kind == "extension")
                expected_scope = "primary" if expected_root == "primary" else "root_local"
                self.assertEqual((claim.scope, claim.root), (expected_scope, expected_root))
                self.assertEqual(len(unsupported_negative_existence_claims(content, [non_supporting_result])), 1)
                self.assertEqual(unsupported_negative_existence_claims(content, [supporting_result]), ())

    def test_git_claims_cannot_borrow_or_target_additional_root_probes(self) -> None:
        primary_probe = ToolResultSummary(
            "git_status",
            "not a git repository",
            is_error=True,
            metadata={
                "git_probe_root": True,
                "git_repository": False,
                "evidence_root_label": "primary",
            },
        )
        additional_probe = ToolResultSummary(
            "git_status",
            "not a git repository",
            is_error=True,
            metadata={
                "git_probe_root": True,
                "git_repository": False,
                "evidence_root_label": "/workspace/additional",
            },
        )

        self.assertEqual(len(unsupported_negative_existence_claims("附加 root 不是 Git 仓库。", [primary_probe])), 1)
        self.assertEqual(len(unsupported_negative_existence_claims("附加 root 不是 Git 仓库。", [additional_probe])), 1)
        self.assertEqual(unsupported_negative_existence_claims("primary 不是 Git 仓库。", [primary_probe]), ())
        execution_error = ToolResultSummary(
            "git_status",
            "git executable failed",
            is_error=True,
            metadata={
                "git_probe_root": True,
                "git_repository": None,
                "evidence_root_label": "primary",
            },
        )
        self.assertEqual(len(unsupported_negative_existence_claims("primary 不是 Git 仓库。", [execution_error])), 1)

    def test_bare_java_claim_requires_file_or_workspace_scope(self) -> None:
        for content in (
            "团队没有 Java 经验。",
            "项目没有 Java 依赖。",
            "没有 Java 版本要求。",
            "该 root 的团队没有 Java 经验。",
            "workspace 没有 Java 依赖。",
            "The team has no Java experience.",
            "The project has no Java dependency.",
            "There is no Java version requirement.",
            "This workspace has no Java version requirement.",
        ):
            with self.subTest(content=content):
                self.assertEqual(parse_negative_evidence_claims(content), ())

        for content in ("当前 root 没有 Java，但仍需验证。", "This root has no Java."):
            with self.subTest(content=content):
                scoped = parse_negative_evidence_claims(content)
                self.assertEqual([(claim.kind, claim.stance) for claim in scoped], [("extension", ASSERTED_ABSENCE)])

    def test_git_claim_for_additional_root_is_rewritten_without_git_probe(self) -> None:
        request = "只读分析附加目录中的项目，不要修改文件。"
        context = FinalAnswerContext(
            request=request,
            content="已验证：附加目录不是 Git 仓库。",
            messages=[],
            run_start_index=0,
            requirement_contract=generate_requirement_contract(request),
            tool_results=[ToolResultSummary("git_status", "not a git repository", is_error=True)],
            read_file_evidence_paths=[],
            source_evidence=[],
            open_todos=[],
            is_code_implementation_request=False,
            steer_counts={},
        )

        decision = NegativeExistenceSteerer(max_steers=2).decide(context)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.kind, "negative_existence")
        self.assertTrue(decision.force_final_answer_without_tools)
        self.assertIsNone(decision.temporary_tool_allowlist)
        self.assertIn("/move", decision.message)

    def test_extension_claim_steerer_only_allows_discovery_tools(self) -> None:
        request = "只读分析当前目录有哪些 Java 文件，不要修改文件。"
        context = FinalAnswerContext(
            request=request,
            content="已验证：search_code 无匹配，因此没有 Java 文件。",
            messages=[],
            run_start_index=0,
            requirement_contract=generate_requirement_contract(request),
            tool_results=[
                ToolResultSummary(
                    "search_code",
                    "No matches.",
                    useless=True,
                    metadata={"negative_evidence_type": "content_no_match", "pattern": "java", "complete": True},
                )
            ],
            read_file_evidence_paths=[],
            source_evidence=[],
            open_todos=[],
            is_code_implementation_request=False,
            steer_counts={},
        )

        decision = NegativeExistenceSteerer(max_steers=2).decide(context)

        self.assertIsNotNone(decision)
        self.assertFalse(decision.force_final_answer_without_tools)
        self.assertEqual(decision.temporary_tool_allowlist, {"glob_files"})
        self.assertEqual(decision.payload["claim_metrics"]["blocked_assertions"], 1)

    def test_negative_existence_steerer_respects_its_bounded_retry_cap(self) -> None:
        request = "只读确认当前目录是否有 Java 文件。"
        context = FinalAnswerContext(
            request=request,
            content="该 root 没有 Java 源码。",
            messages=[],
            run_start_index=0,
            requirement_contract=generate_requirement_contract(request),
            tool_results=[],
            read_file_evidence_paths=[],
            source_evidence=[],
            open_todos=[],
            is_code_implementation_request=False,
            steer_counts={"negative_existence": 1},
        )

        self.assertIsNone(NegativeExistenceSteerer(max_steers=1).decide(context))


if __name__ == "__main__":
    unittest.main()

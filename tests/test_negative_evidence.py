from __future__ import annotations

import unittest

from local_agent.negative_evidence import unsupported_negative_existence_claims
from local_agent.steering.final_answer import FinalAnswerContext
from local_agent.steering.final_answer import NegativeExistenceSteerer
from local_agent.task_contract import generate_requirement_contract
from local_agent.tool_choice_queue import ToolResultSummary


class NegativeEvidenceTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

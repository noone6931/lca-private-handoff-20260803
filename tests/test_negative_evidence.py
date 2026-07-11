from __future__ import annotations

import unittest

from local_agent.negative_evidence import unsupported_negative_existence_claims
from local_agent.steering.final_answer import FinalAnswerContext
from local_agent.steering.final_answer import NegativeExistenceSteerer
from local_agent.task_contract import generate_requirement_contract
from local_agent.tool_choice_queue import ToolResultSummary


class NegativeEvidenceTests(unittest.TestCase):
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

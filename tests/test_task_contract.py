from __future__ import annotations

import unittest

from local_agent.task_contract import generate_requirement_contract
from local_agent.task_contract import render_contract_context


class RequirementContractTests(unittest.TestCase):
    def test_read_only_code_evidence_question_contract(self) -> None:
        contract = generate_requirement_contract(
            "只读代码，帮我确认登录密码是否在后端加密，必须给代码证据，不要修改文件。"
        )

        self.assertEqual(contract.task_kind, "read-only")
        self.assertIn("without modifying files", contract.scope)
        self.assertTrue(any("repository-grounded evidence" in item for item in contract.acceptance_items))
        self.assertTrue(any("file paths" in item for item in contract.evidence_requirements))
        self.assertTrue(any("read/search" in item for item in contract.verification_requirements))

        rendered = render_contract_context(contract)

        self.assertIn("Requirement Contract", rendered)
        self.assertIn("Task kind: read-only", rendered)
        self.assertIn("Evidence:", rendered)
        self.assertIn("Verification:", rendered)

    def test_code_implementation_task_contract(self) -> None:
        contract = generate_requirement_contract("请实现用户注册接口的邮箱唯一性校验，并补充单元测试。")

        self.assertEqual(contract.task_kind, "code-implementation")
        self.assertIn("Code implementation work", contract.scope)
        self.assertTrue(any("smallest practical change" in item for item in contract.acceptance_items))
        self.assertTrue(any("modified files" in item for item in contract.evidence_requirements))
        self.assertTrue(any("test command" in item for item in contract.verification_requirements))

    def test_implementation_that_mentions_a_read_only_literal_is_not_misclassified(self) -> None:
        contract = generate_requirement_contract(
            "请在任务分类器中添加精确标记‘只读核实’，并补充单元测试，断言 task_kind is read-only。"
        )

        self.assertEqual(contract.task_kind, "code-implementation")

    def test_chinese_status_questions_are_read_only(self) -> None:
        for prompt in (
            "这个功能实现了吗？",
            "当前是否支持批量导出？",
            "已经实现用户注册了吗？",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(generate_requirement_contract(prompt).task_kind, "read-only")

    def test_service_fee_settlement_design_contract_is_clarification_first(self) -> None:
        contract = generate_requirement_contract("帮我设计服务费结算需求：下单、退款、商家分账都要考虑。")

        self.assertEqual(contract.task_kind, "unclear")
        self.assertIn("Requirements/design clarification", contract.scope)
        self.assertTrue(any("business goal" in item for item in contract.acceptance_items))
        self.assertTrue(any("assumptions" in item for item in contract.evidence_requirements))
        self.assertTrue(any("rounding" in item for item in contract.verification_requirements))
        self.assertTrue(any("Settlement requirements" in item for item in contract.risk_notes))

    def test_very_short_plain_question_contract_is_unclear(self) -> None:
        contract = generate_requirement_contract("能吗？")

        self.assertEqual(contract.task_kind, "unclear")
        self.assertEqual(contract.objective, "Clarify the user's request: 能吗？")
        self.assertIn("Clarification-first task", contract.scope)
        self.assertTrue(any("too short" in item for item in contract.risk_notes))

    def test_generation_is_deterministic(self) -> None:
        prompt = "请实现导出按钮的 loading 状态，并补充测试。"

        self.assertEqual(generate_requirement_contract(prompt), generate_requirement_contract(prompt))


if __name__ == "__main__":
    unittest.main()

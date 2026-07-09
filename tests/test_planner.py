from __future__ import annotations

import unittest

from local_agent.planner import planner_phase
from local_agent.planner import render_planner_explore_context
from local_agent.task_contract import generate_requirement_contract
from local_agent.tool_choice_queue import ToolResultSummary


class PlannerExploreTests(unittest.TestCase):
    def test_implementation_starts_in_explore_phase(self) -> None:
        contract = generate_requirement_contract("请实现用户注册接口邮箱唯一性校验，并补充测试。")

        phase = planner_phase(contract, prompt=contract.objective, tool_results=[])
        context = render_planner_explore_context(contract, prompt=contract.objective, tool_results=[])

        self.assertEqual(phase, "explore")
        self.assertIn("Current phase: explore", context)
        self.assertIn("do not write files yet", context)

    def test_implementation_moves_to_ready_after_code_evidence(self) -> None:
        contract = generate_requirement_contract("请实现用户注册接口邮箱唯一性校验，并补充测试。")

        phase = planner_phase(
            contract,
            prompt=contract.objective,
            tool_results=[ToolResultSummary("read_file", "class UserService {}", path="src/UserService.java")],
        )

        self.assertEqual(phase, "ready_to_implement")

    def test_implementation_moves_to_verify_after_write(self) -> None:
        contract = generate_requirement_contract("请实现用户注册接口邮箱唯一性校验，并补充测试。")

        phase = planner_phase(
            contract,
            prompt=contract.objective,
            tool_results=[
                ToolResultSummary("read_file", "class UserService {}", path="src/UserService.java"),
                ToolResultSummary("apply_patch", "Applied patch. Patch id: p1", changed=True),
            ],
        )

        self.assertEqual(phase, "verify")

    def test_read_only_task_has_no_planner_context(self) -> None:
        contract = generate_requirement_contract("只读代码，请根据源码证据说明登录逻辑。")

        self.assertEqual(planner_phase(contract, prompt=contract.objective, tool_results=[]), "not_applicable")
        self.assertEqual(render_planner_explore_context(contract, prompt=contract.objective, tool_results=[]), "")


if __name__ == "__main__":
    unittest.main()

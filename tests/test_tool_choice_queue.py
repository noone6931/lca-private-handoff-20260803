from __future__ import annotations

import unittest

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
from local_agent.tool_choice_queue import ToolResultSummary
from local_agent.tool_choice_queue import evaluate_tool_choice_state
from local_agent.tool_choice_queue import session_evidence_reuse_directive


class ToolChoiceQueueTests(unittest.TestCase):
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
        self.assertNotIn("apply_patch", before_read.allowed_tool_names)

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
        self.assertNotIn("glob_files", initial.allowed_tool_names)

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
        backend = "/workspace/backend"
        frontend = "/workspace/frontend"
        decision = evaluate_tool_choice_state(
            task_kind="read-only",
            prompt="arbitrary",
            tool_results=[
                ToolResultSummary("read_file", "class Service {}", path=f"{backend}/src/Service.java"),
                ToolResultSummary("read_file", "export default {}", path=f"{frontend}/src/Page.vue"),
            ],
            design_evidence_roots=(backend, frontend),
            read_only_review_profile="design",
        )
        self.assertTrue(decision.force_final_answer_without_tools)
        self.assertEqual(decision.missing_requirements, ())


if __name__ == "__main__":
    unittest.main()

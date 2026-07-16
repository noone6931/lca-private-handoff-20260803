from __future__ import annotations

from pathlib import Path
import ast
import importlib
import re
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRODUCTION_MODULE_LINE_LIMIT = 900
OWNER_COMPLEXITY_CEILINGS = {
    "src/local_agent/tool_choice_queue.py": 185,
    "src/local_agent/read_only_reviewer.py": 42,
    "src/local_agent/tool_choice_decision.py": 333,
    "src/local_agent/tool_choice_read_only.py": 779,
    "src/local_agent/tool_choice_implementation.py": 310,
    "src/local_agent/tool_choice_task_classification.py": 69,
    "src/local_agent/read_only_reviewer_types.py": 235,
    "src/local_agent/read_only_reviewer_claims.py": 495,
    "src/local_agent/read_only_reviewer_contract.py": 372,
    "src/local_agent/read_only_reviewer_validation.py": 419,
    "src/local_agent/reviewer_correction_contract.py": 148,
    "src/local_agent/reviewer_output_lifecycle.py": 418,
    "src/local_agent/runtime_read_only_review.py": 641,
    "src/local_agent/runtime_read_only_review_round.py": 450,
    "src/local_agent/safe_partial_report.py": 446,
    "src/local_agent/tools/shell.py": 357,
    "src/local_agent/tools/test_runner_policy.py": 217,
    "src/local_agent/steering/pre_review.py": 83,
    "src/local_agent/steering/final_answer.py": 59,
    "src/local_agent/workflow_profile.py": 165,
    "src/local_agent/runtime_workflow_profile.py": 44,
    "src/local_agent/execution_policy.py": 170,
    "src/local_agent/tools/base.py": 550,
}
LEGACY_COMPLEXITY_DEBT_CEILINGS = {
    "src/local_agent/agent.py": 1808,
    "src/local_agent/tools/lsp.py": 1166,
    "src/local_agent/completion_audit.py": 1093,
    "src/local_agent/explore_handoff.py": 991,
    "src/local_agent/benchmark.py": 989,
    "src/local_agent/steering/evidence.py": 985,
    "src/local_agent/read_only_explore.py": 981,
    "src/local_agent/document_consistency.py": 939,
    "src/local_agent/task_contract.py": 935,
}


def _production_complexity_failures(root: Path) -> list[tuple[str, int, int]]:
    ceilings = {**LEGACY_COMPLEXITY_DEBT_CEILINGS, **OWNER_COMPLEXITY_CEILINGS}
    failures: list[tuple[str, int, int]] = []
    for path in sorted((root / "src/local_agent").rglob("*.py")):
        relative_path = path.relative_to(root).as_posix()
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        limit = ceilings.get(relative_path, DEFAULT_PRODUCTION_MODULE_LINE_LIMIT)
        if line_count > limit:
            failures.append((relative_path, line_count, limit))
    return failures


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_runtime_orchestrator_stays_within_line_budget(self) -> None:
        agent = ROOT / "src/local_agent/agent.py"
        self.assertLessEqual(len(agent.read_text(encoding="utf-8").splitlines()), 2100)

    def test_runtime_orchestrator_stays_within_method_budget(self) -> None:
        content = (ROOT / "src/local_agent/agent.py").read_text(encoding="utf-8")
        runtime_methods = re.findall(r"^    def ", content, flags=re.MULTILINE)
        self.assertLessEqual(len(runtime_methods), 75)

    def test_final_answer_facade_stays_thin(self) -> None:
        facade = ROOT / "src/local_agent/steering/final_answer.py"
        self.assertLessEqual(
            len(facade.read_text(encoding="utf-8").splitlines()),
            OWNER_COMPLEXITY_CEILINGS["src/local_agent/steering/final_answer.py"],
        )

    def test_runtime_does_not_reintroduce_migrated_domain_helpers(self) -> None:
        content = (ROOT / "src/local_agent/agent.py").read_text(encoding="utf-8")
        for helper in (
            "def _messages_with_runtime_context",
            "def _messages_to_memory_transcript",
            "def _tool_choice_steering_message",
            "def _record_workspace_roots_change",
            "def _capture_session_evidence",
            "def _maybe_consolidate_session_memory",
        ):
            self.assertNotIn(helper, content)
        self.assertNotIn("def __getattr__", content)

    def test_runtime_calls_phase_owners_explicitly(self) -> None:
        content = (ROOT / "src/local_agent/agent.py").read_text(encoding="utf-8")
        for call in (
            "self._provider_context_phase.messages_for_model(",
            "self._evidence_phase.hydrate_session_evidence(",
            "self._evidence_phase.append_session_evidence_reuse_directive(",
            "self._memory_phase.consolidate_session_memory(",
            "self._read_only_review_phase.review_candidate(",
        ):
            self.assertIn(call, content)

    def test_runtime_only_dispatches_generic_tool_choice_outcomes(self) -> None:
        content = (ROOT / "src/local_agent/agent.py").read_text(encoding="utf-8")
        directive = (ROOT / "src/local_agent/runtime_tool_choice_directive.py").read_text(encoding="utf-8")
        self.assertIn("requeue_required", directive)
        self.assertNotIn("implementation_readiness_required", content)
        self.assertNotIn("read_only_unlocated", content)

    def test_runtime_uses_explicit_phase_components_not_a_provider_mixin(self) -> None:
        provider_context = (ROOT / "src/local_agent/provider_context.py").read_text(encoding="utf-8")
        self.assertIn("class ProviderContextPhase:", provider_context)
        self.assertNotIn("class ProviderContextMixin:", provider_context)

    def test_tool_choice_steering_helpers_live_with_queue_owner(self) -> None:
        queue = (ROOT / "src/local_agent/tool_choice_queue.py").read_text(encoding="utf-8")
        decision = (ROOT / "src/local_agent/tool_choice_decision.py").read_text(encoding="utf-8")
        gateway = (ROOT / "src/local_agent/tool_gateway.py").read_text(encoding="utf-8")
        self.assertIn("from .tool_choice_decision import tool_choice_steering_message", queue)
        self.assertIn("def tool_choice_steering_message", decision)
        self.assertIn("def tool_choice_steering_signature", decision)
        self.assertNotIn("def _tool_choice_steering_message", gateway)
        self.assertNotIn("def _tool_choice_steering_signature", gateway)

    def test_split_facades_and_debt_files_obey_complexity_ratchets(self) -> None:
        self.assertEqual(_production_complexity_failures(ROOT), [])

    def test_unregistered_production_module_cannot_bypass_global_line_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            module = root / "src/local_agent/unregistered_large_owner.py"
            module.parent.mkdir(parents=True)
            module.write_text("value = 1\n" * 901, encoding="utf-8")
            self.assertEqual(
                _production_complexity_failures(root),
                [("src/local_agent/unregistered_large_owner.py", 901, DEFAULT_PRODUCTION_MODULE_LINE_LIMIT)],
            )

    def test_split_facades_preserve_public_api_and_phase_order(self) -> None:
        queue = importlib.import_module("local_agent.tool_choice_queue")
        queue_decision = importlib.import_module("local_agent.tool_choice_decision")
        reviewer = importlib.import_module("local_agent.read_only_reviewer")
        reviewer_claims = importlib.import_module("local_agent.read_only_reviewer_claims")
        reviewer_contract = importlib.import_module("local_agent.read_only_reviewer_contract")
        reviewer_validation = importlib.import_module("local_agent.read_only_reviewer_validation")
        self.assertIs(queue.ToolChoiceDecision, queue_decision.ToolChoiceDecision)
        self.assertIs(queue.tool_choice_steering_message, queue_decision.tool_choice_steering_message)
        self.assertIs(reviewer.candidate_claim_units, reviewer_claims.candidate_claim_units)
        self.assertIs(reviewer.reviewer_output_tool_schema, reviewer_contract.reviewer_output_tool_schema)
        self.assertIs(reviewer.parse_reviewer_payload, reviewer_validation.parse_reviewer_payload)
        self.assertIs(reviewer.reviewer_rewrite_message, reviewer_validation.reviewer_rewrite_message)
        self.assertIs(reviewer.rewrite_complies_with_review, reviewer_validation.rewrite_complies_with_review)
        content = (ROOT / "src/local_agent/tool_choice_queue.py").read_text(encoding="utf-8")
        self.assertLess(
            content.index("if is_inspection_forbidden(prompt):"),
            content.index("read_only_decision = evaluate_read_only_phase("),
        )
        self.assertLess(
            content.index("read_only_decision = evaluate_read_only_phase("),
            content.index("implementation_decision = evaluate_implementation_phase("),
        )

    def test_split_owner_import_direction_is_acyclic_and_runtime_free(self) -> None:
        split_modules = {
            "tool_choice_queue",
            "tool_choice_decision",
            "tool_choice_read_only",
            "tool_choice_implementation",
            "tool_choice_task_classification",
            "read_only_reviewer",
            "read_only_reviewer_types",
            "read_only_reviewer_claims",
            "read_only_reviewer_contract",
            "read_only_reviewer_validation",
        }
        edges: dict[str, set[str]] = {name: set() for name in split_modules}
        for name in split_modules:
            path = ROOT / f"src/local_agent/{name}.py"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.level != 1 or not node.module:
                    continue
                imported = node.module.split(".", 1)[0]
                if imported in split_modules:
                    edges[name].add(imported)
                self.assertFalse(
                    name not in {"tool_choice_queue", "read_only_reviewer"}
                    and (imported in {"tool_choice_queue", "read_only_reviewer"} or imported.startswith("runtime_"))
                )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            self.assertNotIn(name, visiting, f"split owner import cycle at {name}")
            visiting.add(name)
            for dependency in edges[name]:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in split_modules:
            visit(name)

    def test_workflow_profile_owner_and_runtime_facade_have_one_way_dependencies(self) -> None:
        owner = (ROOT / "src/local_agent/workflow_profile.py").read_text(encoding="utf-8")
        facade = (ROOT / "src/local_agent/runtime_workflow_profile.py").read_text(encoding="utf-8")
        runtime = (ROOT / "src/local_agent/agent.py").read_text(encoding="utf-8")
        self.assertNotIn("from .runtime_", owner)
        self.assertIn("from .workflow_profile import", facade)
        self.assertIn("from .runtime_read_only_review import ReadOnlyReviewPhase", facade)
        self.assertNotIn("implementation_readiness_required", runtime)
        self.assertNotIn("if self._config.workflow_profile", runtime)

    def test_execution_policy_owner_is_pure_and_registry_has_no_duplicate_policy_tree(self) -> None:
        owner = (ROOT / "src/local_agent/execution_policy.py").read_text(encoding="utf-8")
        registry = (ROOT / "src/local_agent/tools/base.py").read_text(encoding="utf-8")
        self.assertNotIn("from .tools", owner)
        self.assertNotIn("from .agent", owner)
        self.assertNotIn("input(", owner)
        self.assertNotIn("event_callback", owner)
        self.assertIn("return evaluate_execution_policy(", registry)
        self.assertNotIn("def _approval_denial_reason", registry)
        self.assertNotIn("config_policy in", registry)

    def test_runtime_strategy_owners_do_not_reintroduce_business_keyword_guards(self) -> None:
        strategy_files = (
            ROOT / "src/local_agent/task_contract.py",
            ROOT / "src/local_agent/tool_choice_queue.py",
            ROOT / "src/local_agent/tool_choice_decision.py",
            ROOT / "src/local_agent/tool_choice_read_only.py",
            ROOT / "src/local_agent/tool_choice_implementation.py",
            ROOT / "src/local_agent/tool_choice_task_classification.py",
            ROOT / "src/local_agent/read_only_explore.py",
            ROOT / "src/local_agent/read_only_reviewer.py",
            ROOT / "src/local_agent/read_only_reviewer_claims.py",
            ROOT / "src/local_agent/read_only_reviewer_contract.py",
            ROOT / "src/local_agent/read_only_reviewer_validation.py",
            ROOT / "src/local_agent/runtime_read_only_review.py",
            ROOT / "src/local_agent/safe_partial_report.py",
            ROOT / "src/local_agent/reviewer_output_lifecycle.py",
            ROOT / "src/local_agent/steering/evidence.py",
            ROOT / "src/local_agent/implementation_readiness.py",
            ROOT / "src/local_agent/workflow_profile.py",
            ROOT / "src/local_agent/runtime_workflow_profile.py",
        )
        forbidden = re.compile(
            r"(拓展服务费|结算单|服务费|分账|退款|计费|(?<![A-Za-z])settlement(?![A-Za-z])|"
            r"(?<![A-Za-z])billing(?![A-Za-z])|(?<![A-Za-z])fee(?![A-Za-z]))",
            flags=re.IGNORECASE,
        )
        hits: list[str] = []
        for path in strategy_files:
            content = path.read_text(encoding="utf-8")
            for match in forbidden.finditer(content):
                hits.append(f"{path.relative_to(ROOT)}:{content.count(chr(10), 0, match.start()) + 1}:{match.group(0)}")
        self.assertEqual(hits, [])

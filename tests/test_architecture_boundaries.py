from __future__ import annotations

from pathlib import Path
import ast
import importlib
from importlib.util import resolve_name
import re
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRODUCTION_MODULE_LINE_LIMIT = 900
ROOT_IMPLEMENTATION_ENTRYPOINTS = {"agent.py", "cli.py", "config.py"}
IMPORT_FREE_PACKAGE_INITIALIZERS = (
    "src/local_agent/session/__init__.py",
    "src/local_agent/review/__init__.py",
    "src/local_agent/workflows/__init__.py",
    "src/local_agent/workflows/tool_choice/__init__.py",
    "src/local_agent/frontends/terminal/__init__.py",
    "src/local_agent/platform/__init__.py",
)
OWNER_COMPLEXITY_CEILINGS = {
    "src/local_agent/workflows/tool_choice/queue.py": 185,
    "src/local_agent/review/read_only.py": 42,
    "src/local_agent/workflows/tool_choice/decision.py": 333,
    "src/local_agent/workflows/tool_choice/read_only.py": 779,
    "src/local_agent/workflows/tool_choice/implementation.py": 310,
    "src/local_agent/workflows/tool_choice/classification.py": 69,
    "src/local_agent/review/types.py": 235,
    "src/local_agent/review/claims.py": 495,
    "src/local_agent/review/contract.py": 372,
    "src/local_agent/review/validation.py": 419,
    "src/local_agent/review/correction.py": 148,
    "src/local_agent/review/output_lifecycle.py": 418,
    "src/local_agent/runtime/review.py": 643,
    "src/local_agent/runtime/review_round.py": 452,
    "src/local_agent/review/safe_partial.py": 446,
    "src/local_agent/tools/shell.py": 355,
    "src/local_agent/tools/process_environment.py": 64,
    "src/local_agent/tools/process_output.py": 250,
    "src/local_agent/tools/process_runtime.py": 201,
    "src/local_agent/tools/test_runner_policy.py": 217,
    "src/local_agent/steering/pre_review.py": 83,
    "src/local_agent/steering/final_answer.py": 59,
    "src/local_agent/workflows/profile.py": 165,
    "src/local_agent/runtime/workflow_profile.py": 44,
    "src/local_agent/tools/policy.py": 170,
    "src/local_agent/tools/base.py": 607,
    "src/local_agent/runtime/commands.py": 221,
    "src/local_agent/providers/stream.py": 340,
    "src/local_agent/providers/deadline.py": 179,
    "src/local_agent/providers/llm.py": 226,
    "src/local_agent/protocol/events.py": 228,
    "src/local_agent/frontends/terminal/renderer.py": 165,
    "src/local_agent/frontends/terminal/assistant.py": 100,
    "src/local_agent/frontends/text.py": 60,
    "src/local_agent/frontends/tui/markdown.py": 120,
    "src/local_agent/frontends/composer_history.py": 237,
    "src/local_agent/frontends/tui/history_search.py": 130,
    "src/local_agent/frontends/tui/composer_layout.py": 183,
    "src/local_agent/frontends/tui/pending_prompt.py": 58,
    "src/local_agent/runtime/assistant_message.py": 170,
    "src/local_agent/runtime/run_output.py": 130,
    "src/local_agent/providers/protocol.py": 379,
    "src/local_agent/runtime/prompt.py": 490,
    "src/local_agent/runtime/collector.py": 668,
    "src/local_agent/runtime/evidence.py": 518,
    "src/local_agent/tools/gateway.py": 404,
    "src/local_agent/platform/terminal.py": 122,
    "src/local_agent/protocol/cancellation.py": 66,
    "src/local_agent/workflows/explore_subagent.py": 561,
    "src/local_agent/lsp/workspace_edit.py": 380,
    "src/local_agent/tools/lsp_rename.py": 187,
    "src/local_agent/tools/lsp_code_action.py": 430,
    "src/local_agent/session/continuity.py": 284,
}
LEGACY_COMPLEXITY_DEBT_CEILINGS = {
    "src/local_agent/agent.py": 1632,
    "src/local_agent/tools/lsp.py": 1166,
    "src/local_agent/evidence/completion.py": 1093,
    "src/local_agent/review/handoff.py": 991,
    "src/local_agent/devtools/benchmark.py": 989,
    "src/local_agent/steering/evidence.py": 985,
    "src/local_agent/workflows/explore.py": 981,
    "src/local_agent/review/document_consistency.py": 939,
    "src/local_agent/workflows/contracts.py": 935,
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


def _resolved_import_targets(path: Path) -> list[tuple[int, str]]:
    module = ".".join(path.relative_to(ROOT / "src").with_suffix("").parts)
    package = module.rpartition(".")[0]
    targets: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            targets.extend((node.lineno, alias.name) for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            target = resolve_name("." * node.level + (node.module or ""), package)
        else:
            target = node.module or ""
        targets.append((node.lineno, target))
    return targets


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_read_helper_containment_stays_with_git_search_process_owners(self) -> None:
        git_owner = (ROOT / "src/local_agent/tools/git.py").read_text(encoding="utf-8")
        search_owner = (ROOT / "src/local_agent/tools/search.py").read_text(encoding="utf-8")
        agent = (ROOT / "src/local_agent/agent.py").read_text(encoding="utf-8")
        for marker in ("--no-optional-locks", "core.fsmonitor=false", "--no-ext-diff", "--no-textconv"):
            self.assertIn(marker, git_owner)
        self.assertIn("--get-regexp", git_owner)
        self.assertIn("filter\\..*\\.(clean|process|smudge)", git_owner)
        self.assertIn("read_tier_external_filter_unsupported", git_owner)
        self.assertIn("--no-config", search_owner)
        self.assertIn("build_child_process_environment", git_owner)
        self.assertIn("build_child_process_environment", search_owner)
        self.assertNotIn("read_helper", agent)

    def test_process_capture_is_bounded_binary_and_owned_outside_shell_runtime(self) -> None:
        capture = (ROOT / "src/local_agent/tools/process_output.py").read_text(encoding="utf-8")
        runtime = (ROOT / "src/local_agent/tools/process_runtime.py").read_text(encoding="utf-8")
        shell = (ROOT / "src/local_agent/tools/shell.py").read_text(encoding="utf-8")
        agent = (ROOT / "src/local_agent/agent.py").read_text(encoding="utf-8")
        self.assertIn("PROCESS_TOTAL_CAPTURE_LIMIT_BYTES", capture)
        self.assertIn("class BoundedByteCapture:", capture)
        self.assertNotIn("communicate(", runtime)
        self.assertNotIn("text=True", runtime)
        self.assertEqual(runtime.count("if progress == 0:"), 2)
        self.assertNotIn("[:30000]", shell)
        self.assertNotIn("process_output", agent)

    def test_session_task_continuity_is_typed_and_owned_outside_runtime(self) -> None:
        owner = (ROOT / "src/local_agent/session/continuity.py").read_text(encoding="utf-8")
        runtime = (ROOT / "src/local_agent/agent.py").read_text(encoding="utf-8")
        self.assertIn("class SessionTaskContinuityLifecycle:", owner)
        self.assertIn("current_contract.task_kind != \"unclear\"", owner)
        self.assertNotIn("import re", owner)
        self.assertNotIn("generate_requirement_contract", owner)
        self.assertNotIn("UNFINISHED_TERMINATIONS", owner)
        self.assertIn("and not delivered", owner)
        self.assertNotIn("tool_choice_queue", owner)
        self.assertNotIn("PatchReviewer", owner)
        self.assertNotIn("def _resolve_session_task_continuity", runtime)
        self.assertIn("turn_is_delivered(reason)", runtime)
        self.assertEqual(runtime.count("self._task_continuity."), 3)

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
            "def _tool_choice_result_metadata",
            "def _repeated_read_file_result",
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
        directive = (ROOT / "src/local_agent/runtime/tool_choice_directive.py").read_text(encoding="utf-8")
        self.assertIn("requeue_required", directive)
        self.assertNotIn("implementation_readiness_required", content)
        self.assertNotIn("read_only_unlocated", content)

    def test_runtime_uses_explicit_phase_components_not_a_provider_mixin(self) -> None:
        provider_context = (ROOT / "src/local_agent/runtime/provider_context.py").read_text(encoding="utf-8")
        self.assertIn("class ProviderContextPhase:", provider_context)
        self.assertNotIn("class ProviderContextMixin:", provider_context)

    def test_tool_choice_steering_helpers_live_with_queue_owner(self) -> None:
        queue = (ROOT / "src/local_agent/workflows/tool_choice/queue.py").read_text(encoding="utf-8")
        decision = (ROOT / "src/local_agent/workflows/tool_choice/decision.py").read_text(encoding="utf-8")
        gateway = (ROOT / "src/local_agent/tools/gateway.py").read_text(encoding="utf-8")
        self.assertIn("from .decision import tool_choice_steering_message", queue)
        self.assertIn("def tool_choice_steering_message", decision)
        self.assertIn("def tool_choice_steering_signature", decision)
        self.assertNotIn("def _tool_choice_steering_message", gateway)
        self.assertNotIn("def _tool_choice_steering_signature", gateway)

    def test_split_facades_and_debt_files_obey_complexity_ratchets(self) -> None:
        self.assertEqual(_production_complexity_failures(ROOT), [])

    def test_root_package_contains_only_entrypoints_and_thin_compatibility_facades(self) -> None:
        root_package = ROOT / "src/local_agent"
        for path in sorted(root_package.glob("*.py")):
            if path.name == "__init__.py" or path.name in ROOT_IMPLEMENTATION_ENTRYPOINTS:
                continue
            content = path.read_text(encoding="utf-8")
            self.assertIn("Compatibility", content, f"root implementation owner: {path.name}")
            self.assertLessEqual(
                len(content.splitlines()),
                64,
                f"compatibility facade grew into an implementation owner: {path.name}",
            )

    def test_cycle_sensitive_package_initializers_remain_import_free(self) -> None:
        for relative_path in IMPORT_FREE_PACKAGE_INITIALIZERS:
            tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
            imports = [
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
            ]
            self.assertEqual(imports, [], f"eager package import reintroduced in {relative_path}")

    def test_implementation_packages_do_not_depend_on_root_compatibility_facades(self) -> None:
        root_package = ROOT / "src/local_agent"
        facade_modules = {
            f"local_agent.{path.stem}"
            for path in root_package.glob("*.py")
            if path.name != "__init__.py" and path.name not in ROOT_IMPLEMENTATION_ENTRYPOINTS
        }
        dependencies: list[str] = []
        for path in sorted(root_package.rglob("*.py")):
            if path.parent == root_package:
                continue
            for line, target in _resolved_import_targets(path):
                if target in facade_modules:
                    dependencies.append(f"{path.relative_to(ROOT)}:{line} -> {target}")
        self.assertEqual(dependencies, [])

    def test_tools_and_providers_do_not_depend_on_runtime_or_frontend_implementations(self) -> None:
        dependencies: list[str] = []
        for package_name in ("tools", "providers"):
            for path in sorted((ROOT / f"src/local_agent/{package_name}").rglob("*.py")):
                for line, target in _resolved_import_targets(path):
                    if target.startswith(("local_agent.frontends", "local_agent.runtime")):
                        dependencies.append(f"{path.relative_to(ROOT)}:{line} -> {target}")
        self.assertEqual(dependencies, [])

    def test_cross_boundary_cancellation_and_terminal_input_have_one_low_level_owner(self) -> None:
        production = list((ROOT / "src/local_agent").rglob("*.py"))
        cancellation_owners = [
            path.relative_to(ROOT).as_posix()
            for path in production
            if "class RunCancellation:" in path.read_text(encoding="utf-8")
        ]
        terminal_input_owners = [
            path.relative_to(ROOT).as_posix()
            for path in production
            if "class TerminalInputSilencer:" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(cancellation_owners, ["src/local_agent/protocol/cancellation.py"])
        self.assertEqual(terminal_input_owners, ["src/local_agent/platform/terminal.py"])

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
        content = (ROOT / "src/local_agent/workflows/tool_choice/queue.py").read_text(encoding="utf-8")
        self.assertLess(
            content.index("if is_inspection_forbidden(prompt):"),
            content.index("read_only_decision = evaluate_read_only_phase("),
        )
        self.assertLess(
            content.index("read_only_decision = evaluate_read_only_phase("),
            content.index("implementation_decision = evaluate_implementation_phase("),
        )

    def test_split_owner_import_direction_is_acyclic_and_runtime_free(self) -> None:
        groups = (
            {
                "queue": "src/local_agent/workflows/tool_choice/queue.py",
                "decision": "src/local_agent/workflows/tool_choice/decision.py",
                "read_only": "src/local_agent/workflows/tool_choice/read_only.py",
                "implementation": "src/local_agent/workflows/tool_choice/implementation.py",
                "classification": "src/local_agent/workflows/tool_choice/classification.py",
            },
            {
                "read_only": "src/local_agent/review/read_only.py",
                "types": "src/local_agent/review/types.py",
                "claims": "src/local_agent/review/claims.py",
                "contract": "src/local_agent/review/contract.py",
                "validation": "src/local_agent/review/validation.py",
            },
        )
        for modules in groups:
            edges: dict[str, set[str]] = {name: set() for name in modules}
            for name, relative_path in modules.items():
                tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.ImportFrom) or node.level != 1 or not node.module:
                        continue
                    imported = node.module.split(".", 1)[0]
                    if imported in modules:
                        edges[name].add(imported)
                    self.assertFalse(imported.startswith("runtime"))
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

            for name in modules:
                visit(name)

    def test_workflow_profile_owner_and_runtime_facade_have_one_way_dependencies(self) -> None:
        owner = (ROOT / "src/local_agent/workflows/profile.py").read_text(encoding="utf-8")
        facade = (ROOT / "src/local_agent/runtime/workflow_profile.py").read_text(encoding="utf-8")
        runtime = (ROOT / "src/local_agent/agent.py").read_text(encoding="utf-8")
        self.assertNotIn("from .runtime_", owner)
        self.assertIn("from ..workflows.profile import", facade)
        self.assertIn("from .review import ReadOnlyReviewPhase", facade)
        self.assertNotIn("implementation_readiness_required", runtime)
        self.assertNotIn("if self._config.workflow_profile", runtime)

    def test_execution_policy_owner_is_pure_and_registry_has_no_duplicate_policy_tree(self) -> None:
        owner = (ROOT / "src/local_agent/tools/policy.py").read_text(encoding="utf-8")
        registry = (ROOT / "src/local_agent/tools/base.py").read_text(encoding="utf-8")
        self.assertNotIn("from .tools", owner)
        self.assertNotIn("from .agent", owner)
        self.assertNotIn("input(", owner)
        self.assertNotIn("event_callback", owner)
        self.assertIn("return evaluate_execution_policy(", registry)
        self.assertNotIn("def _approval_denial_reason", registry)
        self.assertNotIn("config_policy in", registry)

    def test_frontends_submit_typed_commands_through_one_runtime_boundary(self) -> None:
        cli = (ROOT / "src/local_agent/cli.py").read_text(encoding="utf-8")
        terminal = (ROOT / "src/local_agent/frontends/terminal/app.py").read_text(encoding="utf-8")
        registry = (ROOT / "src/local_agent/frontends/terminal/command_registry.py").read_text(encoding="utf-8")
        dispatcher = (ROOT / "src/local_agent/runtime/commands.py").read_text(encoding="utf-8")
        production = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "src/local_agent").rglob("*.py")
        )
        self.assertNotIn("runtime.run(", cli)
        self.assertNotIn("runtime.run(", terminal)
        self.assertIn("runtime.commands.dispatch(", cli)
        self.assertIn("runtime.commands.dispatch(", terminal)
        self.assertNotIn("class TerminalRuntime", registry)
        for method in (
            "def add_workspace_root",
            "def remove_workspace_root",
            "def reset_workspace_roots",
            "def move_workspace",
            "def set_session_approval_mode",
            "def set_session_tool_policy",
        ):
            self.assertNotIn(method, registry)
        self.assertIn("from ...protocol.commands import new_command", registry)
        self.assertNotIn("from .agent import", dispatcher)
        self.assertNotIn("tool_choice", dispatcher)
        self.assertNotIn('"SessionFinished"', production)

    def test_shared_composer_history_is_the_only_persistent_prompt_owner(self) -> None:
        terminal = (ROOT / "src/local_agent/frontends/terminal/app.py").read_text(encoding="utf-8")
        prompt = (ROOT / "src/local_agent/frontends/terminal/prompt.py").read_text(encoding="utf-8")
        controller = (ROOT / "src/local_agent/frontends/tui/controller.py").read_text(encoding="utf-8")
        history = (ROOT / "src/local_agent/frontends/composer_history.py").read_text(encoding="utf-8")
        cli = (ROOT / "src/local_agent/cli.py").read_text(encoding="utf-8")
        self.assertNotIn("PromptSession", terminal)
        self.assertNotIn("FileHistory", terminal)
        self.assertIn("from .prompt import build_terminal_prompt", terminal)
        self.assertIn("from prompt_toolkit import PromptSession", prompt)
        self.assertNotIn("FileHistory", prompt)
        self.assertIn("class ComposerHistory:", history)
        self.assertIn("composer_history=history", cli)
        self.assertIn("history.append(text)", terminal)
        self.assertIn("self._history.append(text)", controller)
        self.assertNotIn("write_text", terminal + prompt + controller)
        for forbidden_owner in (
            "AgentRuntime",
            "EvidenceLedger",
            "ToolChoiceQueue",
            "SessionStore",
            "runtime.commands",
        ):
            self.assertNotIn(forbidden_owner, history)
        self.assertEqual(
            sum(
                "class ComposerHistory:" in path.read_text(encoding="utf-8")
                for path in (ROOT / "src/local_agent/frontends").rglob("*.py")
            ),
            1,
        )

    def test_tui_history_search_is_pure_frontend_state_over_composer_snapshot(self) -> None:
        search = (ROOT / "src/local_agent/frontends/tui/history_search.py").read_text(encoding="utf-8")
        controller = (ROOT / "src/local_agent/frontends/tui/controller.py").read_text(encoding="utf-8")
        self.assertIn("from ..composer_history import HistorySnapshot", search)
        self.assertIn("class ComposerHistorySearch:", search)
        self.assertIn("self._history.snapshot", controller)
        for forbidden in (
            "AgentRuntime",
            "EvidenceLedger",
            "SessionStore",
            "runtime.commands",
            "write_text",
            "json",
            "re.compile",
        ):
            self.assertNotIn(forbidden, search)

    def test_tui_multiline_composer_has_one_pure_layout_owner(self) -> None:
        layout_path = ROOT / "src/local_agent/frontends/tui/composer_layout.py"
        layout = layout_path.read_text(encoding="utf-8")
        view = (ROOT / "src/local_agent/frontends/tui/view.py").read_text(encoding="utf-8")
        controller = (ROOT / "src/local_agent/frontends/tui/controller.py").read_text(encoding="utf-8")
        self.assertIn("class ComposerLayout:", layout)
        self.assertIn("def layout_composer(", layout)
        self.assertIn("def move_composer_cursor_vertical(", layout)
        self.assertIn("MAX_COMPOSER_ROWS = 6", layout)
        self.assertIn("from .composer_layout import layout_composer", view)
        self.assertIn("from .composer_layout import move_composer_cursor_vertical", controller)
        self.assertEqual(
            sum(
                "class ComposerLayout:" in path.read_text(encoding="utf-8")
                for path in (ROOT / "src/local_agent/frontends/tui").glob("*.py")
            ),
            1,
        )
        for forbidden in (
            "AgentRuntime",
            "EvidenceLedger",
            "SessionStore",
            "ToolChoiceQueue",
            "runtime.commands",
            "write_text",
            "open(",
        ):
            self.assertNotIn(forbidden, layout)

    def test_tui_pending_prompt_queue_is_one_pure_frontend_single_slot_owner(self) -> None:
        owner = (ROOT / "src/local_agent/frontends/tui/pending_prompt.py").read_text(encoding="utf-8")
        controller = (ROOT / "src/local_agent/frontends/tui/controller.py").read_text(encoding="utf-8")
        self.assertIn("class PendingPromptQueue:", owner)
        self.assertIn("self._pending: PendingPrompt | None", owner)
        self.assertIn("self._pending_prompts = PendingPromptQueue()", controller)
        self.assertEqual(
            sum(
                "class PendingPromptQueue:" in path.read_text(encoding="utf-8")
                for path in (ROOT / "src/local_agent/frontends/tui").glob("*.py")
            ),
            1,
        )
        for forbidden in (
            "AgentRuntime",
            "EvidenceLedger",
            "SessionStore",
            "runtime.commands",
            "write_text",
            "open(",
            "json",
        ):
            self.assertNotIn(forbidden, owner)

    def test_tui_is_an_independent_single_writer_frontend(self) -> None:
        tui_root = ROOT / "src/local_agent/frontends/tui"
        production = {
            path.name: path.read_text(encoding="utf-8")
            for path in tui_root.glob("*.py")
        }
        controller = production["controller.py"]
        model = production["model.py"]
        worker = production["worker.py"]
        screen = production["screen.py"]
        native_renderer = production["native_renderer.py"]
        cli = (ROOT / "src/local_agent/cli.py").read_text(encoding="utf-8")
        runtime = (ROOT / "src/local_agent/agent.py").read_text(encoding="utf-8")

        forbidden = (
            "AgentRuntime",
            "ToolRegistry",
            "ExecutionPolicy",
            "EvidenceLedger",
            "Finalization",
            "tool_choice_queue",
            "provider_protocol",
        )
        for filename, content in production.items():
            for owner in forbidden:
                self.assertNotIn(owner, content, f"{filename} duplicates Runtime owner {owner}")
        self.assertNotIn("runtime.commands.dispatch", controller)
        self.assertIn("self._runtime.commands.dispatch(command)", worker)
        self.assertNotIn("AgentRuntime", model)
        self.assertNotIn("AgentRuntime", screen)
        self.assertNotIn("curses", "\n".join(production.values()))
        self.assertIn("class NativeScrollbackRenderer:", native_renderer)
        self.assertIn("self._commit_entries(pending, width)", native_renderer)
        self.assertEqual(native_renderer.count("\\x1b[?1049h"), 1)
        self.assertNotIn("\\x1b[3J", native_renderer)
        self.assertIn("from .screen import run_inline_screen", production["app.py"])
        self.assertNotIn("arguments", model.split("def project_agent_event", 1)[1].split("def _todo_text", 1)[0])
        self.assertIn('frontend.add_argument("--tui"', cli)
        self.assertIn("TuiEventSink(tui_mailbox, show_tools=not args.hide_tools)", cli)
        self.assertLessEqual(len(re.findall(r"^    def ", runtime, flags=re.MULTILINE)), 71)
        self.assertLessEqual(len(runtime.splitlines()), LEGACY_COMPLEXITY_DEBT_CEILINGS["src/local_agent/agent.py"])

    def test_tui_cross_thread_messages_are_typed_and_bounded(self) -> None:
        messages = (ROOT / "src/local_agent/frontends/tui/messages.py").read_text(encoding="utf-8")
        mailbox = (ROOT / "src/local_agent/frontends/tui/mailbox.py").read_text(encoding="utf-8")
        worker = (ROOT / "src/local_agent/frontends/tui/worker.py").read_text(encoding="utf-8")

        self.assertIn("@dataclass(frozen=True)", messages)
        self.assertIn("class TuiMailbox:", mailbox)
        self.assertIn("self._capacity = capacity", mailbox)
        self.assertIn("_coalesce_delta", mailbox)
        self.assertNotIn("Queue()", worker)
        self.assertIn("Queue(maxsize=command_capacity)", worker)
        self.assertIn("class TuiInteractionBridge:", worker)
        input_owner = (ROOT / "src/local_agent/frontends/tui/input.py").read_text(encoding="utf-8")
        self.assertIn("class BracketedPasteDecoder:", input_owner)
        self.assertEqual(
            sum(
                "class BracketedPasteDecoder:" in path.read_text(encoding="utf-8")
                for path in (ROOT / "src/local_agent/frontends/tui").glob("*.py")
            ),
            1,
        )
        self.assertNotIn("commands.dispatch", input_owner)

    def test_all_runtime_provider_waits_receive_the_shared_cancel_signal(self) -> None:
        missing: list[str] = []
        for path in (ROOT / "src/local_agent").rglob("*.py"):
            if path.name in {"benchmark.py", "chat_runtime.py"}:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                if node.func.id != "call_chat_with_timeout":
                    continue
                if not any(keyword.arg == "cancel_event" for keyword in node.keywords):
                    missing.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(missing, [])

    def test_provider_streaming_has_one_parser_owner_and_redacted_delta_boundary(self) -> None:
        production = list((ROOT / "src/local_agent").rglob("*.py"))
        parser_owners = [
            path.relative_to(ROOT).as_posix()
            for path in production
            if "class _SseDecoder" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(parser_owners, ["src/local_agent/providers/stream.py"])

        owner = (ROOT / "src/local_agent/providers/stream.py").read_text(encoding="utf-8")
        llm = (ROOT / "src/local_agent/providers/llm.py").read_text(encoding="utf-8")
        runtime = (ROOT / "src/local_agent/agent.py").read_text(encoding="utf-8")
        prompt = (ROOT / "src/local_agent/runtime/prompt.py").read_text(encoding="utf-8")
        renderer_path = ROOT / "src/local_agent/frontends/terminal/assistant.py"
        renderer = renderer_path.read_text(encoding="utf-8")
        self.assertNotIn("from .agent", owner)
        self.assertNotIn("from .runtime_", owner)
        self.assertIn("from .stream import iter_chat_completion_response", llm)
        self.assertNotIn("class _SseDecoder", runtime)
        self.assertNotIn("data: [DONE]", runtime)
        self.assertEqual(runtime.count("use_stream=True"), 1)
        self.assertNotIn("assistant_delta_callback", runtime)
        lifecycle = (ROOT / "src/local_agent/runtime/assistant_message.py").read_text(encoding="utf-8")
        run_output = (ROOT / "src/local_agent/runtime/run_output.py").read_text(encoding="utf-8")
        mailbox = (ROOT / "src/local_agent/frontends/tui/mailbox.py").read_text(encoding="utf-8")
        self.assertIn("class AssistantMessageLifecycle:", lifecycle)
        self.assertIn("class RunOutputLifecycle:", run_output)
        self.assertIn('"AssistantDelta"', lifecycle)
        self.assertIn('"AssistantMessage"', lifecycle)
        self.assertIn('"AssistantMessageAborted"', lifecycle)
        self.assertNotIn("self._events.finish_turn", runtime)
        self.assertIn('"AssistantMessage"', mailbox)
        self.assertIn('"TurnFinished"', mailbox)
        self.assertNotIn("arguments_preview", prompt)

        tree = ast.parse(renderer)
        delta_renderer = next(
            node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "render_delta"
        )
        delta_source = ast.get_source_segment(renderer, delta_renderer) or ""
        self.assertNotIn("arguments", delta_source)

    def test_explore_subagent_is_opt_in_readonly_nonrecursive_and_runtime_thin(self) -> None:
        owner = (ROOT / "src/local_agent/workflows/explore_subagent.py").read_text(encoding="utf-8")
        runtime = (ROOT / "src/local_agent/agent.py").read_text(encoding="utf-8")
        tools = importlib.import_module("local_agent.tools")
        subagent = importlib.import_module("local_agent.explore_subagent")
        expected_tools = {
            "read_file", "list_files", "glob_files", "search_code", "lsp_symbols",
            "lsp_workspace_symbols", "lsp_document_symbols", "lsp_definition",
            "lsp_references", "lsp_diagnostics", "lsp_status",
        }

        self.assertEqual(set(subagent.EXPLORE_TOOL_NAMES), expected_tools)
        self.assertNotIn("delegate_explore", subagent.EXPLORE_TOOL_NAMES)
        self.assertNotIn("AgentRuntime(", owner)
        self.assertNotIn("from .agent", owner)
        self.assertNotIn("EXPLORE_TOOL_NAMES", runtime)
        self.assertNotIn('name == "delegate_explore"', runtime)
        self.assertNotIn("delegate_explore", tools.create_default_registry().tool_names())
        enabled = tools.create_runtime_registry(object(), True, 60)
        self.assertEqual(enabled.tool_names().count("delegate_explore"), 1)

    def test_lsp_rename_preview_has_one_readonly_owner_and_no_evidence_or_write_bypass(self) -> None:
        tools = importlib.import_module("local_agent.tools")
        workspace_edit = (ROOT / "src/local_agent/lsp/workspace_edit.py").read_text(encoding="utf-8")
        rename_tool = (ROOT / "src/local_agent/tools/lsp_rename.py").read_text(encoding="utf-8")
        legacy_lsp = (ROOT / "src/local_agent/tools/lsp.py").read_text(encoding="utf-8")
        verification = (ROOT / "src/local_agent/evidence/verification.py").read_text(encoding="utf-8")
        runtime = (ROOT / "src/local_agent/agent.py").read_text(encoding="utf-8")

        registered = tools.create_default_registry()
        schema = next(item for item in registered.schemas() if item["function"]["name"] == "lsp_rename_preview")
        self.assertFalse(schema["function"]["parameters"]["additionalProperties"])
        self.assertNotIn("apply", schema["function"]["parameters"]["properties"])
        self.assertNotIn("lsp_rename_preview", verification)
        self.assertNotIn("lsp_rename_preview", legacy_lsp)
        self.assertNotIn("lsp_rename_preview", runtime)
        self.assertNotIn("write_bytes", workspace_edit)
        self.assertNotIn("write_text", workspace_edit)
        self.assertNotIn("apply_patch", rename_tool.split("description=", 1)[0])
        self.assertNotIn("ToolChoice", rename_tool)

    def test_lsp_code_action_preview_has_one_owner_and_cannot_execute_or_write(self) -> None:
        tools = importlib.import_module("local_agent.tools")
        owner = (ROOT / "src/local_agent/tools/lsp_code_action.py").read_text(encoding="utf-8")
        client = (ROOT / "src/local_agent/lsp/client.py").read_text(encoding="utf-8")
        legacy_lsp = (ROOT / "src/local_agent/tools/lsp.py").read_text(encoding="utf-8")
        verification = (ROOT / "src/local_agent/evidence/verification.py").read_text(encoding="utf-8")
        runtime = (ROOT / "src/local_agent/agent.py").read_text(encoding="utf-8")
        definitions = [
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "src/local_agent").rglob("*.py")
            if 'name="lsp_code_action_preview"' in path.read_text(encoding="utf-8")
        ]

        self.assertEqual(definitions, ["src/local_agent/tools/lsp_code_action.py"])
        schema = next(
            item for item in tools.create_default_registry().schemas()
            if item["function"]["name"] == "lsp_code_action_preview"
        )
        self.assertFalse(schema["function"]["parameters"]["additionalProperties"])
        self.assertNotIn("apply", schema["function"]["parameters"]["properties"])
        self.assertNotIn("query", schema["function"]["parameters"]["properties"])
        self.assertIn('self.request("codeAction/resolve"', client)
        self.assertNotIn("lsp_code_action_preview", verification)
        self.assertNotIn("lsp_code_action_preview", legacy_lsp)
        self.assertNotIn("lsp_code_action_preview", runtime)
        self.assertNotIn("_parse_workspace_edit", owner)
        self.assertNotIn("_parse_text_edits", owner)
        self.assertNotIn("workspace/executeCommand", owner)
        self.assertNotIn("workspace/applyEdit", owner)
        self.assertNotIn("execute_command(", owner)
        self.assertNotIn("write_bytes", owner)
        self.assertNotIn("write_text", owner)
        self.assertEqual(owner.count("build_workspace_edit_preview("), 1)

    def test_jdtls_metadata_containment_stays_in_config_and_process_launch_owners(self) -> None:
        config = (ROOT / "src/local_agent/lsp/config.py").read_text(encoding="utf-8")
        client = (ROOT / "src/local_agent/lsp/client.py").read_text(encoding="utf-8")
        rename = (ROOT / "src/local_agent/tools/lsp_rename.py").read_text(encoding="utf-8")
        code_action = (ROOT / "src/local_agent/tools/lsp_code_action.py").read_text(encoding="utf-8")

        self.assertIn("java.import.generatesMetadataFilesAtProjectRoot=false", config)
        self.assertNotIn("java.import.generatesMetadataFilesAtProjectRoot", client)
        self.assertIn("env=_child_process_environment(server)", client)
        self.assertNotIn("JAVA_TOOL_OPTIONS", rename)
        self.assertNotIn("JAVA_TOOL_OPTIONS", code_action)
        self.assertNotIn("process_environment", rename)
        self.assertNotIn("process_environment", code_action)
        self.assertNotIn("no files were written", rename)
        self.assertNotIn("no files were written", code_action)
        self.assertNotIn("LspServerConfig", ROOT.joinpath("src/local_agent/agent.py").read_text(encoding="utf-8"))

    def test_external_lsp_execution_containment_stays_in_config_and_client_owners(self) -> None:
        config = (ROOT / "src/local_agent/lsp/config.py").read_text(encoding="utf-8")
        client = (ROOT / "src/local_agent/lsp/client.py").read_text(encoding="utf-8")
        process_environment = (ROOT / "src/local_agent/tools/process_environment.py").read_text(encoding="utf-8")
        runtime = (ROOT / "src/local_agent/agent.py").read_text(encoding="utf-8")

        self.assertNotIn("node_modules", config)
        self.assertNotIn(".venv", config)
        self.assertNotIn("venv/bin", config)
        self.assertIn("allow_workspace_executable", config)
        self.assertIn("build_child_process_environment", client)
        self.assertIn("is_provider_credential_environment_key", client)
        self.assertIn("start_new_session=os.name == \"posix\"", client)
        self.assertIn("os.killpg", client)
        self.assertIn("_process_group_id", client)
        self.assertNotIn("LspServerConfig", runtime)
        self.assertNotIn("StdioLspClient", runtime)
        self.assertNotIn("local_agent.lsp", process_environment)

    def test_runtime_strategy_owners_do_not_reintroduce_business_keyword_guards(self) -> None:
        strategy_files = (
            ROOT / "src/local_agent/workflows/contracts.py",
            ROOT / "src/local_agent/workflows/tool_choice/queue.py",
            ROOT / "src/local_agent/workflows/tool_choice/decision.py",
            ROOT / "src/local_agent/workflows/tool_choice/read_only.py",
            ROOT / "src/local_agent/workflows/tool_choice/implementation.py",
            ROOT / "src/local_agent/workflows/tool_choice/classification.py",
            ROOT / "src/local_agent/workflows/explore.py",
            ROOT / "src/local_agent/review/read_only.py",
            ROOT / "src/local_agent/review/claims.py",
            ROOT / "src/local_agent/review/contract.py",
            ROOT / "src/local_agent/review/validation.py",
            ROOT / "src/local_agent/runtime/review.py",
            ROOT / "src/local_agent/review/safe_partial.py",
            ROOT / "src/local_agent/review/output_lifecycle.py",
            ROOT / "src/local_agent/steering/evidence.py",
            ROOT / "src/local_agent/review/readiness.py",
            ROOT / "src/local_agent/workflows/profile.py",
            ROOT / "src/local_agent/runtime/workflow_profile.py",
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

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from local_agent.agent import AgentRuntime
from local_agent.config import AgentConfig
from local_agent.patch.anchored import hash_text
from local_agent.run_context import RunContext
from local_agent.session.jsonl_store import JsonlSessionStore
from local_agent.session_task_continuity import CONTINUITY_EVENT
from local_agent.session_task_continuity import SessionTaskContinuityLifecycle
from local_agent.protocol.events import ListEventSink
from local_agent.task_contract import generate_requirement_contract
from local_agent.tool_observation import ToolResultSummary
from local_agent.tools.base import ToolContext
from local_agent.tools.files import patch_file
from local_agent.tools.git import capture_git_baseline


class SessionTaskContinuityTests(unittest.TestCase):
    def test_agent_continuation_cannot_deliver_current_test_without_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            state_dir, store = self._prepare_pending_session(workspace)
            sink = ListEventSink()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                state_dir=state_dir,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            _ContinuationMissingDiffClient.calls = 0
            with patch("local_agent.agent.OpenAICompatibleClient", _ContinuationMissingDiffClient):
                runtime = AgentRuntime(
                    config,
                    session_id=store.session_id,
                    show_tool_logs=False,
                    event_sink=sink,
                )
                final = runtime.run("Continue after the interruption.")
            summary = next(event.payload for event in sink.events if event.type == "RunSummary")
            finished = next(event for event in sink.events if event.type == "TurnFinished")

        self.assertIn("未完成/未验证", final)
        self.assertEqual(summary["termination_reason"], "incomplete_delivery")
        self.assertEqual(summary["task_continuity"]["status"], "pending")
        self.assertFalse(finished.payload["delivered"])

    def test_agent_continuation_closes_only_after_current_test_diff_and_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            self._init_git(workspace)
            (workspace / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
            (workspace / "test_value.py").write_text(
                "import unittest\nimport value\n\nclass ValueTest(unittest.TestCase):\n"
                "    def test_value(self):\n        self.assertEqual(value.VALUE, 2)\n",
                encoding="utf-8",
            )
            self._git(workspace, "add", "value.py", "test_value.py")
            self._git(workspace, "commit", "-m", "baseline")
            state_dir = workspace / ".state"
            store = JsonlSessionStore(workspace, state_dir=state_dir)
            owner_runtime = self._runtime(workspace, state_dir, store)
            result = patch_file(
                {
                    "path": "value.py",
                    "tag": hash_text("VALUE = 1\n"),
                    "start_line": 1,
                    "end_line": 1,
                    "old_text": "VALUE = 1\n",
                    "new_text": "VALUE = 2\n",
                    "mode": "replace",
                },
                owner_runtime._tool_context,
            )
            self.assertFalse(result.is_error, result.content)
            self._begin_code_run(owner_runtime, workspace, "run-1")
            owner_runtime._run.tool_choice_results.append(self._write_result("value.py"))
            SessionTaskContinuityLifecycle(owner_runtime).finish("interrupt")

            sink = ListEventSink()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                state_dir=state_dir,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            _ContinuationDeliveryClient.calls = 0
            with patch("local_agent.agent.OpenAICompatibleClient", _ContinuationDeliveryClient):
                runtime = AgentRuntime(
                    config,
                    session_id=store.session_id,
                    show_tool_logs=False,
                    event_sink=sink,
                )
                final = runtime.run("Continue after the interruption.")

            summaries = [event.payload for event in sink.events if event.type == "RunSummary"]
            finished = [event for event in sink.events if event.type == "TurnFinished"]

        self.assertEqual(runtime._run.requirement_contract.task_kind, "code-implementation")
        self.assertIn("[Runtime delivery report]", final)
        self.assertEqual(summaries[-1]["verification_plan"]["passed"], 4)
        self.assertEqual(summaries[-1]["task_continuity"]["status"], "closed")
        self.assertEqual(len(finished), 1)
        self.assertTrue(finished[0].payload["delivered"])

    def test_interrupt_after_write_inherits_typed_contract_without_prompt_grammar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            self._init_git(workspace)
            state_dir = workspace / ".state"
            store = JsonlSessionStore(workspace, state_dir=state_dir)
            runtime = self._runtime(workspace, state_dir, store)
            lifecycle = SessionTaskContinuityLifecycle(runtime)
            self._write_with_patch(runtime, "value.py", "VALUE = 1\n", "VALUE = 2\n")
            runtime._run.begin(
                run_id="run-1",
                started_monotonic=1.0,
                deadline_monotonic=None,
                run_start_index=0,
                git_baseline=capture_git_baseline(workspace),
                prompt="Fix the value and verify it.",
                requirement_contract=generate_requirement_contract("Fix the value and verify it."),
                requirement_contract_context="",
                design_evidence_roots=(),
            )
            runtime._run.tool_choice_results.append(self._write_result("value.py"))

            summary = lifecycle.finish("interrupt")
            current = generate_requirement_contract("Continue after the interruption.")
            inherited, pending = lifecycle.resolve(current, capture_git_baseline(workspace))
            payload = store.load_event_payloads(CONTINUITY_EVENT)[-1]

        self.assertEqual(current.task_kind, "unclear")
        self.assertEqual(inherited.task_kind, "code-implementation")
        self.assertIsNotNone(pending)
        self.assertEqual(pending.write_paths, ("value.py",))
        self.assertEqual(summary["status"], "pending")
        self.assertNotIn("prompt", payload)
        self.assertNotIn("arguments", payload)
        self.assertNotIn("objective", payload)

    def test_unfinished_terminations_are_bounded_and_before_write_closes(self) -> None:
        for reason in ("interrupt", "budget", "length", "incomplete_delivery"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp).resolve()
                self._init_git(workspace)
                state_dir = workspace / ".state"
                store = JsonlSessionStore(workspace, state_dir=state_dir)
                runtime = self._runtime(workspace, state_dir, store)
                lifecycle = SessionTaskContinuityLifecycle(runtime)
                self._write_with_patch(runtime, "value.py", "VALUE = 1\n", "VALUE = 2\n")
                self._begin_code_run(runtime, workspace, "run-write")
                runtime._run.tool_choice_results.append(self._write_result("value.py"))
                self.assertEqual(lifecycle.finish(reason)["status"], "pending")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            self._init_git(workspace)
            state_dir = workspace / ".state"
            store = JsonlSessionStore(workspace, state_dir=state_dir)
            runtime = self._runtime(workspace, state_dir, store)
            lifecycle = SessionTaskContinuityLifecycle(runtime)
            self._begin_code_run(runtime, workspace, "run-no-write")
            self.assertEqual(lifecycle.finish("interrupt")["status"], "closed")
            current = generate_requirement_contract("Continue after the interruption.")
            resolved, pending = lifecycle.resolve(current, capture_git_baseline(workspace))
            self.assertEqual(resolved.task_kind, "unclear")
            self.assertIsNone(pending)

    def test_explicit_new_task_wins_and_external_content_or_revision_invalidates(self) -> None:
        for mutation in ("content", "revision"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp).resolve()
                self._init_git(workspace)
                state_dir = workspace / ".state"
                store = JsonlSessionStore(workspace, state_dir=state_dir)
                runtime = self._runtime(workspace, state_dir, store)
                lifecycle = SessionTaskContinuityLifecycle(runtime)
                self._write_with_patch(runtime, "value.py", "VALUE = 1\n", "VALUE = 2\n")
                self._begin_code_run(runtime, workspace, "run-1")
                runtime._run.tool_choice_results.append(self._write_result("value.py"))
                lifecycle.finish("interrupt")

                explicit = generate_requirement_contract("Read the repository and explain the current value. Do not edit.")
                resolved, pending = lifecycle.resolve(explicit, capture_git_baseline(workspace))
                self.assertEqual(resolved.task_kind, "read-only")
                self.assertIsNone(pending)

                if mutation == "content":
                    (workspace / "value.py").write_text("VALUE = 3\n", encoding="utf-8")
                else:
                    (workspace / "note.txt").write_text("external\n", encoding="utf-8")
                    self._git(workspace, "add", "note.txt")
                    self._git(workspace, "commit", "-m", "external revision")
                current = generate_requirement_contract("Continue after the interruption.")
                stale, pending = lifecycle.resolve(current, capture_git_baseline(workspace))
                self.assertEqual(stale.task_kind, "unclear")
                self.assertIsNone(pending)

    def test_external_change_after_resolution_blocks_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            state_dir, store = self._prepare_pending_session(workspace)
            runtime = self._runtime(workspace, state_dir, store)
            lifecycle = SessionTaskContinuityLifecycle(runtime)
            current = generate_requirement_contract("Continue after the interruption.")
            contract, pending = lifecycle.resolve(current, capture_git_baseline(workspace))
            runtime._run.begin(
                run_id="run-2",
                started_monotonic=2.0,
                deadline_monotonic=None,
                run_start_index=0,
                git_baseline=capture_git_baseline(workspace),
                prompt="Continue after the interruption.",
                requirement_contract=contract,
                requirement_contract_context="",
                design_evidence_roots=(),
                pending_task=pending,
            )
            (workspace / "value.py").write_text("VALUE = 3\n", encoding="utf-8")

            self.assertFalse(lifecycle.revalidate(runtime._run.verification_plan))
            self.assertEqual(runtime._run.verification_plan.coverage(delivery_only=True)["blocked"], 4)

    def test_reinterrupted_continuation_preserves_carried_and_current_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            self._init_git(workspace)
            (workspace / "first.py").write_text("FIRST = 1\n", encoding="utf-8")
            (workspace / "second.py").write_text("SECOND = 1\n", encoding="utf-8")
            self._git(workspace, "add", "first.py", "second.py")
            self._git(workspace, "commit", "-m", "baseline")
            state_dir = workspace / ".state"
            store = JsonlSessionStore(workspace, state_dir=state_dir)
            runtime = self._runtime(workspace, state_dir, store)
            lifecycle = SessionTaskContinuityLifecycle(runtime)

            self._apply_patch(runtime, "first.py", "FIRST = 1\n", "FIRST = 2\n")
            self._begin_code_run(runtime, workspace, "run-1")
            runtime._run.tool_choice_results.append(self._write_result("first.py"))
            lifecycle.finish("interrupt")

            contract, pending = lifecycle.resolve(
                generate_requirement_contract("Continue after the interruption."),
                capture_git_baseline(workspace),
            )
            runtime._run.begin(
                run_id="run-2",
                started_monotonic=2.0,
                deadline_monotonic=None,
                run_start_index=0,
                git_baseline=capture_git_baseline(workspace),
                prompt="Continue after the interruption.",
                requirement_contract=contract,
                requirement_contract_context="",
                design_evidence_roots=(),
                pending_task=pending,
            )
            self._apply_patch(runtime, "first.py", "FIRST = 2\n", "FIRST = 3\n")
            runtime._run.tool_choice_results.append(self._write_result("first.py"))
            self._apply_patch(runtime, "second.py", "SECOND = 1\n", "SECOND = 2\n")
            runtime._run.tool_choice_results.append(self._write_result("second.py"))

            summary = lifecycle.finish("interrupt")
            _, carried = lifecycle.resolve(
                generate_requirement_contract("Continue after the interruption."),
                capture_git_baseline(workspace),
            )

        self.assertEqual(summary["status"], "pending")
        self.assertIsNotNone(carried)
        self.assertEqual(carried.write_paths, ("first.py", "second.py"))

    def test_continuation_requires_current_read_test_diff_and_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            self._init_git(workspace)
            state_dir = workspace / ".state"
            store = JsonlSessionStore(workspace, state_dir=state_dir)
            runtime = self._runtime(workspace, state_dir, store)
            lifecycle = SessionTaskContinuityLifecycle(runtime)
            self._write_with_patch(runtime, "value.py", "VALUE = 1\n", "VALUE = 2\n")
            self._begin_code_run(runtime, workspace, "run-1")
            runtime._run.tool_choice_results.append(self._write_result("value.py"))
            lifecycle.finish("interrupt")
            current = generate_requirement_contract("Continue after the interruption.")
            contract, pending = lifecycle.resolve(current, capture_git_baseline(workspace))
            runtime._run.begin(
                run_id="run-2",
                started_monotonic=2.0,
                deadline_monotonic=None,
                run_start_index=0,
                git_baseline=capture_git_baseline(workspace),
                prompt="Continue after the interruption.",
                requirement_contract=contract,
                requirement_contract_context="",
                design_evidence_roots=(),
                pending_task=pending,
            )
            read = ToolResultSummary("read_file", "current source", False, False, path="value.py")
            test = ToolResultSummary(
                "run_tests",
                "ok",
                False,
                False,
                metadata={"executed_command": "python3 -m unittest", "execution_status": "succeeded"},
            )
            runtime._run.tool_choice_results.extend((read, test))
            runtime._run.verification_plan.observe(runtime._run.tool_choice_results)
            self.assertEqual(runtime._run.verification_plan.coverage(delivery_only=True)["passed"], 2)
            self.assertTrue(runtime._run.verification_plan.unresolved_delivery_items())

            diff = ToolResultSummary(
                "git_diff",
                "diff --git a/value.py b/value.py\n--- a/value.py\n+++ b/value.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n",
                False,
                False,
                metadata={"patch_review": {"changed_paths": ["value.py"]}},
            )
            runtime._run.tool_choice_results.append(diff)
            runtime._run.verification_plan.observe(runtime._run.tool_choice_results)
            runtime._run.verification_plan.record_patch_review(
                passed=True,
                reason="deterministic review passed",
                refs=["git_diff:post-write"],
            )
            self.assertFalse(runtime._run.verification_plan.unresolved_delivery_items())

    def _runtime(self, workspace: Path, state_dir: Path, store: JsonlSessionStore) -> SimpleNamespace:
        context = ToolContext(workspace=workspace, approval_mode="yolo", state_dir=state_dir, session_id=store.session_id)
        return SimpleNamespace(
            _run=RunContext(),
            _session=store,
            _tool_context=context,
            _workspace_context=SimpleNamespace(primary=workspace, additional_roots=()),
        )

    def _prepare_pending_session(self, workspace: Path) -> tuple[Path, JsonlSessionStore]:
        self._init_git(workspace)
        (workspace / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
        (workspace / "test_value.py").write_text(
            "import unittest\nimport value\n\nclass ValueTest(unittest.TestCase):\n"
            "    def test_value(self):\n        self.assertEqual(value.VALUE, 2)\n",
            encoding="utf-8",
        )
        self._git(workspace, "add", "value.py", "test_value.py")
        self._git(workspace, "commit", "-m", "baseline")
        state_dir = workspace / ".state"
        store = JsonlSessionStore(workspace, state_dir=state_dir)
        runtime = self._runtime(workspace, state_dir, store)
        result = patch_file(
            {
                "path": "value.py",
                "tag": hash_text("VALUE = 1\n"),
                "start_line": 1,
                "end_line": 1,
                "old_text": "VALUE = 1\n",
                "new_text": "VALUE = 2\n",
                "mode": "replace",
            },
            runtime._tool_context,
        )
        self.assertFalse(result.is_error, result.content)
        self._begin_code_run(runtime, workspace, "run-1")
        runtime._run.tool_choice_results.append(self._write_result("value.py"))
        SessionTaskContinuityLifecycle(runtime).finish("interrupt")
        return state_dir, store

    def _begin_code_run(self, runtime: SimpleNamespace, workspace: Path, run_id: str) -> None:
        prompt = "Fix the value and verify it."
        runtime._run.begin(
            run_id=run_id,
            started_monotonic=1.0,
            deadline_monotonic=None,
            run_start_index=0,
            git_baseline=capture_git_baseline(workspace),
            prompt=prompt,
            requirement_contract=generate_requirement_contract(prompt),
            requirement_contract_context="",
            design_evidence_roots=(),
        )

    def _write_with_patch(self, runtime: SimpleNamespace, path: str, before: str, after: str) -> None:
        target = runtime._workspace_context.primary / path
        target.write_text(before, encoding="utf-8")
        self._git(runtime._workspace_context.primary, "add", path)
        self._git(runtime._workspace_context.primary, "commit", "-m", "baseline")
        self._apply_patch(runtime, path, before, after)

    def _apply_patch(self, runtime: SimpleNamespace, path: str, before: str, after: str) -> None:
        result = patch_file(
            {
                "path": path,
                "tag": hash_text(before),
                "start_line": 1,
                "end_line": 1,
                "old_text": before,
                "new_text": after,
                "mode": "replace",
            },
            runtime._tool_context,
        )
        self.assertFalse(result.is_error, result.content)

    def _write_result(self, path: str) -> ToolResultSummary:
        return ToolResultSummary("apply_patch", "patched", False, False, changed=True, path=path)

    def _init_git(self, workspace: Path) -> None:
        self._git(workspace, "init")
        self._git(workspace, "config", "user.email", "test@example.com")
        self._git(workspace, "config", "user.name", "Test User")

    def _git(self, workspace: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True, text=True)


class _ContinuationDeliveryClient:
    calls = 0

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        del messages, tools, timeout
        responses = (
            self._tool("read", "read_file", {"path": "value.py"}),
            self._tool("test", "run_tests", {"command": "python3 -m unittest"}),
            self._tool("diff", "git_diff", {}),
            type("Response", (), {"message": {"content": "The carried patch is verified by the current test and diff."}})(),
        )
        response = responses[min(self.calls, len(responses) - 1)]
        self.__class__.calls += 1
        return response

    @staticmethod
    def _tool(call_id: str, name: str, arguments: dict[str, object]) -> object:
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                }
            },
        )()


class _ContinuationMissingDiffClient(_ContinuationDeliveryClient):
    calls = 0

    def chat(self, messages, tools, *, timeout=None):
        del messages, tools, timeout
        if self.calls == 0:
            response = self._tool("read", "read_file", {"path": "value.py"})
        elif self.calls == 1:
            response = self._tool("test", "run_tests", {"command": "python3 -m unittest"})
        else:
            response = type("Response", (), {"message": {"content": "The current test passes."}})()
        self.__class__.calls += 1
        return response


if __name__ == "__main__":
    unittest.main()

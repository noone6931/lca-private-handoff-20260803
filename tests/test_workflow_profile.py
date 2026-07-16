from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent.agent import AgentRuntime
from local_agent.cli import main
from local_agent.config import AgentConfig, ConfigError, load_config
from local_agent.protocol.events import ListEventSink
from local_agent.run_context import RunContext
from local_agent.task_contract import generate_requirement_contract
from local_agent.workflow_profile import resolve_workflow_profile
from local_agent.workflow_profile import workflow_profile_for_run


class _ProfileRuntimeClient:
    calls: list[dict[str, object]] = []
    reviewer_calls = 0
    primary_calls = 0

    def __init__(self, config: AgentConfig):
        self.config = config

    @classmethod
    def reset(cls) -> None:
        cls.calls = []
        cls.reviewer_calls = 0
        cls.primary_calls = 0

    def chat(self, messages, tools, *, timeout=None, model=None, tool_choice=None):
        review = any(
            "LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content"))
            for message in messages
        )
        type(self).calls.append(
            {
                "review": review,
                "tools": [schema["function"]["name"] for schema in tools],
                "tool_choice": tool_choice,
            }
        )
        if review:
            type(self).reviewer_calls += 1
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "review-pass",
                                "type": "function",
                                "function": {
                                    "name": "submit_read_only_review",
                                    "arguments": json.dumps(
                                        {
                                            "verdict": "pass",
                                            "confidence": 0.95,
                                            "reason": "The source-scoped answer preserves its unlocated boundary.",
                                        }
                                    ),
                                },
                            }
                        ],
                    }
                },
            )()
        type(self).primary_calls += 1
        if type(self).primary_calls == 1:
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "read-owner",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": "src/Owner.py"}),
                                },
                            }
                        ],
                    }
                },
            )()
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": (
                        "## Source facts\n"
                        "- `CandidateOwner` is declared in `src/Owner.py:1`.\n"
                        "## Unlocated\n"
                        "- Callers remain unlocated after the bounded inspection; no implementation change was made."
                    )
                }
            },
        )()


class _CliRuntime:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def run(self, _prompt: str) -> str:
        return "done"


class WorkflowProfileTests(unittest.TestCase):
    def test_auto_resolves_only_from_typed_requirement_contract(self) -> None:
        coding = generate_requirement_contract("实现缓存失效修复并补充测试。")
        enterprise = generate_requirement_contract(
            "只读分析当前服务 owner、调用链和影响范围，不要修改。"
        )
        readiness = generate_requirement_contract(
            "只读做证据化技术设计，选择可实施切片；如果 owner 和依赖不闭合则 blocked。"
        )

        self.assertEqual(resolve_workflow_profile("auto", coding).resolved_profile, "coding")
        self.assertEqual(
            resolve_workflow_profile("auto", enterprise).resolved_profile,
            "enterprise-evidence",
        )
        self.assertEqual(
            resolve_workflow_profile("auto", readiness).resolved_profile,
            "readiness-audit",
        )
        explicit_readiness = resolve_workflow_profile("readiness-audit", readiness)
        self.assertTrue(explicit_readiness.capabilities.read_only_explore)
        self.assertTrue(explicit_readiness.capabilities.implementation_readiness_review)
        self.assertTrue(explicit_readiness.capabilities.safe_partial_delivery)

    def test_explicit_profiles_do_not_widen_inapplicable_typed_contracts(self) -> None:
        ordinary = generate_requirement_contract("实现缓存失效修复并补充测试。")
        readiness = generate_requirement_contract(
            "只读做证据化技术设计，选择可实施切片；如果 owner 和依赖不闭合则 blocked。"
        )

        explicit_readiness = resolve_workflow_profile("readiness-audit", ordinary)
        explicit_enterprise = resolve_workflow_profile("enterprise-evidence", readiness)

        self.assertFalse(explicit_readiness.capabilities.implementation_readiness_review)
        self.assertIn("typed_readiness_not_required", explicit_readiness.reason)
        self.assertFalse(explicit_enterprise.capabilities.isolated_read_only_review)
        self.assertIn("requires_readiness-audit", explicit_enterprise.reason)
        self.assertEqual(ordinary.task_kind, "code-implementation")
        self.assertEqual(readiness.task_kind, "read-only")

    def test_absent_typed_contract_enables_no_optional_heavy_capability(self) -> None:
        run = SimpleNamespace(requirement_contract=None, workflow_profile=None)
        resolution = workflow_profile_for_run(run)
        context = RunContext()
        context.design_evidence_coverage.reset(("/workspace/source",))

        self.assertEqual(resolution.resolved_profile, "coding")
        self.assertIn("typed_requirement_contract_absent", resolution.reason)
        self.assertEqual(
            resolution.capabilities.enabled_names(),
            (
                "agent_loop",
                "tools",
                "approval",
                "requirement_contract",
                "evidence_ledger",
                "tool_choice_queue",
                "completion_audit",
                "patch_reviewer",
                "verification_plan",
                "finalization",
            ),
        )
        self.assertEqual(context.active_design_evidence_roots(), ())

    def test_auto_profile_is_resolved_again_at_each_run_boundary(self) -> None:
        context = RunContext(workflow_profile_selector="auto")
        contracts = (
            generate_requirement_contract("实现缓存失效修复并补充测试。"),
            generate_requirement_contract(
                "只读做证据化技术设计，选择可实施切片；如果 owner 和依赖不闭合则 blocked。"
            ),
        )
        for index, contract in enumerate(contracts, start=1):
            context.begin(
                run_id=f"run-{index}",
                started_monotonic=float(index),
                deadline_monotonic=None,
                run_start_index=0,
                git_baseline={},
                prompt=contract.objective,
                requirement_contract=contract,
                requirement_contract_context="typed contract",
                design_evidence_roots=("/workspace/source",),
            )

        self.assertEqual(context.workflow_profile.resolved_profile, "readiness-audit")
        self.assertEqual(context.requirement_contract, contracts[-1])

    def test_config_accepts_cli_json_and_environment_selectors_and_rejects_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "agent.json"
            config_path.write_text(json.dumps({"workflow_profile": "enterprise-evidence"}), encoding="utf-8")
            base = dict(
                config_path=str(config_path),
                cwd=tmp,
                provider="bailian",
                api_base_url=None,
                api_key=None,
                model=None,
                max_steps=None,
                budget_seconds=None,
                approval_mode=None,
            )
            with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "token"}, clear=True):
                from_json = load_config(**base)
                overridden = load_config(**base, workflow_profile="coding")
                defaulted = load_config(**{**base, "config_path": None})
            with patch.dict(
                os.environ,
                {"DASHSCOPE_API_KEY": "token", "LCA_WORKFLOW_PROFILE": "readiness-audit"},
                clear=True,
            ):
                from_env = load_config(**{**base, "config_path": None})

        self.assertEqual(from_json.workflow_profile, "enterprise-evidence")
        self.assertEqual(overridden.workflow_profile, "coding")
        self.assertEqual(defaulted.workflow_profile, "auto")
        self.assertEqual(from_env.workflow_profile, "readiness-audit")
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "token"}, clear=True):
                with self.assertRaisesRegex(ConfigError, "workflow_profile must be one of"):
                    load_config(
                        config_path=None,
                        cwd=tmp,
                        provider="bailian",
                        api_base_url=None,
                        api_key=None,
                        model=None,
                        max_steps=None,
                        budget_seconds=None,
                        approval_mode=None,
                        workflow_profile="wide-open",
                    )

    def test_cli_forwards_workflow_profile_without_changing_other_config_inputs(self) -> None:
        config = SimpleNamespace(workspace=Path("/tmp"), state_dir=None)
        with (
            patch("local_agent.cli.load_config", return_value=config) as load,
            patch("local_agent.cli.AgentRuntime", _CliRuntime),
        ):
            code = main(["--workflow-profile", "coding", "inspect", "this"])

        self.assertEqual(code, 0)
        self.assertEqual(load.call_args.kwargs["workflow_profile"], "coding")
        self.assertEqual(load.call_args.kwargs["approval_mode"], None)

    def test_explicit_coding_disables_heavy_read_only_hooks_but_keeps_source_read_and_final(self) -> None:
        runtime, sink, result = self._run_owner_task("coding")

        self.assertIn("CandidateOwner", result)
        self.assertEqual(_ProfileRuntimeClient.reviewer_calls, 0)
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["triggers"], 0)
        self.assertEqual(runtime._last_run_summary["safe_partial_report"]["emitted"], 0)
        self.assertEqual(runtime._last_run_summary["workflow_profile"]["resolved_profile"], "coding")
        self.assertEqual(runtime._run.requirement_contract.task_kind, "read-only")
        self.assertEqual(runtime._tool_context.approval_mode, "yolo")
        self.assertIn("apply_patch", runtime._registry.tool_names())
        self.assertFalse(runtime._run.read_only_explore_finalized)
        self.assertEqual(runtime._read_only_review_phase.safe_partial_for_terminal("provider_error"), "")
        self.assertTrue(all(not call["review"] for call in _ProfileRuntimeClient.calls))
        self.assertTrue(all(call["tool_choice"] is None for call in _ProfileRuntimeClient.calls))
        self.assertIn("selector=coding, resolved=coding", runtime.status_summary())
        profile_events = [
            event for event in sink.events
            if event.type == "ContextUpdated" and event.payload.get("kind") == "workflow_profile"
        ]
        self.assertEqual(len(profile_events), 1)
        self.assertEqual(profile_events[0].payload["resolved_profile"], "coding")
        self.assertFalse(
            any(
                event.type == "RuntimeSteering" and event.payload.get("kind") == "workflow_profile"
                for event in sink.events
            )
        )
        session_records = [
            json.loads(line) for line in runtime._session.path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(
            any(
                record.get("event") == "workflow_profile"
                and record.get("payload", {}).get("selector") == "coding"
                for record in session_records
            )
        )

    def test_explicit_enterprise_evidence_preserves_isolated_reviewer(self) -> None:
        runtime, _sink, result = self._run_owner_task("enterprise-evidence")

        self.assertIn("CandidateOwner", result)
        self.assertEqual(_ProfileRuntimeClient.reviewer_calls, 1)
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["triggers"], 1)
        self.assertEqual(
            runtime._last_run_summary["workflow_profile"]["resolved_profile"],
            "enterprise-evidence",
        )

    def _run_owner_task(self, profile: str) -> tuple[AgentRuntime, ListEventSink, str]:
        _ProfileRuntimeClient.reset()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        workspace = Path(temp.name).resolve()
        (workspace / "src").mkdir()
        (workspace / "src/Owner.py").write_text("class CandidateOwner:\n    pass\n", encoding="utf-8")
        (workspace / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
        sink = ListEventSink()
        config = AgentConfig(
            provider="openai-compatible",
            api_base_url="https://example.invalid/v1",
            api_key="token",
            model="model",
            workspace=workspace,
            max_steps=0,
            budget_seconds=None,
            approval_mode="yolo",
            workflow_profile=profile,
        )
        with patch("local_agent.agent.OpenAICompatibleClient", _ProfileRuntimeClient):
            runtime = AgentRuntime(config, show_tool_logs=False, event_sink=sink)
            result = runtime.run(
                "只读分析当前服务 owner、调用链和影响范围，给出源码证据；不要修改。"
            )
        return runtime, sink, result


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.agent import AgentRuntime
from local_agent.config import AgentConfig
from local_agent.explore_handoff import build_explore_handoff
from local_agent.read_only_reviewer import parse_reviewer_result
from local_agent.read_only_reviewer import reviewer_messages
from local_agent.read_only_reviewer import should_review_read_only_candidate
from local_agent.steering.final_answer import SourceEvidence
from local_agent.task_contract import generate_requirement_contract
from local_agent.llm import LlmTimeoutError


class _ReviewerFlowClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config
        self._primary_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        is_reviewer = any(
            message.get("role") == "user" and "LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content"))
            for message in messages
        )
        if is_reviewer:
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "verdict": "revise",
                                "confidence": 0.96,
                                "findings": [
                                    {
                                        "claim": "已证实真实 owner 是 PayServiceImpl",
                                        "issue": "same-domain code is only analogous without a requested behavior binding",
                                        "action": "label it as a reusable candidate and keep the owner unlocated",
                                    }
                                ],
                                "reason": "owner binding is absent",
                            }
                        )
                    }
                },
            )()
        self._primary_calls += 1
        if self._primary_calls == 1:
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "read-pay",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path":"src/PayServiceImpl.java"}'},
                            }
                        ],
                    }
                },
            )()
        if self._primary_calls == 2:
            return type("Response", (), {"message": {"content": "已证实真实 owner 是 PayServiceImpl，DDL 和 API 也由它负责。"}})()
        return type(
            "Response",
            (),
            {"message": {"content": "PayServiceImpl 仅是已读到的可复用候选；真实 owner、DDL 和 API 仍未定位，需要进一步证据。"}},
        )()


class _InventedDesignReviewerClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config
        self._primary_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        is_reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        if is_reviewer:
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "verdict": "revise",
                                "confidence": 0.99,
                                "findings": [
                                    {
                                        "claim": "PayBillRecordInfoVo and SF prefix already exist",
                                        "issue": "invented repository types and numbering behavior",
                                        "action": "move them to proposal or pending confirmation",
                                    }
                                ],
                                "reason": "no source binding exists",
                            }
                        )
                    }
                },
            )()
        self._primary_calls += 1
        if self._primary_calls == 1:
            return type("Response", (), {"message": {"content": "现有 PayBillRecordInfoVo 使用 SF 前缀，并包含多级审批和退款复用。"}})()
        return type(
            "Response",
            (),
            {"message": {"content": "设计建议：可新增结算记录 DTO 和编号规则；现有表、审批流、退款复用与前缀均待确认。"}},
        )()


class _InvalidReviewerClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            return type("Response", (), {"message": {"content": "not-json"}})()
        return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()


class _ReviewerPassClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config
        self.primary_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            return type("Response", (), {"message": {"content": '{"verdict":"pass","confidence":0.92,"findings":[],"reason":"explicit binding is present"}'}})()
        self.primary_calls += 1
        if self.primary_calls == 1:
            return type("Response", (), {"message": {"content": None, "tool_calls": [{"id": "read", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"src/SettlementOwner.java"}'}}]}})()
        return type("Response", (), {"message": {"content": "SettlementOwner.handle is directly bound by the inspected source call chain."}})()


class _ReviewerProtocolClient(_InvalidReviewerClient):
    def chat(self, messages, tools, *, timeout=None):
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            return type("Response", (), {"message": {"content": None, "tool_calls": [{"id": "forbidden", "function": {"name": "read_file", "arguments": "{}"}}]}})()
        return super().chat(messages, tools, timeout=timeout)


class _ReviewerTimeoutClient(_InvalidReviewerClient):
    def chat(self, messages, tools, *, timeout=None):
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            raise LlmTimeoutError("reviewer timeout")
        return super().chat(messages, tools, timeout=timeout)


class _NoncompliantRewriteClient(_ReviewerFlowClient):
    def chat(self, messages, tools, *, timeout=None):
        is_reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        if is_reviewer:
            return super().chat(messages, tools, timeout=timeout)
        self._primary_calls += 1
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if self._primary_calls == 1:
            return type("Response", (), {"message": {"content": "已证实真实 owner 是 PayServiceImpl。"}})()
        return type("Response", (), {"message": {"content": "已证实真实 owner 是 PayServiceImpl。"}})()


def _config(workspace: Path) -> AgentConfig:
    return AgentConfig(
        provider="openai-compatible",
        api_base_url="https://example.invalid/v1",
        api_key="token",
        model="model",
        workspace=workspace,
        max_steps=0,
        budget_seconds=None,
        approval_mode="yolo",
    )


class ReadOnlyReviewerTests(unittest.TestCase):
    def test_handoff_is_bounded_and_conservative_about_source_reads(self) -> None:
        contract = generate_requirement_contract("只读分析服务 owner 和影响范围，不要修改。")
        handoff = build_explore_handoff(
            request="只读分析服务 owner 和影响范围，不要修改。",
            contract=contract,
            requirement_evidence=(),
            source_evidence=(SourceEvidence("src/PayServiceImpl.java", "class PayServiceImpl {}", root="primary"),),
            records=(),
            tool_results=(),
        )
        classifications = {item.classification for item in handoff.items}
        self.assertIn("observed_candidate", classifications)
        self.assertIn("direct_binding", handoff.to_dict()["review_categories"])
        self.assertIn("proposal", handoff.to_dict()["review_categories"])
        messages = reviewer_messages(handoff, "PayServiceImpl is the owner")
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        self.assertNotIn("class PayServiceImpl", messages[0]["content"])

    def test_handoff_preserves_requirement_and_multi_root_evidence_with_late_binding(self) -> None:
        contract = generate_requirement_contract("只读分析 SettlementOwner 的 owner 和 impact。")
        late_binding = "package demo; " + ("padding " * 200) + " SettlementOwner.handle(request);"
        handoff = build_explore_handoff(
            request="只读分析 SettlementOwner 的 owner 和 impact。",
            contract=contract,
            requirement_evidence=(
                type("Requirement", (), {"path": "requirements.md", "content": "must settle", "root": "primary", "scope": "root_local"})(),
            ),
            source_evidence=(
                SourceEvidence("src/primary/Owner.java", late_binding, root="primary"),
                SourceEvidence("src/additional/Client.java", "SettlementOwner.handle()", root="additional"),
            ),
            records=(),
            tool_results=(),
        )
        items = handoff.items
        self.assertTrue(any(item.classification == "requirement_fact" and item.path == "requirements.md" for item in items))
        self.assertTrue(any(item.classification == "observed_candidate" and item.root == "primary" and "SettlementOwner.handle" in item.summary for item in items))
        self.assertTrue(any(item.root == "additional" for item in items))

    def test_handoff_prioritizes_precise_binding_over_many_generic_records(self) -> None:
        contract = generate_requirement_contract("只读分析 owner 和 impact。")
        precise = type(
            "Tool",
            (),
            {
                "name": "lsp_references", "path": "service-b/Owner.java", "is_error": False, "useless": False,
                "metadata": {"evidence_paths": ["service-b/Owner.java"], "evidence_root_label": "service-b", "evidence_scope": "root_local"},
                "content": "service-b/Owner.java:42: SettlementOwner.handle(request)",
            },
        )()
        records = tuple(
            type("Record", (), {"tool": "read_file", "subject": f"src/{index}.java", "status": "ok", "summary": "generic", "details": {"evidence_root_label": f"root-{index % 3}"}})()
            for index in range(30)
        )
        handoff = build_explore_handoff(
            request="只读分析 owner 和 impact。", contract=contract,
            requirement_evidence=(type("Requirement", (), {"path": "requirements.md", "content": "rule", "root": "primary", "scope": "root_local"})(),),
            source_evidence=(), records=records, tool_results=(precise,),
        )
        self.assertTrue(any(item.classification == "requirement_fact" for item in handoff.items))
        self.assertTrue(any(item.classification == "direct_binding" and item.tool == "lsp_references" for item in handoff.items))
        self.assertTrue(all(root in {item.root for item in handoff.items} for root in {"root-0", "root-1", "root-2"}))

    def test_reviewer_result_requires_typed_validated_json(self) -> None:
        parsed = parse_reviewer_result(
            '{"verdict":"revise","confidence":0.8,"findings":[{"claim":"owner","issue":"gap","action":"qualify"}],"reason":"missing binding"}'
        )
        self.assertEqual(parsed.verdict, "revise")
        with self.assertRaises(ValueError):
            parse_reviewer_result('{"verdict":"pass","confidence":1,"findings":[{"claim":"x","issue":"x","action":"x"}],"reason":"x"}')
        with self.assertRaises(ValueError):
            parse_reviewer_result("review looks good")

    def test_trigger_policy_excludes_document_and_git_metadata(self) -> None:
        self.assertTrue(should_review_read_only_candidate(
            generate_requirement_contract("只读分析当前服务 owner、调用链和影响范围，不要修改。"),
            "只读分析当前服务 owner、调用链和影响范围，不要修改。",
        ))
        self.assertFalse(should_review_read_only_candidate(
            generate_requirement_contract("只根据需求文档 Markdown 分析需求，不要检查代码。"),
            "只根据需求文档 Markdown 分析需求，不要检查代码。",
        ))
        self.assertFalse(should_review_read_only_candidate(
            generate_requirement_contract("当前primary是不是Git仓库？"),
            "当前primary是不是Git仓库？",
        ))

    def test_owner_candidate_is_rewritten_once_with_isolated_tools_empty_review(self) -> None:
        _ReviewerFlowClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "src").mkdir()
            (workspace / "src/PayServiceImpl.java").write_text("class PayServiceImpl {}\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerFlowClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
        self.assertIn("可复用候选", answer)
        self.assertIn("仍未定位", answer)
        review_calls = [call for call in _ReviewerFlowClient.calls if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(m.get("content")) for m in call["messages"])]
        self.assertEqual(len(review_calls), 1)
        self.assertEqual(review_calls[0]["tools"], [])
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["rewrites"], 1)
        self.assertNotIn("LCA_READ_ONLY_EVIDENCE_REVIEW", "\n".join(str(m) for m in runtime._messages))

    def test_invented_design_is_rewritten_as_proposal(self) -> None:
        _InventedDesignReviewerClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            with patch("local_agent.agent.OpenAICompatibleClient", _InventedDesignReviewerClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("基于当前代码给出可实施设计草案：数据模型、状态流转和接口，禁止编造现有类型。")
        self.assertIn("设计建议", answer)
        self.assertIn("待确认", answer)
        self.assertNotIn("PayBillRecordInfoVo", answer)
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["rewrites"], 1)

    def test_invalid_reviewer_output_returns_truthful_unverified_terminal(self) -> None:
        _InvalidReviewerClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _InvalidReviewerClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改。")
        self.assertIn("未完成/未验证", answer)
        self.assertEqual(runtime._last_run_summary["termination_reason"], "read_only_reviewer_unverified")
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["errors"], {"invalid_output": 1})

    def test_explicit_source_binding_can_pass_one_isolated_review(self) -> None:
        _ReviewerPassClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "src").mkdir()
            (workspace / "src/SettlementOwner.java").write_text("class SettlementOwner { void handle() {} }\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerPassClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("只读分析 SettlementOwner 的 owner 和调用链，不要修改。")
        self.assertIn("directly bound", answer)
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["verdicts"], {"pass": 1})
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["rewrites"], 0)

    def test_reviewer_protocol_and_timeout_are_unverified_terminal_outcomes(self) -> None:
        for client, expected in ((_ReviewerProtocolClient, "protocol_error"), (_ReviewerTimeoutClient, "provider_error")):
            with self.subTest(client=client.__name__), tempfile.TemporaryDirectory() as tmp:
                client.calls = []
                with patch("local_agent.agent.OpenAICompatibleClient", client):
                    runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                    answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改。")
                self.assertIn("未完成/未验证", answer)
                self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["errors"], {expected: 1})

    def test_deadline_reserve_skips_reviewer_without_returning_unreviewed_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
            contract = generate_requirement_contract("只读分析当前服务 owner 和影响范围，不要修改。")
            now = time.monotonic()
            runtime._run.begin(
                run_id="review-budget", started_monotonic=now, deadline_monotonic=now + 0.1,
                run_start_index=0, git_baseline={}, prompt="只读分析当前服务 owner 和影响范围，不要修改。",
                requirement_contract=contract, requirement_contract_context="", design_evidence_roots=(),
            )
            outcome = runtime._read_only_review_phase.review_candidate("PayServiceImpl is the verified owner.")
        self.assertEqual(outcome.kind, "unverified")
        self.assertIn("未经审查", outcome.terminal_message)

    def test_rewrite_that_repeats_exact_reviewed_claim_stops_unverified(self) -> None:
        _NoncompliantRewriteClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _NoncompliantRewriteClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改。")
        self.assertIn("未完成/未验证", answer)
        self.assertEqual(runtime._last_run_summary["termination_reason"], "read_only_reviewer_unverified")
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["errors"], {"rewrite_noncompliant": 1})


if __name__ == "__main__":
    unittest.main()

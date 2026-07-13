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
from local_agent.read_only_reviewer import candidate_claim_units
from local_agent.read_only_reviewer import rewrite_complies_with_review
from local_agent.read_only_reviewer import parse_reviewer_result
from local_agent.read_only_reviewer import ReviewerValidationError
from local_agent.read_only_reviewer import reviewer_repair_messages
from local_agent.read_only_reviewer import reviewer_messages
from local_agent.read_only_reviewer import reviewer_output_tool_schema
from local_agent.read_only_reviewer import REVIEWER_OUTPUT_TOOL_NAME
from local_agent.read_only_reviewer import should_review_read_only_candidate
from local_agent.steering.final_answer import SourceEvidence
from local_agent.steering.final_answer import SteeringDecision
from local_agent.task_contract import generate_requirement_contract
from local_agent.llm import LlmTimeoutError


def _review_submit(payload: dict) -> object:
    return type(
        "Response",
        (),
        {
            "message": {
                "content": None,
                "tool_calls": [
                    {
                        "id": "review-submit",
                        "type": "function",
                        "function": {"name": REVIEWER_OUTPUT_TOOL_NAME, "arguments": json.dumps(payload)},
                    }
                ],
            }
        },
    )()


class _ReviewerFlowClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config
        self._primary_calls = 0
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        is_reviewer = any(
            message.get("role") == "user" and "LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content"))
            for message in messages
        )
        if is_reviewer:
            self._review_calls += 1
            if self._review_calls > 1:
                return _review_submit({"verdict": "pass", "confidence": 0.96, "findings": [], "reason": "rewrite is scoped"})
            return _review_submit(
                {
                    "verdict": "revise",
                    "confidence": 0.96,
                    "findings": [
                        {
                            "claim_id": "c001",
                            "claim": "已证实真实 owner 是 PayServiceImpl",
                            "issue": "same-domain code is only analogous without a requested behavior binding",
                            "action": "label it as a reusable candidate and keep the owner unlocated",
                        }
                    ],
                    "reason": "owner binding is absent",
                }
            )
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
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        is_reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        if is_reviewer:
            self._review_calls += 1
            if self._review_calls > 1:
                return _review_submit({"verdict": "pass", "confidence": 0.99, "findings": [], "reason": "rewrite is proposal-only"})
            return _review_submit(
                {
                    "verdict": "revise",
                    "confidence": 0.99,
                    "findings": [
                        {
                            "claim_id": "c001",
                            "claim": "现有 PayBillRecordInfoVo 使用 SF 前缀，并包含多级审批和退款复用",
                            "issue": "invented repository types and numbering behavior",
                            "action": "move them to proposal or pending confirmation",
                        }
                    ],
                    "reason": "no source binding exists",
                }
            )
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


class _ReviewerRepairClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config
        self._primary_calls = 0
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        if reviewer:
            self._review_calls += 1
            if self._review_calls == 1:
                return type("Response", (), {"message": {"content": json.dumps({
                    "verdict": "pass", "confidence": 0.9,
                    "findings": [{"claim_id": "c001", "issue": "unsupported", "action": "downgrade"}],
                    "reason": "contradictory shape",
                })}})()
            if self._review_calls == 2:
                return _review_submit({
                    "verdict": "revise", "confidence": 0.9,
                    "findings": [{"claim_id": "c001", "issue": "unsupported owner", "action": "mark unlocated"}],
                    "reason": "binding absent",
                })
            return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite is scoped"})
        self._primary_calls += 1
        if self._primary_calls == 1:
            return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()
        return type("Response", (), {"message": {"content": "PayServiceImpl is an analogous candidate; the owner is unlocated."}})()


class _ReviewerRepairExhaustedClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            return type("Response", (), {"message": {"content": '{"verdict":"revise","confidence":0.9,"findings":"wrong","reason":"x"}'}})()
        return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()


class _ReviewerUnverifiedRewriteClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config
        self.primary_calls = 0
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            if self._review_calls > 1:
                return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite is truthful"})
            return _review_submit(
                {
                    "verdict": "unverified",
                    "confidence": 0.9,
                    "findings": [
                        {
                            "claim_id": "c001",
                            "claim": "owner conclusion is unsupported",
                            "issue": "no direct behavior-to-owner binding exists",
                            "action": "report the owner as unlocated and the inspected code as analogous",
                        }
                    ],
                    "reason": "owner cannot be verified from the bounded evidence",
                }
            )
        self.primary_calls += 1
        content = (
            "已证实真实 owner 是 PayServiceImpl。"
            if self.primary_calls == 1
            else "真实 owner 仍未定位；PayServiceImpl 仅是已读到的弱相关候选。"
        )
        return type("Response", (), {"message": {"content": content}})()


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


class _ReviewerMultipleOutputClient(_InvalidReviewerClient):
    def chat(self, messages, tools, *, timeout=None):
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            payload = json.dumps({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "x"})
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {"id": "one", "function": {"name": REVIEWER_OUTPUT_TOOL_NAME, "arguments": payload}},
                            {"id": "two", "function": {"name": REVIEWER_OUTPUT_TOOL_NAME, "arguments": payload}},
                        ],
                    }
                },
            )()
        return super().chat(messages, tools, timeout=timeout)


class _ReviewerMalformedOutputClient(_InvalidReviewerClient):
    def chat(self, messages, tools, *, timeout=None):
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "bad-submit",
                                "function": {"name": REVIEWER_OUTPUT_TOOL_NAME, "arguments": "{"},
                            }
                        ],
                    }
                },
            )()
        return super().chat(messages, tools, timeout=timeout)


class _ReviewerTimeoutClient(_InvalidReviewerClient):
    def chat(self, messages, tools, *, timeout=None):
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            raise LlmTimeoutError("reviewer timeout")
        return super().chat(messages, tools, timeout=timeout)


class _ParaphraseReviewerClient(_InvalidReviewerClient):
    def chat(self, messages, tools, *, timeout=None):
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            return type(
                "Response",
                (),
                {"message": {"content": '{"verdict":"revise","confidence":0.9,"findings":[{"claim_id":"c999","claim":"PayServiceImpl is the verified owner","issue":"unsupported","action":"qualify"}],"reason":"missing binding"}'}},
            )()
        return type("Response", (), {"message": {"content": "已证实真实 owner 是 PayServiceImpl。"}})()


class _ReviewerXmlArtifactClient(_InvalidReviewerClient):
    def chat(self, messages, tools, *, timeout=None):
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            return type(
                "Response",
                (),
                {"message": {"content": "<tool_call><function=read_file><parameter=path>secret</parameter></function></tool_call>"}},
            )()
        return super().chat(messages, tools, timeout=timeout)


class _ReviewerRepairTimeoutClient(_InvalidReviewerClient):
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            if self._review_calls == 1:
                return type("Response", (), {"message": {"content": "not-json"}})()
            raise LlmTimeoutError("repair timeout")
        return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()


class _ReviewerRepairProtocolClient(_InvalidReviewerClient):
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            if self._review_calls == 1:
                return type("Response", (), {"message": {"content": "not-json"}})()
            return type(
                "Response",
                (),
                {"message": {"content": None, "tool_calls": [{"id": "forbidden", "function": {"name": "read_file", "arguments": '{"path":"secret"}'}}]}},
            )()
        return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()


class _ReviewerRepairXmlClient(_InvalidReviewerClient):
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            if self._review_calls == 1:
                return type("Response", (), {"message": {"content": "not-json"}})()
            return type("Response", (), {"message": {"content": "<tool_call><function=read_file><parameter=path>secret</parameter></function></tool_call>"}})()
        return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()


class _NoncompliantRewriteClient(_ReviewerFlowClient):
    def chat(self, messages, tools, *, timeout=None):
        is_reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        if is_reviewer:
            self._review_calls += 1
            return _review_submit(
                {
                    "verdict": "revise",
                    "confidence": 0.9,
                    "findings": [{"claim_id": "c001", "issue": "unsupported", "action": "qualify"}],
                    "reason": "binding absent",
                }
            )
        self._primary_calls += 1
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if self._primary_calls == 1:
            return type("Response", (), {"message": {"content": "已证实真实 owner 是 PayServiceImpl。"}})()
        return type("Response", (), {"message": {"content": "已证实真实 owner 是 PayServiceImpl。"}})()


class _ReviewerLastGateClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config
        self._primary_calls = 0
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        if reviewer:
            self._review_calls += 1
            return _review_submit(
                {
                    "verdict": "revise",
                    "confidence": 0.9,
                    "findings": [{"claim_id": "c001", "issue": "unsupported repository detail", "action": "mark it unverified"}],
                    "reason": "no direct binding",
                }
            )
        self._primary_calls += 1
        content = (
            "现有 Redis key、权限接口和模板服务已经由 PayServiceImpl 实现。"
            if self._primary_calls in {1, 3}
            else "需要补充需求引用后再给出结论。"
        )
        return type("Response", (), {"message": {"content": content}})()


class _OneShotDeterministicRewrite:
    kind = "requirement_evidence"

    def __init__(self) -> None:
        self._used = False

    def decide(self, context):
        if self._used or context.content != "需要补充需求引用后再给出结论。":
            return None
        self._used = True
        return SteeringDecision(
            kind=self.kind,
            message="Runtime steering: rewrite the final answer with a requirement citation.",
            payload={},
            force_final_answer_without_tools=True,
        )


class _SpoofedDocumentConsistencyClient:
    """A reviewer that falsely labels a direct reconciliation as unresolved."""

    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self._primary_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        if reviewer:
            return _review_submit(
                {
                    "verdict": "pass",
                    "confidence": 0.99,
                    "findings": [],
                    "reason": "claimed unresolved",
                    "document_consistency": {
                        "stance": "reported_unresolved",
                        "conflict_evidence_ids": ["e001", "e002"],
                        "supporting_evidence_ids": [],
                    },
                }
            )
        self._primary_calls += 1
        if self._primary_calls == 1:
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {"id": "read-policy", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"policy.md"}'}},
                            {"id": "read-prototype", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"prototype.html"}'}},
                        ],
                    }
                },
            )()
        return type(
            "Response",
            (),
            {"message": {"content": "policy.md 要求字段留空；prototype.html 显示有值。两份资料没有冲突，因为 prototype 是完成态。 FORGED_RECONCILIATION"}},
        )()


def _config(workspace: Path, *, provider: str = "openai-compatible") -> AgentConfig:
    return AgentConfig(
        provider=provider,
        api_base_url="https://example.invalid/v1",
        api_key="token",
        model="model",
        workspace=workspace,
        max_steps=0,
        budget_seconds=None,
        approval_mode="yolo",
    )


class ReadOnlyReviewerTests(unittest.TestCase):
    def test_document_consistency_spoofed_unresolved_pass_becomes_safe_partial(self) -> None:
        _SpoofedDocumentConsistencyClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "policy.md").write_text("Field must remain blank.\n", encoding="utf-8")
            (workspace / "prototype.html").write_text("<p>Field has a value.</p>\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _SpoofedDocumentConsistencyClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("只根据 Markdown 和 HTML 分析资料一致性，不要检查代码。")

        self.assertIn("安全部分交付", answer)
        self.assertIn("未消解的资料冲突", answer)
        self.assertIn("policy.md", answer)
        self.assertIn("prototype.html", answer)
        self.assertNotIn("FORGED_RECONCILIATION", answer)
        self.assertEqual(runtime._last_run_summary["termination_reason"], "read_only_reviewer_unverified")
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["errors"], {"invalid_output": 1})

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
        messages = reviewer_messages(handoff, candidate_claim_units("PayServiceImpl is the owner"))
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        schema = reviewer_output_tool_schema(candidate_claim_units("PayServiceImpl is the owner"))
        self.assertEqual(schema["function"]["name"], REVIEWER_OUTPUT_TOOL_NAME)
        self.assertEqual(schema["function"]["parameters"]["additionalProperties"], False)
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
        units = candidate_claim_units("owner")
        parsed = parse_reviewer_result(
            '{"verdict":"revise","confidence":0.8,"findings":[{"claim_id":"c001","claim":"owner","issue":"gap","action":"qualify"}],"reason":"missing binding"}',
            claim_units=units,
        )
        self.assertEqual(parsed.verdict, "revise")
        with self.assertRaises(ValueError):
            parse_reviewer_result('{"verdict":"pass","confidence":1,"findings":[{"claim_id":"c001","claim":"x","issue":"x","action":"x"}],"reason":"x"}', claim_units=units)
        with self.assertRaises(ValueError):
            parse_reviewer_result("review looks good", claim_units=units)

    def test_output_tool_schema_and_parser_reject_extra_or_oversized_payload_fields(self) -> None:
        units = candidate_claim_units("unsupported owner claim")
        schema = reviewer_output_tool_schema(units)
        parameters = schema["function"]["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(parameters["properties"]["findings"]["maxItems"], 8)

        invalid_payloads = (
            {
                "verdict": "pass", "confidence": 0.9, "findings": [], "reason": "ok", "extra": "no",
            },
            {
                "verdict": "revise",
                "confidence": 0.9,
                "findings": [{"claim_id": "c001", "issue": "x", "action": "y", "extra": "no"}],
                "reason": "x",
            },
            {
                "verdict": "pass", "confidence": 0.9, "findings": [], "reason": "x" * 1601,
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ReviewerValidationError):
                    parse_reviewer_result(json.dumps(payload), claim_units=units)

    def test_reviewer_prompt_and_repair_repeat_the_parser_shape_limits(self) -> None:
        handoff = build_explore_handoff(
            request="review", contract=generate_requirement_contract("只读分析当前服务 owner，不要修改文件。"),
            requirement_evidence=(), source_evidence=(), records=(), tool_results=(),
        )
        messages = reviewer_messages(handoff, candidate_claim_units("candidate"))
        prompt = messages[0]["content"]
        self.assertIn("shorter than 9000", prompt)
        self.assertIn("at most 8", prompt)
        self.assertIn("`pass` verdict requires exactly 0", prompt)
        repair = reviewer_repair_messages(handoff, candidate_claim_units("candidate"), {"error_code": "findings_too_many"})
        self.assertIn("at most 8", repair[0]["content"])
        self.assertIn("no more than 8 highest-risk findings", repair[-1]["content"])
        self.assertIn("original output tool", repair[-1]["content"])

    def test_document_consistency_contract_passes_an_explicit_unresolved_conflict(self) -> None:
        contract = generate_requirement_contract(
            "只根据需求 Markdown、原型 HTML 和示例图分析需求，不要检查代码。"
        )
        handoff = build_explore_handoff(
            request="compare the requested documents",
            contract=contract,
            requirement_evidence=(),
            source_evidence=(),
            records=(),
            tool_results=(),
        )
        prompt = reviewer_messages(
            handoff,
            candidate_claim_units(
                "文档 A 要求字段留空；图片观察到字段有值。两份资料的冲突尚未解决，"
                "可由资料维护方确认以图或文档为准。"
            ),
        )[0]["content"]
        self.assertIn("submit `pass` with no findings", prompt)
        self.assertIn("source materials disagreeing by itself is not a candidate defect", prompt)
        self.assertIn("unsupported reconciliation", prompt)

    def test_claim_ids_address_markdown_units_without_reviewer_text_matching(self) -> None:
        candidate = "| Scope | Owner |\n| --- | --- |\n| Frontend | **platformPayment** |\n\n**Conclusion:** platformPayment is the verified owner."
        units = candidate_claim_units(candidate)
        self.assertEqual([unit.claim_id for unit in units], ["c001", "c002", "c003"])
        result = parse_reviewer_result(
            '{"verdict":"revise","confidence":0.9,"findings":[{"claim_id":"c002","claim":"front-end ownership is overstated","issue":"no direct binding","action":"mark as analogous candidate"}],"reason":"unlocated owner"}',
            claim_units=units,
        )
        rewritten = "| Scope | Owner |\n| --- | --- |\n| Frontend | analogous candidate |\n\n**Conclusion:** the true owner remains unlocated."
        self.assertTrue(rewrite_complies_with_review(rewritten, units, result.findings))
        self.assertFalse(rewrite_complies_with_review(candidate, units, result.findings))
        with self.assertRaises(ValueError):
            parse_reviewer_result(
                '{"verdict":"revise","confidence":0.9,"findings":[{"claim_id":"c999","claim":"summary","issue":"gap","action":"qualify"}],"reason":"x"}',
                claim_units=units,
            )

    def test_claim_unit_sampling_covers_middle_and_tail_with_original_ids(self) -> None:
        candidate = "\n".join(
            [f"- observed item {index}" for index in range(1, 101)]
            + ["| Area | Owner |", "| --- | --- |", "| Frontend | **platformPayment** is verified owner |", "Final conclusion: platformPayment is the true owner."]
        )
        units = candidate_claim_units(candidate)
        ids = [unit.claim_id for unit in units]
        self.assertEqual(len(units), 80)
        self.assertEqual(ids[0], "c001")
        self.assertTrue(any(45 <= int(claim_id[1:]) <= 60 for claim_id in ids))
        self.assertIn("c102", ids)
        self.assertIn("c103", ids)
        tail_unit = next(unit for unit in units if unit.claim_id == "c102")
        result = parse_reviewer_result(
            '{"verdict":"revise","confidence":0.9,"findings":[{"claim_id":"c102","claim":"owner claim","issue":"no binding","action":"mark unlocated"},{"claim_id":"c103","claim":"conclusion","issue":"no binding","action":"mark unlocated"}],"reason":"x"}',
            claim_units=units,
        )
        self.assertIn("platformPayment", tail_unit.text)
        rewritten = candidate.replace("**platformPayment** is verified owner", "analogous candidate").replace("platformPayment is the true owner", "the true owner is unlocated")
        self.assertTrue(rewrite_complies_with_review(rewritten, units, result.findings))

    def test_trigger_policy_scopes_document_consistency_and_excludes_single_document_and_git_metadata(self) -> None:
        self.assertTrue(should_review_read_only_candidate(
            generate_requirement_contract("只读分析当前服务 owner、调用链和影响范围，不要修改。"),
            "只读分析当前服务 owner、调用链和影响范围，不要修改。",
        ))
        self.assertFalse(should_review_read_only_candidate(
            generate_requirement_contract("只根据需求文档 Markdown 分析需求，不要检查代码。"),
            "只根据需求文档 Markdown 分析需求，不要检查代码。",
        ))
        self.assertTrue(should_review_read_only_candidate(
            generate_requirement_contract("只根据需求文档 Markdown、原型 HTML 和示例图分析需求，不要检查代码。"),
            "只根据需求文档 Markdown、原型 HTML 和示例图分析需求，不要检查代码。",
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
        self.assertEqual(len(review_calls), 2)
        self.assertTrue(all([tool["function"]["name"] for tool in call["tools"]] == [REVIEWER_OUTPUT_TOOL_NAME] for call in review_calls))
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["rewrites"], 1)
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["typed_submits"], 2)
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

    def test_schema_repair_turns_pass_with_findings_into_one_valid_revise(self) -> None:
        _ReviewerRepairClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerRepairClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
        self.assertIn("analogous candidate", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["triggers"], 1)
        self.assertEqual(summary["attempts"], 3)
        self.assertEqual(summary["schema_failures"], 1)
        self.assertEqual(summary["repairs"], 1)
        self.assertEqual(summary["repair_successes"], 1)
        review_calls = [
            call for call in _ReviewerRepairClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(len(review_calls), 3)
        self.assertTrue(all([tool["function"]["name"] for tool in call["tools"]] == [REVIEWER_OUTPUT_TOOL_NAME] for call in review_calls))
        self.assertIn("LCA_READ_ONLY_EVIDENCE_REVIEW_SCHEMA_REPAIR", str(review_calls[1]["messages"][-1]["content"]))

    def test_schema_repair_exhaustion_stays_unverified_without_raw_payload(self) -> None:
        _ReviewerRepairExhaustedClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerRepairExhaustedClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
        self.assertIn("未完成/未验证", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["attempts"], 3)
        self.assertEqual(summary["schema_failures"], 3)
        self.assertEqual(summary["repair_exhausted"], 1)
        self.assertNotIn('"findings":"wrong"', answer)

    def test_schema_diagnostics_are_typed_and_do_not_echo_claim_text(self) -> None:
        units = candidate_claim_units("secret candidate claim")
        with self.assertRaises(ReviewerValidationError) as raised:
            parse_reviewer_result(
                '{"verdict":"pass","confidence":0.9,"findings":[{"claim_id":"c001","issue":"secret claim","action":"repair"}],"reason":"x"}',
                claim_units=units,
            )
        repair = reviewer_repair_messages(
            build_explore_handoff(
                request="review",
                contract=generate_requirement_contract("只读分析当前服务 owner，不要修改文件。"),
                requirement_evidence=(), source_evidence=(), records=(), tool_results=(),
            ),
            units,
            raised.exception.diagnostics,
        )
        feedback = repair[-1]["content"]
        self.assertIn("pass_with_findings", feedback)
        self.assertNotIn("secret candidate claim", feedback)
        self.assertNotIn("secret claim", feedback)

    def test_valid_unverified_findings_request_one_truthful_rewrite(self) -> None:
        _ReviewerUnverifiedRewriteClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerUnverifiedRewriteClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改。")
        self.assertIn("真实 owner 仍未定位", answer)
        self.assertIn("弱相关候选", answer)
        self.assertEqual(runtime._last_run_summary["termination_reason"], "final")
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["verdicts"], {"pass": 1, "unverified": 1})
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["rewrites"], 1)

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

    def test_reviewer_protocol_timeout_and_markup_are_auditable_unverified_outcomes(self) -> None:
        cases = (
            (_ReviewerProtocolClient, "openai-compatible", "protocol_error", 3),
            (_ReviewerMultipleOutputClient, "openai-compatible", "protocol_error", 3),
            (_ReviewerMalformedOutputClient, "openai-compatible", "protocol_error", 3),
            (_ReviewerTimeoutClient, "openai-compatible", "timeout", 0),
            (_ReviewerXmlArtifactClient, "bailian", "protocol_error", 3),
        )
        for client, provider, expected, protocol_count in cases:
            with self.subTest(client=client.__name__), tempfile.TemporaryDirectory() as tmp:
                client.calls = []
                with patch("local_agent.agent.OpenAICompatibleClient", client):
                    runtime = AgentRuntime(_config(Path(tmp).resolve(), provider=provider), show_tool_logs=False)
                    answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改。")
                self.assertIn("未完成/未验证", answer)
                self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["errors"], {expected: 1})
                self.assertEqual(runtime._last_run_summary["provider_protocol_violations"], 0)
                self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["protocol_failures"], protocol_count)

    def test_reviewer_repair_turn_timeout_and_protocol_violations_are_terminal_and_redacted(self) -> None:
        cases = (
            (_ReviewerRepairTimeoutClient, "openai-compatible", "timeout", 0, 2),
            (_ReviewerRepairProtocolClient, "openai-compatible", "protocol_error", 2, 3),
            (_ReviewerRepairXmlClient, "bailian", "protocol_error", 2, 3),
        )
        for client, provider, expected, protocol_count, attempts in cases:
            with self.subTest(client=client.__name__), tempfile.TemporaryDirectory() as tmp:
                client.calls = []
                with patch("local_agent.agent.OpenAICompatibleClient", client):
                    runtime = AgentRuntime(_config(Path(tmp).resolve(), provider=provider), show_tool_logs=False)
                    answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改。")
                summary = runtime._last_run_summary["read_only_reviewer"]
                self.assertIn("未完成/未验证", answer)
                self.assertEqual(summary["attempts"], attempts)
                self.assertEqual(summary["schema_failures"], 1 if attempts == 2 else 3)
                self.assertEqual(summary["repairs"], 1 if attempts == 2 else 2)
                self.assertEqual(summary["errors"], {expected: 1})
                self.assertEqual(runtime._last_run_summary["provider_protocol_violations"], 0)
                self.assertEqual(summary["protocol_failures"], protocol_count)
                self.assertNotIn("secret", answer)
                self.assertNotIn("secret", runtime._session.path.read_text(encoding="utf-8"))

    def test_reviewer_schema_repair_is_not_attempted_without_remaining_deadline(self) -> None:
        _ReviewerRepairExhaustedClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerRepairExhaustedClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                contract = generate_requirement_contract("只读分析当前服务 owner 和影响范围，不要修改。")
                now = time.monotonic()
                runtime._run.begin(
                    run_id="repair-budget", started_monotonic=now, deadline_monotonic=now + 10,
                    run_start_index=0, git_baseline={}, prompt="只读分析当前服务 owner 和影响范围，不要修改。",
                    requirement_contract=contract, requirement_contract_context="", design_evidence_roots=(),
                )
                with patch.object(runtime._provider_context_phase, "remaining_timeout", side_effect=[1.0, 0.0]):
                    outcome = runtime._read_only_review_phase.review_candidate("PayServiceImpl is the verified owner.")
        self.assertEqual(outcome.kind, "unverified")
        summary = runtime._run.read_only_review.snapshot()
        self.assertEqual(summary["provider_attempts"], 1)
        self.assertEqual(summary["schema_failures"], 1)
        self.assertEqual(summary["repairs"], 0)

    def test_reviewer_unknown_claim_id_cannot_queue_a_rewrite(self) -> None:
        _ParaphraseReviewerClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ParaphraseReviewerClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改。")
        self.assertIn("未完成/未验证", answer)
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["rewrites"], 0)
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["errors"], {"invalid_output": 1})

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
                safe_events = runtime._session.load_event_payloads("safe_partial_report")
        self.assertIn("安全部分交付", answer)
        self.assertIn("未完成/未验证", answer)
        self.assertNotIn("PayServiceImpl 是已证实", answer)
        self.assertEqual(runtime._last_run_summary["termination_reason"], "read_only_reviewer_unverified")
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["errors"], {"second_review_nonpass": 1})
        self.assertEqual(runtime._last_run_summary["safe_partial_report"]["emitted"], 1)
        self.assertNotIn("PayServiceImpl", str(runtime._last_run_summary["safe_partial_report"]))
        self.assertEqual(len(safe_events), 1)
        self.assertNotIn("PayServiceImpl", str(safe_events[0]))

    def test_deterministic_rewrite_after_first_review_must_pass_a_second_last_gate(self) -> None:
        _ReviewerLastGateClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerLastGateClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                runtime._final_answer_steerers = (_OneShotDeterministicRewrite(),)
                answer = runtime.run("基于当前代码给出可实施设计草案：接口和权限；不得编造现有服务。")

        self.assertIn("未完成/未验证", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["triggers"], 1)
        self.assertEqual(summary["rewrites"], 1)
        self.assertEqual(summary["typed_submits"], 2)
        self.assertEqual(summary["errors"], {"second_review_nonpass": 1})
        review_calls = [
            call
            for call in _ReviewerLastGateClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(len(review_calls), 2)
        primary_calls = [
            call
            for call in _ReviewerLastGateClient.calls
            if not any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(len(primary_calls), 3)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.agent import AgentRuntime
from local_agent.config import AgentConfig
from local_agent.document_consistency import DocumentConsistencyAssessment
from local_agent.explore_handoff import build_explore_handoff
from local_agent.read_only_reviewer import ReviewerResult
from local_agent.read_only_reviewer import candidate_claim_units
from local_agent.read_only_reviewer import rewrite_complies_with_review
from local_agent.read_only_reviewer import parse_reviewer_payload
from local_agent.read_only_reviewer import parse_reviewer_result
from local_agent.read_only_reviewer import ReviewerValidationError
from local_agent.read_only_reviewer import reviewer_repair_messages
from local_agent.read_only_reviewer import reviewer_messages
from local_agent.read_only_reviewer import reviewer_finding_tool_schema
from local_agent.read_only_reviewer import reviewer_output_tool_schema
from local_agent.read_only_reviewer import reviewer_output_tool_schemas
from local_agent.read_only_reviewer import REVIEWER_FINDING_TOOL_NAME
from local_agent.read_only_reviewer import REVIEWER_OUTPUT_TOOL_NAME
from local_agent.read_only_reviewer import should_review_read_only_candidate
from local_agent.steering.final_answer import SourceEvidence
from local_agent.steering.final_answer import SteeringDecision
from local_agent.task_contract import generate_requirement_contract
from local_agent.llm import LlmTimeoutError
from local_agent.requirement_evidence import RequirementEvidence
from local_agent.tool_observation import ToolResultSummary


def _review_submit(payload: dict) -> object:
    findings = payload.get("findings") if isinstance(payload.get("findings"), list) else []
    final_payload = {key: value for key, value in payload.items() if key != "findings"}
    tool_calls = [
        {
            "id": f"review-finding-{index}",
            "type": "function",
            "function": {"name": REVIEWER_FINDING_TOOL_NAME, "arguments": json.dumps(finding)},
        }
        for index, finding in enumerate(findings, start=1)
    ]
    tool_calls.append(
        {
            "id": "review-submit",
            "type": "function",
            "function": {"name": REVIEWER_OUTPUT_TOOL_NAME, "arguments": json.dumps(final_payload)},
        }
    )
    return type(
        "Response",
        (),
        {
            "message": {
                "content": None,
                "tool_calls": tool_calls,
            }
        },
    )()


def _raw_review_output_tool(arguments: str, *, name: str = REVIEWER_OUTPUT_TOOL_NAME, call_id: str = "review-submit") -> object:
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
                        "function": {"name": name, "arguments": arguments},
                    }
                ],
            }
        },
    )()


def _first_candidate_claim(messages) -> str:
    for message in reversed(messages):
        if message.get("role") != "user" or "LCA_READ_ONLY_EVIDENCE_REVIEW" not in str(message.get("content")):
            continue
        payload = json.loads(message["content"])
        claims = payload.get("candidate_claims") or ()
        if claims:
            return str(claims[0].get("text") or "")
    return ""


def _candidate_claim(messages, claim_id: str = "c001") -> str:
    for message in reversed(messages):
        if message.get("role") != "user" or "LCA_READ_ONLY_EVIDENCE_REVIEW" not in str(message.get("content")):
            continue
        payload = json.loads(message["content"])
        for claim in payload.get("candidate_claims") or ():
            if claim.get("claim_id") == claim_id:
                return str(claim.get("text") or "")
    return ""


def _finding_call(
    call_id: str,
    claim_id: str,
    claim: str,
    *,
    issue: str = "unsupported",
    action: str = "qualify",
    scope: str = "candidate_defect",
) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": REVIEWER_FINDING_TOOL_NAME,
            "arguments": json.dumps(
                {
                    "claim_id": claim_id,
                    "claim": claim,
                    "finding_scope": scope,
                    "issue": issue,
                    "action": action,
                }
            ),
        },
    }


def _final_call(call_id: str, payload: dict | str) -> dict:
    arguments = payload if isinstance(payload, str) else json.dumps(payload)
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": REVIEWER_OUTPUT_TOOL_NAME, "arguments": arguments},
    }


def _review_tool_calls_response(tool_calls: list[dict]) -> object:
    return type("Response", (), {"message": {"content": None, "tool_calls": tool_calls}})()


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
                            "claim": "已证实真实 owner 是 PayServiceImpl，DDL 和 API 也由它负责。",
                            "finding_scope": "candidate_defect",
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
                            "claim": "现有 PayBillRecordInfoVo 使用 SF 前缀，并包含多级审批和退款复用。",
                            "finding_scope": "candidate_defect",
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
        type(self).calls.append({
            "messages": json.loads(json.dumps(messages, ensure_ascii=False)),
            "tools": json.loads(json.dumps(tools, ensure_ascii=False)),
            "timeout": timeout,
        })
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
            claim = _first_candidate_claim(messages)
            if self._review_calls == 1:
                return _raw_review_output_tool(json.dumps({
                    "verdict": "pass", "confidence": 0.9,
                    "findings": [{"claim_id": "c001", "claim": claim, "finding_scope": "candidate_defect", "issue": "unsupported", "action": "downgrade"}],
                    "reason": "contradictory shape",
                }))
            if self._review_calls == 2:
                return _review_submit({
                    "verdict": "revise", "confidence": 0.9,
                    "findings": [{"claim_id": "c001", "claim": claim, "finding_scope": "candidate_defect", "issue": "unsupported owner", "action": "mark unlocated"}],
                    "reason": "binding absent",
                })
            return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite is scoped"})
        self._primary_calls += 1
        if self._primary_calls == 1:
            return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()
        return type("Response", (), {"message": {"content": "PayServiceImpl is an analogous candidate; the owner is unlocated."}})()


class _ReviewerMissingIdAfterFindingClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self._primary_calls = 0
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({
            "messages": json.loads(json.dumps(messages, ensure_ascii=False)),
            "tools": json.loads(json.dumps(tools, ensure_ascii=False)),
            "timeout": timeout,
        })
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            claim = _first_candidate_claim(messages)
            if self._review_calls == 1:
                return _review_tool_calls_response(
                    [
                        _finding_call("valid-local", "c001", claim, action="mark unlocated"),
                        {
                            "type": "function",
                            "function": {
                                "name": REVIEWER_OUTPUT_TOOL_NAME,
                                "arguments": json.dumps({"verdict": "revise", "confidence": 0.9, "reason": "missing id"}),
                            },
                        },
                    ]
                )
            if self._review_calls == 2:
                return _review_tool_calls_response([
                    _final_call("bad-pass", {"verdict": "pass", "confidence": 0.9, "reason": "drop required finding"})
                ])
            if self._review_calls == 3:
                return _review_tool_calls_response([
                    _finding_call("resubmitted", "c001", claim, action="mark unlocated"),
                    _final_call("valid-final", {"verdict": "revise", "confidence": 0.9, "reason": "required finding restored"}),
                ])
            return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite scoped"})
        self._primary_calls += 1
        if self._primary_calls == 1:
            return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()
        return type("Response", (), {"message": {"content": "PayServiceImpl is an analogous candidate; the owner is unlocated."}})()


class _ReviewerIncrementalRepairLifecycleClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self._primary_calls = 0
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            claim = _first_candidate_claim(messages)
            if self._review_calls == 1:
                return _review_tool_calls_response([
                    _finding_call("mismatch", "c001", claim + " (paraphrased)")
                ])
            if self._review_calls == 2:
                return _review_tool_calls_response([
                    _finding_call("valid-finding", "c001", claim, action="mark owner unlocated")
                ])
            if self._review_calls == 3:
                return _review_tool_calls_response([_final_call("bad-final", "{")])
            if self._review_calls == 4:
                return _review_tool_calls_response([
                    _final_call("final-revise", {"verdict": "revise", "confidence": 0.9, "reason": "accepted defect"})
                ])
            return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite scoped"})
        self._primary_calls += 1
        if self._primary_calls == 1:
            return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()
        return type("Response", (), {"message": {"content": "PayServiceImpl is an analogous candidate; the owner is unlocated."}})()


class _ReviewerFinalBeforeFindingClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            claim = _first_candidate_claim(messages)
            return _review_tool_calls_response([
                _final_call(f"final-{self._review_calls}", {"verdict": "pass", "confidence": 0.9, "reason": "premature"}),
                _finding_call(f"late-finding-{self._review_calls}", "c001", claim),
            ])
        return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()


class _ReviewerFindingMalformedFinalThenPassClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self._review_calls = 0
        self._primary_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            claim = _first_candidate_claim(messages)
            if self._review_calls == 1:
                return _review_tool_calls_response([
                    _finding_call("valid-before-bad-final", "c001", claim, action="mark unlocated"),
                    _final_call("bad-final", "{"),
                ])
            if self._review_calls == 2:
                return _review_tool_calls_response([
                    _final_call("valid-final", {"verdict": "revise", "confidence": 0.9, "reason": "kept candidate defect"}),
                ])
            return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite scoped"})
        self._primary_calls += 1
        if self._primary_calls == 1:
            return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()
        return type("Response", (), {"message": {"content": "PayServiceImpl is an analogous candidate; the owner is unlocated."}})()


class _ReviewerEightFindingsClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self._review_calls = 0
        self._primary_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            if self._review_calls <= 8:
                claim_id = f"c{self._review_calls:03d}"
                return _review_tool_calls_response([
                    _finding_call(f"finding-{self._review_calls}", claim_id, _candidate_claim(messages, claim_id))
                ])
            if self._review_calls == 9:
                return _review_tool_calls_response([
                    _final_call("final-revise", {"verdict": "revise", "confidence": 0.9, "reason": "eight findings"})
                ])
            return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite scoped"})
        self._primary_calls += 1
        if self._review_calls < 9:
            content = "\n".join(f"- Unsupported owner claim {index}" for index in range(1, 9))
            return type("Response", (), {"message": {"content": content}})()
        return type("Response", (), {"message": {"content": "All owner claims are now scoped as unlocated."}})()


class _ReviewerNinthFindingClient(_ReviewerEightFindingsClient):
    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            if self._review_calls == 1:
                return _review_tool_calls_response([
                    _finding_call(f"finding-{index}", f"c{index:03d}", _candidate_claim(messages, f"c{index:03d}"))
                    for index in range(1, 10)
                ])
            if self._review_calls == 2:
                return _review_tool_calls_response([
                    _final_call("final-revise", {"verdict": "revise", "confidence": 0.9, "reason": "top eight findings recorded"})
                ])
            return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite scoped"})
        self._primary_calls += 1
        if self._review_calls >= 2:
            content = "All owner claims are now scoped as unlocated."
        else:
            content = "\n".join(f"- Unsupported owner claim {index}" for index in range(1, 10))
        return type("Response", (), {"message": {"content": content}})()


class _ReviewerNineFindingsWithFinalClient(_ReviewerEightFindingsClient):
    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            if self._review_calls == 1:
                return _review_tool_calls_response(
                    [
                        _finding_call(f"finding-{index}", f"c{index:03d}", _candidate_claim(messages, f"c{index:03d}"))
                        for index in range(1, 10)
                    ]
                    + [
                        _final_call("final-revise", {"verdict": "revise", "confidence": 0.9, "reason": "top eight findings"})
                    ]
                )
            return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite scoped"})
        self._primary_calls += 1
        if self._review_calls < 1:
            content = "\n".join(f"- Unsupported owner claim {index}" for index in range(1, 10))
            return type("Response", (), {"message": {"content": content}})()
        return type("Response", (), {"message": {"content": "All owner claims are now scoped as unlocated."}})()


class _ReviewerAcceptedFindingThenPassClient(_ReviewerFindingMalformedFinalThenPassClient):
    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            claim = _first_candidate_claim(messages)
            if self._review_calls == 1:
                return _review_tool_calls_response([_finding_call("finding", "c001", claim, action="mark unlocated")])
            if self._review_calls == 2:
                return _review_tool_calls_response([
                    _final_call("bad-pass", {"verdict": "pass", "confidence": 0.9, "reason": "drop finding"})
                ])
            if self._review_calls == 3:
                return _review_tool_calls_response([
                    _final_call("valid-final", {"verdict": "revise", "confidence": 0.9, "reason": "preserve finding"})
                ])
            return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite scoped"})
        self._primary_calls += 1
        if self._primary_calls == 1:
            return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()
        return type("Response", (), {"message": {"content": "PayServiceImpl is an analogous candidate; the owner is unlocated."}})()


class _ReviewerValidFindingUnknownLaterClient(_ReviewerFindingMalformedFinalThenPassClient):
    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            claim = _first_candidate_claim(messages)
            if self._review_calls == 1:
                return _review_tool_calls_response([
                    _finding_call("finding", "c001", claim, action="mark unlocated"),
                    {
                        "id": "unknown-output",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": json.dumps({"path": "secret.txt"})},
                    },
                ])
            if self._review_calls == 2:
                return _review_tool_calls_response([
                    _final_call("valid-final", {"verdict": "revise", "confidence": 0.9, "reason": "preserve finding"})
                ])
            return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite scoped"})
        self._primary_calls += 1
        if self._primary_calls == 1:
            return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()
        return type("Response", (), {"message": {"content": "PayServiceImpl is an analogous candidate; the owner is unlocated."}})()


class _ReviewerRepeatedFindingAfterCapacityClient(_ReviewerEightFindingsClient):
    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            if self._review_calls == 1:
                return _review_tool_calls_response([
                    _finding_call(f"finding-{index}", f"c{index:03d}", _candidate_claim(messages, f"c{index:03d}"))
                    for index in range(1, 10)
                ])
            return _review_tool_calls_response([
                _finding_call(f"extra-{self._review_calls}", "c009", _candidate_claim(messages, "c009"))
            ])
        self._primary_calls += 1
        content = "\n".join(f"- Unsupported owner claim {index}" for index in range(1, 10))
        return type("Response", (), {"message": {"content": content}})()


class _ReviewerTenFindingsClient(_ReviewerEightFindingsClient):
    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            if self._review_calls == 1:
                return _review_tool_calls_response([
                    _finding_call(f"finding-{index}", f"c{index:03d}", _candidate_claim(messages, f"c{index:03d}"))
                    for index in range(1, 11)
                ])
            if self._review_calls == 2:
                return _review_tool_calls_response([
                    _final_call("final-revise", {"verdict": "revise", "confidence": 0.9, "reason": "top eight findings recorded"})
                ])
            return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite scoped"})
        self._primary_calls += 1
        if self._review_calls >= 2:
            content = "All owner claims are now scoped as unlocated."
        else:
            content = "\n".join(f"- Unsupported owner claim {index}" for index in range(1, 10))
        return type("Response", (), {"message": {"content": content}})()


class _ReviewerInvalidFindingThenPassClient(_ReviewerFindingMalformedFinalThenPassClient):
    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            claim = _first_candidate_claim(messages)
            if self._review_calls == 1:
                return _review_tool_calls_response([
                    _finding_call("bad-finding", "c001", claim + " paraphrased", action="mark unlocated"),
                    _final_call("bad-pass", {"verdict": "pass", "confidence": 0.9, "reason": "ignore invalid finding"}),
                ])
            if self._review_calls == 2:
                return _review_tool_calls_response([
                    _finding_call("fixed-finding", "c001", claim, action="mark unlocated"),
                    _final_call("valid-final", {"verdict": "revise", "confidence": 0.9, "reason": "finding fixed"}),
                ])
            return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite scoped"})
        self._primary_calls += 1
        if self._primary_calls == 1:
            return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()
        return type("Response", (), {"message": {"content": "PayServiceImpl is an analogous candidate; the owner is unlocated."}})()


class _ReviewerUnknownThenPassClient(_ReviewerFindingMalformedFinalThenPassClient):
    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            claim = _first_candidate_claim(messages)
            if self._review_calls == 1:
                return _review_tool_calls_response([
                    {
                        "id": "unknown-output",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": json.dumps({"path": "secret.txt"})},
                    },
                    _final_call("bad-pass", {"verdict": "pass", "confidence": 0.9, "reason": "ignore unknown"}),
                ])
            if self._review_calls == 2:
                return _review_tool_calls_response([
                    _finding_call("fixed-finding", "c001", claim, action="mark unlocated"),
                    _final_call("valid-final", {"verdict": "revise", "confidence": 0.9, "reason": "unknown fixed"}),
                ])
            return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite scoped"})
        self._primary_calls += 1
        if self._primary_calls == 1:
            return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()
        return type("Response", (), {"message": {"content": "PayServiceImpl is an analogous candidate; the owner is unlocated."}})()


class _ReviewerTwoFinalsThenFixClient(_ReviewerFindingMalformedFinalThenPassClient):
    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            claim = _first_candidate_claim(messages)
            if self._review_calls == 1:
                return _review_tool_calls_response([
                    _final_call("first-final", {"verdict": "pass", "confidence": 0.9, "reason": "premature"}),
                    _final_call("second-final", {"verdict": "pass", "confidence": 0.9, "reason": "second pass"}),
                ])
            if self._review_calls == 2:
                return _review_tool_calls_response([
                    _finding_call("fixed-finding", "c001", claim, action="mark unlocated"),
                    _final_call("valid-final", {"verdict": "revise", "confidence": 0.9, "reason": "final order fixed"}),
                ])
            return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite scoped"})
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
            return _raw_review_output_tool('{"verdict":"revise","confidence":0.9,"findings":"wrong","reason":"x"}')
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
            claim = _first_candidate_claim(messages)
            if self._review_calls > 1:
                return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite is truthful"})
            return _review_submit(
                {
                    "verdict": "unverified",
                    "confidence": 0.9,
                    "findings": [
                        {
                            "claim_id": "c001",
                            "claim": claim,
                            "finding_scope": "candidate_defect",
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
            return _review_submit({"verdict": "pass", "confidence": 0.92, "findings": [], "reason": "explicit binding is present"})
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
            payload = json.dumps({"verdict": "pass", "confidence": 0.9, "reason": "x"})
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
            return _raw_review_output_tool(
                '{"claim_id":"c999","claim":"PayServiceImpl is the verified owner","finding_scope":"candidate_defect","issue":"unsupported","action":"qualify"}',
                name=REVIEWER_FINDING_TOOL_NAME,
                call_id="bad-finding",
            )
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
                return _raw_review_output_tool('{"verdict":"revise","confidence":0.9,"findings":"wrong","reason":"x"}')
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
                return _raw_review_output_tool('{"verdict":"revise","confidence":0.9,"findings":"wrong","reason":"x"}')
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
                return _raw_review_output_tool('{"verdict":"revise","confidence":0.9,"findings":"wrong","reason":"x"}')
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
                    "findings": [{"claim_id": "c001", "claim": "已证实真实 owner 是 PayServiceImpl。", "finding_scope": "candidate_defect", "issue": "unsupported", "action": "qualify"}],
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
                    "findings": [{"claim_id": "c001", "claim": "现有 Redis key、权限接口和模板服务已经由 PayServiceImpl 实现。", "finding_scope": "candidate_defect", "issue": "unsupported repository detail", "action": "mark it unverified"}],
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


class _DocumentConsistencySourceGapReviewerClient:
    def __init__(self, config: AgentConfig):
        self.config = config
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        self._review_calls += 1
        if self._review_calls > 1:
            return _review_submit(
                {
                    "verdict": "pass",
                    "confidence": 0.93,
                    "findings": [],
                    "reason": "candidate already reports the source-material gap",
                    "document_consistency": {
                        "stance": "reported_unresolved",
                        "conflict_evidence_ids": ["e001", "e002"],
                        "supporting_evidence_ids": [],
                    },
                }
            )
        return _review_submit(
            {
                "verdict": "revise",
                "confidence": 0.91,
                "findings": [
                    {
                        "claim_id": "c001",
                        "claim": "The document and image are not consistent; artifact role remains unresolved.",
                        "finding_scope": "source_material_gap",
                        "issue": "source materials still need confirmation",
                        "action": "ask the document owner to decide the final artifact wording",
                    }
                ],
                "reason": "source conflict remains",
                "document_consistency": {
                    "stance": "reported_unresolved",
                    "conflict_evidence_ids": ["e001", "e002"],
                    "supporting_evidence_ids": [],
                },
            }
        )


class _DocumentConsistencyTwoStageReviewerClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self._primary_calls = 0
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        if reviewer:
            self._review_calls += 1
            if self._review_calls == 1:
                claim = _first_candidate_claim(messages)
                return _review_tool_calls_response([
                    _finding_call(
                        "doc-finding",
                        "c001",
                        claim,
                        issue="unsupported reconciliation",
                        action="keep both artifacts unresolved",
                    )
                ])
            if self._review_calls == 2:
                return _review_tool_calls_response([
                    _final_call(
                        "doc-final",
                        {
                            "verdict": "revise",
                            "confidence": 0.9,
                            "reason": "unsupported reconciliation",
                            "document_consistency": {
                                "stance": "asserted_reconciled",
                                "conflict_evidence_ids": ["e001", "e002"],
                                "supporting_evidence_ids": [],
                            },
                        },
                    )
                ])
            return _review_submit(
                {
                    "verdict": "pass",
                    "confidence": 0.93,
                    "findings": [],
                    "reason": "candidate preserves unresolved conflict",
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
        if self._primary_calls == 2:
            return type("Response", (), {"message": {"content": "policy.md 要求字段留空；prototype.html 显示有值。两份资料没有冲突，因为 prototype 是完成态。"}})()
        return type("Response", (), {"message": {"content": "policy.md 要求字段留空；prototype.html 显示有值。资料角色未说明，因此当前仍未消解。"}})()


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
        self.assertNotIn("未消解的资料冲突", answer)
        self.assertIn("policy.md", answer)
        self.assertIn("prototype.html", answer)
        self.assertNotIn("FORGED_RECONCILIATION", answer)
        self.assertEqual(runtime._last_run_summary["termination_reason"], "read_only_reviewer_unverified")
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["errors"], {"invalid_output": 1})

    def test_invalid_document_consistency_assessment_is_not_persisted_for_safe_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
            contract = generate_requirement_contract(
                "只根据需求 Markdown、原型 HTML 和示例图分析资料一致性，不要检查代码。"
            )
            runtime._run.begin(
                run_id="invalid-doc-consistency",
                started_monotonic=time.monotonic(),
                deadline_monotonic=None,
                run_start_index=0,
                git_baseline={},
                prompt="只根据需求 Markdown、原型 HTML 和示例图分析资料一致性，不要检查代码。",
                requirement_contract=contract,
                requirement_contract_context="",
                design_evidence_roots=(),
            )
            runtime._run.evidence.pinned_requirement_evidence = [
                RequirementEvidence(
                    "requirements.md",
                    "1: Requirement says the field is blank.\n2: Requirement repeats blank state.",
                    root="/workspace/root",
                )
            ]
            handoff = runtime._read_only_review_phase._handoff(
                "requirements.md:1 and requirements.md:2 conflict with the image."
            )
            result = ReviewerResult(
                "unverified",
                0.7,
                reason="single artifact only",
                document_consistency=DocumentConsistencyAssessment("reported_unresolved", ("e001", "e002"), ()),
            )

            with self.assertRaises(ReviewerValidationError) as raised:
                runtime._read_only_review_phase._validate_document_consistency(
                    result,
                    handoff,
                    "The requirement and image are not consistent; the role is unresolved.",
                )

            self.assertEqual(raised.exception.code, "document_conflict_evidence_insufficient")
            self.assertIsNone(runtime._run.read_only_review.document_consistency)
            self.assertEqual(runtime._run.read_only_review.document_consistency_handoff_signature, ())

    def test_safe_partial_uses_document_consistency_only_for_the_same_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
            contract = generate_requirement_contract(
                "只根据需求 Markdown、原型 HTML 和示例图分析资料一致性，不要检查代码。"
            )
            runtime._run.begin(
                run_id="handoff-bound-doc-consistency",
                started_monotonic=time.monotonic(),
                deadline_monotonic=None,
                run_start_index=0,
                git_baseline={},
                prompt="只根据需求 Markdown、原型 HTML 和示例图分析资料一致性，不要检查代码。",
                requirement_contract=contract,
                requirement_contract_context="",
                design_evidence_roots=(),
            )
            runtime._run.tool_choice_results.extend(
                [
                    ToolResultSummary(
                        "read_file",
                        "Requirement says blank.",
                        path="requirements.md",
                        metadata={
                            "evidence_root": "/workspace/root",
                            "resolved_path": "/workspace/root/requirements.md",
                        },
                    ),
                    ToolResultSummary(
                        "read_file",
                        "Prototype shows value.",
                        path="prototype.html",
                        metadata={
                            "evidence_root": "/workspace/root",
                            "resolved_path": "/workspace/root/prototype.html",
                        },
                    ),
                ]
            )
            state = runtime._run.read_only_review
            state.document_consistency = DocumentConsistencyAssessment("reported_unresolved", ("e001", "e002"), ())

            state.document_consistency_handoff_signature = (("candidate", "handoff"),)
            stale_report = runtime._read_only_review_phase.safe_partial_for_terminal("invalid_output")
            self.assertNotIn("未消解的资料冲突", stale_report)

            state.safe_partial_emitted = False
            terminal_handoff = runtime._read_only_review_phase._handoff()
            state.document_consistency_handoff_signature = runtime._read_only_review_phase._handoff_signature(terminal_handoff)
            matching_report = runtime._read_only_review_phase.safe_partial_for_terminal("invalid_output")
            self.assertIn("未消解的资料冲突", matching_report)
            self.assertIn("requirements.md", matching_report)
            self.assertIn("prototype.html", matching_report)

    def test_document_consistency_source_gap_finding_is_repaired_not_runtime_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _DocumentConsistencySourceGapReviewerClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                contract = generate_requirement_contract(
                    "只根据需求 Markdown、原型 HTML 和示例图分析资料一致性，不要检查代码。"
                )
                runtime._run.begin(
                    run_id="document-source-gap",
                    started_monotonic=time.monotonic(),
                    deadline_monotonic=None,
                    run_start_index=0,
                    git_baseline={},
                    prompt="只根据需求 Markdown、原型 HTML 和示例图分析资料一致性，不要检查代码。",
                    requirement_contract=contract,
                    requirement_contract_context="",
                    design_evidence_roots=(),
                )
                runtime._run.tool_choice_results.extend(
                    [
                        ToolResultSummary(
                            "read_file",
                            "Requirement says blank.",
                            path="requirements.md",
                            metadata={"evidence_root": "/workspace/root", "resolved_path": "/workspace/root/requirements.md"},
                        ),
                        ToolResultSummary(
                            "inspect_image",
                            "Image shows a value.",
                            path="example.png",
                            metadata={
                                "evidence_root": "/workspace/root",
                                "resolved_path": "/workspace/root/example.png",
                                "image_observation": True,
                            },
                        ),
                    ]
                )

                outcome = runtime._read_only_review_phase.review_candidate(
                    "The document and image are not consistent; artifact role remains unresolved."
                )

        self.assertEqual(outcome.kind, "pass")
        self.assertEqual(runtime._run.read_only_review.verdict, "pass")
        self.assertEqual(runtime._run.read_only_review.schema_failures, 0)
        self.assertEqual(runtime._run.read_only_review.repairs, 0)
        self.assertEqual(runtime._run.read_only_review.rejected_finding_submits, 1)
        self.assertNotIn("source-gap", runtime._run.read_only_review.reason or "")

    def test_document_consistency_uses_two_stage_shallow_output_protocol(self) -> None:
        _DocumentConsistencyTwoStageReviewerClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "policy.md").write_text("Field must remain blank.\n", encoding="utf-8")
            (workspace / "prototype.html").write_text("<p>Field has a value.</p>\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _DocumentConsistencyTwoStageReviewerClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("只根据 Markdown 和 HTML 分析资料一致性，不要检查代码。")

        self.assertIn("仍未消解", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["finding_submits"], 1)
        self.assertEqual(summary["final_submits"], 2)
        self.assertEqual(summary["rewrites"], 1)
        review_calls = [
            call for call in _DocumentConsistencyTwoStageReviewerClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(
            [tool["function"]["name"] for tool in review_calls[0]["tools"]],
            [REVIEWER_FINDING_TOOL_NAME, REVIEWER_OUTPUT_TOOL_NAME],
        )
        self.assertEqual(
            [tool["function"]["name"] for tool in review_calls[1]["tools"]],
            [REVIEWER_FINDING_TOOL_NAME, REVIEWER_OUTPUT_TOOL_NAME],
        )

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
            '{"verdict":"revise","confidence":0.8,"findings":[{"claim_id":"c001","claim":"owner","finding_scope":"candidate_defect","issue":"gap","action":"qualify"}],"reason":"missing binding"}',
            claim_units=units,
        )
        self.assertEqual(parsed.verdict, "revise")
        with self.assertRaises(ValueError):
            parse_reviewer_result('{"verdict":"pass","confidence":1,"findings":[{"claim_id":"c001","claim":"owner","finding_scope":"candidate_defect","issue":"x","action":"x"}],"reason":"x"}', claim_units=units)
        with self.assertRaises(ValueError):
            parse_reviewer_result("review looks good", claim_units=units)

    def test_output_tool_schema_and_parser_reject_extra_or_oversized_payload_fields(self) -> None:
        units = candidate_claim_units("unsupported owner claim")
        schema = reviewer_output_tool_schema(units)
        parameters = schema["function"]["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertNotIn("findings", parameters["properties"])
        finding_schema = reviewer_finding_tool_schema(units)
        self.assertEqual(finding_schema["function"]["name"], REVIEWER_FINDING_TOOL_NAME)
        self.assertEqual(finding_schema["function"]["parameters"]["additionalProperties"], False)

        invalid_payloads = (
            {
                "verdict": "pass", "confidence": 0.9, "findings": [], "reason": "ok", "extra": "no",
            },
            {
                "verdict": "revise",
                "confidence": 0.9,
                "findings": [{"claim_id": "c001", "claim": "unsupported owner claim", "finding_scope": "candidate_defect", "issue": "x", "action": "y", "extra": "no"}],
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
        self.assertIn("under 9000", prompt)
        self.assertIn("at most 8", prompt)
        self.assertIn("`pass` verdict requires 0", prompt)
        self.assertIn("finding_scope to `candidate_defect`", prompt)
        self.assertIn("copy that exact candidate_claims text", prompt)
        self.assertIn("action must change the candidate answer", prompt)
        self.assertIn("must not ask to modify the requirements, images, prototypes", prompt)
        self.assertIn("report_read_only_finding once per finding", prompt)
        self.assertIn("submit_read_only_review with verdict, confidence, and reason only", prompt)
        schema = reviewer_finding_tool_schema(candidate_claim_units("candidate"))
        action_description = schema["function"]["parameters"]["properties"]["action"]["description"]
        self.assertIn("candidate-answer change", action_description)
        self.assertIn("Do not ask to modify source documents", action_description)
        repair = reviewer_repair_messages(handoff, candidate_claim_units("candidate"), {"error_code": "findings_too_many"})
        self.assertIn("at most 8", repair[0]["content"])
        self.assertIn("no more than 8 highest-risk findings", repair[-1]["content"])
        self.assertIn("original output tools", repair[-1]["content"])

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
        self.assertIn("submit `pass` with no reported findings", prompt)
        self.assertIn("source materials disagreeing by itself is not a candidate defect", prompt)
        self.assertIn("unsupported reconciliation", prompt)
        self.assertIn("supporting_evidence_ids to []", prompt)
        self.assertIn("conflict_evidence_ids and supporting_evidence_ids must never overlap", prompt)

        schema = reviewer_output_tool_schema(
            candidate_claim_units("candidate"),
            document_consistency=True,
            evidence_ids=handoff.evidence_ids,
        )
        support_schema = schema["function"]["parameters"]["properties"]["document_consistency"]["properties"]["supporting_evidence_ids"]
        self.assertIn("independent non-visual", support_schema["description"])

    def test_document_consistency_overlap_repair_requires_empty_support_for_unresolved(self) -> None:
        contract = generate_requirement_contract(
            "只根据需求 Markdown、原型 HTML 和示例图分析需求，不要检查代码。"
        )
        handoff = build_explore_handoff(
            request="compare the requested documents",
            contract=contract,
            requirement_evidence=(),
            source_evidence=(),
            records=(),
            tool_results=(
                type("Tool", (), {"name": "read_file", "content": "A says blank.", "path": "policy.md", "is_error": False, "useless": False, "metadata": {}})(),
                type("Tool", (), {"name": "inspect_image", "content": "B shows a value.", "path": "example.png", "is_error": False, "useless": False, "metadata": {}})(),
            ),
        )
        units = candidate_claim_units("A and B are consistent because B is the completed state.")
        bad_payload = {
            "verdict": "unverified",
            "confidence": 0.5,
            "findings": [
                {
                    "claim_id": "c001",
                    "claim": "A and B are consistent because B is the completed state.",
                    "finding_scope": "candidate_defect",
                    "issue": "unsupported reconciliation",
                    "action": "keep unresolved",
                }
            ],
            "reason": "bad support ids",
            "document_consistency": {
                "stance": "explicitly_supported_reconciliation",
                "conflict_evidence_ids": ["e001", "e002"],
                "supporting_evidence_ids": ["e001"],
            },
        }
        with self.assertRaises(ReviewerValidationError) as raised:
            parse_reviewer_payload(
                bad_payload,
                claim_units=units,
                document_consistency=True,
                evidence_ids=handoff.evidence_ids,
            )
        self.assertEqual(raised.exception.code, "document_consistency_evidence_roles_overlap")

        repair = reviewer_repair_messages(handoff, units, raised.exception.diagnostics)
        repair_text = repair[-1]["content"]
        self.assertIn("disjoint", repair_text)
        self.assertIn("supporting_evidence_ids to []", repair_text)
        self.assertIn("independent non-visual", repair_text)

        repaired = dict(bad_payload)
        repaired["document_consistency"] = {
            "stance": "reported_unresolved",
            "conflict_evidence_ids": ["e001", "e002"],
            "supporting_evidence_ids": [],
        }
        parsed = parse_reviewer_payload(
            repaired,
            claim_units=units,
            document_consistency=True,
            evidence_ids=handoff.evidence_ids,
        )
        self.assertEqual(parsed.document_consistency.stance, "reported_unresolved")

    def test_document_consistency_non_explicit_stance_rejects_non_overlapping_support_ids(self) -> None:
        contract = generate_requirement_contract(
            "只根据需求 Markdown、原型 HTML 和示例图分析需求，不要检查代码。"
        )
        handoff = build_explore_handoff(
            request="compare the requested documents",
            contract=contract,
            requirement_evidence=(),
            source_evidence=(),
            records=(),
            tool_results=(
                type("Tool", (), {"name": "read_file", "content": "A says blank.", "path": "policy.md", "is_error": False, "useless": False, "metadata": {}})(),
                type("Tool", (), {"name": "inspect_image", "content": "B shows a value.", "path": "example.png", "is_error": False, "useless": False, "metadata": {}})(),
            ),
        )
        units = candidate_claim_units("A and B remain unresolved.")
        payload = {
            "verdict": "unverified",
            "confidence": 0.5,
            "findings": [
                {
                    "claim_id": "c001",
                    "claim": "A and B remain unresolved.",
                    "finding_scope": "candidate_defect",
                    "issue": "unresolved",
                    "action": "keep unresolved",
                }
            ],
            "reason": "bad support ids",
            "document_consistency": {
                "stance": "reported_unresolved",
                "conflict_evidence_ids": ["e001"],
                "supporting_evidence_ids": ["e002"],
            },
        }
        with self.assertRaises(ReviewerValidationError) as raised:
            parse_reviewer_payload(
                payload,
                claim_units=units,
                document_consistency=True,
                evidence_ids=handoff.evidence_ids,
            )
        self.assertEqual(raised.exception.code, "document_consistency_support_requires_explicit_stance")
        repair = reviewer_repair_messages(handoff, units, raised.exception.diagnostics)
        self.assertIn("supporting_evidence_ids to []", repair[-1]["content"])
        conflict_repair = reviewer_repair_messages(
            handoff,
            units,
            {"error_code": "document_conflict_evidence_insufficient"},
        )
        self.assertIn("at least two", conflict_repair[-1]["content"])
        self.assertIn("Do not cite only one side", conflict_repair[-1]["content"])

        payload["document_consistency"] = {
            "stance": "conditional_reconciliation",
            "conflict_evidence_ids": ["e001", "e002"],
            "supporting_evidence_ids": [],
        }
        parsed = parse_reviewer_payload(
            payload,
            claim_units=units,
            document_consistency=True,
            evidence_ids=handoff.evidence_ids,
        )
        self.assertEqual(parsed.document_consistency.stance, "conditional_reconciliation")

    def test_source_material_gap_finding_requires_repair_not_silent_normalization(self) -> None:
        units = candidate_claim_units("The document and image are not consistent; artifact role remains unresolved.")
        with self.assertRaises(ReviewerValidationError) as raised:
            parse_reviewer_payload(
                {
                    "verdict": "revise",
                    "confidence": 0.7,
                    "findings": [
                        {
                            "claim_id": "c001",
                            "claim": "The document and image are not consistent; artifact role remains unresolved.",
                            "finding_scope": "source_material_gap",
                            "issue": "source owner must choose final wording",
                            "action": "ask the artifact owner to update sources",
                        }
                    ],
                    "reason": "source gap",
                },
                claim_units=units,
            )
        self.assertEqual(raised.exception.code, "source_material_gap_finding")
        repair = reviewer_repair_messages(
            build_explore_handoff(
                request="review",
                contract=generate_requirement_contract("只根据 Markdown 和图片分析资料一致性，不要检查代码。"),
                requirement_evidence=(), source_evidence=(), records=(), tool_results=(),
            ),
            units,
            raised.exception.diagnostics,
        )
        self.assertIn("Do not submit source_material_gap findings", repair[-1]["content"])

    def test_mixed_source_gap_repair_must_preserve_candidate_defect(self) -> None:
        units = candidate_claim_units(
            "The image value conflicts with the document and remains unresolved.\n\n"
            "The screenshot is the completed state, so the materials are consistent."
        )
        claim_by_id = {unit.claim_id: unit.text for unit in units}
        with self.assertRaises(ReviewerValidationError) as raised:
            parse_reviewer_payload(
                {
                    "verdict": "revise",
                    "confidence": 0.7,
                    "findings": [
                        {
                            "claim_id": "c001",
                            "claim": claim_by_id["c001"],
                            "finding_scope": "source_material_gap",
                            "issue": "source owner must choose the final artifact",
                            "action": "ask the source owner to decide",
                        },
                        {
                            "claim_id": "c002",
                            "claim": claim_by_id["c002"],
                            "finding_scope": "candidate_defect",
                            "issue": "candidate invents lifecycle support",
                            "action": "remove the completed-state reconciliation",
                        },
                    ],
                    "reason": "mixed source gap and candidate defect",
                },
                claim_units=units,
            )
        self.assertEqual(raised.exception.code, "source_material_gap_finding")
        self.assertEqual(raised.exception.pending_candidate_claim_ids, ("c002",))

        with self.assertRaises(ReviewerValidationError) as pass_raised:
            parse_reviewer_payload(
                {"verdict": "pass", "confidence": 0.8, "findings": [], "reason": "only source gap remains"},
                claim_units=units,
                required_candidate_claim_ids=raised.exception.pending_candidate_claim_ids,
            )
        self.assertEqual(pass_raised.exception.code, "candidate_defect_findings_missing")

        repaired = parse_reviewer_payload(
            {
                "verdict": "revise",
                "confidence": 0.8,
                "findings": [
                    {
                        "claim_id": "c002",
                        "claim": claim_by_id["c002"],
                        "finding_scope": "candidate_defect",
                        "issue": "candidate invents lifecycle support",
                        "action": "remove the completed-state reconciliation",
                    }
                ],
                "reason": "candidate defect remains",
            },
            claim_units=units,
            required_candidate_claim_ids=raised.exception.pending_candidate_claim_ids,
        )
        self.assertEqual([finding.claim_id for finding in repaired.findings], ["c002"])

    def test_source_gap_only_repair_can_pass(self) -> None:
        units = candidate_claim_units("The document and image are not consistent; artifact role remains unresolved.")
        parsed = parse_reviewer_payload(
            {"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "candidate accurately reports source gap"},
            claim_units=units,
            required_candidate_claim_ids=(),
        )
        self.assertEqual(parsed.verdict, "pass")

    def test_finding_claim_must_match_selected_claim_id(self) -> None:
        units = candidate_claim_units("First unsupported claim.\n\nSecond unsupported claim.")
        with self.assertRaises(ReviewerValidationError) as raised:
            parse_reviewer_payload(
                {
                    "verdict": "revise",
                    "confidence": 0.7,
                    "findings": [
                        {
                            "claim_id": "c001",
                            "claim": "Second unsupported claim.",
                            "finding_scope": "candidate_defect",
                            "issue": "wrong claim id",
                            "action": "choose matching id",
                        }
                    ],
                    "reason": "mismatch",
                },
                claim_units=units,
            )
        self.assertEqual(raised.exception.code, "finding_claim_mismatch")
        repair = reviewer_repair_messages(
            build_explore_handoff(
                request="review",
                contract=generate_requirement_contract("只读分析 owner，不要修改文件。"),
                requirement_evidence=(), source_evidence=(), records=(), tool_results=(),
            ),
            units,
            raised.exception.diagnostics,
        )
        self.assertIn("copy the exact candidate_claims text", repair[-1]["content"])

    def test_claim_ids_address_markdown_units_without_reviewer_text_matching(self) -> None:
        candidate = "| Scope | Owner |\n| --- | --- |\n| Frontend | **platformPayment** |\n\n**Conclusion:** platformPayment is the verified owner."
        units = candidate_claim_units(candidate)
        self.assertEqual([unit.claim_id for unit in units], ["c001", "c002", "c003"])
        result = parse_reviewer_result(
            '{"verdict":"revise","confidence":0.9,"findings":[{"claim_id":"c002","claim":"| Frontend | **platformPayment** |","finding_scope":"candidate_defect","issue":"no direct binding","action":"mark as analogous candidate"}],"reason":"unlocated owner"}',
            claim_units=units,
        )
        rewritten = "| Scope | Owner |\n| --- | --- |\n| Frontend | analogous candidate |\n\n**Conclusion:** the true owner remains unlocated."
        self.assertTrue(rewrite_complies_with_review(rewritten, units, result.findings))
        self.assertFalse(rewrite_complies_with_review(candidate, units, result.findings))
        with self.assertRaises(ValueError):
            parse_reviewer_result(
                '{"verdict":"revise","confidence":0.9,"findings":[{"claim_id":"c999","claim":"summary","finding_scope":"candidate_defect","issue":"gap","action":"qualify"}],"reason":"x"}',
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
            '{"verdict":"revise","confidence":0.9,"findings":[{"claim_id":"c102","claim":"| Frontend | **platformPayment** is verified owner |","finding_scope":"candidate_defect","issue":"no binding","action":"mark unlocated"},{"claim_id":"c103","claim":"Final conclusion: platformPayment is the true owner.","finding_scope":"candidate_defect","issue":"no binding","action":"mark unlocated"}],"reason":"x"}',
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
        self.assertTrue(
            all(
                [tool["function"]["name"] for tool in call["tools"]]
                == [REVIEWER_FINDING_TOOL_NAME, REVIEWER_OUTPUT_TOOL_NAME]
                for call in review_calls
            )
        )
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["rewrites"], 1)
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["typed_submits"], 3)
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["finding_submits"], 1)
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["final_submits"], 2)
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
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["errors"], {"protocol_error": 1})

    def test_invalid_final_shape_is_tool_local_and_then_valid_revise(self) -> None:
        _ReviewerRepairClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerRepairClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
        self.assertIn("analogous candidate", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["triggers"], 1)
        self.assertEqual(summary["attempts"], 3)
        self.assertEqual(summary["schema_failures"], 0)
        self.assertEqual(summary["repairs"], 0)
        self.assertEqual(summary["repair_successes"], 0)
        self.assertEqual(summary["rejected_final_submits"], 1)
        review_calls = [
            call for call in _ReviewerRepairClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(len(review_calls), 3)
        self.assertTrue(
            all(
                [tool["function"]["name"] for tool in call["tools"]]
                == [REVIEWER_FINDING_TOOL_NAME, REVIEWER_OUTPUT_TOOL_NAME]
                for call in review_calls
            )
        )
        self.assertNotIn("LCA_READ_ONLY_EVIDENCE_REVIEW_SCHEMA_REPAIR", str(review_calls[1]["messages"][-1]["content"]))

    def test_unpairable_call_after_valid_finding_requires_resubmit_not_accepted(self) -> None:
        _ReviewerMissingIdAfterFindingClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerMissingIdAfterFindingClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
        self.assertIn("analogous candidate", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["schema_failures"], 1)
        self.assertEqual(summary["repairs"], 1)
        self.assertEqual(summary["finding_submits"], 1)
        self.assertEqual(summary["rejected_final_submits"], 1)
        review_calls = [
            call for call in _ReviewerMissingIdAfterFindingClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        repair_messages = [
            message for message in review_calls[1]["messages"]
            if message.get("role") == "user" and "LCA_READ_ONLY_EVIDENCE_REVIEW_SCHEMA_REPAIR" in str(message.get("content"))
        ]
        self.assertEqual(len(repair_messages), 1)
        repair_payload = json.loads(repair_messages[0]["content"])
        self.assertEqual(repair_payload["accepted_candidate_defect_claim_ids"], [])
        self.assertEqual(repair_payload["required_resubmit_candidate_defect_claim_ids"], ["c001"])
        self.assertNotIn("mark unlocated", repair_messages[0]["content"])

    def test_incremental_finding_turns_do_not_consume_schema_repair_budget(self) -> None:
        _ReviewerIncrementalRepairLifecycleClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerIncrementalRepairLifecycleClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
        self.assertIn("analogous candidate", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["schema_failures"], 0)
        self.assertEqual(summary["repairs"], 0)
        self.assertEqual(summary["repair_exhausted"], 0)
        self.assertEqual(summary["finding_submits"], 1)
        self.assertEqual(summary["rejected_finding_submits"], 1)
        self.assertEqual(summary["rejected_final_submits"], 1)
        self.assertEqual(summary["final_submits"], 2)
        review_calls = [
            call for call in _ReviewerIncrementalRepairLifecycleClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertGreaterEqual(len(review_calls), 4)
        rejected_tool_results = [
            message for message in review_calls[2]["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == "bad-final"
        ]
        self.assertEqual(len(rejected_tool_results), 1)
        self.assertIn("output_tool_arguments_json_invalid", str(rejected_tool_results[0].get("content")))
        prior_tool_results = [
            message for message in review_calls[2]["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == "valid-finding"
        ]
        self.assertEqual(len(prior_tool_results), 1)

    def test_final_before_finding_is_rejected_instead_of_passed(self) -> None:
        _ReviewerFinalBeforeFindingClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerFinalBeforeFindingClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
        self.assertIn("未完成/未验证", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["final_submits"], 0)
        self.assertEqual(summary["finding_submits"], 0)
        self.assertEqual(summary["verdicts"], {})
        self.assertEqual(summary["rejected_final_submits"], 2)
        self.assertEqual(summary["rejected_finding_submits"], 2)
        self.assertEqual(summary["output_lifecycle_exhausted"], 1)
        self.assertEqual(summary["errors"], {"invalid_output": 1})

    def test_valid_finding_before_malformed_final_cannot_be_dropped_by_later_pass(self) -> None:
        _ReviewerFindingMalformedFinalThenPassClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerFindingMalformedFinalThenPassClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
        self.assertIn("analogous candidate", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["schema_failures"], 0)
        self.assertEqual(summary["repairs"], 0)
        self.assertEqual(summary["rejected_final_submits"], 1)
        self.assertEqual(summary["verdicts"], {"pass": 1, "revise": 1})
        self.assertEqual(summary["finding_submits"], 1)

    def test_eight_incremental_findings_can_assemble_before_final(self) -> None:
        _ReviewerEightFindingsClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerEightFindingsClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
        self.assertIn("unlocated", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["finding_submits"], 8)
        self.assertEqual(summary["findings"], 8)
        self.assertEqual(summary["repair_exhausted"], 0)

    def test_ninth_incremental_finding_is_tool_local_capacity_rejection(self) -> None:
        _ReviewerNinthFindingClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerNinthFindingClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
        self.assertIn("unlocated", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["finding_submits"], 8)
        self.assertEqual(summary["rejected_finding_submits"], 1)
        self.assertEqual(summary["finding_limit_hits"], 1)
        self.assertEqual(summary["repair_exhausted"], 0)
        self.assertEqual(summary["errors"], {})
        review_calls = [
            call for call in _ReviewerNinthFindingClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(
            [tool["function"]["name"] for tool in review_calls[1]["tools"]],
            [REVIEWER_OUTPUT_TOOL_NAME],
        )
        tool_results = [message for message in review_calls[1]["messages"] if message.get("role") == "tool"]
        self.assertEqual(len([message for message in tool_results if message.get("tool_call_id") == "finding-9"]), 1)
        self.assertIn("capacity reached", str(tool_results[-1].get("content")))

    def test_tenth_finding_same_turn_gets_limit_results_but_next_final_turn_allowed(self) -> None:
        _ReviewerTenFindingsClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerTenFindingsClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
        self.assertIn("unlocated", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["finding_submits"], 8)
        self.assertEqual(summary["rejected_finding_submits"], 2)
        self.assertEqual(summary["finding_limit_hits"], 2)
        self.assertEqual(summary["output_lifecycle_exhausted"], 0)
        review_calls = [
            call for call in _ReviewerTenFindingsClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(
            [tool["function"]["name"] for tool in review_calls[1]["tools"]],
            [REVIEWER_OUTPUT_TOOL_NAME],
        )
        tool_results = [message for message in review_calls[1]["messages"] if message.get("role") == "tool"]
        for index in range(1, 11):
            self.assertEqual(len([message for message in tool_results if message.get("tool_call_id") == f"finding-{index}"]), 1)

    def test_ninth_finding_plus_valid_final_assembles_top_eight(self) -> None:
        _ReviewerNineFindingsWithFinalClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerNineFindingsWithFinalClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
        self.assertIn("unlocated", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["finding_submits"], 8)
        self.assertEqual(summary["findings"], 8)
        self.assertEqual(summary["rejected_finding_submits"], 1)
        self.assertEqual(summary["finding_limit_hits"], 1)
        self.assertEqual(summary["final_submits"], 2)

    def test_pass_after_accepted_finding_is_rejected_until_revise_preserves_it(self) -> None:
        _ReviewerAcceptedFindingThenPassClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerAcceptedFindingThenPassClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
        self.assertIn("analogous candidate", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["finding_submits"], 1)
        self.assertEqual(summary["rejected_final_submits"], 1)
        self.assertEqual(summary["verdicts"], {"pass": 1, "revise": 1})

    def test_valid_finding_survives_unknown_later_output_call(self) -> None:
        _ReviewerValidFindingUnknownLaterClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerValidFindingUnknownLaterClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
        self.assertIn("analogous candidate", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["finding_submits"], 1)
        self.assertEqual(summary["protocol_failures"], 1)
        self.assertEqual(summary["verdicts"], {"pass": 1, "revise": 1})

    def test_blocking_rejection_plus_valid_final_in_same_response_cannot_terminal(self) -> None:
        cases = (
            (_ReviewerInvalidFindingThenPassClient, "bad-finding", "bad-pass"),
            (_ReviewerUnknownThenPassClient, "unknown-output", "bad-pass"),
            (_ReviewerTwoFinalsThenFixClient, "first-final", "second-final"),
        )
        for client, first_call_id, second_call_id in cases:
            with self.subTest(client=client.__name__), tempfile.TemporaryDirectory() as tmp:
                client.calls = []
                with patch("local_agent.agent.OpenAICompatibleClient", client):
                    runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                    answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
            self.assertIn("analogous candidate", answer)
            summary = runtime._last_run_summary["read_only_reviewer"]
            self.assertEqual(summary["verdicts"], {"pass": 1, "revise": 1})
            self.assertEqual(summary["final_submits"], 2)
            review_calls = [
                call for call in client.calls
                if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
            ]
            paired = [message for message in review_calls[1]["messages"] if message.get("role") == "tool"]
            self.assertEqual(len([message for message in paired if message.get("tool_call_id") == first_call_id]), 1)
            self.assertEqual(len([message for message in paired if message.get("tool_call_id") == second_call_id]), 1)

    def test_repeated_finding_after_capacity_exhausts_output_lifecycle(self) -> None:
        _ReviewerRepeatedFindingAfterCapacityClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerRepeatedFindingAfterCapacityClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
        self.assertIn("未完成/未验证", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["finding_submits"], 8)
        self.assertEqual(summary["finding_limit_hits"], 2)
        self.assertEqual(summary["output_lifecycle_exhausted"], 1)
        self.assertEqual(summary["errors"], {"invalid_output": 1})

    def test_schema_repair_exhaustion_stays_unverified_without_raw_payload(self) -> None:
        _ReviewerRepairExhaustedClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerRepairExhaustedClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
        self.assertIn("未完成/未验证", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["attempts"], 3)
        self.assertEqual(summary["schema_failures"], 0)
        self.assertEqual(summary["repair_exhausted"], 0)
        self.assertEqual(summary["rejected_final_submits"], 3)
        self.assertEqual(summary["output_lifecycle_exhausted"], 1)
        self.assertNotIn('"findings":"wrong"', answer)

    def test_schema_diagnostics_are_typed_and_do_not_echo_claim_text(self) -> None:
        units = candidate_claim_units("secret candidate claim")
        with self.assertRaises(ReviewerValidationError) as raised:
            parse_reviewer_result(
                '{"verdict":"pass","confidence":0.9,"findings":[{"claim_id":"c001","claim":"secret candidate claim","finding_scope":"candidate_defect","issue":"secret claim","action":"repair"}],"reason":"x"}',
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
            (_ReviewerMultipleOutputClient, "openai-compatible", "invalid_output", 0),
            (_ReviewerMalformedOutputClient, "openai-compatible", "invalid_output", 0),
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
            (_ReviewerRepairTimeoutClient, "openai-compatible", "timeout", 0, 2, 0, 0),
            (_ReviewerRepairProtocolClient, "openai-compatible", "protocol_error", 2, 3, 0, 0),
            (_ReviewerRepairXmlClient, "bailian", "protocol_error", 3, 4, 3, 2),
        )
        for client, provider, expected, protocol_count, attempts, schema_failures, repairs in cases:
            with self.subTest(client=client.__name__), tempfile.TemporaryDirectory() as tmp:
                client.calls = []
                with patch("local_agent.agent.OpenAICompatibleClient", client):
                    runtime = AgentRuntime(_config(Path(tmp).resolve(), provider=provider), show_tool_logs=False)
                    answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改。")
                summary = runtime._last_run_summary["read_only_reviewer"]
                self.assertIn("未完成/未验证", answer)
                self.assertEqual(summary["attempts"], attempts)
                self.assertEqual(summary["schema_failures"], schema_failures)
                self.assertEqual(summary["repairs"], repairs)
                self.assertEqual(summary["errors"], {expected: 1})
                self.assertEqual(runtime._last_run_summary["provider_protocol_violations"], 0)
                self.assertEqual(summary["protocol_failures"], protocol_count)
                self.assertNotIn("secret", answer)
                self.assertNotIn("secret", runtime._session.path.read_text(encoding="utf-8"))

    def test_reviewer_schema_repair_is_not_attempted_without_remaining_deadline(self) -> None:
        _ReviewerMissingIdAfterFindingClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerMissingIdAfterFindingClient):
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
        self.assertEqual(summary["typed_submits"], 4)
        self.assertEqual(summary["finding_submits"], 2)
        self.assertEqual(summary["final_submits"], 2)
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

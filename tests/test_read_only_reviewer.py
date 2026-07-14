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
from local_agent.document_consistency import document_consistency_rewrite_context
from local_agent.document_consistency import MAX_REWRITE_CONTEXT_CHARS
from local_agent.explore_handoff import build_explore_handoff
from local_agent.explore_handoff import ClaimEvidenceItem
from local_agent.explore_handoff import ExploreHandoff
from local_agent.read_only_reviewer import ReviewerResult
from local_agent.read_only_reviewer import ReviewerFinding
from local_agent.read_only_reviewer import candidate_claim_projection_issues
from local_agent.read_only_reviewer import candidate_claim_units
from local_agent.read_only_reviewer import rewrite_complies_with_review
from local_agent.read_only_reviewer import parse_reviewer_payload
from local_agent.read_only_reviewer import parse_reviewer_result
from local_agent.read_only_reviewer import ReviewerValidationError
from local_agent.read_only_reviewer import reviewer_repair_messages
from local_agent.read_only_reviewer import reviewer_messages
from local_agent.read_only_reviewer import reviewer_transport_rewrite_message
from local_agent.read_only_reviewer import reviewer_rewrite_message
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
from local_agent.finalization import MAX_FINALIZATION_ATTEMPTS
from local_agent.requirement_evidence import RequirementEvidence
from local_agent.requirement_evidence import parse_document_locators
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


SCOPED_OWNER_BEFORE_REVIEW = "Verified owner is unlocated; PayServiceImpl remains an analogous candidate only."
SCOPED_OWNER_AFTER_REVIEW = "PayServiceImpl remains only an analogous candidate; verified owner is unlocated."


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
    include_claim: bool = True,
) -> dict:
    arguments = {
        "claim_id": claim_id,
        "finding_scope": scope,
        "issue": issue,
        "action": action,
    }
    if include_claim:
        arguments["claim"] = claim
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": REVIEWER_FINDING_TOOL_NAME,
            "arguments": json.dumps(arguments),
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


def _assert_provider_safe_reviewer_history(
    messages: list[dict],
    *,
    expected_ids: tuple[str, ...],
    canonicalized_ids: tuple[str, ...],
) -> None:
    assistant_calls: dict[str, dict] = {}
    tool_result_ids: list[str] = []
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or ():
                call_id = str(call.get("id") or "")
                if call_id:
                    assistant_calls[call_id] = call
                    arguments = call.get("function", {}).get("arguments")
                    if not isinstance(arguments, str):
                        raise AssertionError(f"tool call {call_id} has non-string arguments")
                    json.loads(arguments)
        elif message.get("role") == "tool":
            tool_result_ids.append(str(message.get("tool_call_id") or ""))
    for call_id in expected_ids:
        if call_id not in assistant_calls:
            raise AssertionError(f"missing assistant tool call {call_id}")
        if tool_result_ids.count(call_id) != 1:
            raise AssertionError(f"missing paired tool result for {call_id}")
    for call_id in canonicalized_ids:
        arguments = assistant_calls[call_id]["function"]["arguments"]
        if arguments != "{}":
            raise AssertionError(f"tool call {call_id} was not canonicalized")


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
        content = SCOPED_OWNER_AFTER_REVIEW if self._review_calls else SCOPED_OWNER_BEFORE_REVIEW
        return type("Response", (), {"message": {"content": content}})()


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
        content = SCOPED_OWNER_AFTER_REVIEW if self._review_calls else SCOPED_OWNER_BEFORE_REVIEW
        return type("Response", (), {"message": {"content": content}})()


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
        content = SCOPED_OWNER_AFTER_REVIEW if self._review_calls else SCOPED_OWNER_BEFORE_REVIEW
        return type("Response", (), {"message": {"content": content}})()


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
        content = SCOPED_OWNER_AFTER_REVIEW if self._review_calls else SCOPED_OWNER_BEFORE_REVIEW
        return type("Response", (), {"message": {"content": content}})()


class _ReviewerFindingReplayAfterMalformedFinalClient(_ReviewerFindingMalformedFinalThenPassClient):
    calls: list[dict] = []

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            claim = _first_candidate_claim(messages)
            finding = _finding_call("finding", "c001", claim, action="mark unlocated")
            if self._review_calls == 1:
                return _review_tool_calls_response([finding, _final_call("bad-final", "{")])
            if self._review_calls == 2:
                finding["id"] = "finding-replayed"
                return _review_tool_calls_response(
                    [finding, _final_call("valid-final", {"verdict": "revise", "confidence": 0.9, "reason": "kept candidate defect"})]
                )
            return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite scoped"})
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
                                "id": "read-src",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path":"src/Evidence.java"}'},
                            }
                        ],
                    }
                },
            )()
        if self._primary_calls == 2:
            return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()
        return type("Response", (), {"message": {"content": SCOPED_OWNER_AFTER_REVIEW}})()


class _ReviewerConflictingFindingThenFinalClient(_ReviewerFindingMalformedFinalThenPassClient):
    calls: list[dict] = []

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            if self._review_calls == 1:
                return _review_tool_calls_response([
                    _finding_call(
                        "first-finding",
                        "c001",
                        _candidate_claim(messages, "c001"),
                        issue="owner evidence is insufficient",
                        action="mark owner unlocated",
                    )
                ])
            if self._review_calls == 2:
                return _review_tool_calls_response([
                    _finding_call(
                        "conflicting-finding",
                        "c001",
                        _candidate_claim(messages, "c001"),
                        issue="a second defect for the same claim",
                        action="replace the first immutable finding",
                    )
                ])
            if self._review_calls == 3:
                return _review_tool_calls_response([
                    _final_call(
                        "valid-final",
                        {"verdict": "revise", "confidence": 0.9, "reason": "first finding preserved"},
                    )
                ])
            return _review_tool_calls_response([
                _final_call("rewrite-pass", {"verdict": "pass", "confidence": 0.95, "reason": "rewrite scoped"})
            ])
        self._primary_calls += 1
        content = SCOPED_OWNER_AFTER_REVIEW if self._review_calls else SCOPED_OWNER_BEFORE_REVIEW
        return type("Response", (), {"message": {"content": content}})()


class _ReviewerProviderSafeContinuationClient(_ReviewerFindingMalformedFinalThenPassClient):
    calls: list[dict] = []

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            if self._review_calls == 1:
                return _review_tool_calls_response([
                    _finding_call("valid-finding", "c001", _candidate_claim(messages, "c001"), action="mark unlocated"),
                    {
                        "id": "native-finding",
                        "type": "function",
                        "function": {
                            "name": REVIEWER_FINDING_TOOL_NAME,
                            "arguments": {
                                "claim_id": "c002",
                                "finding_scope": "candidate_defect",
                                "issue": "native arguments",
                                "action": "mark unverified",
                            },
                        },
                    },
                    _final_call("bad-final", "{"),
                ])
            if self._review_calls == 2:
                _assert_provider_safe_reviewer_history(
                    messages,
                    expected_ids=("valid-finding", "native-finding", "bad-final"),
                    canonicalized_ids=("native-finding", "bad-final"),
                )
                return _review_tool_calls_response([
                    _final_call("valid-final", {"verdict": "revise", "confidence": 0.9, "reason": "kept safe continuation"}),
                ])
            return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite scoped"})
        self._primary_calls += 1
        if self._primary_calls == 1:
            return type(
                "Response",
                (),
                {"message": {"content": "PayServiceImpl is the verified owner.\nExisting DDL is complete."}},
            )()
        return type(
            "Response",
            (),
            {"message": {"content": SCOPED_OWNER_AFTER_REVIEW if self._review_calls else SCOPED_OWNER_BEFORE_REVIEW}},
        )()


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
        content = SCOPED_OWNER_AFTER_REVIEW if self._review_calls else SCOPED_OWNER_BEFORE_REVIEW
        return type("Response", (), {"message": {"content": content}})()


class _ReviewerNoClaimFindingsClient(_ReviewerFindingMalformedFinalThenPassClient):
    calls: list[dict] = []

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            if self._review_calls == 1:
                return _review_tool_calls_response([
                    _finding_call("finding-1", "c001", "", issue="unsupported owner", action="mark unlocated", include_claim=False),
                    _finding_call("finding-2", "c002", "", issue="unsupported DDL", action="mark unverified", include_claim=False),
                ])
            if self._review_calls == 2:
                return _review_tool_calls_response([
                    _finding_call("finding-3", "c003", "", issue="unsupported API", action="mark proposal", include_claim=False),
                    _final_call("final-revise", {"verdict": "revise", "confidence": 0.9, "reason": "anchor-bound findings"})
                ])
            return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite scoped"})
        self._primary_calls += 1
        if self._primary_calls == 1:
            return type(
                "Response",
                (),
                {"message": {"content": "- PayServiceImpl is verified owner.\n- Existing DDL is complete.\n- API path is implemented."}},
            )()
        content = (
            "- Owner evidence remains unlocated.\n- DDL remains unverified.\n- API path is only a proposal."
            if self._review_calls
            else "- Owner remains unlocated.\n- DDL is unverified.\n- API path is only a proposal."
        )
        return type("Response", (), {"message": {"content": content}})()


class _RewriteVerificationParaphraseClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self._primary_calls = 0
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        is_reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        is_verification = any("LCA_READ_ONLY_REWRITE_VERIFICATION" in str(message.get("content")) for message in messages)
        if is_reviewer:
            self._review_calls += 1
            if not is_verification:
                findings = [
                    _finding_call(
                        f"finding-{index}",
                        f"c{index:03d}",
                        _candidate_claim(messages, f"c{index:03d}"),
                        issue="claim overstates bounded evidence",
                        action="downgrade to unlocated or proposal",
                        include_claim=False,
                    )
                    for index in range(1, 9)
                ]
                return _review_tool_calls_response([
                    *findings,
                    _final_call("initial-revise", {"verdict": "revise", "confidence": 0.9, "reason": "eight blockers"}),
                ])
            candidate_text = " ".join(
                str(claim.get("text") or "")
                for message in messages
                if message.get("role") == "user" and "LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content"))
                for claim in json.loads(message["content"]).get("candidate_claims", ())
            )
            if "repository conclusively lacks" in candidate_text:
                return _review_tool_calls_response([
                    _finding_call(
                        "verify-finding",
                        "c001",
                        _candidate_claim(messages, "c001"),
                        issue="paraphrased absence claim is still beyond bounded evidence",
                        action="make it a proposal or pending confirmation",
                        include_claim=False,
                    ),
                    _final_call("verify-revise", {"verdict": "revise", "confidence": 0.9, "reason": "paraphrase still blocks"}),
                ])
            return _review_tool_calls_response([
                _final_call("verify-pass", {"verdict": "pass", "confidence": 0.95, "reason": "all blockers downgraded"})
            ])
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
                                "id": "read-evidence",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path":"src/Evidence.java"}'},
                            }
                        ],
                    }
                },
            )()
        if self._primary_calls == 2:
            claims = [
                "Current code proves no settlement DDL exists.",
                "Current code proves no settlement API exists.",
                "Current code proves no menu route exists.",
                "Current code proves no template engine exists.",
                "Current code proves no owner class exists.",
                "Current code proves no status field exists.",
                "Current code proves no rollback flow exists.",
                "Current code proves no export mapping exists.",
            ]
            return type("Response", (), {"message": {"content": "\n".join(f"- {claim}" for claim in claims)}})()
        if self._primary_calls == 3:
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": (
                            "The repository conclusively lacks database schema, service API, menu route, template engine, "
                            "owner class, status field, rollback flow, and export mapping."
                        )
                    }
                },
            )()
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": (
                        "已读证据未定位直接 owner。设计建议：可提出新增 settlement model/API/menu/template 作为候选方案；"
                        "待确认是否复用旧实体、字段和下载能力。"
                    )
                }
            },
        )()


class _ProposalSemanticsClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self._primary_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            prompt = "\n".join(str(message.get("content") or "") for message in messages if message.get("role") == "system")
            if "clearly labeled design proposal" not in prompt:
                raise AssertionError("reviewer prompt did not preserve proposal semantics")
            return _review_tool_calls_response([
                _final_call("proposal-pass", {"verdict": "pass", "confidence": 0.95, "reason": "proposal is labeled and prerequisites remain pending"})
            ])
        self._primary_calls += 1
        if self._primary_calls == 1:
            return type("Response", (), {"message": {"content": None, "tool_calls": [
                {"id": "read-src", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"src/Mapper.xml"}'}}
            ]}})()
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": (
                        "设计建议：建议新建 SettlementBill 表、SettlementDetail 表和 /api/settlement/create 接口；"
                        "这些名称是候选方案/待确认项，前置条件是确认旧实体字段、Owner 和下载能力是否可复用。"
                    )
                }
            },
        )()


class _RewriteVerificationSameClaimIdClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self._primary_calls = 0
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        is_reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        is_verification = any("LCA_READ_ONLY_REWRITE_VERIFICATION" in str(message.get("content")) for message in messages)
        if is_reviewer:
            self._review_calls += 1
            if self._review_calls == 1:
                return _review_tool_calls_response([
                    _finding_call(
                        "initial-finding",
                        "c001",
                        _candidate_claim(messages, "c001"),
                        issue="initial owner claim unsupported",
                        action="downgrade owner to unlocated",
                        include_claim=False,
                    ),
                    _final_call("initial-final", {"verdict": "revise", "confidence": 0.9, "reason": "initial blocker"}),
                ])
            if self._review_calls == 2 and is_verification:
                return _review_tool_calls_response([
                    _finding_call(
                        "verification-finding",
                        "c001",
                        _candidate_claim(messages, "c001"),
                        issue="c001 paraphrased owner absence still overstates evidence",
                        action="make owner absence pending confirmation",
                        include_claim=False,
                    ),
                    _final_call("verification-final", {"verdict": "revise", "confidence": 0.9, "reason": "verification blocker"}),
                ])
            if self._review_calls == 3 and is_verification:
                closure_payloads = [
                    json.loads(message["content"])
                    for message in messages
                    if message.get("role") == "user"
                    and "LCA_READ_ONLY_REWRITE_VERIFICATION" in str(message.get("content"))
                ]
                closure = closure_payloads[-1]
                if closure.get("prior_review") != {
                    "verdict": "revise",
                    "blocking_finding_count": 1,
                    "historical_details_withheld": True,
                }:
                    raise AssertionError(f"prior review summary was not bounded: {closure!r}")
                if "original_findings" in closure or "c001 paraphrased" in json.dumps(closure, ensure_ascii=False):
                    raise AssertionError(f"superseded finding details leaked into fresh review: {closure!r}")
                return _review_tool_calls_response([
                    _final_call("verification-pass", {"verdict": "pass", "confidence": 0.95, "reason": "all closure findings scoped"})
                ])
            return _review_tool_calls_response([
                _final_call("fallback-pass", {"verdict": "pass", "confidence": 0.95, "reason": "fallback"})
            ])
        self._primary_calls += 1
        if self._primary_calls == 1:
            return type("Response", (), {"message": {"content": None, "tool_calls": [
                {"id": "read-src", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"src/Evidence.java"}'}}
            ]}})()
        if self._primary_calls == 2:
            return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()
        if self._primary_calls == 3:
            return type("Response", (), {"message": {"content": "The repository proves no other owner exists."}})()
        return type("Response", (), {"message": {"content": "Owner evidence remains unlocated; reuse remains pending confirmation."}})()


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
        content = SCOPED_OWNER_AFTER_REVIEW if self._review_calls else SCOPED_OWNER_BEFORE_REVIEW
        return type("Response", (), {"message": {"content": content}})()


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


class _ReviewerMixedRejectedTurnThenFinalClient(_ReviewerEightFindingsClient):
    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            if self._review_calls == 1:
                return _review_tool_calls_response(
                    [
                        _finding_call(f"finding-{index}", f"c{index:03d}", _candidate_claim(messages, f"c{index:03d}"))
                        for index in range(1, 8)
                    ]
                    + [
                        _finding_call("bad-finding", "c008", _candidate_claim(messages, "c008") + " paraphrased"),
                        _final_call("blocked-final-1", {"verdict": "pass", "confidence": 0.9, "reason": "bad turn"}),
                    ]
                )
            if self._review_calls == 2:
                return _review_tool_calls_response([
                    _finding_call("duplicate-1", "c001", _candidate_claim(messages, "c001")),
                    _finding_call("duplicate-2", "c002", _candidate_claim(messages, "c002")),
                    _finding_call(
                        "conflict-3",
                        "c003",
                        _candidate_claim(messages, "c003"),
                        action="use a conflicting replacement action",
                    ),
                    _finding_call("mismatch-4", "c004", _candidate_claim(messages, "c004") + " replayed"),
                    _finding_call("mismatch-5", "c005", _candidate_claim(messages, "c005") + " replayed"),
                    _finding_call("finding-8", "c008", _candidate_claim(messages, "c008")),
                    _finding_call("finding-9", "c009", _candidate_claim(messages, "c009")),
                    _final_call("blocked-final-2", {"verdict": "pass", "confidence": 0.9, "reason": "still bad"}),
                ])
            if self._review_calls == 3:
                return _review_tool_calls_response([
                    _final_call("final-revise", {"verdict": "revise", "confidence": 0.9, "reason": "eight findings recorded"})
                ])
            return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite scoped"})
        self._primary_calls += 1
        if self._review_calls >= 3:
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
        content = SCOPED_OWNER_AFTER_REVIEW if self._review_calls else SCOPED_OWNER_BEFORE_REVIEW
        return type("Response", (), {"message": {"content": content}})()


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
        content = SCOPED_OWNER_AFTER_REVIEW if self._review_calls else SCOPED_OWNER_BEFORE_REVIEW
        return type("Response", (), {"message": {"content": content}})()


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
        content = SCOPED_OWNER_AFTER_REVIEW if self._review_calls else SCOPED_OWNER_BEFORE_REVIEW
        return type("Response", (), {"message": {"content": content}})()


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
            else (
                "真实 owner 未由本轮证据定位；PayServiceImpl 仅能作为弱相关候选。"
                if self._review_calls
                else "PayServiceImpl 只是弱相关候选；真实 owner 未在本轮证据中定位。"
            )
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
            type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
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


class _PartialProgressRewriteClient(_ReviewerFlowClient):
    def chat(self, messages, tools, *, timeout=None):
        is_reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        is_verification = any("LCA_READ_ONLY_REWRITE_VERIFICATION" in str(message.get("content")) for message in messages)
        if is_reviewer:
            type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
            self._review_calls += 1
            if is_verification:
                return _review_submit(
                    {
                        "verdict": "pass",
                        "confidence": 0.9,
                        "findings": [],
                        "reason": "one blocking owner claim was downgraded and remaining advice is non-blocking",
                    }
                )
            first_claim = _candidate_claim(messages, "c001")
            second_claim = _candidate_claim(messages, "c002")
            return _review_submit(
                {
                    "verdict": "revise",
                    "confidence": 0.9,
                    "findings": [
                        {
                            "claim_id": "c001",
                            "claim": first_claim,
                            "finding_scope": "candidate_defect",
                            "issue": "unsupported owner",
                            "action": "mark owner unlocated",
                        },
                        {
                            "claim_id": "c002",
                            "claim": second_claim,
                            "finding_scope": "candidate_defect",
                            "issue": "unsupported API",
                            "action": "mark API unverified",
                        },
                    ],
                    "reason": "some repository facts are unsupported",
                }
            )
        self._primary_calls += 1
        if self._primary_calls == 1:
            return type(
                "Response",
                (),
                {"message": {"content": "- 已证实真实 owner 是 PayServiceImpl。\n- 导出接口已经实现。"}},
            )()
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": (
                        "- Owner evidence remains unlocated.\n- 导出接口已经实现。"
                        if self._review_calls
                        else "- Owner remains unlocated.\n- 导出接口已经实现。"
                    )
                }
            },
        )()


class _ReviewerLastGateClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config
        self._primary_calls = 0
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        is_verification = any("LCA_READ_ONLY_REWRITE_VERIFICATION" in str(message.get("content")) for message in messages)
        if reviewer:
            self._review_calls += 1
            if is_verification:
                return _review_submit({"verdict": "pass", "confidence": 0.9, "findings": [], "reason": "rewrite downgraded unsupported repository detail"})
            return _review_submit(
                {
                    "verdict": "revise",
                    "confidence": 0.9,
                    "findings": [{"claim_id": "c001", "claim": "现有 Redis key、权限接口和模板服务已经由 PayServiceImpl 实现。", "finding_scope": "candidate_defect", "issue": "unsupported repository detail", "action": "mark it unverified"}],
                    "reason": "no direct binding",
                }
            )
        self._primary_calls += 1
        if self._primary_calls == 1:
            content = "现有 Redis key、权限接口和模板服务已经由 PayServiceImpl 实现。"
        elif self._primary_calls == 2:
            content = "需要补充需求引用后再给出结论。"
        else:
            content = "Redis key、权限接口和模板服务仅作为设计建议/待确认项；未验证为现有实现。"
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


class _SpoofedDocumentPriorityClient(_SpoofedDocumentConsistencyClient):
    """A reviewer that falsely passes an invented source-priority rule."""

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
            {"message": {"content": "policy.md 要求字段留空；prototype.html 显示有值。需求文档为最高优先级依据，因此 prototype 只作参考。 FORGED_PRIORITY"}},
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
                                "stance": "reported_unresolved",
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
        return type("Response", (), {"message": {"content": "policy.md 要求字段留空；prototype.html 显示有值。两份资料表面存在差异，资料角色未说明，因此当前仍未消解。"}})()


class _DocumentConsistencyInvalidFindingRecoveryClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self._primary_calls = 0
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        if reviewer:
            self._review_calls += 1
            final_payload = {
                "verdict": "revise",
                "confidence": 0.9,
                "reason": "document conflict remains bounded",
                "document_consistency": {
                    "stance": "reported_unresolved",
                    "conflict_evidence_ids": ["e001", "e002"],
                    "supporting_evidence_ids": [],
                },
            }
            if self._review_calls == 1:
                findings = [
                    _finding_call(
                        f"scope-{index}",
                        f"c{index:03d}",
                        _candidate_claim(messages, f"c{index:03d}"),
                        issue="ordinary unsupported scope detail",
                        action="mark this detail unverified",
                    )
                    for index in range(1, 8)
                ]
                findings.append(
                    _finding_call(
                        "bad-conflict",
                        "c008",
                        _candidate_claim(messages, "c008"),
                        issue="the candidate discusses the artifact conflict",
                        action="Treat the prototype as the later completed state.",
                    )
                )
                return _review_tool_calls_response([*findings, _final_call("bad-final", final_payload)])
            if self._review_calls == 2:
                return _review_tool_calls_response(
                    [
                        _finding_call(
                            "fixed-conflict",
                            "c008",
                            _candidate_claim(messages, "c008"),
                            issue="the candidate discusses the artifact conflict",
                            action="Delete the unsupported reconciliation and state that artifact role remains unresolved.",
                        ),
                        _final_call("valid-final", final_payload),
                    ]
                )
            raise AssertionError("second open-ended reviewer call should not be reached")
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
                                "id": "read-policy",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path":"policy.md"}'},
                            },
                            {
                                "id": "read-prototype",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path":"prototype.html"}'},
                            },
                        ],
                    }
                },
            )()
        if self._review_calls == 0:
            content = "\n\n".join(
                [f"Requirement claim {index} is verified." for index in range(1, 8)]
                + ["The document and prototype conflict remains unresolved."]
            )
            return type("Response", (), {"message": {"content": content}})()
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": (
                        "Requirement claims 1-7 are unverified. "
                        "policy.md:1 says the field is blank; prototype.html:1 shows a value. "
                        "The policy/prototype discrepancy remains unresolved because artifact role is not established."
                    )
                }
            },
        )()


class _DocumentConsistencyFinalSubmitRecoveryClient:
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
                findings = [
                    _finding_call(
                        f"finding-{index}",
                        f"c{index:03d}",
                        _candidate_claim(messages, f"c{index:03d}"),
                        issue="candidate fact needs qualification",
                        action="mark this reviewed claim unverified or unresolved",
                    )
                    for index in range(1, 8)
                ]
                bad_keys = {
                    "verdict": "revise",
                    "confidence": 0.9,
                    "reason": "document conflict needs rewrite",
                    "document_consistency": {
                        "stance": "reported_unresolved",
                        "conflict_ids": ["e001", "e002"],
                        "supporting_evidence_ids": [],
                    },
                }
                return _review_tool_calls_response([*findings, _final_call("bad-keys", bad_keys)])
            if self._review_calls == 2:
                one_sided = {
                    "verdict": "revise",
                    "confidence": 0.9,
                    "reason": "document conflict needs rewrite",
                    "document_consistency": {
                        "stance": "reported_unresolved",
                        "conflict_evidence_ids": ["e001"],
                        "supporting_evidence_ids": [],
                    },
                }
                return _review_tool_calls_response([_final_call("one-sided", one_sided)])
            if self._review_calls == 3:
                corrected = {
                    "verdict": "revise",
                    "confidence": 0.9,
                    "reason": "document conflict needs rewrite",
                    "document_consistency": {
                        "stance": "reported_unresolved",
                        "conflict_evidence_ids": ["e001", "e002"],
                        "supporting_evidence_ids": [],
                    },
                }
                return _review_tool_calls_response([_final_call("corrected", corrected)])
            raise AssertionError("second open-ended reviewer call should not be reached")
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
                                "id": "read-policy",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path":"policy.md"}'},
                            },
                            {
                                "id": "read-prototype",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path":"prototype.html"}'},
                            },
                        ],
                    }
                },
            )()
        if self._review_calls == 0:
            content = "\n\n".join(
                [f"Requirement claim {index} is verified." for index in range(1, 7)]
                + [
                    "policy.md:1 says the field is blank; prototype.html:1 shows a value, "
                    "but the prototype is a later completed state so there is no conflict."
                ]
            )
            return type("Response", (), {"message": {"content": content}})()
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": (
                        "Requirement claims 1-6 are unverified. policy.md:1 says the field is blank; "
                        "prototype.html:1 shows a value. The policy/prototype discrepancy remains unresolved because "
                        "artifact role and lifecycle are not established."
                    )
                }
            },
        )()


class _DocumentConsistencyMixedCorrectionRecoveryClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self._primary_calls = 0
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        if reviewer:
            self._review_calls += 1
            final_payload = {
                "verdict": "revise",
                "confidence": 0.9,
                "reason": "document conflict remains unresolved",
                "document_consistency": {
                    "stance": "reported_unresolved",
                    "conflict_evidence_ids": ["e001", "e002"],
                    "supporting_evidence_ids": [],
                },
            }
            if self._review_calls == 1:
                findings = [
                    _finding_call(
                        f"finding-{index}",
                        f"c{index:03d}",
                        _candidate_claim(messages, f"c{index:03d}"),
                        issue="candidate fact needs qualification",
                        action="mark this reviewed claim unverified",
                    )
                    for index in range(1, 8)
                ]
                findings.append(
                    _finding_call(
                        "bad-conflict",
                        "c008",
                        _candidate_claim(messages, "c008"),
                        issue="the candidate discusses an artifact conflict",
                        action="Treat the prototype as the later completed state.",
                    )
                )
                return _review_tool_calls_response([*findings, _final_call("malformed-final-1", "{")])
            if self._review_calls == 2:
                return _review_tool_calls_response([_final_call("malformed-final-2", "{")])
            if self._review_calls == 3:
                return _review_tool_calls_response([_final_call("invalid-document-final", final_payload)])
            if self._review_calls == 4:
                return _review_tool_calls_response(
                    [
                        _finding_call(
                            "fixed-conflict",
                            "c008",
                            _candidate_claim(messages, "c008"),
                            issue="the candidate discusses an artifact conflict",
                            action="Remove the unsupported reconciliation and keep artifact role/lifecycle unresolved.",
                        ),
                        _final_call("valid-final", final_payload),
                    ]
                )
            raise AssertionError("unexpected reviewer turn")
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
                                "id": "read-policy",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path":"policy.md"}'},
                            },
                            {
                                "id": "read-prototype",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path":"prototype.html"}'},
                            },
                        ],
                    }
                },
            )()
        if self._review_calls >= 4:
            content = (
                "Requirement claims 1-7 are unverified. policy.md:1 says the field is blank; "
                "prototype.html:1 shows a value. The policy/prototype discrepancy remains unresolved because "
                "artifact role and lifecycle are not established."
            )
        else:
            content = "\n\n".join(
                [f"Requirement claim {index} is verified." for index in range(1, 8)]
                + [
                    "policy.md:1 says the field is blank; prototype.html:1 shows a value, "
                    "but the prototype is a later completed state so there is no conflict."
                ]
            )
        return type("Response", (), {"message": {"content": content}})()


class _DocumentConsistencyRepeatedInvalidFindingClient(_DocumentConsistencyInvalidFindingRecoveryClient):
    calls: list[dict] = []

    def chat(self, messages, tools, *, timeout=None):
        reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        if reviewer:
            type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
            self._review_calls += 1
            final_payload = {
                "verdict": "revise",
                "confidence": 0.9,
                "reason": "bad conflict finding repeats",
                "document_consistency": {
                    "stance": "reported_unresolved",
                    "conflict_evidence_ids": ["e001", "e002"],
                    "supporting_evidence_ids": [],
                },
            }
            calls = []
            if self._review_calls == 1:
                calls.extend(
                    _finding_call(
                        f"scope-{index}",
                        f"c{index:03d}",
                        _candidate_claim(messages, f"c{index:03d}"),
                        issue="ordinary unsupported scope detail",
                        action="mark this detail unverified",
                    )
                    for index in range(1, 8)
                )
            calls.append(
                _finding_call(
                    f"bad-conflict-{self._review_calls}",
                    "c008",
                    _candidate_claim(messages, "c008"),
                    issue="the candidate discusses the artifact conflict",
                    action="Treat the prototype as the later completed state.",
                )
            )
            calls.append(_final_call(f"bad-final-{self._review_calls}", final_payload))
            return _review_tool_calls_response(calls)
        return super().chat(messages, tools, timeout=timeout)


class _DocumentConsistencyRewriteContextClient:
    calls: list[dict] = []
    rewrite_directives: list[str] = []

    def __init__(self, config: AgentConfig):
        self._primary_calls = 0
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        if reviewer:
            self._review_calls += 1
            if self._review_calls == 1:
                return _review_tool_calls_response(
                    [
                        _finding_call(
                            "doc-finding",
                            "c002",
                            "",
                            issue="unsupported reconciliation",
                            action="keep the discrepancy unresolved",
                            include_claim=False,
                        ),
                        _final_call(
                            "doc-final",
                            {
                                "verdict": "revise",
                                "confidence": 0.9,
                                "reason": "unsupported reconciliation",
                                "document_consistency": {
                                    "stance": "reported_unresolved",
                                    "conflict_evidence_ids": ["e001", "e002"],
                                    "supporting_evidence_ids": [],
                                },
                            },
                        ),
                    ]
                )
            return _review_tool_calls_response(
                [
                    _final_call(
                        "doc-pass",
                        {
                            "verdict": "pass",
                            "confidence": 0.95,
                            "reason": "rewrite preserves unresolved conflict",
                            "document_consistency": {
                                "stance": "reported_unresolved",
                                "conflict_evidence_ids": ["e001", "e002"],
                                "supporting_evidence_ids": [],
                            },
                        },
                    )
                ]
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
            return type("Response", (), {"message": {"content": "policy.md says blank; prototype.html shows value. They are consistent because the prototype is a final state."}})()
        rewrite_contexts = [
            str(message.get("content") or "")
            for message in messages
            if "[Read-only evidence review]" in str(message.get("content") or "")
            and "Typed document-consistency context" in str(message.get("content") or "")
        ]
        rewrite_context = rewrite_contexts[-1] if rewrite_contexts else ""
        type(self).rewrite_directives.append(rewrite_context)
        required = (
            "Do not choose source priority or infer lifecycle",
            "Document A says blank",
            "Image B shows value",
            "No valid supporting evidence",
            "role/lifecycle/precedence is not established",
        )
        if all(fragment in rewrite_context for fragment in required):
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": (
                            "policy.md says blank; prototype.html shows value. "
                            "The artifact role/lifecycle/precedence is not established, so the discrepancy remains unresolved."
                        )
                    }
                },
            )()
        return type("Response", (), {"message": {"content": "policy.md says blank; prototype.html shows value. The prototype is a final state."}})()


class _DocumentConsistencyMissingDispositionClient(_DocumentConsistencyRewriteContextClient):
    calls: list[dict] = []
    rewrite_directives: list[str] = []

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        if reviewer:
            self._review_calls += 1
            return _review_tool_calls_response(
                [
                    _finding_call(
                        "doc-finding",
                        "c001",
                        "",
                        issue="unsupported reconciliation",
                        action="keep the discrepancy unresolved",
                        include_claim=False,
                    ),
                    _final_call(
                        "doc-final",
                        {
                            "verdict": "revise",
                            "confidence": 0.9,
                            "reason": "unsupported reconciliation",
                            "document_consistency": {
                                "stance": "reported_unresolved",
                                "conflict_evidence_ids": ["e001", "e002"],
                                "supporting_evidence_ids": [],
                            },
                        },
                    ),
                ]
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
            return type("Response", (), {"message": {"content": "policy.md says blank; prototype.html shows value. They are consistent because the prototype is a final state."}})()
        return type("Response", (), {"message": {"content": "policy.md says blank; prototype.html shows value."}})()


class _DocumentConsistencyAssertedRewriteClient(_DocumentConsistencyMissingDispositionClient):
    calls: list[dict] = []
    rewrite_directives: list[str] = []

    def chat(self, messages, tools, *, timeout=None):
        response = super().chat(messages, tools, timeout=timeout)
        reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        if reviewer or self._primary_calls < 3:
            return response
        return type("Response", (), {"message": {"content": "policy.md says blank; prototype.html shows value. They are consistent because the prototype is a final state."}})()


class _ClaimScopedRequirementEvidenceClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self._primary_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        reviewer = any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages)
        if reviewer:
            payload = next(
                json.loads(message["content"])
                for message in messages
                if message.get("role") == "user" and "LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content"))
            )
            claims = {claim["claim_id"]: claim["text"] for claim in payload["candidate_claims"]}
            matrix = payload["handoff"]["claim_matrix"]
            locator = next(
                (
                    item
                    for item in matrix
                    if item.get("classification") == "requirement_locator"
                    and any(claim_id in claims for claim_id in item.get("claim_ids", ()))
                    and any("policy.md:191-212" not in claims[claim_id] for claim_id in item.get("claim_ids", ()) if claim_id in claims)
                ),
                None,
            )
            summary = locator.get("summary", "") if locator else ""
            if locator and "191:" in summary and "212:" in summary and "settlement period" in summary and "grand total" in summary:
                return _review_tool_calls_response(
                    [
                        _final_call(
                            "claim-scoped-pass",
                            {
                                "verdict": "pass",
                                "confidence": 0.96,
                                "reason": "range evidence is visible for the reviewed claim",
                                "document_consistency": {
                                    "stance": "reported_unresolved",
                                    "conflict_evidence_ids": [],
                                    "supporting_evidence_ids": [],
                                },
                            },
                        )
                    ]
                )
            claim_id = next(iter(claims), "c001")
            return _review_tool_calls_response(
                [
                    _finding_call(
                        "missing-range",
                        claim_id,
                        "",
                        issue="range evidence missing from handoff",
                        action="remove unsupported requirement facts",
                        include_claim=False,
                    ),
                    _final_call(
                        "claim-scoped-revise",
                        {
                            "verdict": "revise",
                            "confidence": 0.6,
                            "reason": "range evidence missing",
                            "document_consistency": {
                                "stance": "reported_unresolved",
                                "conflict_evidence_ids": [],
                                "supporting_evidence_ids": [],
                            },
                        },
                    ),
                ]
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
            {
                "message": {
                    "content": (
                        "### 2. Word summary (policy.md:191-212；prototype.html)\n"
                        "\n"
                        "| category | requirement fact |\n"
                        "| --- | --- |\n"
                        "| period | settlement period is monthly |\n"
                        "| mappings | company/count/amount/footer mappings come from the policy |\n"
                        "| totals | subtotal and grand total follow those lines |\n"
                        "prototype.html only supplies layout."
                    )
                }
            },
        )()


class _OverlongClaimClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self._primary_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            return _review_tool_calls_response(
                [
                    _final_call(
                        "overflow-pass",
                        {
                            "verdict": "pass",
                            "confidence": 0.99,
                            "reason": "would pass if the overflow claim were silently absent",
                        },
                    )
                ]
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
                            {"id": "read-src", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"src/Candidate.java"}'}},
                        ],
                    }
                },
            )()
        long_claim = "UNSUPPORTED_LONG_OWNER_CLAIM " + ("x" * 620)
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": f"源码事实：src/Candidate.java 已读取。\n{long_claim}"
                }
            },
        )()


class _TransportOmittedClaimClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self._primary_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            return _review_tool_calls_response(
                [
                    _final_call(
                        "omitted-pass",
                        {
                            "verdict": "pass",
                            "confidence": 0.99,
                            "reason": "would pass visible claims only",
                        },
                    )
                ]
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
                        ],
                    }
                },
            )()
        claim_lines = [f"- policy.md:{index * 10} states rule {index * 10}." for index in range(1, 75)]
        claim_lines.append("- policy.md:210 states rule 210; PayServiceImpl is the verified owner.")
        return type("Response", (), {"message": {"content": "\n".join(claim_lines)}})()


class _TransportRecoveryDocumentClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self._primary_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            payload = next(
                json.loads(message["content"])
                for message in messages
                if message.get("role") == "user" and "LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content"))
            )
            self._assert_reviewable_compact_payload(payload)
            conflict_ids = self._conflict_ids(payload)
            return _review_tool_calls_response([
                _final_call(
                    "doc-pass",
                    {
                        "verdict": "pass",
                        "confidence": 0.96,
                        "reason": "compact candidate preserves the unresolved artifact discrepancy",
                        "document_consistency": {
                            "stance": "reported_unresolved",
                            "conflict_evidence_ids": list(conflict_ids),
                            "supporting_evidence_ids": [],
                        },
                    },
                )
            ])
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
                            {"id": "inspect-example", "type": "function", "function": {"name": "inspect_image", "arguments": '{"path":"example.png","question":"Describe visible reviewer and signature fields."}'}},
                        ],
                    }
                },
            )()
        if any("bounded transport recovery" in str(message.get("content")) for message in messages):
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": (
                            "## Compact reviewable answer\n\n"
                            "- Current scope: policy.md:1 says the generated document keeps reviewer/signature blank.\n"
                            "- Prototype scope: prototype.html:1 describes the corresponding form area.\n"
                            "- Visual observation: example.png visibly shows reviewer/signature fields filled.\n"
                            "- Source discrepancy: policy.md:1 and example.png differ; artifact role, lifecycle, and precedence are not established, so the discrepancy remains unresolved / pending confirmation.\n"
                            "- Open confirmation: do not treat either artifact as authoritative without explicit support."
                        )
                    }
                },
            )()
        claim_lines = [f"- policy.md:{index * 10} states requirement rule {index * 10}." for index in range(1, 75)]
        claim_lines.append(
            "- policy.md:1 says reviewer/signature blank; example.png shows reviewer/signature filled; "
            "therefore the image is only an illustrative template and there is no substantive conflict."
        )
        return type("Response", (), {"message": {"content": "\n".join(claim_lines)}})()

    def inspect_image(self, **_kwargs):
        return json.dumps(
            {
                "observations": [
                    "The example image visibly shows reviewer/signature fields filled.",
                ],
                "uncertainties": [],
                "inferences": [],
            }
        )

    def _assert_reviewable_compact_payload(self, payload: dict) -> None:
        self.assertEqual(payload.get("handoff", {}).get("transport_omitted_claim_ids"), [])
        claims = " ".join(str(claim.get("text") or "") for claim in payload.get("candidate_claims") or ())
        matrix = payload.get("handoff", {}).get("claim_matrix") or ()
        self.assertIn("unresolved", claims)
        self.assertNotIn("illustrative template", claims)
        self.assertIn("example.png", claims)
        self.assertIn("visibly shows reviewer/signature fields filled", claims)
        self.assertTrue(any("policy.md" in str(item.get("path") or "") for item in matrix))
        self.assertTrue(any("prototype.html" in str(item.get("path") or "") for item in matrix))
        conflict_ids = self._conflict_ids(payload)
        prototype_ids = {
            str(item.get("evidence_id") or "")
            for item in matrix
            if "prototype.html" in str(item.get("path") or "")
        }
        self.assertTrue(conflict_ids[0] not in prototype_ids and conflict_ids[1] not in prototype_ids)

    def _conflict_ids(self, payload: dict) -> tuple[str, str]:
        matrix = payload.get("handoff", {}).get("claim_matrix") or ()
        policy_ids = [
            str(item.get("evidence_id") or "")
            for item in matrix
            if item.get("tool") == "read_file"
            and "policy.md" in str(item.get("path") or "")
            and "blank" in str(item.get("summary") or "").lower()
        ]
        image_ids = [
            str(item.get("evidence_id") or "")
            for item in matrix
            if item.get("tool") == "inspect_image"
            and item.get("classification") == "visual_observation"
            and "example.png" in str(item.get("path") or "")
            and "filled" in str(item.get("summary") or "").lower()
        ]
        self.assertTrue(policy_ids)
        self.assertTrue(image_ids)
        return (policy_ids[0], image_ids[0])

    def assertEqual(self, left, right) -> None:
        if left != right:
            raise AssertionError(f"{left!r} != {right!r}")

    def assertIn(self, member, container) -> None:
        if member not in container:
            raise AssertionError(f"{member!r} not found")

    def assertNotIn(self, member, container) -> None:
        if member in container:
            raise AssertionError(f"{member!r} unexpectedly found")

    def assertTrue(self, value) -> None:
        if not value:
            raise AssertionError("expected truthy value")


class _TransportReviewerRewriteDocumentClient(_TransportRecoveryDocumentClient):
    calls: list[dict] = []

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            payload = next(
                json.loads(message["content"])
                for message in messages
                if message.get("role") == "user" and "LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content"))
            )
            self._assert_reviewable_compact_payload(payload)
            conflict_ids = self._conflict_ids(payload)
            claim = str(payload["candidate_claims"][0]["text"])
            return _review_tool_calls_response(
                [
                    _finding_call(
                        "scoped-alignment-finding",
                        "c001",
                        claim,
                        issue="the alignment wording is broader than the cited field-area observation",
                        action="limit the alignment statement to the observed field area",
                    ),
                    _final_call(
                        "doc-revise",
                        {
                            "verdict": "revise",
                            "confidence": 0.96,
                            "reason": "scope the unrelated alignment claim while preserving the unresolved discrepancy",
                            "document_consistency": {
                                "stance": "reported_unresolved",
                                "conflict_evidence_ids": list(conflict_ids),
                                "supporting_evidence_ids": [],
                            },
                        },
                    ),
                ]
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
                            {"id": "inspect-example", "type": "function", "function": {"name": "inspect_image", "arguments": '{"path":"example.png","question":"Describe visible reviewer and signature fields."}'}},
                        ],
                    }
                },
            )()
        if tools:
            raise AssertionError("primary transport and reviewer rewrites must use tools=[]")
        latest_user = next(
            (str(message.get("content") or "") for message in reversed(messages) if message.get("role") == "user"),
            "",
        )
        if "bounded transport recovery" in latest_user:
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": (
                            "## Current scope\n\n"
                            "- policy.md and prototype.html align on the reviewer/signature field area.\n"
                            "- policy.md:1 says the generated document keeps reviewer/signature blank.\n"
                            "- example.png visibly shows reviewer/signature fields filled.\n"
                            "- policy.md:1 and example.png differ; artifact role, lifecycle, and precedence are not established, so the discrepancy remains unresolved.\n\n"
                            "## Later planned / not current\n\n"
                            "- Automated delivery remains later planned.\n"
                            "- Digital signing remains later planned.\n\n"
                            "Evidence: policy.md:40-41"
                        )
                    }
                },
            )()
        if latest_user.startswith("[Read-only evidence review]"):
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": (
                            "## Current scope\n\n"
                            "- Scoped observation: policy.md and prototype.html both include the reviewer/signature field area.\n"
                            "- policy.md:1 says the generated document keeps reviewer/signature blank.\n"
                            "- example.png visibly shows reviewer/signature fields filled.\n"
                            "- policy.md:1 and example.png differ; artifact role, lifecycle, and precedence are not established, so the discrepancy remains unresolved.\n\n"
                            "## Later planned / not current\n\n"
                            "- Automated delivery remains later planned.\n"
                            "- Digital signing remains later planned.\n\n"
                            "Evidence: policy.md:40-41"
                        )
                    }
                },
            )()
        claim_lines = [f"- policy.md:{index * 10} states requirement rule {index * 10}." for index in range(1, 75)]
        claim_lines.append(
            "- policy.md:1 says reviewer/signature blank; example.png shows reviewer/signature filled; "
            "therefore the image is only an illustrative template and there is no substantive conflict."
        )
        return type("Response", (), {"message": {"content": "\n".join(claim_lines)}})()


class _PostReviewTransportRecoveryDocumentClient(_TransportRecoveryDocumentClient):
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        self._review_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            self._review_calls += 1
            payload = next(
                json.loads(message["content"])
                for message in messages
                if message.get("role") == "user" and "LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content"))
            )
            conflict_ids = self._conflict_ids(payload)
            return _review_tool_calls_response(
                [
                    _finding_call(
                        "scope-finding",
                        "c001",
                        str(payload["candidate_claims"][0]["text"]),
                        issue="the alignment claim is too broad",
                        action="limit it to the observed field area",
                    ),
                    _final_call(
                        "doc-revise",
                        {
                            "verdict": "revise",
                            "confidence": 0.96,
                            "reason": "scope the alignment claim",
                            "document_consistency": {
                                "stance": "reported_unresolved",
                                "conflict_evidence_ids": list(conflict_ids),
                                "supporting_evidence_ids": [],
                            },
                        },
                    ),
                ]
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
                            {"id": "inspect-example", "type": "function", "function": {"name": "inspect_image", "arguments": '{"path":"example.png","question":"Describe visible reviewer and signature fields."}'}},
                        ],
                    }
                },
            )()
        if tools:
            raise AssertionError("document synthesis and rewrites must use tools=[]")
        latest_user = next(
            (str(message.get("content") or "") for message in reversed(messages) if message.get("role") == "user"),
            "",
        )
        if "bounded transport recovery" in latest_user:
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": (
                            "- Scoped observation: policy.md and prototype.html both include the reviewer/signature field area.\n"
                            "- policy.md:1 requires blank fields; example.png visibly shows filled fields.\n"
                            "- The artifact role and priority are not established, so the discrepancy remains unresolved."
                        )
                    }
                },
            )()
        if latest_user.startswith("[Read-only evidence review]"):
            claim_lines = [f"- policy.md:{index * 10} states requirement rule {index * 10}." for index in range(1, 75)]
            claim_lines.extend(
                [
                    "- Scoped observation: policy.md and prototype.html both include the reviewer/signature field area.",
                    "- policy.md:1 requires blank fields; example.png visibly shows filled fields.",
                    "- The artifact role and priority are not established, so the discrepancy remains unresolved.",
                ]
            )
            return type("Response", (), {"message": {"content": "\n".join(claim_lines)}})()
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": (
                        "- policy.md and prototype.html fully align.\n"
                        "- policy.md:1 requires blank fields; example.png visibly shows filled fields.\n"
                        "- The artifact role and priority are not established, so the discrepancy remains unresolved."
                    )
                }
            },
        )()


class _PostRewriteProjectionOverflowClient(_OverlongClaimClient):
    calls: list[dict] = []

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            return _review_tool_calls_response(
                [
                    _finding_call(
                        "overflow-finding",
                        "c001",
                        _candidate_claim(messages, "c001"),
                        action="mark owner unlocated",
                    ),
                    _final_call("overflow-final", {"verdict": "revise", "confidence": 0.9, "reason": "unsupported owner"}),
                ]
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
                            {"id": "read-src", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"src/Candidate.java"}'}},
                        ],
                    }
                },
            )()
        if self._review_calls == 0:
            return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()
        long_claim = "UNSUPPORTED_LONG_OWNER_CLAIM " + ("x" * 620)
        return type("Response", (), {"message": {"content": f"Owner remains unlocated.\n{long_claim}"}})()


class _PostRewriteTransportOmittedClaimClient(_TransportOmittedClaimClient):
    calls: list[dict] = []

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            return _review_tool_calls_response(
                [
                    _finding_call(
                        "transport-finding",
                        "c001",
                        _candidate_claim(messages, "c001"),
                        action="mark owner unlocated",
                    ),
                    _final_call("transport-final", {"verdict": "revise", "confidence": 0.9, "reason": "unsupported owner"}),
                ]
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
                        ],
                    }
                },
            )()
        if self._primary_calls == 2 or self._review_calls == 0:
            return type("Response", (), {"message": {"content": "PayServiceImpl is the verified owner."}})()
        claim_lines = [f"- policy.md:{index * 10} states rule {index * 10}." for index in range(1, 75)]
        claim_lines.append("- policy.md:210 states rule 210; PayServiceImpl is the verified owner.")
        return type("Response", (), {"message": {"content": "\n".join(claim_lines)}})()


class _PackedLocatorRuntimeClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self._primary_calls = 0

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in messages):
            payload = next(
                json.loads(message["content"])
                for message in messages
                if message.get("role") == "user" and "LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content"))
            )
            claims = {claim["claim_id"]: claim["text"] for claim in payload["candidate_claims"]}
            matrix = payload["handoff"]["claim_matrix"]
            late_claim = next((claim_id for claim_id, text in claims.items() if "275-312" in text), "")
            middle_claim = next((claim_id for claim_id, text in claims.items() if "178-252" in text), "")
            has_277 = any(
                item.get("classification") == "requirement_locator"
                and late_claim in item.get("claim_ids", ())
                and "277:" in item.get("summary", "")
                for item in matrix
            )
            has_211 = any(
                item.get("classification") == "requirement_locator"
                and middle_claim in item.get("claim_ids", ())
                and "211:" in item.get("summary", "")
                for item in matrix
            )
            if has_211 and has_277:
                return _review_tool_calls_response(
                    [
                        _final_call(
                            "packed-pass",
                            {
                                "verdict": "pass",
                                "confidence": 0.97,
                                "reason": "packed locator evidence is visible",
                                "document_consistency": {
                                    "stance": "reported_unresolved",
                                    "conflict_evidence_ids": [],
                                    "supporting_evidence_ids": [],
                                },
                            },
                        )
                    ]
                )
            return _review_tool_calls_response(
                [
                    _finding_call(
                        "missing-packed-evidence",
                        late_claim or "c001",
                        "",
                        issue="late cited evidence is not visible",
                        action="do not assert the cited requirement",
                        include_claim=False,
                    ),
                    _final_call(
                        "packed-revise",
                        {
                            "verdict": "revise",
                            "confidence": 0.6,
                            "reason": "missing packed evidence",
                            "document_consistency": {
                                "stance": "reported_unresolved",
                                "conflict_evidence_ids": [],
                                "supporting_evidence_ids": [],
                            },
                        },
                    ),
                ]
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
                            {"id": "read-html", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"prototype.html"}'}},
                        ],
                    }
                },
            )()
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": "\n".join(
                        [
                            "- policy.md#L93-L128 states rule 127.",
                            "- policy.md:178-252 states rule 211 and rule 252.",
                            "- policy.md:275-312 states rule 277.",
                            "- prototype.html#L203 states html rule 203.",
                        ]
                    )
                }
            },
        )()


def _config(workspace: Path, *, provider: str = "openai-compatible", vision_model: str = "") -> AgentConfig:
    return AgentConfig(
        provider=provider,
        api_base_url="https://example.invalid/v1",
        api_key="token",
        model="model",
        workspace=workspace,
        max_steps=0,
        budget_seconds=None,
        approval_mode="yolo",
        vision_model=vision_model,
    )


class ReadOnlyReviewerTests(unittest.TestCase):
    def test_reviewer_timeout_uses_deadline_after_finalization_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
            runtime._run.started_monotonic = 100.0
            runtime._run.deadline_monotonic = 150.0
            with (
                patch.object(runtime._provider_context_phase, "remaining_timeout", return_value=120.0),
                patch("local_agent.runtime_read_only_review.time.monotonic", return_value=120.0),
            ):
                timeout = runtime._read_only_review_phase._review_timeout()

        self.assertEqual(timeout, 20.0)

    def test_reviewer_timeout_allows_normal_provider_latency_without_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
            runtime._run.deadline_monotonic = None
            with patch.object(runtime._provider_context_phase, "remaining_timeout", return_value=120.0):
                timeout = runtime._read_only_review_phase._review_timeout()

        self.assertEqual(timeout, 45.0)

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

    def test_document_consistency_spoofed_source_priority_pass_becomes_safe_partial(self) -> None:
        _SpoofedDocumentPriorityClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "policy.md").write_text("Field must remain blank.\n", encoding="utf-8")
            (workspace / "prototype.html").write_text("<p>Field has a value.</p>\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _SpoofedDocumentPriorityClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("只根据 Markdown 和 HTML 分析资料一致性，不要检查代码；不能自行选择资料优先级。")

        self.assertIn("安全部分交付", answer)
        self.assertIn("policy.md", answer)
        self.assertIn("prototype.html", answer)
        self.assertNotIn("FORGED_PRIORITY", answer)
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
        self.assertEqual(summary["final_submits"], 1)
        self.assertEqual(summary["rewrites"], 1)
        self.assertEqual(summary["rewrite_acceptances"], 1)
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
        self.assertEqual(len(review_calls), 2)

    def test_document_consistency_invalid_finding_is_revoked_and_can_be_resubmitted(self) -> None:
        _DocumentConsistencyInvalidFindingRecoveryClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "policy.md").write_text("Field must remain blank.\n", encoding="utf-8")
            (workspace / "prototype.html").write_text("<p>Field has a value.</p>\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _DocumentConsistencyInvalidFindingRecoveryClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("只根据 Markdown 和 HTML 分析资料一致性，不要检查代码。资料角色未说明时保持未消解。")

        self.assertIn("unresolved", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["finding_submits"], 9)
        self.assertEqual(summary["invalidated_finding_submits"], 1)
        self.assertEqual(summary["rejected_final_submits"], 1)
        self.assertEqual(summary["final_submits"], 1)
        self.assertEqual(summary["rewrites"], 1)
        self.assertEqual(summary["rewrite_acceptances"], 1)
        self.assertEqual(summary["output_lifecycle_exhausted"], 0)
        self.assertEqual(summary["errors"], {})
        final_findings = runtime._run.read_only_review.findings
        self.assertEqual([finding.claim_id for finding in final_findings], [f"c{index:03d}" for index in range(1, 9)])
        self.assertEqual(len([finding for finding in final_findings if finding.claim_id == "c008"]), 1)
        corrected = next(finding for finding in final_findings if finding.claim_id == "c008")
        self.assertNotIn("completed state", corrected.action)
        self.assertNotIn("later", corrected.action.casefold())
        self.assertIn("unresolved", corrected.action)
        review_calls = [
            call
            for call in _DocumentConsistencyInvalidFindingRecoveryClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(len(review_calls), 2)
        self.assertEqual(
            [tool["function"]["name"] for tool in review_calls[1]["tools"]],
            [REVIEWER_FINDING_TOOL_NAME, REVIEWER_OUTPUT_TOOL_NAME],
        )
        rejected_results = [
            message
            for message in review_calls[1]["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == "bad-final"
        ]
        self.assertEqual(len(rejected_results), 1)
        self.assertIn("c008", str(rejected_results[0].get("content")))

    def test_document_consistency_final_submit_recovers_after_key_and_evidence_rejections(self) -> None:
        _DocumentConsistencyFinalSubmitRecoveryClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "policy.md").write_text("Field must remain blank.\n", encoding="utf-8")
            (workspace / "prototype.html").write_text("<p>Field has a value.</p>\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _DocumentConsistencyFinalSubmitRecoveryClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("只根据 Markdown 和 HTML 分析资料一致性，不要检查代码。资料角色未说明时保持未消解。")

        self.assertIn("unresolved", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["finding_submits"], 7)
        self.assertEqual(summary["invalidated_finding_submits"], 0)
        self.assertEqual(summary["rejected_final_submits"], 2)
        self.assertEqual(summary["final_submits"], 1)
        self.assertEqual(summary["rewrites"], 1)
        self.assertEqual(summary["rewrite_acceptances"], 1)
        self.assertEqual(summary["output_lifecycle_exhausted"], 0)
        self.assertEqual([finding.claim_id for finding in runtime._run.read_only_review.findings], [f"c{index:03d}" for index in range(1, 8)])
        review_calls = [
            call
            for call in _DocumentConsistencyFinalSubmitRecoveryClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(len(review_calls), 3)
        bad_key_result = [
            message
            for message in review_calls[1]["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == "bad-keys"
        ]
        self.assertEqual(len(bad_key_result), 1)
        self.assertIn("stance, conflict_evidence_ids, supporting_evidence_ids", str(bad_key_result[0].get("content")))
        one_side_result = [
            message
            for message in review_calls[2]["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == "one-sided"
        ]
        self.assertEqual(len(one_side_result), 1)
        self.assertIn("Accepted findings remain recorded", str(one_side_result[0].get("content")))
        self.assertEqual(
            summary["output_lifecycle_corrections"],
            {"arguments": 0, "document_consistency": 2, "protocol": 0},
        )

    def test_document_consistency_reviewer_separates_argument_and_semantic_corrections(self) -> None:
        _DocumentConsistencyMixedCorrectionRecoveryClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "policy.md").write_text("Field must remain blank.\n", encoding="utf-8")
            (workspace / "prototype.html").write_text("<p>Field has a value.</p>\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _DocumentConsistencyMixedCorrectionRecoveryClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("只根据 Markdown 和 HTML 分析资料一致性，不要检查代码。资料角色未说明时保持未消解。")

        self.assertIn("unresolved", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["finding_submits"], 9)
        self.assertEqual(summary["invalidated_finding_submits"], 1)
        self.assertEqual(summary["rejected_final_submits"], 3)
        self.assertEqual(summary["final_submits"], 1)
        self.assertEqual(summary["rewrites"], 1)
        self.assertEqual(summary["rewrite_acceptances"], 1)
        self.assertEqual(summary["output_lifecycle_exhausted"], 0)
        self.assertEqual(summary["output_lifecycle_exhausted_categories"], {})
        self.assertEqual(
            summary["output_lifecycle_corrections"],
            {"arguments": 2, "document_consistency": 1, "protocol": 0},
        )
        final_findings = runtime._run.read_only_review.findings
        self.assertEqual([finding.claim_id for finding in final_findings], [f"c{index:03d}" for index in range(1, 9)])
        corrected = next(finding for finding in final_findings if finding.claim_id == "c008")
        self.assertNotIn("completed state", corrected.action)
        self.assertIn("unresolved", corrected.action)
        review_calls = [
            call
            for call in _DocumentConsistencyMixedCorrectionRecoveryClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(len(review_calls), 4)
        self.assertEqual(
            [tool["function"]["name"] for tool in review_calls[3]["tools"]],
            [REVIEWER_FINDING_TOOL_NAME, REVIEWER_OUTPUT_TOOL_NAME],
        )

    def test_document_consistency_repeated_bad_finding_remains_bounded_unverified(self) -> None:
        _DocumentConsistencyRepeatedInvalidFindingClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "policy.md").write_text("Field must remain blank.\n", encoding="utf-8")
            (workspace / "prototype.html").write_text("<p>Field has a value.</p>\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _DocumentConsistencyRepeatedInvalidFindingClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("只根据 Markdown 和 HTML 分析资料一致性，不要检查代码。资料角色未说明时保持未消解。")

        self.assertIn("未完成/未验证", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["invalidated_finding_submits"], 3)
        self.assertEqual(summary["rejected_final_submits"], 3)
        self.assertEqual(summary["output_lifecycle_exhausted"], 1)
        self.assertEqual(summary["output_lifecycle_exhausted_categories"], {"document_consistency": 1})
        self.assertEqual(summary["errors"], {"invalid_output": 1})

    def test_document_consistency_rewrite_context_drives_runtime_rewrite_then_second_pass(self) -> None:
        _DocumentConsistencyRewriteContextClient.calls = []
        _DocumentConsistencyRewriteContextClient.rewrite_directives = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "policy.md").write_text("Document A says blank.\n", encoding="utf-8")
            (workspace / "prototype.html").write_text("Image B shows value.\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _DocumentConsistencyRewriteContextClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run(
                    "只根据 policy.md 和 prototype.html 分析资料一致性，不要检查代码。Do not choose source priority or infer lifecycle. "
                    "Keep discrepancies unresolved unless evidence supports reconciliation."
                )

        self.assertIn("discrepancy remains unresolved", answer)
        self.assertNotIn("final state", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["rewrites"], 1)
        self.assertEqual(runtime._run.read_only_review.verdict, "pass")
        self.assertEqual(summary["verdicts"], {"revise": 1})
        self.assertEqual(summary["rewrite_acceptances"], 1)
        self.assertEqual(summary["errors"], {})
        self.assertEqual(runtime._last_run_summary["termination_reason"], "final")
        self.assertEqual(len(_DocumentConsistencyRewriteContextClient.rewrite_directives), 1)
        rewrite_directive = _DocumentConsistencyRewriteContextClient.rewrite_directives[0]
        self.assertIn("Do not choose source priority or infer lifecycle", rewrite_directive)
        self.assertIn("Document A says blank", rewrite_directive)
        self.assertIn("Image B shows value", rewrite_directive)
        self.assertIn("No valid supporting evidence", rewrite_directive)
        self.assertIn("role/lifecycle/precedence is not established", rewrite_directive)
        review_calls = [
            call
            for call in _DocumentConsistencyRewriteContextClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(len(review_calls), 1)

    def test_document_consistency_rewrite_missing_disposition_fails_closed_without_second_reviewer(self) -> None:
        _DocumentConsistencyMissingDispositionClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "policy.md").write_text("Document A says blank.\n", encoding="utf-8")
            (workspace / "prototype.html").write_text("Image B shows value.\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _DocumentConsistencyMissingDispositionClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run(
                    "只根据 policy.md 和 prototype.html 分析资料一致性，不要检查代码。Do not choose source priority or infer lifecycle. "
                    "Keep discrepancies unresolved unless evidence supports reconciliation."
                )

        self.assertIn("安全部分交付", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["rewrites"], 2)
        self.assertEqual(summary["rewrite_acceptances"], 0)
        self.assertEqual(summary["errors"], {"rewrite_noncompliant": 1})
        review_calls = [
            call
            for call in _DocumentConsistencyMissingDispositionClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(len(review_calls), 1)

    def test_document_consistency_rewrite_asserted_reconciliation_fails_closed_without_second_reviewer(self) -> None:
        _DocumentConsistencyAssertedRewriteClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "policy.md").write_text("Document A says blank.\n", encoding="utf-8")
            (workspace / "prototype.html").write_text("Image B shows value.\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _DocumentConsistencyAssertedRewriteClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run(
                    "只根据 policy.md 和 prototype.html 分析资料一致性，不要检查代码。Do not choose source priority or infer lifecycle. "
                    "Keep discrepancies unresolved unless evidence supports reconciliation."
                )

        self.assertIn("安全部分交付", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["rewrites"], 2)
        self.assertEqual(summary["rewrite_acceptances"], 0)
        self.assertEqual(summary["errors"], {"rewrite_noncompliant": 1})
        review_calls = [
            call
            for call in _DocumentConsistencyAssertedRewriteClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(len(review_calls), 1)

    def test_claim_scoped_range_evidence_reaches_runtime_reviewer(self) -> None:
        _ClaimScopedRequirementEvidenceClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            lines = [f"{index}: filler line {index}" for index in range(1, 301)]
            lines[190] = "191: settlement period is monthly"
            lines[194] = "195: company mapping comes from requirement source"
            lines[197] = "198: count and amount mapping comes from requirement source"
            lines[207] = "208: subtotal is the sum of detail amounts"
            lines[208] = "209: footer fields follow the listed mappings"
            lines[211] = "212: grand total equals subtotal plus adjustments"
            (workspace / "policy.md").write_text("\n".join(lines), encoding="utf-8")
            (workspace / "prototype.html").write_text("<table><tr><td>layout only</td></tr></table>\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _ClaimScopedRequirementEvidenceClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("只根据 policy.md 和 prototype.html 分析资料一致性，不要检查代码。")

        self.assertIn("settlement period is monthly", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["verdicts"], {"pass": 1})
        self.assertEqual(summary["errors"], {})
        self.assertEqual(runtime._last_run_summary["termination_reason"], "final")

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

    def test_handoff_reuses_source_evidence_generator_for_locators_and_source_items(self) -> None:
        contract = generate_requirement_contract("只根据 policy.md 分析资料一致性，不要检查代码。")
        source_iter = (
            item
            for item in (
                SourceEvidence(
                    "policy.md",
                    "\n".join(f"{index}: rule {index}" for index in range(1, 220)),
                    root="/workspace",
                ),
            )
        )
        handoff = build_explore_handoff(
            request="只根据 policy.md 分析资料一致性，不要检查代码。",
            contract=contract,
            requirement_evidence=(),
            source_evidence=source_iter,
            records=(),
            tool_results=(),
            candidate="- policy.md:191 states rule 191.",
            claim_units=candidate_claim_units("- policy.md:191 states rule 191."),
        )
        classifications = {item.classification for item in handoff.items}

        self.assertIn("requirement_locator", classifications)
        self.assertIn("observed_candidate", classifications)

    def test_claim_scoped_line_locators_include_repository_source_files(self) -> None:
        contract = generate_requirement_contract(
            "读取后端 Processor.java 和前端 Panel.vue，区分源码事实与设计建议。"
        )
        backend_path = "/workspace/backend/src/Processor.java"
        frontend_path = "/workspace/frontend/src/Panel.vue"
        backend = "\n".join(
            [
                "1: package example;",
                "2: public class Processor {",
                "3:   void process() {}",
                "4: }",
            ]
        )
        frontend = "\n".join(
            [
                "1: <template>",
                "2:   <button @click=\"process\">Run</button>",
                "3: </template>",
            ]
        )
        candidate = (
            f"- 源码事实：{backend_path}:2-3 定义 Processor.process。\n"
            f"- 源码事实：{frontend_path}:1-2 渲染 Run 按钮。\n"
            "- 设计建议：可新增协调接口，此项不是当前实现。"
        )
        units = candidate_claim_units(candidate)
        handoff = build_explore_handoff(
            request="读取后端 Processor.java 和前端 Panel.vue，区分源码事实与设计建议。",
            contract=contract,
            requirement_evidence=(),
            source_evidence=(
                SourceEvidence(backend_path, backend, root="/workspace/backend"),
                SourceEvidence(frontend_path, frontend, root="/workspace/frontend"),
            ),
            records=(),
            tool_results=(),
            candidate=candidate,
            claim_units=units,
        )
        locator_items = [item for item in handoff.items if item.classification == "source_locator"]
        backend_claim = next(unit.claim_id for unit in units if "Processor.process" in unit.text)
        frontend_claim = next(unit.claim_id for unit in units if "Run 按钮" in unit.text)

        self.assertTrue(any("2: public class Processor" in item.summary for item in locator_items))
        self.assertTrue(any(backend_claim in item.claim_ids for item in locator_items))
        self.assertTrue(any('2: <button @click="process">' in item.summary for item in locator_items))
        self.assertTrue(any(frontend_claim in item.claim_ids for item in locator_items))
        self.assertFalse(any(item.classification == "requirement_locator" for item in handoff.items))
        self.assertEqual(handoff.transport_omitted_claim_ids, ())

    def test_source_fact_section_without_claim_locator_requires_transport_rewrite(self) -> None:
        contract = generate_requirement_contract("读取源码并输出证据化技术设计。")
        source_path = "/workspace/backend/src/Processor.java"
        candidate = (
            "## 源码当前事实\n"
            "- Processor currently owns the workflow.\n"
            "## 设计建议\n"
            "- 建议新增协调接口。\n"
        )
        units = candidate_claim_units(candidate)
        handoff = build_explore_handoff(
            request="读取源码并输出证据化技术设计。",
            contract=contract,
            requirement_evidence=(),
            source_evidence=(
                SourceEvidence(source_path, "1: class Processor {}", root="/workspace/backend"),
            ),
            records=(),
            tool_results=(),
            candidate=candidate,
            claim_units=units,
        )
        source_claim_id = next(unit.claim_id for unit in units if "currently owns" in unit.text)
        proposal_claim_id = next(unit.claim_id for unit in units if "建议新增" in unit.text)

        self.assertIn(source_claim_id, handoff.transport_omitted_claim_ids)
        self.assertNotIn(proposal_claim_id, handoff.transport_omitted_claim_ids)

    def test_nested_source_fact_section_retains_parent_context_and_fails_closed(self) -> None:
        contract = generate_requirement_contract("读取源码并输出证据化技术设计。")
        source_path = "/workspace/backend/src/Processor.java"
        candidate = (
            "## 源码当前事实\n"
            "### 后端 Processor\n"
            "- 当前文件超过 14500 行，并实现完整结算能力。\n"
            "## 设计建议\n"
            "### 后端建议\n"
            "- 建议新增协调层。\n"
        )
        units = candidate_claim_units(candidate)
        handoff = build_explore_handoff(
            request="读取源码并输出证据化技术设计。",
            contract=contract,
            requirement_evidence=(),
            source_evidence=(
                SourceEvidence(source_path, "1: class Processor {}", root="/workspace/backend"),
            ),
            records=(),
            tool_results=(),
            candidate=candidate,
            claim_units=units,
        )
        source_claim = next(unit for unit in units if "14500" in unit.text)
        proposal_claim = next(unit for unit in units if "协调层" in unit.text)

        self.assertEqual(source_claim.section_context, "源码当前事实 > 后端 Processor")
        self.assertEqual(proposal_claim.section_context, "设计建议 > 后端建议")
        self.assertIn(source_claim.claim_id, handoff.transport_omitted_claim_ids)
        self.assertNotIn(proposal_claim.claim_id, handoff.transport_omitted_claim_ids)

    def test_derived_count_without_explicit_source_total_requires_transport_rewrite(self) -> None:
        contract = generate_requirement_contract("只根据 policy.md 输出证据化设计。")
        candidate = "- policy.md:1-3 列出 2 项变量。"
        units = candidate_claim_units(candidate)
        handoff = build_explore_handoff(
            request="只根据 policy.md 输出证据化设计。",
            contract=contract,
            requirement_evidence=(
                RequirementEvidence(
                    "policy.md",
                    "1: `{first}` variable\n2: `{second}` variable\n3: `{third}` variable",
                    root="/workspace",
                ),
            ),
            source_evidence=(),
            records=(),
            tool_results=(),
            candidate=candidate,
            claim_units=units,
        )

        self.assertEqual(handoff.transport_omitted_claim_ids, (units[0].claim_id,))

    def test_explicit_source_total_keeps_count_transportable(self) -> None:
        contract = generate_requirement_contract("只根据 policy.md 输出证据化设计。")
        candidate = "- policy.md:1 明确要求 2 项变量。"
        units = candidate_claim_units(candidate)
        handoff = build_explore_handoff(
            request="只根据 policy.md 输出证据化设计。",
            contract=contract,
            requirement_evidence=(
                RequirementEvidence("policy.md", "1: 文档明确要求 2 项变量。", root="/workspace"),
            ),
            source_evidence=(),
            records=(),
            tool_results=(),
            candidate=candidate,
            claim_units=units,
        )

        self.assertEqual(handoff.transport_omitted_claim_ids, ())

    def test_source_fact_section_with_claim_locator_is_transportable(self) -> None:
        contract = generate_requirement_contract("读取源码并输出证据化技术设计。")
        source_path = "/workspace/backend/src/Processor.java"
        candidate = (
            "## Source facts\n"
            f"- {source_path}:2 defines Processor.process.\n"
            "## Design proposal\n"
            "- Add a coordination API.\n"
        )
        units = candidate_claim_units(candidate)
        handoff = build_explore_handoff(
            request="读取源码并输出证据化技术设计。",
            contract=contract,
            requirement_evidence=(),
            source_evidence=(
                SourceEvidence(
                    source_path,
                    "1: class Processor {\n2:   void process() {}\n3: }",
                    root="/workspace/backend",
                ),
            ),
            records=(),
            tool_results=(),
            candidate=candidate,
            claim_units=units,
        )
        source_claim_id = next(unit.claim_id for unit in units if "Processor.process" in unit.text)

        self.assertNotIn(source_claim_id, handoff.transport_omitted_claim_ids)
        self.assertTrue(
            any(
                item.classification == "source_locator" and source_claim_id in item.claim_ids
                for item in handoff.items
            )
        )

        rewrite_message = reviewer_transport_rewrite_message(
            handoff=handoff,
            omitted_claim_ids=(source_claim_id,),
        )
        self.assertIn(source_path, rewrite_message)
        self.assertIn("copy one verbatim", rewrite_message)
        self.assertIn("do not shorten it to a class name or basename", rewrite_message)

    def test_path_bullet_binds_following_line_bullets_to_repository_source(self) -> None:
        contract = generate_requirement_contract("读取源码并输出证据化技术设计。")
        source_path = "/workspace/backend/src/Processor.java"
        content = "\n".join(f"{number}: source line {number}" for number in range(1, 80))
        candidate = (
            "## 源码当前事实\n"
            f"- 路径：`{source_path}`\n"
            "- 证据点：\n"
            "  - 第 25 行：存在 process 方法。\n"
            "  - 第 51–52 行：存在状态检查。\n"
        )
        units = candidate_claim_units(candidate)
        handoff = build_explore_handoff(
            request="读取源码并输出证据化技术设计。",
            contract=contract,
            requirement_evidence=(),
            source_evidence=(SourceEvidence(source_path, content, root="/workspace/backend"),),
            records=(),
            tool_results=(),
            candidate=candidate,
            claim_units=units,
        )
        locator_items = [item for item in handoff.items if item.classification == "source_locator"]
        line_25_claim = next(unit.claim_id for unit in units if "第 25 行" in unit.text)
        line_51_claim = next(unit.claim_id for unit in units if "第 51–52 行" in unit.text)

        self.assertTrue(any("25: source line 25" in item.summary and line_25_claim in item.claim_ids for item in locator_items))
        self.assertTrue(any("51: source line 51" in item.summary and line_51_claim in item.claim_ids for item in locator_items))
        self.assertTrue(any("52: source line 52" in item.summary and line_51_claim in item.claim_ids for item in locator_items))

    def test_reviewer_contract_uses_omp_provable_finding_bar(self) -> None:
        contract = generate_requirement_contract("读取源码并输出证据化技术设计。")
        handoff = build_explore_handoff(
            request="读取源码并输出证据化技术设计。",
            contract=contract,
            requirement_evidence=(),
            source_evidence=(SourceEvidence("src/Processor.java", "1: class Processor {}", root="/workspace"),),
            records=(),
            tool_results=(),
        )
        prompt = reviewer_messages(handoff, candidate_claim_units("源码事实：src/Processor.java:1 定义 Processor。"))[0]["content"]

        self.assertIn("Report a finding only when it is provable", prompt)
        self.assertIn("There is no target finding count", prompt)
        self.assertIn("Requirement facts do not need to be observed in repository source", prompt)
        self.assertIn("A failed or missing path in one root never invalidates a successful read in another root", prompt)
        self.assertIn("every candidate statement presented as a current repository/source fact", prompt)
        self.assertIn("they never substitute for claim-scoped support", prompt)
        self.assertIn("A bare path, symbol name, or assertion that files were read is not evidence", prompt)
        self.assertIn("Check every factual predicate in a repository-source claim", prompt)
        self.assertIn("does not by itself prove an end-to-end capability", prompt)
        self.assertIn("Validate asserted counts against the claim-scoped excerpt", prompt)
        self.assertIn("handler reference proves only that the name is referenced", prompt)
        self.assertIn("A failed guessed path in an additional root is only a scoped inspection failure", prompt)
        self.assertIn("branches, versions, mirrors", prompt)
        self.assertIn("Do not review design taste, completeness, naming preference", prompt)
        self.assertIn("is not itself a candidate-answer defect", prompt)

    def test_transport_rewrite_does_not_promote_cross_root_guesses_or_compound_claims(self) -> None:
        contract = generate_requirement_contract("读取源码并输出证据化技术设计。")
        source_path = "/workspace/backend/src/Processor.java"
        handoff = build_explore_handoff(
            request="读取源码并输出证据化技术设计。",
            contract=contract,
            requirement_evidence=(),
            source_evidence=(
                SourceEvidence(source_path, "1: class Processor {}", root="/workspace/backend"),
            ),
            records=(),
            tool_results=(),
        )

        claim_units = candidate_claim_units(
            "## 源码当前事实\n"
            "1. **后端 Processor.java**\n"
            "- 当前没有找到任何结算实现。\n"
        )
        message = reviewer_transport_rewrite_message(
            handoff=handoff,
            omitted_claim_ids=tuple(unit.claim_id for unit in claim_units),
            claim_units=claim_units,
        )

        self.assertIn("Treat failed guessed paths in other roots only as scoped inspection failures", message)
        self.assertIn("branches, versions, mirrors", message)
        self.assertIn("For a compound source claim, each factual predicate must be supported", message)
        self.assertIn("do not by themselves prove an end-to-end capability", message)
        self.assertIn("Recount asserted totals from the cited excerpt", message)
        self.assertIn("handler reference is not a method or function definition", message)
        self.assertIn("Do not state a derived item/variable/field/method count", message)
        self.assertIn("Exact candidate claims that failed transport", message)
        self.assertIn("后端 Processor.java", message)
        self.assertIn("当前没有找到任何结算实现", message)
        self.assertIn("converting a presentation-only label into a Markdown heading", message)
        self.assertIn("source facts only as short bullets, never as tables or blockquotes", message)
        self.assertIn("one supported factual predicate and its locator in the same text", message)
        self.assertIn("Never use `+`", message)
        self.assertIn("Delete global `未找到` / `未发现`", message)

    def test_rewrite_treats_reviewer_action_as_advisory_not_source_evidence(self) -> None:
        result = ReviewerResult(
            verdict="revise",
            confidence=0.9,
            findings=(
                ReviewerFinding(
                    "c001",
                    "the proposal needs a rollback state",
                    "add ROLLBACK and GenerateReqNoUtils",
                    "建议新增回退流程。",
                    "candidate_defect",
                ),
            ),
            reason="revise",
        )

        message = reviewer_rewrite_message(result, profile="design")

        self.assertIn("Reviewer issue/action text is advisory, not evidence", message)
        self.assertIn("Prefer deleting or narrowly downgrading", message)
        self.assertIn("Never copy a reviewer-suggested class, field, state, service", message)

    def test_path_bound_line_locator_variants_parse_full_ranges(self) -> None:
        variants = (
            ("policy.md:93-128", "93-128"),
            ("policy.md#L93-L128", "93-L128"),
            ("policy.md:#L93-L128", "93-L128"),
            ("policy.md:93–128", "93–128"),
            ("policy.md#L93–L128", "93–L128"),
        )
        for text, expected in variants:
            with self.subTest(text=text):
                locators = parse_document_locators(text, "policy.md")
                self.assertEqual([(locator.kind, locator.value) for locator in locators], [("line", expected)])

    def test_tool_content_tag_line_range_is_a_path_bound_locator(self) -> None:
        content = (
            "源码事实（[PrepareOrderApplication.java#5007b19a], L90-104）："
            "cancelPrepareOrder performs a guarded status transition."
        )

        locators = parse_document_locators(content, "/workspace/backend/PrepareOrderApplication.java")

        self.assertEqual(
            [(locator.path, locator.kind, locator.value) for locator in locators],
            [("PrepareOrderApplication.java", "line", "90-104")],
        )

    def test_claim_scoped_locator_items_inherit_heading_range_and_serialize_claim_id(self) -> None:
        contract = generate_requirement_contract(
            "只根据 Markdown 和 HTML 分析资料一致性，不要检查代码。"
        )
        lines = [f"{index}: filler line {index}" for index in range(1, 301)]
        lines[190] = "191: settlement period is monthly"
        lines[194] = "195: company mapping is explicit"
        lines[197] = "198: amount mapping is explicit"
        lines[207] = "208: subtotal is explicit"
        lines[208] = "209: footer mapping is explicit"
        lines[211] = "212: grand total is explicit"
        candidate = (
            "### 2. Word requirements (policy.md:191-212；example.png)\n"
            "\n"
            "| item | fact |\n"
            "| --- | --- |\n"
            "| period | settlement period is monthly |\n"
            "| total | subtotal and grand total are explicit |\n"
            "\n"
            "### 3. Other section\n"
            "| item | fact |\n"
            "| --- | --- |\n"
            "| other | this row must not inherit the policy range |\n"
        )
        units = candidate_claim_units(candidate)
        handoff = build_explore_handoff(
            request="只根据 Markdown 和 HTML 分析资料一致性，不要检查代码。",
            contract=contract,
            requirement_evidence=(),
            source_evidence=(),
            records=(),
            tool_results=(
                ToolResultSummary(
                    "read_file",
                    "\n".join(lines),
                    path="policy.md",
                    metadata={"evidence_root": "/workspace", "resolved_path": "/workspace/policy.md"},
                ),
            ),
            candidate=candidate,
            claim_units=units,
        )
        locator_items = [item for item in handoff.items if item.classification == "requirement_locator"]

        self.assertTrue(locator_items)
        self.assertTrue(all(item.claim_ids for item in locator_items))
        range_items = [item for item in locator_items if "line 191-212" in item.summary]
        self.assertEqual(len(range_items), 1)
        self.assertGreaterEqual(len(range_items[0].claim_ids), 2)
        self.assertTrue(any("195:" in item.summary and "198:" in item.summary for item in locator_items))
        self.assertTrue(any("208:" in item.summary and "209:" in item.summary for item in locator_items))
        self.assertTrue(any("212:" in item.summary for item in locator_items))
        self.assertFalse(any("other | this row" in item.summary for item in locator_items))
        inherited_claim_texts = {
            unit.claim_id: unit.text
            for unit in units
            if unit.locator_context and "policy.md:191-212" in unit.locator_context
        }
        self.assertTrue(any("settlement period" in text for text in inherited_claim_texts.values()))
        self.assertTrue(any("subtotal and grand total" in text for text in inherited_claim_texts.values()))
        self.assertFalse(any("must not inherit" in text for text in inherited_claim_texts.values()))
        self.assertFalse(any(item.summary.endswith("...") for item in locator_items))
        serialized = handoff.to_dict()["claim_matrix"]
        serialized_locators = [item for item in serialized if item["classification"] == "requirement_locator"]
        self.assertTrue(any(item.get("claim_ids") for item in serialized_locators))
        self.assertFalse(any("claim_id" in item and item.get("claim_ids") for item in serialized_locators))

    def test_claim_units_keep_section_context_and_bind_trailing_shared_locator(self) -> None:
        contract = generate_requirement_contract("只根据 policy.md 分析资料一致性，不要检查代码。")
        lines = [f"{index}: filler line {index}" for index in range(1, 80)]
        lines[44] = "45: later planned item A"
        lines[45] = "46: later planned item B"
        candidate = (
            "## Later planned / not current\n\n"
            "This intro paragraph explains the section but is not part of the cited bullet group.\n\n"
            "- Item A is later planned.\n"
            "- Item B is not current.\n\n"
            "Evidence: policy.md:45-46\n\n"
            "- After citation item should not inherit the shared citation.\n\n"
            "## Current scope\n\n"
            "- Current item must not inherit the later citation.\n"
        )
        units = candidate_claim_units(candidate)
        later_units = [unit for unit in units if "Item" in unit.text]

        self.assertTrue(later_units)
        self.assertTrue(all(unit.section_context == "Later planned / not current" for unit in later_units[:2]))
        self.assertTrue(all("policy.md:45-46" in unit.locator_context for unit in later_units[:2]))
        self.assertFalse(next(unit.locator_context for unit in units if "intro paragraph" in unit.text))
        self.assertFalse(next(unit.locator_context for unit in units if "After citation item" in unit.text))
        self.assertEqual(next(unit.section_context for unit in units if "Current item" in unit.text), "Current scope")
        self.assertFalse(any("policy.md:45-46" in unit.locator_context for unit in units if "Current item" in unit.text))
        serialized_claims = [unit.to_dict() for unit in units]
        self.assertTrue(any(item.get("section_context") == "Later planned / not current" for item in serialized_claims))

        handoff = build_explore_handoff(
            request="只根据 Markdown 分析资料。",
            contract=contract,
            requirement_evidence=(),
            source_evidence=(),
            records=(),
            tool_results=(
                ToolResultSummary(
                    "read_file",
                    "\n".join(lines),
                    path="policy.md",
                    metadata={"evidence_root": "/workspace", "resolved_path": "/workspace/policy.md"},
                ),
            ),
            candidate=candidate,
            claim_units=units,
        )
        locator_items = [item for item in handoff.items if item.classification == "requirement_locator"]
        shared = [item for item in locator_items if "line 45-46" in item.summary]
        self.assertEqual(len(shared), 1)
        self.assertGreaterEqual(len(shared[0].claim_ids), 2)
        current_id = next(unit.claim_id for unit in units if "Current item" in unit.text)
        self.assertNotIn(current_id, shared[0].claim_ids)

    def test_claim_units_do_not_split_filenames_decimals_or_table_headers(self) -> None:
        candidate = (
            "prototype.html only supplies layout. Version 2.1 remains draft.\n\n"
            "| category | requirement fact |\n"
            "| --- | --- |\n"
            "| period | settlement period is monthly |\n"
            "---\n"
            "1. First ordered item remains whole.\n"
            "2. Second ordered item remains whole.\n"
        )
        texts = [unit.text for unit in candidate_claim_units(candidate)]

        self.assertIn("prototype.html only supplies layout.", texts)
        self.assertIn("Version 2.1 remains draft.", texts)
        self.assertNotIn("prototype.", texts)
        self.assertNotIn("html only supplies layout.", texts)
        self.assertNotIn("| category | requirement fact |", texts)
        self.assertIn("| period | settlement period is monthly |", texts)
        self.assertIn("1. First ordered item remains whole.", texts)
        self.assertIn("2. Second ordered item remains whole.", texts)
        self.assertNotIn("1.", texts)
        self.assertNotIn("---", texts)

    def test_claim_units_skip_presentation_only_list_container_labels(self) -> None:
        candidate = (
            "2. **Feature**：\n"
            "   - child requirement remains addressable.\n"
            "- **Observed image** shows:\n"
            "  - image child observation remains addressable.\n"
            "- 状态：未消解\n"
            "- Owner: unlocated candidate.\n"
        )
        texts = [unit.text for unit in candidate_claim_units(candidate)]

        self.assertNotIn("2. **Feature**：", texts)
        self.assertNotIn("- **Observed image** shows:", texts)
        self.assertIn("- child requirement remains addressable.", texts)
        self.assertIn("- image child observation remains addressable.", texts)
        self.assertIn("- 状态：未消解", texts)
        self.assertIn("- Owner: unlocated candidate.", texts)

    def test_overlong_factual_units_are_projection_issues_not_silent_drops(self) -> None:
        long_sentence = "Unsupported owner fact " + ("x" * 620)
        long_cell = "| Owner | " + ("y" * 620) + " |"

        prose_issues = candidate_claim_projection_issues(f"Normal scoped fact.\n{long_sentence}")
        table_issues = candidate_claim_projection_issues(f"| owner | fact |\n| --- | --- |\n{long_cell}")

        self.assertTrue(any(issue.code == "candidate_claim_projection_overflow" for issue in prose_issues))
        self.assertTrue(any(issue.code == "candidate_claim_projection_overflow" for issue in table_issues))
        self.assertIn("Normal scoped fact.", [unit.text for unit in candidate_claim_units(f"Normal scoped fact.\n{long_sentence}")])
        self.assertFalse(any("Unsupported owner fact" in unit.text for unit in candidate_claim_units(f"Normal scoped fact.\n{long_sentence}")))

    def test_runtime_does_not_release_candidate_with_silent_overlong_claim(self) -> None:
        _OverlongClaimClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "src").mkdir()
            (workspace / "src" / "Candidate.java").write_text("class Candidate {}\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _OverlongClaimClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner、调用链和影响范围，不要修改文件。")

        self.assertNotIn("UNSUPPORTED_LONG_OWNER_CLAIM", answer)
        self.assertEqual(runtime._last_run_summary["termination_reason"], "read_only_reviewer_unverified")
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["errors"], {"invalid_output": 1})
        review_calls = [
            call
            for call in _OverlongClaimClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(review_calls, [])

    def test_runtime_fails_closed_when_transport_rewrite_still_omits_claim_evidence(self) -> None:
        _TransportOmittedClaimClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "policy.md").write_text(
                "\n".join(f"{index}: rule {index}" for index in range(1, 900)),
                encoding="utf-8",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _TransportOmittedClaimClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner、调用链和影响范围，不要修改文件。")

        self.assertNotIn("PayServiceImpl is the verified owner", answer)
        self.assertEqual(runtime._last_run_summary["termination_reason"], "read_only_reviewer_unverified")
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(
            summary["errors"],
            {"claim_evidence_transport_incomplete": 1},
        )
        self.assertEqual(summary["claim_transport_rewrites"], 1)
        self.assertEqual(summary["claim_transport_rewrite_acceptances"], 0)
        self.assertEqual(summary["claim_transport_rewrite_exhausted"], 1)
        review_calls = [
            call
            for call in _TransportOmittedClaimClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(review_calls, [])
        transport_rewrite_calls = [
            call
            for call in _TransportOmittedClaimClient.calls
            if any("bounded transport recovery" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(len(transport_rewrite_calls), 1)

    def test_transport_omission_without_finalization_budget_keeps_safe_partial(self) -> None:
        _TransportOmittedClaimClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            with patch("local_agent.agent.OpenAICompatibleClient", _TransportOmittedClaimClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                contract = generate_requirement_contract("只读分析当前服务 owner、调用链和影响范围，不要修改文件。")
                started = time.monotonic()
                runtime._run.begin(
                    run_id="transport-budget",
                    started_monotonic=started,
                    deadline_monotonic=None,
                    run_start_index=0,
                    git_baseline={},
                    prompt="只读分析当前服务 owner、调用链和影响范围，不要修改文件。",
                    requirement_contract=contract,
                    requirement_contract_context="",
                    design_evidence_roots=(),
                )
                runtime._run.collector.start(
                    "transport-budget",
                    "只读分析当前服务 owner、调用链和影响范围，不要修改文件。",
                    started,
                    guard_start={},
                    steer_start={},
                )
                runtime._run.tool_choice_results.append(
                    ToolResultSummary(
                        "read_file",
                        "\n".join(f"{index}: rule {index}" for index in range(1, 900)),
                        path="policy.md",
                        metadata={"resolved_path": str(workspace / "policy.md"), "evidence_root": str(workspace)},
                    )
                )
                runtime._run.finalization.aggregate_attempts = MAX_FINALIZATION_ATTEMPTS
                candidate = "\n".join(
                    [f"- policy.md:{index * 10} states rule {index * 10}." for index in range(1, 75)]
                    + ["- policy.md:210 states rule 210; PayServiceImpl is the verified owner."]
                )
                outcome = runtime._read_only_review_phase.review_candidate(candidate)

        self.assertEqual(outcome.kind, "unverified")
        self.assertIn("安全部分交付", outcome.terminal_message)
        self.assertIn("policy.md", outcome.terminal_message)
        self.assertIn("rule 1", outcome.terminal_message)
        self.assertNotIn("PayServiceImpl is the verified owner", outcome.terminal_message)
        summary = runtime._run.read_only_review.snapshot()
        self.assertFalse(summary["transport_rewrite_requested"])
        self.assertFalse(summary["transport_rewrite_accepted"])
        self.assertTrue(summary["transport_rewrite_exhausted"])
        run_summary = runtime._run.collector.finish("read_only_reviewer_unverified", guard_values={}, steering_values={})
        reviewer_summary = run_summary["read_only_reviewer"]
        self.assertEqual(reviewer_summary["claim_transport_rewrites"], 0)
        self.assertEqual(reviewer_summary["claim_transport_rewrite_acceptances"], 0)
        self.assertEqual(reviewer_summary["claim_transport_rewrite_exhausted"], 1)
        self.assertEqual(_TransportOmittedClaimClient.calls, [])

    def test_transport_omitted_claims_get_one_bounded_compact_rewrite_before_review(self) -> None:
        _TransportRecoveryDocumentClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "policy.md").write_text(
                "Reviewer and signature are blank in this phase.\n"
                + "\n".join(f"{index}: requirement rule {index}" for index in range(2, 900)),
                encoding="utf-8",
            )
            (workspace / "prototype.html").write_text(
                "Reviewer and signature are visibly filled in the example artifact.\n",
                encoding="utf-8",
            )
            (workspace / "example.png").write_bytes(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01"
                b"\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _TransportRecoveryDocumentClient):
                runtime = AgentRuntime(_config(workspace, vision_model="vision"), show_tool_logs=False)
                answer = runtime.run(
                    "只读分析 policy.md、prototype.html 和示例图片，说明本期范围、关键规则，以及资料之间是否有冲突。"
                    "每项结论给出证据；证据不足不要推测。"
                )
                steering_events = runtime._session.load_event_payloads("runtime_steering")

        self.assertIn("discrepancy remains unresolved", answer)
        self.assertNotIn("illustrative template", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(runtime._last_run_summary["termination_reason"], "final")
        self.assertEqual(summary["claim_transport_rewrites"], 1)
        self.assertEqual(summary["claim_transport_rewrite_acceptances"], 1)
        self.assertEqual(summary["claim_transport_rewrite_exhausted"], 0)
        self.assertEqual(summary["triggers"], 1)
        self.assertEqual(summary["verdicts"], {"pass": 1})
        self.assertEqual(summary["rewrites"], 0)
        self.assertEqual(summary["rewrite_acceptances"], 0)
        self.assertEqual(runtime._last_run_summary["finalization_attempts"], 2)
        synthesis_events = [event for event in steering_events if event.get("rule_id") == "document_artifacts_synthesis"]
        self.assertEqual(len(synthesis_events), 1)
        rewrite_messages = [
            str(message.get("content") or "")
            for call in _TransportRecoveryDocumentClient.calls
            for message in call["messages"]
            if "bounded transport recovery" in str(message.get("content"))
        ]
        self.assertEqual(len(rewrite_messages), 1)
        self.assertIn("merge repeated table rows", rewrite_messages[0])
        self.assertIn("discrepancy remains unresolved", rewrite_messages[0])

    def test_document_transport_and_reviewer_rewrite_use_one_queue_directive(self) -> None:
        _TransportReviewerRewriteDocumentClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "policy.md").write_text(
                "Reviewer and signature are blank in this phase.\n"
                + "\n".join(f"{index}: requirement rule {index}" for index in range(2, 900)),
                encoding="utf-8",
            )
            (workspace / "prototype.html").write_text(
                "Reviewer and signature use the corresponding field area.\n",
                encoding="utf-8",
            )
            (workspace / "example.png").write_bytes(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01"
                b"\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _TransportReviewerRewriteDocumentClient):
                runtime = AgentRuntime(_config(workspace, vision_model="vision"), show_tool_logs=False)
                answer = runtime.run(
                    "只读分析 policy.md、prototype.html 和示例图片，说明本期范围、后期规划、关键规则，以及资料之间是否有冲突。"
                    "每项结论给出证据；证据不足不要推测。"
                )
                steering_events = runtime._session.load_event_payloads("runtime_steering")

        self.assertEqual(runtime._last_run_summary["termination_reason"], "final")
        self.assertEqual(runtime._last_run_summary["finalization_attempts"], 3)
        self.assertIn("Scoped observation", answer)
        self.assertIn("Later planned / not current", answer)
        self.assertIn("discrepancy remains unresolved", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["claim_transport_rewrites"], 1)
        self.assertEqual(summary["claim_transport_rewrite_acceptances"], 1)
        self.assertEqual(summary["triggers"], 1)
        self.assertEqual(summary["rewrites"], 1)
        self.assertEqual(summary["rewrite_acceptances"], 1)
        synthesis_events = [event for event in steering_events if event.get("rule_id") == "document_artifacts_synthesis"]
        self.assertEqual(len(synthesis_events), 1)
        primary_calls = [
            call
            for call in _TransportReviewerRewriteDocumentClient.calls
            if not any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertGreaterEqual(len(primary_calls), 3)
        self.assertTrue(all(call["tools"] == [] for call in primary_calls[1:]))

    def test_post_review_transport_omission_gets_one_bounded_compact_rewrite(self) -> None:
        _PostReviewTransportRecoveryDocumentClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "policy.md").write_text(
                "Reviewer and signature are blank in this phase.\n"
                + "\n".join(f"{index}: requirement rule {index}" for index in range(2, 900)),
                encoding="utf-8",
            )
            (workspace / "prototype.html").write_text(
                "Reviewer and signature use the corresponding field area.\n",
                encoding="utf-8",
            )
            (workspace / "example.png").write_bytes(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01"
                b"\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _PostReviewTransportRecoveryDocumentClient):
                runtime = AgentRuntime(_config(workspace, vision_model="vision"), show_tool_logs=False)
                answer = runtime.run(
                    "只读分析 policy.md、prototype.html 和示例图片，说明本期范围、关键规则，以及资料之间是否有冲突。"
                    "每项结论给出证据；证据不足不要推测。"
                )

        self.assertEqual(runtime._last_run_summary["termination_reason"], "final")
        self.assertIn("Scoped observation", answer)
        self.assertIn("discrepancy remains unresolved", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["triggers"], 1)
        self.assertEqual(summary["rewrites"], 1)
        self.assertEqual(summary["rewrite_acceptances"], 1)
        self.assertEqual(summary["claim_transport_rewrites"], 1)
        self.assertEqual(summary["claim_transport_rewrite_acceptances"], 1)
        self.assertEqual(summary["claim_transport_rewrite_exhausted"], 0)
        transport_rewrite_calls = [
            call
            for call in _PostReviewTransportRecoveryDocumentClient.calls
            if any("bounded transport recovery" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(len(transport_rewrite_calls), 1)

    def test_post_rewrite_projection_overflow_fails_closed_without_second_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
            contract = generate_requirement_contract("只读分析当前服务 owner、调用链和影响范围，不要修改文件。")
            now = time.monotonic()
            runtime._run.begin(
                run_id="rewrite-projection-overflow",
                started_monotonic=now,
                deadline_monotonic=now + 120,
                run_start_index=0,
                git_baseline={},
                prompt="只读分析当前服务 owner、调用链和影响范围，不要修改文件。",
                requirement_contract=contract,
                requirement_contract_context="",
                design_evidence_roots=(),
            )
            state = runtime._run.read_only_review
            state.attempted = True
            state.rewrite_requested = True
            state.claim_units = candidate_claim_units("PayServiceImpl is the verified owner.")
            state.findings = (
                ReviewerFinding("c001", "unsupported", "mark owner unlocated", "PayServiceImpl is the verified owner.", "candidate_defect"),
            )
            runtime._run.collector.record_read_only_review_rewrite()
            outcome = runtime._read_only_review_phase.review_candidate(
                "Owner remains unlocated.\nUNSUPPORTED_LONG_OWNER_CLAIM " + ("x" * 620)
            )

        self.assertEqual(outcome.kind, "unverified")
        self.assertIn("invalid_output", outcome.terminal_message)
        self.assertFalse(runtime._run.read_only_review.rewrite_accepted)
        self.assertTrue(runtime._run.read_only_review.rewrite_requested)

    def test_post_rewrite_transport_omission_fails_closed_after_transport_rewrite_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
            contract = generate_requirement_contract("只读分析当前服务 owner、调用链和影响范围，不要修改文件。")
            now = time.monotonic()
            runtime._run.begin(
                run_id="rewrite-transport-omitted",
                started_monotonic=now,
                deadline_monotonic=now + 120,
                run_start_index=0,
                git_baseline={},
                prompt="只读分析当前服务 owner、调用链和影响范围，不要修改文件。",
                requirement_contract=contract,
                requirement_contract_context="",
                design_evidence_roots=(),
            )
            runtime._run.evidence.source_evidence.append(
                SourceEvidence(
                    "policy.md",
                    "\n".join(f"{index}: rule {index}" for index in range(1, 900)),
                    root=str(workspace),
                    scope="root_local",
                )
            )
            state = runtime._run.read_only_review
            state.attempted = True
            state.rewrite_requested = True
            state.transport_rewrite_accepted = True
            state.claim_units = candidate_claim_units("PayServiceImpl is the verified owner.")
            state.findings = (
                ReviewerFinding("c001", "unsupported", "mark owner unlocated", "PayServiceImpl is the verified owner.", "candidate_defect"),
            )
            runtime._run.collector.record_read_only_review_rewrite()
            claim_lines = [f"- policy.md:{index * 10} states rule {index * 10}." for index in range(1, 75)]
            claim_lines.append("- policy.md:210 states rule 210; PayServiceImpl is the verified owner.")
            outcome = runtime._read_only_review_phase.review_candidate("\n".join(claim_lines))

        self.assertEqual(outcome.kind, "unverified")
        self.assertIn("claim_evidence_transport_incomplete", outcome.terminal_message)
        self.assertFalse(runtime._run.read_only_review.rewrite_accepted)
        self.assertTrue(runtime._run.read_only_review.rewrite_requested)

    def test_claim_scoped_locator_selection_exceeds_six_and_ignores_unread_citations(self) -> None:
        contract = generate_requirement_contract(
            "只根据 Markdown 和 HTML 分析资料一致性，不要检查代码。"
        )
        content = "\n".join(f"{index}: rule {index}" for index in range(1, 900))
        candidate = "\n".join(
            f"- policy.md:{index * 10} states rule {index * 10}."
            for index in range(1, 76)
        ) + "\n- missing.md:10 states an unsupported rule."
        units = candidate_claim_units(candidate)
        handoff = build_explore_handoff(
            request="只根据 Markdown 和 HTML 分析资料一致性，不要检查代码。",
            contract=contract,
            requirement_evidence=(RequirementEvidence("policy.md", content, root="/workspace"),),
            source_evidence=(),
            records=(),
            tool_results=(),
            candidate=candidate,
            claim_units=units,
        )
        locator_items = [item for item in handoff.items if item.classification == "requirement_locator"]
        covered_claims = {claim_id for item in locator_items for claim_id in item.claim_ids}
        missing_claim = next(unit.claim_id for unit in units if "missing.md" in unit.text)
        omitted = set(handoff.transport_omitted_claim_ids)

        self.assertGreater(len(locator_items), 6)
        self.assertLessEqual(len(locator_items), 16)
        self.assertGreater(len(units), 70)
        self.assertGreaterEqual(len(covered_claims), 16)
        self.assertTrue(omitted)
        self.assertTrue(omitted.isdisjoint(covered_claims))
        self.assertNotIn(missing_claim, omitted)
        self.assertFalse(any("missing.md" in item.summary for item in locator_items))

    def test_live_shaped_overlapping_ranges_pack_into_bounded_claim_scoped_windows(self) -> None:
        contract = generate_requirement_contract(
            "只根据 Markdown 和 HTML 分析资料一致性，不要检查代码。"
        )
        md_content = "\n".join(f"{index}: markdown rule {index}" for index in range(1, 340))
        html_content = "\n".join(f"{index}: html rule {index}" for index in range(1, 230))
        candidate = "\n".join(
            [
                "- policy.md:56-62 states markdown rule 56.",
                "- policy.md:74 states markdown rule 74.",
                "- policy.md:86-90 states markdown rule 86.",
                "- policy.md#L93-L128 states markdown rule 127.",
                "- policy.md:93-218 states aggregate page rules.",
                "- policy.md:94-95 states markdown rule 95.",
                "- policy.md:96-106 states markdown rule 106.",
                "- policy.md:107-126 states markdown rule 126.",
                "- policy.md:129-176 states markdown rule 176.",
                "- policy.md:178-252 states markdown rule 252.",
                "- policy.md:210-212 states markdown rule 211.",
                "- policy.md:216-246 states markdown rule 246.",
                "- policy.md:275-312 states markdown rule 277.",
                "- prototype.html#L203 states html rule 203.",
            ]
        )
        units = candidate_claim_units(candidate)
        handoff = build_explore_handoff(
            request="只根据 Markdown 和 HTML 分析资料一致性，不要检查代码。",
            contract=contract,
            requirement_evidence=(RequirementEvidence("policy.md", md_content, root="/workspace"),),
            source_evidence=(
                SourceEvidence("prototype.html", html_content, root="/workspace"),
            ),
            records=(),
            tool_results=(),
            candidate=candidate,
            claim_units=units,
        )
        locator_items = [item for item in handoff.items if item.classification == "requirement_locator"]
        aggregate_claim = next(unit.claim_id for unit in units if "93-218" in unit.text)
        late_claim = next(unit.claim_id for unit in units if "275-312" in unit.text)
        html_claim = next(unit.claim_id for unit in units if "prototype.html" in unit.text)

        self.assertLessEqual(len(locator_items), 16)
        self.assertLessEqual(sum(len(item.summary) for item in locator_items), 12000)
        self.assertEqual(handoff.transport_omitted_claim_ids, ())
        self.assertTrue(any("93:" in item.summary and "128:" in item.summary for item in locator_items))
        self.assertTrue(any("129:" in item.summary for item in locator_items))
        self.assertTrue(any("176:" in item.summary for item in locator_items))
        self.assertTrue(any("178:" in item.summary for item in locator_items))
        self.assertTrue(any("252:" in item.summary for item in locator_items))
        self.assertTrue(any("93:" in item.summary and aggregate_claim in item.claim_ids for item in locator_items))
        self.assertTrue(any("133:" in item.summary and aggregate_claim in item.claim_ids for item in locator_items))
        self.assertTrue(any("173:" in item.summary and aggregate_claim in item.claim_ids for item in locator_items))
        self.assertTrue(any("218:" in item.summary and aggregate_claim in item.claim_ids for item in locator_items))
        self.assertTrue(any("277:" in item.summary and late_claim in item.claim_ids for item in locator_items))
        self.assertTrue(any("203:" in item.summary and html_claim in item.claim_ids for item in locator_items))

    def test_locator_source_span_is_derived_from_window_budget(self) -> None:
        contract = generate_requirement_contract(
            "只根据 Markdown 分析资料一致性，不要检查代码。"
        )
        content = "\n".join(f"{index}: rule {index}" for index in range(1, 220))

        allowed_units = candidate_claim_units("- policy.md:1-160 states the bounded rule.")
        allowed = build_explore_handoff(
            request="只根据 Markdown 分析资料一致性，不要检查代码。",
            contract=contract,
            requirement_evidence=(RequirementEvidence("policy.md", content, root="/workspace"),),
            source_evidence=(),
            records=(),
            tool_results=(),
            candidate="- policy.md:1-160 states the bounded rule.",
            claim_units=allowed_units,
        )
        allowed_items = [item for item in allowed.items if item.classification == "requirement_locator"]
        self.assertEqual(allowed.transport_omitted_claim_ids, ())
        self.assertEqual(len(allowed_items), 4)
        self.assertTrue(any("1:" in item.summary for item in allowed_items))
        self.assertTrue(any("160:" in item.summary for item in allowed_items))

        too_wide_units = candidate_claim_units("- policy.md:1-161 states an over-wide rule.")
        too_wide = build_explore_handoff(
            request="只根据 Markdown 分析资料一致性，不要检查代码。",
            contract=contract,
            requirement_evidence=(RequirementEvidence("policy.md", content, root="/workspace"),),
            source_evidence=(),
            records=(),
            tool_results=(),
            candidate="- policy.md:1-161 states an over-wide rule.",
            claim_units=too_wide_units,
        )
        self.assertEqual(
            [item for item in too_wide.items if item.classification == "requirement_locator"],
            [],
        )
        self.assertEqual(too_wide.transport_omitted_claim_ids, ("c001",))

    def test_runtime_reviewer_receives_late_packed_locator_evidence(self) -> None:
        _PackedLocatorRuntimeClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "policy.md").write_text(
                "\n".join(f"{index}: markdown rule {index}" for index in range(1, 340)),
                encoding="utf-8",
            )
            (workspace / "prototype.html").write_text(
                "\n".join(f"{index}: html rule {index}" for index in range(1, 230)),
                encoding="utf-8",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _PackedLocatorRuntimeClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("只根据 policy.md 和 prototype.html 分析资料一致性，不要检查代码。")

        self.assertIn("policy.md:275-312", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["verdicts"], {"pass": 1})
        self.assertEqual(summary["attempts"], 1)
        self.assertEqual(summary["errors"], {})
        self.assertEqual(runtime._last_run_summary["termination_reason"], "final")

    def test_multi_window_range_requires_every_window_to_transport(self) -> None:
        contract = generate_requirement_contract(
            "只根据 Markdown 分析资料一致性，不要检查代码。"
        )
        content = "\n".join(f"{index}: rule {index}" for index in range(1, 1120))
        filler_claims = "\n".join(f"- policy.md:{10 + index * 50} states rule {10 + index * 50}." for index in range(15))
        candidate = filler_claims + "\n- policy.md:1000-1073 states a multi-window requirement."
        units = candidate_claim_units(candidate)
        handoff = build_explore_handoff(
            request="只根据 Markdown 分析资料一致性，不要检查代码。",
            contract=contract,
            requirement_evidence=(RequirementEvidence("policy.md", content, root="/workspace"),),
            source_evidence=(),
            records=(),
            tool_results=(),
            candidate=candidate,
            claim_units=units,
        )
        target_claim = next(unit.claim_id for unit in units if "1000-1073" in unit.text)
        target_items = [
            item
            for item in handoff.items
            if item.classification == "requirement_locator" and target_claim in item.claim_ids
        ]

        self.assertTrue(target_items)
        self.assertIn(target_claim, handoff.transport_omitted_claim_ids)

    def test_long_window_splits_at_complete_lines_and_keeps_middle_citation_visible(self) -> None:
        contract = generate_requirement_contract(
            "只根据 Markdown 分析资料一致性，不要检查代码。"
        )
        lines = [
            f"{index}: {'x' * 150} rule {index}"
            for index in range(1, 45)
        ]
        candidate = "- policy.md:1-40 states rule 25."
        units = candidate_claim_units(candidate)
        handoff = build_explore_handoff(
            request="只根据 Markdown 分析资料一致性，不要检查代码。",
            contract=contract,
            requirement_evidence=(RequirementEvidence("policy.md", "\n".join(lines), root="/workspace"),),
            source_evidence=(),
            records=(),
            tool_results=(),
            candidate=candidate,
            claim_units=units,
        )
        locator_items = [item for item in handoff.items if item.classification == "requirement_locator"]
        target_claim = units[0].claim_id

        self.assertGreater(len(locator_items), 1)
        self.assertEqual(handoff.transport_omitted_claim_ids, ())
        self.assertTrue(all(len(item.summary) <= 2200 for item in locator_items))
        self.assertTrue(any("25:" in item.summary and target_claim in item.claim_ids for item in locator_items))
        self.assertFalse(any("[omitted middle lines]" in item.summary for item in locator_items))

    def test_oversized_locator_line_is_not_marked_transported(self) -> None:
        contract = generate_requirement_contract(
            "只根据 Markdown 分析资料一致性，不要检查代码。"
        )
        candidate = "- policy.md:10 states an oversized rule."
        units = candidate_claim_units(candidate)
        handoff = build_explore_handoff(
            request="只根据 Markdown 分析资料一致性，不要检查代码。",
            contract=contract,
            requirement_evidence=(RequirementEvidence("policy.md", "10: " + ("x" * 5000), root="/workspace"),),
            source_evidence=(),
            records=(),
            tool_results=(),
            candidate=candidate,
            claim_units=units,
        )
        locator_items = [item for item in handoff.items if item.classification == "requirement_locator"]

        self.assertEqual(locator_items, [])
        self.assertEqual(handoff.transport_omitted_claim_ids, ("c001",))

    def test_shared_range_locator_binds_many_claims_without_repeating_summary(self) -> None:
        contract = generate_requirement_contract(
            "只根据 Markdown 分析资料一致性，不要检查代码。"
        )
        content = "\n".join(f"{index}: rule {index}" for index in range(1, 260))
        candidate = "### Rules (policy.md:191-212)\n\n" + "\n".join(
            f"| row {index} | cites shared range fact {index} |"
            for index in range(1, 24)
        )
        units = candidate_claim_units(candidate)
        handoff = build_explore_handoff(
            request="只根据 Markdown 分析资料一致性，不要检查代码。",
            contract=contract,
            requirement_evidence=(RequirementEvidence("policy.md", content, root="/workspace"),),
            source_evidence=(),
            records=(),
            tool_results=(),
            candidate=candidate,
            claim_units=units,
        )
        locator_items = [item for item in handoff.items if item.classification == "requirement_locator"]
        range_items = [item for item in locator_items if "line 191-212" in item.summary]

        self.assertEqual(len(range_items), 1)
        self.assertGreater(len(range_items[0].claim_ids), 16)
        self.assertLessEqual(len(range_items[0].summary), 2200)
        serialized = handoff.to_dict()["claim_matrix"]
        serialized_range = next(item for item in serialized if item.get("classification") == "requirement_locator")
        self.assertIn("claim_ids", serialized_range)
        self.assertNotIn("claim_id", serialized_range)

    def test_claim_scoped_locator_selection_is_artifact_fair_under_cap(self) -> None:
        contract = generate_requirement_contract(
            "只根据 Markdown 分析资料一致性，不要检查代码。"
        )
        content_a = "\n".join(f"{index}: alpha rule {index}" for index in range(1, 80))
        content_b = "\n".join(f"{index}: beta rule {index}" for index in range(1, 20))
        candidate = "\n".join(
            [f"- alpha.md:{index} states alpha rule {index}." for index in range(1, 25)]
            + ["- beta.md:9 states beta rule 9."]
        )
        units = candidate_claim_units(candidate)
        handoff = build_explore_handoff(
            request="只根据 Markdown 分析资料一致性，不要检查代码。",
            contract=contract,
            requirement_evidence=(
                RequirementEvidence("alpha.md", content_a, root="/workspace"),
                RequirementEvidence("beta.md", content_b, root="/workspace"),
            ),
            source_evidence=(),
            records=(),
            tool_results=(),
            candidate=candidate,
            claim_units=units,
        )
        locator_items = [item for item in handoff.items if item.classification == "requirement_locator"]

        self.assertLessEqual(len(locator_items), 16)
        self.assertTrue(any(item.path == "alpha.md" for item in locator_items))
        self.assertTrue(any(item.path == "beta.md" and "9: beta rule 9" in item.summary for item in locator_items))

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
        finding_parameters = finding_schema["function"]["parameters"]
        self.assertEqual(finding_parameters["additionalProperties"], False)
        self.assertNotIn("claim", finding_parameters["properties"])
        self.assertEqual(finding_parameters["required"], ["claim_id", "finding_scope", "issue", "action"])
        self.assertEqual(finding_parameters["properties"]["finding_scope"]["enum"], ["candidate_defect"])

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
        self.assertIn("Runtime binds the exact candidate text by claim_id", prompt)
        self.assertNotIn("copy that exact candidate_claims text", prompt)
        self.assertIn("action must change the candidate answer", prompt)
        self.assertIn("exact request is mandatory", prompt)
        self.assertIn("source priority", prompt)
        self.assertIn("must not ask to modify the requirements, images, prototypes", prompt)
        self.assertIn("report_read_only_finding once per finding", prompt)
        self.assertIn("submit_read_only_review with verdict, confidence, reason", prompt)
        self.assertIn("profile-required typed summary such as document_consistency", prompt)
        self.assertIn("Do not repeat findings in the final submit", prompt)
        schema = reviewer_finding_tool_schema(candidate_claim_units("candidate"))
        action_description = schema["function"]["parameters"]["properties"]["action"]["description"]
        self.assertIn("candidate-answer change", action_description)
        self.assertIn("Do not ask to modify source documents", action_description)
        repair = reviewer_repair_messages(handoff, candidate_claim_units("candidate"), {"error_code": "findings_too_many"})
        self.assertIn("at most 8", repair[0]["content"])
        self.assertIn("no more than 8 highest-risk findings", repair[-1]["content"])
        self.assertIn("original output tools", repair[-1]["content"])
        self.assertIn("Runtime binds the exact claim by claim_id", repair[-1]["content"])

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

    def test_document_consistency_rewrite_projects_typed_conflict_context_not_actions(self) -> None:
        contract = generate_requirement_contract(
            "Compare Markdown and image artifacts. Do not choose source priority or infer lifecycle."
        )
        handoff = ExploreHandoff(
            request="Compare Markdown and image artifacts. Do not choose source priority or infer lifecycle.",
            contract=contract,
            items=(
                ClaimEvidenceItem(
                    "requirement_fact",
                    "read_file",
                    "policy.md",
                    "primary",
                    "root_local",
                    "ok",
                    "Document A says the field must stay empty.",
                ),
                ClaimEvidenceItem(
                    "visual_observation",
                    "inspect_image",
                    "example.png",
                    "primary",
                    "root_local",
                    "ok",
                    "Image B visibly shows the field has a value.",
                ),
            ),
        )
        result = ReviewerResult(
            "revise",
            0.7,
            findings=(
                ReviewerFinding(
                    "c001",
                    "unsupported reconciliation",
                    "treat Doc A authoritative / image later state",
                    "A and B are consistent because image B is a later state.",
                    "candidate_defect",
                ),
            ),
            reason="unsupported reconciliation",
            document_consistency=DocumentConsistencyAssessment("reported_unresolved", ("e001", "e002"), ()),
        )

        message = reviewer_rewrite_message(result, profile="document_consistency", handoff=handoff)

        self.assertIn("Do not choose source priority or infer lifecycle", message)
        self.assertIn("evidence_id=e001", message)
        self.assertIn("policy.md", message)
        self.assertIn("Document A says the field must stay empty", message)
        self.assertIn("evidence_id=e002", message)
        self.assertIn("example.png", message)
        self.assertIn("Image B visibly shows the field has a value", message)
        self.assertIn("Reviewer stance: reported_unresolved", message)
        self.assertIn("No valid supporting evidence in this handoff establishes artifact lifecycle", message)
        self.assertIn("role/lifecycle/precedence is not established", message)
        self.assertIn("unresolved or pending confirmation", message)
        self.assertIn("mockup, reference-only, example-only", message)
        self.assertNotIn("treat Doc A authoritative", message)
        self.assertNotIn("image later state", message)
        self.assertLessEqual(len(message), 5000)

    def test_document_consistency_rewrite_omits_stale_ids_and_keeps_supported_context(self) -> None:
        contract = generate_requirement_contract(
            "Compare two artifacts and keep reconciliation evidence scoped."
        )
        handoff = ExploreHandoff(
            request="Compare two artifacts and keep reconciliation evidence scoped.",
            contract=contract,
            items=(
                ClaimEvidenceItem(
                    "requirement_fact",
                    "read_file",
                    "policy.md",
                    "primary",
                    "root_local",
                    "ok",
                    "Document A says blank.",
                ),
                ClaimEvidenceItem(
                    "visual_observation",
                    "inspect_image",
                    "example.png",
                    "primary",
                    "root_local",
                    "ok",
                    "Image B shows a value.",
                ),
                ClaimEvidenceItem(
                    "document_reconciliation_support",
                    "read_file",
                    "lifecycle.md",
                    "primary",
                    "root_local",
                    "ok",
                    "This screenshot is captured after manual completion.",
                ),
                ClaimEvidenceItem(
                    "observed_candidate",
                    "search_code",
                    "search.log",
                    "primary",
                    "root_local",
                    "ok",
                    "SHOULD_NOT_APPEAR as a conflict observation.",
                ),
            ),
        )
        supported = ReviewerResult(
            "revise",
            0.7,
            findings=(),
            reason="supported",
            document_consistency=DocumentConsistencyAssessment(
                "explicitly_supported_reconciliation",
                ("e001", "e002"),
                ("e003",),
            ),
        )

        message = reviewer_rewrite_message(supported, profile="document_consistency", handoff=handoff)

        self.assertIn("evidence_id=e003", message)
        self.assertIn("captured after manual completion", message)
        self.assertIn("Any reconciliation may use only the cited support observations", message)
        self.assertNotIn("Do not describe either artifact as mockup", message)

        stale = ReviewerResult(
            "revise",
            0.7,
            findings=(),
            reason="stale",
            document_consistency=DocumentConsistencyAssessment("reported_unresolved", ("e001", "e999"), ()),
        )
        stale_message = reviewer_rewrite_message(stale, profile="document_consistency", handoff=handoff)
        self.assertNotIn("e999", stale_message)
        self.assertIn("evidence_id=e001", stale_message)

        invalid_conflict = ReviewerResult(
            "revise",
            0.7,
            findings=(),
            reason="invalid conflict",
            document_consistency=DocumentConsistencyAssessment("reported_unresolved", ("e001", "e004"), ()),
        )
        invalid_message = reviewer_rewrite_message(invalid_conflict, profile="document_consistency", handoff=handoff)
        self.assertIn("evidence_id=e001", invalid_message)
        self.assertNotIn("evidence_id=e004", invalid_message)
        self.assertNotIn("SHOULD_NOT_APPEAR", invalid_message)

    def test_document_consistency_rewrite_ignores_invalid_support_for_revise(self) -> None:
        contract = generate_requirement_contract(
            "Compare artifacts without inventing lifecycle."
        )
        handoff = ExploreHandoff(
            request="Compare artifacts without inventing lifecycle.",
            contract=contract,
            items=(
                ClaimEvidenceItem(
                    "requirement_fact",
                    "read_file",
                    "policy.md",
                    "primary",
                    "root_local",
                    "ok",
                    "Document A says blank.",
                ),
                ClaimEvidenceItem(
                    "visual_observation",
                    "inspect_image",
                    "example.png",
                    "primary",
                    "root_local",
                    "ok",
                    "Image B shows a value.",
                ),
                ClaimEvidenceItem(
                    "requirement_fact",
                    "read_file",
                    "notes.md",
                    "primary",
                    "root_local",
                    "ok",
                    "General notes mention both artifacts but no lifecycle or precedence.",
                ),
            ),
        )
        result = ReviewerResult(
            "revise",
            0.7,
            findings=(),
            reason="invalid support",
            document_consistency=DocumentConsistencyAssessment(
                "explicitly_supported_reconciliation",
                ("e001", "e002"),
                ("e003",),
            ),
        )

        message = reviewer_rewrite_message(result, profile="document_consistency", handoff=handoff)

        self.assertIn("evidence_id=e001", message)
        self.assertIn("evidence_id=e002", message)
        self.assertNotIn("evidence_id=e003", message)
        self.assertNotIn("may use only the cited support observations", message)
        self.assertIn("No valid supporting evidence", message)
        self.assertIn("role/lifecycle/precedence is not established", message)

    def test_document_consistency_rewrite_context_preserves_hard_policy_under_oversized_items(self) -> None:
        contract = generate_requirement_contract(
            "Compare artifacts. Do not choose source priority or infer lifecycle."
        )
        huge = " ".join(f"detail-{index}" for index in range(500))
        data_url = "data:image/png;base64," + ("A" * 320)
        handoff = ExploreHandoff(
            request="Compare artifacts. Do not choose source priority or infer lifecycle.",
            contract=contract,
            items=tuple(
                ClaimEvidenceItem(
                    "visual_observation" if index == 2 else "requirement_fact",
                    "inspect_image" if index == 2 else "read_file",
                    f"artifact-{index}.md",
                    "primary",
                    "root_local",
                    "ok",
                    data_url if index == 2 else f"Observation side {index}: {huge}",
                )
                for index in range(1, 9)
            ),
        )
        context = document_consistency_rewrite_context(
            handoff,
            DocumentConsistencyAssessment("reported_unresolved", tuple(f"e{index:03d}" for index in range(1, 9)), ()),
        )
        rendered = "\n".join(context)

        self.assertLessEqual(len(rendered), MAX_REWRITE_CONTEXT_CHARS)
        self.assertIn("Do not choose source priority or infer lifecycle", rendered)
        self.assertIn("Reviewer stance: reported_unresolved", rendered)
        self.assertIn("No valid supporting evidence", rendered)
        self.assertIn("Required disposition", rendered)
        self.assertIn("Do not describe either artifact as mockup", rendered)
        self.assertIn("evidence_id=e001", rendered)
        self.assertIn("path=artifact-1.md", rendered)
        self.assertIn("tool=read_file", rendered)
        self.assertIn("root=primary", rendered)
        self.assertIn("scope=root_local", rendered)
        self.assertIn("Observation side 1", rendered)
        self.assertIn("evidence_id=e002", rendered)
        self.assertIn("path=artifact-2.md", rendered)
        self.assertIn("tool=inspect_image", rendered)
        self.assertIn("[omitted data-url/base64 payload", rendered)
        self.assertNotIn("data:image/png;base64", rendered)
        self.assertNotIn("A" * 200, rendered)
        for line in context:
            if line.startswith("  * "):
                self.assertIn("evidence_id=", line)
                self.assertIn("tool=", line)
                self.assertIn("path=", line)
                self.assertIn("root=", line)
                self.assertIn("scope=", line)
                self.assertIn("summary=", line)

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

    def test_document_consistency_rejects_finding_action_that_resolves_unresolved_conflict(self) -> None:
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
        units = candidate_claim_units("The document and image conflict remains unresolved.")
        with self.assertRaises(ReviewerValidationError) as raised:
            parse_reviewer_payload(
                {
                    "verdict": "revise",
                    "confidence": 0.8,
                    "findings": [
                        {
                            "claim_id": "c001",
                            "finding_scope": "candidate_defect",
                            "issue": "candidate should explain the conflict",
                            "action": "示例图中的复核人信息属于历史示例或预留字段展示，不影响本期范围，也不构成待确认项。",
                        }
                    ],
                    "reason": "bad action",
                    "document_consistency": {
                        "stance": "reported_unresolved",
                        "conflict_evidence_ids": ["e001", "e002"],
                        "supporting_evidence_ids": [],
                    },
                },
                claim_units=units,
                document_consistency=True,
                evidence_ids=handoff.evidence_ids,
            )

        self.assertEqual(raised.exception.code, "document_consistency_finding_reconciles_conflict")
        self.assertEqual(raised.exception.pending_candidate_claim_ids, ("c001",))
        repair = reviewer_repair_messages(handoff, units, raised.exception.diagnostics)
        self.assertIn("without inventing", repair[-1]["content"])
        self.assertIn("historical/current role", repair[-1]["content"])

    def test_document_consistency_rejects_multiple_conflict_reconciling_findings_with_ids(self) -> None:
        handoff = build_explore_handoff(
            request="compare the requested documents",
            contract=generate_requirement_contract("只根据 Markdown 和图片分析资料一致性，不要检查代码。"),
            requirement_evidence=(),
            source_evidence=(),
            records=(),
            tool_results=(
                type("Tool", (), {"name": "read_file", "content": "A says blank.", "path": "policy.md", "is_error": False, "useless": False, "metadata": {}})(),
                type("Tool", (), {"name": "inspect_image", "content": "B shows a value.", "path": "example.png", "is_error": False, "useless": False, "metadata": {}})(),
            ),
        )
        units = candidate_claim_units(
            "The document and image conflict remains unresolved.\n\n"
            "The screenshot value conflicts with the document and remains unresolved."
        )
        with self.assertRaises(ReviewerValidationError) as raised:
            parse_reviewer_payload(
                {
                    "verdict": "revise",
                    "confidence": 0.8,
                    "findings": [
                        {
                            "claim_id": "c001",
                            "finding_scope": "candidate_defect",
                            "issue": "candidate discusses the artifact conflict",
                            "action": "Treat the image as the later completed state.",
                        },
                        {
                            "claim_id": "c002",
                            "finding_scope": "candidate_defect",
                            "issue": "candidate discusses the artifact conflict",
                            "action": "以需求文档为准，示例图仅作参考。",
                        },
                    ],
                    "reason": "bad actions",
                    "document_consistency": {
                        "stance": "reported_unresolved",
                        "conflict_evidence_ids": ["e001", "e002"],
                        "supporting_evidence_ids": [],
                    },
                },
                claim_units=units,
                document_consistency=True,
                evidence_ids=handoff.evidence_ids,
            )

        self.assertEqual(raised.exception.code, "document_consistency_finding_reconciles_conflict")
        self.assertEqual(raised.exception.pending_candidate_claim_ids, ("c001", "c002"))
        self.assertEqual(
            raised.exception.diagnostics["invalid_document_finding_claim_ids"],
            ["c001", "c002"],
        )

    def test_document_consistency_preserves_unrelated_scope_findings(self) -> None:
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
        units = candidate_claim_units("Automatic sending is current scope.")
        result = parse_reviewer_payload(
            {
                "verdict": "revise",
                "confidence": 0.8,
                "findings": [
                    {
                        "claim_id": "c001",
                        "finding_scope": "candidate_defect",
                        "issue": "Requirement says this is later phase.",
                        "action": "Classify automatic sending as later.",
                    }
                ],
                "reason": "scope correction",
                "document_consistency": {
                    "stance": "reported_unresolved",
                    "conflict_evidence_ids": ["e001", "e002"],
                    "supporting_evidence_ids": [],
                },
            },
            claim_units=units,
            document_consistency=True,
            evidence_ids=handoff.evidence_ids,
        )

        self.assertEqual(result.verdict, "revise")
        self.assertEqual(result.findings[0].action, "Classify automatic sending as later.")

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
        with self.assertRaises(ReviewerValidationError) as no_claim_raised:
            parse_reviewer_payload(
                {
                    "verdict": "revise",
                    "confidence": 0.7,
                    "findings": [
                        {
                            "claim_id": "c001",
                            "finding_scope": "source_material_gap",
                            "issue": "source materials still need confirmation",
                            "action": "ask the artifact owner to update sources",
                        }
                    ],
                    "reason": "source gap",
                },
                claim_units=units,
            )
        self.assertEqual(no_claim_raised.exception.code, "source_material_gap_finding")
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
        missing_repair = reviewer_repair_messages(
            build_explore_handoff(
                request="review",
                contract=generate_requirement_contract("只根据 Markdown 和图片分析资料一致性，不要检查代码。"),
                requirement_evidence=(), source_evidence=(), records=(), tool_results=(),
            ),
            units,
            pass_raised.exception.diagnostics,
            required_resubmit_claim_ids=pass_raised.exception.pending_candidate_claim_ids,
        )
        missing_repair_text = missing_repair[-1]["content"]
        self.assertIn("same claim_id", missing_repair_text)
        self.assertIn("issue/action only", missing_repair_text)
        self.assertIn("Runtime binds the canonical candidate text by claim_id", missing_repair_text)
        self.assertNotIn("exact copied claim text", missing_repair_text)
        self.assertNotIn("copy the exact", missing_repair_text)

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
        self.assertIn("Do not include claim text", repair[-1]["content"])

    def test_finding_without_claim_binds_canonical_claim_by_id(self) -> None:
        units = candidate_claim_units("First unsupported claim.\n\nSecond unsupported claim.")
        parsed = parse_reviewer_payload(
            {
                "verdict": "revise",
                "confidence": 0.8,
                "findings": [
                    {
                        "claim_id": "c002",
                        "finding_scope": "candidate_defect",
                        "issue": "unsupported",
                        "action": "qualify",
                    }
                ],
                "reason": "anchor-bound finding",
            },
            claim_units=units,
        )
        self.assertEqual(parsed.findings[0].claim_id, "c002")
        self.assertEqual(parsed.findings[0].claim, "Second unsupported claim.")

    def test_claim_ids_address_markdown_units_without_reviewer_text_matching(self) -> None:
        candidate = "| Scope | Owner |\n| --- | --- |\n| Frontend | **platformPayment** |\n\n**Conclusion:** platformPayment is the verified owner."
        units = candidate_claim_units(candidate)
        self.assertEqual([unit.claim_id for unit in units], ["c001", "c002"])
        result = parse_reviewer_result(
            '{"verdict":"revise","confidence":0.9,"findings":[{"claim_id":"c001","claim":"| Frontend | **platformPayment** |","finding_scope":"candidate_defect","issue":"no direct binding","action":"mark as analogous candidate"}],"reason":"unlocated owner"}',
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
        self.assertEqual(len(units), 100)
        self.assertEqual(ids[0], "c001")
        self.assertTrue(any(45 <= int(claim_id[1:]) <= 60 for claim_id in ids))
        self.assertIn("c100", ids)
        self.assertIn("c101", ids)
        self.assertIn("c102", ids)
        tail_unit = next(unit for unit in units if unit.claim_id == "c101")
        result = parse_reviewer_result(
            '{"verdict":"revise","confidence":0.9,"findings":[{"claim_id":"c101","claim":"| Frontend | **platformPayment** is verified owner |","finding_scope":"candidate_defect","issue":"no binding","action":"mark unlocated"},{"claim_id":"c102","claim":"Final conclusion: platformPayment is the true owner.","finding_scope":"candidate_defect","issue":"no binding","action":"mark unlocated"}],"reason":"x"}',
            claim_units=units,
        )
        self.assertIn("platformPayment", tail_unit.text)
        rewritten = candidate.replace("**platformPayment** is verified owner", "analogous candidate").replace("platformPayment is the true owner", "the true owner is unlocated")
        self.assertTrue(rewrite_complies_with_review(rewritten, units, result.findings))

    def test_normal_technical_design_keeps_all_claims_below_transport_cap(self) -> None:
        candidate = "\n".join(f"- source fact {index}" for index in range(1, 86))

        units = candidate_claim_units(candidate)

        self.assertEqual(len(units), 85)
        self.assertEqual([unit.claim_id for unit in units], [f"c{index:03d}" for index in range(1, 86)])

    def test_claim_unit_sampling_preserves_high_risk_document_consistency_windows(self) -> None:
        lines = [f"- filler claim {index}" for index in range(1, 121)]
        lines[38] = "- context before document observation"
        lines[39] = "- Document A says the field is blank."
        lines[40] = "- context after document observation"
        lines[74] = "- The prototype and image remain different; the lifecycle is unresolved."
        lines[95] = "- context before visual observation"
        lines[96] = "- Image B shows a visible value."
        lines[97] = "- context after visual observation"
        lines[116] = "- The Markdown document is the highest priority source of truth."

        units = candidate_claim_units("\n".join(lines))
        text_by_id = {unit.claim_id: unit.text for unit in units}
        self.assertLessEqual(len(units), 100)
        for claim_id in (
            "c039",
            "c040",
            "c041",
            "c075",
            "c096",
            "c097",
            "c098",
            "c117",
        ):
            self.assertIn(claim_id, text_by_id)
        self.assertIn("field is blank", text_by_id["c040"])
        self.assertIn("visible value", text_by_id["c097"])
        self.assertIn("highest priority", text_by_id["c117"])

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
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["rewrite_acceptances"], 1)
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["rewrite_verification_rounds"], 1)
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["typed_submits"], 3)
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["finding_submits"], 1)
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["final_submits"], 2)
        self.assertNotIn("LCA_READ_ONLY_EVIDENCE_REVIEW", "\n".join(str(m) for m in runtime._messages))

    def test_rewrite_verification_rejects_paraphrased_blocking_claims_then_accepts_downgrade(self) -> None:
        _RewriteVerificationParaphraseClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "src").mkdir()
            (workspace / "src/Evidence.java").write_text("class Evidence {}\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _RewriteVerificationParaphraseClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner、表、接口和影响范围，不要修改。")

        self.assertIn("设计建议", answer)
        self.assertIn("待确认", answer)
        self.assertNotIn("conclusively lacks", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["triggers"], 3)
        self.assertEqual(summary["rewrites"], 2)
        self.assertEqual(summary["rewrite_corrections"], 1)
        self.assertEqual(summary["rewrite_verification_rounds"], 2)
        self.assertEqual(summary["rewrite_acceptances"], 1)
        self.assertEqual(summary["verdicts"], {"pass": 1, "revise": 2})

    def test_rewrite_verification_withholds_superseded_finding_details(self) -> None:
        _RewriteVerificationSameClaimIdClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "src").mkdir()
            (workspace / "src/Evidence.java").write_text("class Evidence {}\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _RewriteVerificationSameClaimIdClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner，不要修改。")

        self.assertIn("pending confirmation", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["rewrite_verification_rounds"], 2)
        self.assertEqual(summary["rewrite_acceptances"], 1)
        verification_payloads = [
            json.loads(message["content"])
            for call in _RewriteVerificationSameClaimIdClient.calls
            for message in call["messages"]
            if message.get("role") == "user"
            and "LCA_READ_ONLY_REWRITE_VERIFICATION" in str(message.get("content"))
        ]
        closure = verification_payloads[-1]
        self.assertEqual(
            closure["prior_review"],
            {
                "verdict": "revise",
                "blocking_finding_count": 1,
                "historical_details_withheld": True,
            },
        )
        self.assertNotIn("original_findings", closure)
        self.assertNotIn("c001 paraphrased", json.dumps(closure, ensure_ascii=False))

    def test_explicit_design_proposal_with_pending_reuse_check_can_pass_review(self) -> None:
        _ProposalSemanticsClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "src").mkdir()
            (workspace / "src/Mapper.xml").write_text("<mapper/>\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _ProposalSemanticsClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("基于当前只读证据给出设计建议：数据模型和接口；不能伪装成现有事实。")

        self.assertIn("建议新建", answer)
        self.assertIn("待确认", answer)
        self.assertEqual(runtime._last_run_summary["termination_reason"], "final")
        self.assertEqual(runtime._last_run_summary["read_only_reviewer"]["verdicts"], {"pass": 1})

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
        self.assertEqual(summary["triggers"], 2)
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
        self.assertEqual(
            [tool["function"]["name"] for tool in review_calls[3]["tools"]],
            [REVIEWER_OUTPUT_TOOL_NAME],
        )

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
        self.assertEqual(summary["rejected_final_submits"], 3)
        self.assertEqual(summary["rejected_finding_submits"], 3)
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
        self.assertEqual(summary["verdicts"], {"revise": 1, "pass": 1})
        self.assertEqual(summary["rewrite_acceptances"], 1)
        self.assertEqual(summary["finding_submits"], 1)

    def test_identical_finding_replay_after_malformed_final_is_idempotent(self) -> None:
        _ReviewerFindingReplayAfterMalformedFinalClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "src").mkdir()
            (workspace / "src/Evidence.java").write_text("class Evidence {}\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerFindingReplayAfterMalformedFinalClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
            session = runtime._session.path.read_text(encoding="utf-8")

        self.assertIn("analogous candidate", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["finding_submits"], 1)
        self.assertEqual(summary["rejected_finding_submits"], 0)
        self.assertEqual(summary["rejected_final_submits"], 1)
        self.assertEqual(summary["final_submits"], 2)
        self.assertEqual(summary["output_lifecycle_exhausted"], 0)
        self.assertEqual(summary["errors"], {})
        self.assertIn('"event": "finding_replayed"', session)

    def test_conflicting_second_finding_closes_finding_channel_and_preserves_first(self) -> None:
        _ReviewerConflictingFindingThenFinalClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "src").mkdir()
            (workspace / "src/Evidence.java").write_text("class Evidence {}\n", encoding="utf-8")
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerConflictingFindingThenFinalClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
            session = runtime._session.path.read_text(encoding="utf-8")

        self.assertIn("analogous candidate", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["finding_submits"], 1)
        self.assertEqual(summary["rejected_finding_submits"], 1)
        self.assertEqual(summary["output_lifecycle_exhausted"], 0)
        self.assertEqual(summary["errors"], {})
        review_calls = [
            call
            for call in _ReviewerConflictingFindingThenFinalClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(
            [tool["function"]["name"] for tool in review_calls[2]["tools"]],
            [REVIEWER_OUTPUT_TOOL_NAME],
        )
        self.assertIn('"reason": "claim_id_conflict"', session)

    def test_reviewer_continuation_canonicalizes_malformed_tool_arguments(self) -> None:
        _ReviewerProviderSafeContinuationClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerProviderSafeContinuationClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
        self.assertIn("analogous candidate", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["finding_submits"], 1)
        self.assertEqual(summary["rejected_finding_submits"], 1)
        self.assertEqual(summary["rejected_final_submits"], 1)
        self.assertEqual(summary["verdicts"], {"revise": 1, "pass": 1})
        self.assertEqual(summary["rewrite_acceptances"], 1)
        self.assertEqual(summary["errors"], {})
        review_calls = [
            call
            for call in _ReviewerProviderSafeContinuationClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertGreaterEqual(len(review_calls), 2)

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

    def test_mixed_rejected_turn_counts_as_one_correction_opportunity(self) -> None:
        _ReviewerMixedRejectedTurnThenFinalClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerMixedRejectedTurnThenFinalClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改文件。")
        self.assertIn("unlocated", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["finding_submits"], 8)
        self.assertEqual(summary["rejected_finding_submits"], 5)
        self.assertEqual(summary["rejected_final_submits"], 2)
        self.assertEqual(summary["protocol_failures"], 0)
        self.assertEqual(summary["finding_limit_hits"], 1)
        self.assertEqual(summary["output_lifecycle_exhausted"], 0)
        self.assertEqual(summary["final_submits"], 2)
        review_calls = [
            call for call in _ReviewerMixedRejectedTurnThenFinalClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(
            [tool["function"]["name"] for tool in review_calls[2]["tools"]],
            [REVIEWER_OUTPUT_TOOL_NAME],
        )
        first_turn_results = [message for message in review_calls[1]["messages"] if message.get("role") == "tool"]
        for call_id in [*(f"finding-{index}" for index in range(1, 8)), "bad-finding", "blocked-final-1"]:
            self.assertEqual(len([message for message in first_turn_results if message.get("tool_call_id") == call_id]), 1)
        tool_results = [message for message in review_calls[2]["messages"] if message.get("role") == "tool"]
        for call_id in (
            "duplicate-1",
            "duplicate-2",
            "conflict-3",
            "mismatch-4",
            "mismatch-5",
            "finding-8",
            "finding-9",
            "blocked-final-2",
        ):
            self.assertEqual(len([message for message in tool_results if message.get("tool_call_id") == call_id]), 1)

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
        self.assertEqual(summary["verdicts"], {"revise": 1, "pass": 1})
        self.assertEqual(summary["rewrite_acceptances"], 1)

    def test_incremental_findings_without_claim_use_claim_id_anchor_binding(self) -> None:
        _ReviewerNoClaimFindingsClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerNoClaimFindingsClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner、DDL 和 API，不要修改文件。")
        self.assertIn("Owner evidence remains unlocated", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["finding_submits"], 3)
        self.assertEqual(summary["rejected_finding_submits"], 0)
        self.assertEqual(summary["output_lifecycle_exhausted"], 0)
        self.assertEqual(summary["errors"], {})
        self.assertEqual(summary["verdicts"], {"revise": 1, "pass": 1})
        self.assertEqual(summary["rewrite_acceptances"], 1)
        review_calls = [
            call for call in _ReviewerNoClaimFindingsClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        finding_schema = review_calls[0]["tools"][0]["function"]["parameters"]
        self.assertNotIn("claim", finding_schema["properties"])
        tool_results = [message for message in review_calls[1]["messages"] if message.get("role") == "tool"]
        self.assertEqual(len([message for message in tool_results if message.get("tool_call_id") == "finding-1"]), 1)
        self.assertEqual(len([message for message in tool_results if message.get("tool_call_id") == "finding-2"]), 1)

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
        self.assertEqual(summary["verdicts"], {"revise": 1, "pass": 1})
        self.assertEqual(summary["rewrite_acceptances"], 1)

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
            self.assertEqual(summary["verdicts"], {"revise": 1, "pass": 1})
            self.assertEqual(summary["rewrite_acceptances"], 1)
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
        self.assertEqual(summary["output_lifecycle_exhausted_categories"], {"protocol": 1})
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
        self.assertIn("真实 owner 未由本轮证据定位", answer)
        self.assertIn("弱相关候选", answer)
        self.assertEqual(runtime._last_run_summary["termination_reason"], "final")
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["verdicts"], {"unverified": 1, "pass": 1})
        self.assertEqual(summary["rewrites"], 1)
        self.assertEqual(summary["rewrite_acceptances"], 1)
        review_calls = [
            call
            for call in _ReviewerUnverifiedRewriteClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(len(review_calls), 2)
        self.assertTrue(runtime._run.read_only_review.rewrite_accepted)
        self.assertFalse(runtime._run.read_only_review.rewrite_requested)
        outcome = runtime._read_only_review_phase.review_candidate(answer)
        self.assertEqual(outcome.kind, "pass")
        self.assertTrue(runtime._run.read_only_review.rewrite_accepted)
        self.assertFalse(runtime._run.read_only_review.rewrite_requested)

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
                if client is _ReviewerMalformedOutputClient:
                    self.assertEqual(
                        runtime._last_run_summary["read_only_reviewer"]["output_lifecycle_exhausted_categories"],
                        {"arguments": 1},
                    )

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
        _InvalidReviewerClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            with patch("local_agent.agent.OpenAICompatibleClient", _InvalidReviewerClient):
                runtime = AgentRuntime(_config(workspace), show_tool_logs=False)
                contract = generate_requirement_contract("只读分析当前服务 owner 和影响范围，不要修改。")
                now = time.monotonic()
                runtime._run.begin(
                    run_id="review-budget", started_monotonic=now, deadline_monotonic=now + 0.1,
                    run_start_index=0, git_baseline={}, prompt="只读分析当前服务 owner 和影响范围，不要修改。",
                    requirement_contract=contract, requirement_contract_context="", design_evidence_roots=(),
                )
                runtime._run.pinned_requirement_evidence = [
                    RequirementEvidence(
                        "requirements.md",
                        "1: Requirement states the bounded owner analysis must remain evidence-scoped.",
                        root=str(workspace),
                    )
                ]
                runtime._run.tool_choice_results.append(
                    ToolResultSummary(
                        "read_file",
                        "1: Requirement states the bounded owner analysis must remain evidence-scoped.",
                        path="requirements.md",
                        metadata={
                            "resolved_path": str(workspace / "requirements.md"),
                            "evidence_root": str(workspace),
                        },
                    )
                )
                outcome = runtime._read_only_review_phase.review_candidate("PayServiceImpl is the verified owner.")
        self.assertEqual(outcome.kind, "unverified")
        self.assertIn("安全部分交付", outcome.terminal_message)
        self.assertIn("requirements.md", outcome.terminal_message)
        self.assertIn("bounded owner analysis", outcome.terminal_message)
        self.assertNotIn("PayServiceImpl is the verified owner", outcome.terminal_message)
        self.assertEqual(_InvalidReviewerClient.calls, [])

    def test_reviewer_free_text_advice_does_not_create_second_hard_gate(self) -> None:
        _NoncompliantRewriteClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _NoncompliantRewriteClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改。")
        self.assertIn("安全部分交付", answer)
        self.assertEqual(runtime._last_run_summary["termination_reason"], "read_only_reviewer_unverified")
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["rewrites"], 2)
        self.assertEqual(summary["rewrite_acceptances"], 0)
        self.assertEqual(summary["errors"], {"rewrite_noncompliant": 1})
        review_calls = [
            call
            for call in _NoncompliantRewriteClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(len(review_calls), 1)

    def test_reviewer_free_text_advice_allows_partial_rewrite_progress(self) -> None:
        _PartialProgressRewriteClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _PartialProgressRewriteClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                answer = runtime.run("只读分析当前服务 owner 和影响范围，不要修改。")
        self.assertIn("Owner evidence remains unlocated", answer)
        self.assertIn("导出接口已经实现", answer)
        self.assertEqual(runtime._last_run_summary["termination_reason"], "final")
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["rewrites"], 1)
        self.assertEqual(summary["rewrite_acceptances"], 1)
        self.assertEqual(summary["errors"], {})
        review_calls = [
            call
            for call in _PartialProgressRewriteClient.calls
            if any("LCA_READ_ONLY_EVIDENCE_REVIEW" in str(message.get("content")) for message in call["messages"])
        ]
        self.assertEqual(len(review_calls), 2)

    def test_deterministic_rewrite_after_first_review_does_not_call_second_reviewer(self) -> None:
        _ReviewerLastGateClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("local_agent.agent.OpenAICompatibleClient", _ReviewerLastGateClient):
                runtime = AgentRuntime(_config(Path(tmp).resolve()), show_tool_logs=False)
                runtime._final_answer_steerers = (_OneShotDeterministicRewrite(),)
                answer = runtime.run("基于当前代码给出可实施设计草案：接口和权限；不得编造现有服务。")

        self.assertIn("仅作为设计建议/待确认项", answer)
        self.assertNotIn("已经由 PayServiceImpl 实现", answer)
        summary = runtime._last_run_summary["read_only_reviewer"]
        self.assertEqual(summary["triggers"], 2)
        self.assertEqual(summary["rewrites"], 1)
        self.assertEqual(summary["rewrite_acceptances"], 1)
        self.assertEqual(summary["typed_submits"], 3)
        self.assertEqual(summary["finding_submits"], 1)
        self.assertEqual(summary["final_submits"], 2)
        self.assertEqual(summary["errors"], {})
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

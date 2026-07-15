"""Per-call lifecycle for isolated read-only reviewer output tools."""
from __future__ import annotations

import json
from dataclasses import dataclass
from copy import deepcopy
from typing import Any, Literal

from .provider_protocol import classify_provider_content_artifact
from .document_consistency import document_consistency_rejection_hint
from .read_only_reviewer import MAX_REVIEWER_FINDINGS
from .read_only_reviewer import REVIEWER_FINDING_TOOL_NAME
from .read_only_reviewer import REVIEWER_OUTPUT_TOOL_NAME
from .read_only_reviewer import ReviewerFinding
from .read_only_reviewer import ReviewerResult
from .read_only_reviewer import ReviewerValidationError
from .read_only_reviewer import parse_reviewer_final_payload
from .read_only_reviewer import parse_reviewer_finding_payload


OutputEventKind = Literal[
    "finding",
    "finding_replayed",
    "final",
    "finding_rejected",
    "final_rejected",
    "protocol_rejected",
]


@dataclass(frozen=True)
class ReviewerOutputEvent:
    kind: OutputEventKind
    tool_call_id: str
    call_index: int | None = None
    code: str = ""
    diagnostics: dict[str, Any] | None = None
    finding: ReviewerFinding | None = None

    @property
    def is_accepted(self) -> bool:
        return self.kind in {"finding", "finding_replayed", "final"}

    @property
    def is_rejected(self) -> bool:
        return self.kind.endswith("_rejected")

    @property
    def is_capacity_rejection(self) -> bool:
        return self.kind == "finding_rejected" and self.code in {
            "finding_limit_exceeded",
            "finding_not_allowed_after_capacity",
        }

    @property
    def is_recoverable_rejection(self) -> bool:
        return self.is_capacity_rejection or (
            self.kind == "finding_rejected" and self.code in {"claim_id_conflict", "claim_role_out_of_scope"}
        )

    @property
    def is_blocking_rejection(self) -> bool:
        return self.is_rejected and not self.is_recoverable_rejection

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind, "tool_call_id": self.tool_call_id}
        if self.call_index is not None:
            payload["call_index"] = self.call_index
        if self.code:
            payload["code"] = self.code
        if self.diagnostics:
            payload["diagnostics"] = self.diagnostics
        if self.finding is not None:
            payload["finding"] = self.finding
        return payload


@dataclass(frozen=True)
class ReviewerOutputTurn:
    result: ReviewerResult | None
    events: tuple[ReviewerOutputEvent, ...]

    @property
    def accepted_findings(self) -> tuple[ReviewerFinding, ...]:
        return tuple(event.finding for event in self.events if event.kind == "finding" and event.finding is not None)

    @property
    def blocking_rejections(self) -> tuple[ReviewerOutputEvent, ...]:
        return tuple(event for event in self.events if event.is_blocking_rejection)

    @property
    def capacity_rejections(self) -> tuple[ReviewerOutputEvent, ...]:
        return tuple(event for event in self.events if event.is_capacity_rejection)

    @property
    def claim_conflict_rejections(self) -> tuple[ReviewerOutputEvent, ...]:
        return tuple(
            event
            for event in self.events
            if event.kind == "finding_rejected" and event.code == "claim_id_conflict"
        )

    @property
    def has_terminal_result(self) -> bool:
        return self.result is not None and not self.blocking_rejections


def invalidated_document_finding_claim_ids(events: tuple[ReviewerOutputEvent, ...]) -> tuple[str, ...]:
    claim_ids: list[str] = []
    for event in events:
        if event.kind != "final_rejected" or event.code != "document_consistency_finding_reconciles_conflict":
            continue
        diagnostics = event.diagnostics or {}
        value = diagnostics.get("invalid_document_finding_claim_ids")
        if not isinstance(value, list):
            continue
        for claim_id in value:
            if isinstance(claim_id, str) and claim_id.strip():
                claim_ids.append(claim_id)
    return tuple(dict.fromkeys(claim_ids))


def parse_reviewer_output_turn(
    *,
    response: Any,
    message: dict[str, Any],
    claim_units: tuple[Any, ...],
    provider: str,
    document_consistency: bool,
    implementation_readiness: bool,
    handoff: Any,
    candidate: str,
    required_candidate_claim_ids: tuple[str, ...] = (),
    collected_findings: tuple[ReviewerFinding, ...] = (),
    allow_findings: bool = True,
    validate_document_consistency: Any,
) -> ReviewerOutputTurn:
    """Parse one assistant response into independent output-tool outcomes."""

    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        artifact = getattr(response, "protocol_artifact", None)
        if artifact is None:
            artifact = classify_provider_content_artifact(provider, message.get("content"))
        if artifact is not None:
            raise ReviewerValidationError("provider_markup_artifact", {"artifact_kind": artifact.kind})
        raise ReviewerValidationError("output_tool_missing")

    events: list[ReviewerOutputEvent] = []
    local_findings: list[ReviewerFinding] = []
    finding_by_claim_id = {finding.claim_id: finding for finding in collected_findings}
    seen_tool_call_ids: set[str] = set()
    result: ReviewerResult | None = None
    final_order_invalid = False
    for index, call in enumerate(tool_calls):
        function = call.get("function") if isinstance(call, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        arguments = function.get("arguments") if isinstance(function, dict) else None
        call_id = _validate_tool_call_id(call, index, seen_tool_call_ids, local_findings)
        diagnostics = _call_diagnostics(name, index, arguments)
        if name == REVIEWER_OUTPUT_TOOL_NAME and index != len(tool_calls) - 1:
            final_order_invalid = True
            events.append(_event("final_rejected", call_id, "output_tool_final_not_last", index, {**diagnostics, "tool_call_count": len(tool_calls)}))
            continue
        if final_order_invalid:
            events.append(_event(_rejection_kind_for_name(name), call_id, "output_tool_final_not_last", index, diagnostics))
            continue
        if not isinstance(arguments, str):
            events.append(_argument_rejected_event(name, call_id, "output_tool_arguments_type_invalid", index, diagnostics))
            continue
        try:
            payload = json.loads(arguments)
        except json.JSONDecodeError as exc:
            events.append(
                _argument_rejected_event(
                    name,
                    call_id,
                    "output_tool_arguments_json_invalid",
                    index,
                    {**diagnostics, "json_error_category": type(exc).__name__},
                )
            )
            continue
        if name == REVIEWER_FINDING_TOOL_NAME:
            raw_claim_id = payload.get("claim_id") if isinstance(payload, dict) else None
            prior_finding = finding_by_claim_id.get(raw_claim_id) if isinstance(raw_claim_id, str) else None
            if prior_finding is not None:
                try:
                    finding = parse_reviewer_finding_payload(
                        payload,
                        claim_units=claim_units,
                        document_consistency=document_consistency,
                        implementation_readiness=implementation_readiness,
                    )
                except ReviewerValidationError as exc:
                    events.append(_event("finding_rejected", call_id, exc.code, index, exc.diagnostics))
                    continue
                if finding == prior_finding:
                    events.append(ReviewerOutputEvent("finding_replayed", call_id, index, finding=prior_finding))
                else:
                    events.append(
                        _event(
                            "finding_rejected",
                            call_id,
                            "claim_id_conflict",
                            index,
                            {"conflicting_claim_id_count": 1},
                        )
                    )
                continue
            if not allow_findings:
                events.append(_event("finding_rejected", call_id, "finding_not_allowed_after_capacity", index, diagnostics))
                continue
            if len(collected_findings) + len(local_findings) >= MAX_REVIEWER_FINDINGS:
                events.append(
                    _event(
                        "finding_rejected",
                        call_id,
                        "finding_limit_exceeded",
                        index,
                        {"findings_count": len(collected_findings) + len(local_findings) + 1},
                    )
                )
                continue
            try:
                finding = parse_reviewer_finding_payload(
                    payload,
                    claim_units=claim_units,
                    document_consistency=document_consistency,
                    implementation_readiness=implementation_readiness,
                )
            except ReviewerValidationError as exc:
                events.append(_event("finding_rejected", call_id, exc.code, index, exc.diagnostics))
                continue
            finding_by_claim_id[finding.claim_id] = finding
            local_findings.append(finding)
            events.append(ReviewerOutputEvent("finding", call_id, index, finding=finding))
            continue
        if name == REVIEWER_OUTPUT_TOOL_NAME:
            if any(event.is_blocking_rejection for event in events):
                events.append(_event("final_rejected", call_id, "output_tool_blocked_by_prior_rejection", index, diagnostics))
                continue
            if result is not None:
                events.append(_event("final_rejected", call_id, "output_tool_multiple_final_calls", index, {"tool_call_count": len(tool_calls)}))
                continue
            try:
                result = parse_reviewer_final_payload(
                    payload,
                    findings=(*collected_findings, *local_findings),
                    claim_units=claim_units,
                    document_consistency=document_consistency,
                    implementation_readiness=implementation_readiness,
                    evidence_ids=handoff.evidence_ids,
                    required_candidate_claim_ids=required_candidate_claim_ids,
                    handoff=handoff,
                    candidate=candidate,
                )
                validate_document_consistency(result, handoff, candidate)
            except ReviewerValidationError as exc:
                events.append(_event("final_rejected", call_id, exc.code, index, exc.diagnostics))
                continue
            events.append(ReviewerOutputEvent("final", call_id, index))
            continue
        events.append(_event("protocol_rejected", call_id, "output_tool_name_invalid", index, diagnostics))
    return ReviewerOutputTurn(result=result, events=tuple(events))


def reviewer_tool_result_messages(message: dict[str, Any], events: tuple[ReviewerOutputEvent, ...]) -> list[dict[str, Any]]:
    event_by_id = {event.tool_call_id: event for event in events}
    results: list[dict[str, Any]] = []
    for call in message.get("tool_calls") or []:
        call_id = _tool_call_id(call)
        event = event_by_id.get(call_id)
        if event is None:
            continue
        results.append({"role": "tool", "tool_call_id": call_id, "content": reviewer_tool_result_content(event)})
    return results


def reviewer_assistant_tool_message(message: dict[str, Any], events: tuple[ReviewerOutputEvent, ...]) -> dict[str, Any]:
    """Return a provider-valid assistant tool-call envelope for continuation.

    Rejected reviewer output calls still remain rejected through their tool
    result.  This only prevents malformed/native historical arguments from
    poisoning the next OpenAI-compatible request.
    """

    event_by_id = {event.tool_call_id: event for event in events}
    safe_calls: list[dict[str, Any]] = []
    for call in message.get("tool_calls") or []:
        safe_call = deepcopy(call)
        function = safe_call.get("function") if isinstance(safe_call, dict) else None
        call_id = _tool_call_id(call)
        event = event_by_id.get(call_id)
        if isinstance(function, dict) and event is not None and event.code in {
            "output_tool_arguments_type_invalid",
            "output_tool_arguments_json_invalid",
        }:
            function["arguments"] = "{}"
        safe_calls.append(safe_call)
    return {
        "role": "assistant",
        "content": message.get("content"),
        "tool_calls": safe_calls,
    }


def reviewer_tool_result_content(event: ReviewerOutputEvent) -> str:
    if event.kind == "finding":
        return "finding recorded; continue reporting findings or call submit_read_only_review"
    if event.kind == "finding_replayed":
        return "finding already recorded; replay accepted; continue reporting new findings or call submit_read_only_review"
    if event.kind == "final":
        return "review submitted"
    if event.kind == "finding_rejected":
        if event.code == "finding_limit_exceeded":
            return "finding rejected: capacity reached; finding not recorded; call submit_read_only_review now"
        if event.code == "finding_not_allowed_after_capacity":
            return "finding rejected: findings are closed; call submit_read_only_review now"
        if event.code == "claim_id_conflict":
            return (
                "finding rejected: that claim already has an immutable recorded finding; "
                "the first finding remains recorded; call submit_read_only_review now"
            )
        return f"finding rejected: {event.code}; correct that finding or submit the final verdict"
    if event.kind == "final_rejected":
        invalidated = invalidated_document_finding_claim_ids((event,))
        if invalidated:
            return (
                f"final review rejected: {event.code}; previously recorded findings for claim_ids "
                f"{','.join(invalidated)} were invalidated; resubmit corrected findings for those claim_ids, "
                "then submit the final verdict"
            )
        hint = document_consistency_rejection_hint(event.code)
        return f"final review rejected: {event.code}; submit a corrected final verdict.{hint}"
    return f"output call rejected: {event.code}; use only the reviewer output tools"


def _validate_tool_call_id(
    call: Any,
    index: int,
    seen_tool_call_ids: set[str],
    local_findings: list[ReviewerFinding],
) -> str:
    call_id = call.get("id") if isinstance(call, dict) else None
    function = call.get("function") if isinstance(call, dict) else None
    name = function.get("name") if isinstance(function, dict) else None
    diagnostics = _call_diagnostics(name, index, None)
    if not isinstance(call_id, str) or not call_id.strip():
        _raise_with_pending("output_tool_call_id_missing", diagnostics, local_findings)
    if call_id in seen_tool_call_ids:
        _raise_with_pending("output_tool_call_id_duplicate", diagnostics, local_findings)
    seen_tool_call_ids.add(call_id)
    return call_id


def _call_diagnostics(name: Any, index: int, arguments: Any) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {"call_index": index}
    diagnostics["tool_name"] = name[:80] if isinstance(name, str) else type(name).__name__
    if arguments is not None:
        diagnostics["arguments_type"] = type(arguments).__name__
    return diagnostics


def _raise_with_pending(code: str, diagnostics: dict[str, Any], local_findings: list[ReviewerFinding]) -> None:
    raise ReviewerValidationError(
        code,
        diagnostics,
        pending_candidate_claim_ids=tuple(dict.fromkeys(finding.claim_id for finding in local_findings)),
    )


def _event(kind: OutputEventKind, tool_call_id: str, code: str, call_index: int, diagnostics: dict[str, Any]) -> ReviewerOutputEvent:
    return ReviewerOutputEvent(kind=kind, tool_call_id=tool_call_id, call_index=call_index, code=code, diagnostics=diagnostics)


def _argument_rejected_event(name: Any, tool_call_id: str, code: str, call_index: int, diagnostics: dict[str, Any]) -> ReviewerOutputEvent:
    return _event(_rejection_kind_for_name(name), tool_call_id, code, call_index, diagnostics)


def _rejection_kind_for_name(name: Any) -> OutputEventKind:
    if name == REVIEWER_FINDING_TOOL_NAME:
        return "finding_rejected"
    if name == REVIEWER_OUTPUT_TOOL_NAME:
        return "final_rejected"
    return "protocol_rejected"


def _tool_call_id(call: Any) -> str:
    if isinstance(call, dict) and call.get("id"):
        return str(call["id"])
    return "review-output"

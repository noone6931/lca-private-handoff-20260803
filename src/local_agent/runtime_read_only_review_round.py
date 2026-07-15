"""Shared isolated reviewer round driver.

This module owns the provider/output lifecycle for one reviewer round.  The
runtime phase decides when a round is needed and how to act on its result.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .chat_runtime import call_chat_with_timeout
from .document_consistency import is_document_consistency_rejection_code
from .implementation_readiness import is_implementation_readiness_rejection_code
from .llm import LlmError, LlmTimeoutError
from .read_only_reviewer import MAX_REVIEWER_CAPACITY_DIRECTIVES
from .read_only_reviewer import MAX_REVIEWER_FINDINGS
from .read_only_reviewer import MAX_REVIEWER_OUTPUT_LIFECYCLE_ERRORS
from .read_only_reviewer import MAX_REVIEWER_SCHEMA_REPAIRS
from .read_only_reviewer import ReviewerFinding
from .read_only_reviewer import ReviewerResult
from .read_only_reviewer import ReviewerValidationError
from .read_only_reviewer import reviewer_output_tool_schemas
from .read_only_reviewer import reviewer_repair_message
from .provider_protocol import normalize_provider_dialect_message
from .reviewer_output_lifecycle import invalidated_document_finding_claim_ids
from .reviewer_output_lifecycle import parse_reviewer_output_turn
from .reviewer_output_lifecycle import reviewer_assistant_tool_message
from .reviewer_output_lifecycle import reviewer_tool_result_messages


@dataclass(frozen=True)
class ReviewRoundPort:
    client: Any
    provider: str
    model: str | None
    state: Any
    collector: Any
    session: Any
    events: Any


@dataclass(frozen=True)
class ReviewRoundFailure:
    reason: str
    detail: str
    handoff: Any


@dataclass(frozen=True)
class ReviewRoundOutcome:
    result: ReviewerResult | None = None
    failure: ReviewRoundFailure | None = None
    repaired: bool = False


@dataclass
class ReviewerCorrectionBudget:
    """Own all bounded correction counters for one isolated reviewer round."""

    schema_repairs: int = 0
    output_categories: dict[str, int] = field(default_factory=dict)
    capacity_directives: int = 0

    def request_schema_repair(self, code: str) -> int | None:
        if code == "finding_limit_exceeded" or self.schema_repairs >= MAX_REVIEWER_SCHEMA_REPAIRS:
            return None
        self.schema_repairs += 1
        return self.schema_repairs

    def record_output_rejections(self, events: tuple[Any, ...]) -> str | None:
        categories = tuple(dict.fromkeys(_blocking_rejection_category(event) for event in events))
        for category in categories:
            self.output_categories[category] = self.output_categories.get(category, 0) + 1
        return next(
            (
                category
                for category, count in sorted(self.output_categories.items())
                if count > MAX_REVIEWER_OUTPUT_LIFECYCLE_ERRORS
            ),
            None,
        )

    def record_capacity_rejection(self) -> bool:
        self.capacity_directives += 1
        return self.capacity_directives >= MAX_REVIEWER_CAPACITY_DIRECTIVES


def run_review_round(
    port: ReviewRoundPort,
    *,
    messages: list[dict[str, Any]],
    candidate: str,
    claim_units: tuple[Any, ...],
    handoff: Any,
    document_consistency: bool,
    implementation_readiness: bool,
    timeout: float | None,
    max_provider_turns: int,
    validate_document_consistency: Callable[[Any, Any, str], None],
    has_time_for_repair: Callable[[], bool],
    refresh_timeout: Callable[[], float | None],
    event_prefix: str = "",
) -> ReviewRoundOutcome:
    state = port.state
    saw_protocol_failure = False
    repaired_this_round = False
    required_candidate_claim_ids: tuple[str, ...] = ()
    collected_findings: tuple[ReviewerFinding, ...] = ()
    provider_turn = 0
    corrections = ReviewerCorrectionBudget()
    finding_capacity_reached = False
    finding_submission_closed = False
    while provider_turn < max_provider_turns:
        provider_turn += 1
        state.provider_attempts += 1
        port.collector.record_read_only_review_attempt()
        output_schemas = reviewer_output_tool_schemas(
            claim_units,
            document_consistency=document_consistency,
            implementation_readiness=implementation_readiness,
            evidence_ids=handoff.evidence_ids,
            include_finding_tool=(
                not finding_submission_closed
                and not finding_capacity_reached
                and len(collected_findings) < MAX_REVIEWER_FINDINGS
            ),
        )
        try:
            response = call_chat_with_timeout(
                port.client,
                messages,
                output_schemas,
                timeout=timeout,
                model=port.model,
            )
        except LlmError as exc:
            return _failure(
                "timeout" if isinstance(exc, LlmTimeoutError) else "provider_error",
                type(exc).__name__,
                handoff,
            )
        raw_message = getattr(response, "message", None)
        if isinstance(raw_message, dict):
            message, fallback_normalizations = normalize_provider_dialect_message(
                raw_message,
                provider=port.provider,
            )
        else:
            message, fallback_normalizations = raw_message, ()
        _record_provider_argument_normalizations(
            port,
            (*getattr(response, "protocol_normalizations", ()), *fallback_normalizations),
            event_prefix=event_prefix,
        )
        if not isinstance(message, dict):
            return _failure("protocol_error", "missing_message", handoff)
        try:
            turn = parse_reviewer_output_turn(
                response=response,
                message=message,
                claim_units=claim_units,
                provider=port.provider,
                document_consistency=document_consistency,
                implementation_readiness=implementation_readiness,
                handoff=handoff,
                candidate=candidate,
                required_candidate_claim_ids=required_candidate_claim_ids,
                collected_findings=collected_findings,
                allow_findings=(
                    not finding_submission_closed
                    and not finding_capacity_reached
                    and len(collected_findings) < MAX_REVIEWER_FINDINGS
                ),
                validate_document_consistency=validate_document_consistency,
            )
        except ReviewerValidationError as exc:
            accepted_claim_ids = tuple(dict.fromkeys(finding.claim_id for finding in collected_findings))
            required_candidate_claim_ids = tuple(
                dict.fromkeys((*required_candidate_claim_ids, *exc.pending_candidate_claim_ids))
            )
            if exc.code.startswith("output_tool_") or exc.code == "provider_markup_artifact":
                saw_protocol_failure = True
                state.protocol_failures += 1
                port.collector.record_read_only_review_protocol_failure()
            state.schema_failures += 1
            port.collector.record_read_only_review_schema_failure()
            diagnostic = exc.diagnostics
            repair_number = corrections.request_schema_repair(exc.code)
            if repair_number is None:
                state.repair_exhausted = True
                port.collector.record_read_only_review_repair_exhausted()
                port.session.append(
                    "read_only_reviewer",
                    {
                        "event": f"{event_prefix}schema_repair_exhausted",
                        "attempts": provider_turn,
                        "diagnostic": diagnostic,
                    },
                )
                return _failure("protocol_error" if saw_protocol_failure else "invalid_output", exc.code, handoff)
            if not has_time_for_repair():
                return _failure("deadline_or_finalization_budget", "reviewer_repair_timeout", handoff)
            state.repairs += 1
            repaired_this_round = True
            port.collector.record_read_only_review_repair()
            port.session.append(
                "read_only_reviewer",
                {
                    "event": f"{event_prefix}schema_repair_requested",
                    "attempt": provider_turn,
                    "repair": repair_number,
                    "diagnostic": diagnostic,
                    "accepted_claim_ids": list(accepted_claim_ids),
                    "required_resubmit_claim_ids": list(required_candidate_claim_ids),
                },
            )
            port.events.emit(
                "ContextUpdated",
                {
                    "kind": f"read_only_reviewer_{event_prefix}schema_repair",
                    "attempt": provider_turn,
                    "error_code": exc.code,
                },
            )
            messages.append(
                reviewer_repair_message(
                    diagnostic,
                    accepted_claim_ids=accepted_claim_ids,
                    required_resubmit_claim_ids=required_candidate_claim_ids,
                    document_consistency=document_consistency,
                    implementation_readiness=implementation_readiness,
                )
            )
            timeout = refresh_timeout()
            continue
        result = turn.result if turn.has_terminal_result else None
        for event in turn.events:
            if event.kind == "finding":
                state.typed_submits += 1
                port.collector.record_read_only_review_finding_submit()
                port.session.append(
                    "read_only_reviewer",
                    {
                        "event": f"{event_prefix}finding_submit",
                        "attempt": provider_turn,
                        "claim_id": event.finding.claim_id if event.finding is not None else "",
                    },
                )
            elif event.kind == "finding_replayed":
                port.session.append(
                    "read_only_reviewer",
                    {
                        "event": f"{event_prefix}finding_replayed",
                        "attempt": provider_turn,
                        "claim_id": event.finding.claim_id if event.finding is not None else "",
                    },
                )
            elif event.kind == "final":
                state.typed_submits += 1
                port.collector.record_read_only_review_final_submit()
                port.session.append(
                    "read_only_reviewer",
                    {"event": f"{event_prefix}final_submit", "attempt": provider_turn},
                )
            elif event.kind == "finding_rejected":
                state.rejected_finding_submits += 1
                port.collector.record_read_only_review_rejected_finding_submit()
                code = event.code or "finding_rejected"
                if code in {"finding_limit_exceeded", "finding_not_allowed_after_capacity"}:
                    finding_capacity_reached = True
                    state.finding_limit_hits += 1
                    port.collector.record_read_only_review_finding_limit_hit()
                port.session.append(
                    "read_only_reviewer",
                    {
                        "event": f"{event_prefix}finding_rejected",
                        "attempt": provider_turn,
                        "code": code,
                        "call_index": event.call_index,
                    },
                )
            elif event.kind == "final_rejected":
                state.rejected_final_submits += 1
                port.collector.record_read_only_review_rejected_final_submit()
                port.session.append(
                    "read_only_reviewer",
                    {
                        "event": f"{event_prefix}final_rejected",
                        "attempt": provider_turn,
                        "code": event.code or "final_rejected",
                        "call_index": event.call_index,
                    },
                )
            elif event.kind == "protocol_rejected":
                saw_protocol_failure = True
                state.protocol_failures += 1
                port.collector.record_read_only_review_protocol_failure()
                port.session.append(
                    "read_only_reviewer",
                    {
                        "event": f"{event_prefix}protocol_rejected",
                        "attempt": provider_turn,
                        "code": event.code or "protocol_rejected",
                        "call_index": event.call_index,
                    },
                )
        if result is None:
            if not finding_submission_closed and any(
                event.kind == "final_rejected"
                and event.code in {"output_tool_arguments_type_invalid", "output_tool_arguments_json_invalid"}
                for event in turn.events
            ):
                finding_submission_closed = True
                port.session.append(
                    "read_only_reviewer",
                    {
                        "event": f"{event_prefix}finding_submission_closed",
                        "attempt": provider_turn,
                        "reason": "final_arguments_invalid",
                    },
                )
            if not finding_submission_closed and turn.claim_conflict_rejections:
                finding_submission_closed = True
                port.session.append(
                    "read_only_reviewer",
                    {
                        "event": f"{event_prefix}finding_submission_closed",
                        "attempt": provider_turn,
                        "reason": "claim_id_conflict",
                    },
                )
            exhausted_category = corrections.record_output_rejections(turn.blocking_rejections)
            for category in tuple(dict.fromkeys(_blocking_rejection_category(event) for event in turn.blocking_rejections)):
                port.collector.record_read_only_review_output_lifecycle_correction(category)
            capacity_exhausted = corrections.record_capacity_rejection() if turn.capacity_rejections else False
            invalidated_claim_ids = invalidated_document_finding_claim_ids(turn.events)
            collected_findings = (*collected_findings, *turn.accepted_findings)
            if invalidated_claim_ids:
                invalidated_set = set(invalidated_claim_ids)
                before_count = len(collected_findings)
                collected_findings = tuple(
                    finding for finding in collected_findings if finding.claim_id not in invalidated_set
                )
                invalidated_count = before_count - len(collected_findings)
                if invalidated_count:
                    state.invalidated_finding_submits += invalidated_count
                    port.collector.record_read_only_review_invalidated_finding_submit(invalidated_count)
                    port.session.append(
                        "read_only_reviewer",
                        {
                            "event": f"{event_prefix}finding_invalidated",
                            "attempt": provider_turn,
                            "code": "document_consistency_finding_reconciles_conflict",
                            "claim_ids": list(invalidated_claim_ids),
                        },
                    )
                required_candidate_claim_ids = tuple(
                    dict.fromkeys((*required_candidate_claim_ids, *invalidated_claim_ids))
                )
                finding_capacity_reached = False
                finding_submission_closed = False
            if len(collected_findings) >= MAX_REVIEWER_FINDINGS:
                finding_capacity_reached = True
            messages.append(reviewer_assistant_tool_message(message, turn.events))
            messages.extend(
                reviewer_tool_result_messages(
                    message,
                    turn.events,
                    document_consistency=document_consistency,
                    implementation_readiness=implementation_readiness,
                )
            )
            if capacity_exhausted:
                state.output_lifecycle_exhausted = True
                port.collector.record_read_only_review_output_lifecycle_exhausted()
                port.session.append(
                    "read_only_reviewer",
                    {
                        "event": f"{event_prefix}output_lifecycle_exhausted",
                        "attempts": provider_turn,
                        "capacity_directives": corrections.capacity_directives,
                    },
                )
                return _failure("invalid_output", "output_lifecycle_exhausted", handoff)
            if exhausted_category is not None:
                state.output_lifecycle_exhausted = True
                port.collector.record_read_only_review_output_lifecycle_exhausted()
                port.collector.record_read_only_review_output_lifecycle_exhausted_category(exhausted_category)
                port.session.append(
                    "read_only_reviewer",
                    {
                        "event": f"{event_prefix}output_lifecycle_exhausted",
                        "attempts": provider_turn,
                        "category": exhausted_category,
                        "corrections": dict(sorted(corrections.output_categories.items())),
                    },
                )
                return _failure(
                    "protocol_error" if saw_protocol_failure else "invalid_output",
                    "output_lifecycle_exhausted",
                    handoff,
                )
            if provider_turn >= max_provider_turns:
                state.output_lifecycle_exhausted = True
                port.collector.record_read_only_review_output_lifecycle_exhausted()
                return _failure(
                    "protocol_error" if saw_protocol_failure else "invalid_output",
                    "output_lifecycle_exhausted",
                    handoff,
                )
            continue
        return ReviewRoundOutcome(result=result, repaired=repaired_this_round)
    return ReviewRoundOutcome()


def _failure(reason: str, detail: str, handoff: Any) -> ReviewRoundOutcome:
    return ReviewRoundOutcome(failure=ReviewRoundFailure(reason, detail, handoff))


def _record_provider_argument_normalizations(
    port: ReviewRoundPort,
    artifacts: tuple[Any, ...],
    *,
    event_prefix: str,
) -> None:
    if not artifacts:
        return
    port.collector.record_provider_argument_normalization(len(artifacts))
    for artifact in artifacts:
        port.session.append(
            "provider_argument_normalization",
            {
                "phase": f"read_only_reviewer_{event_prefix or 'initial'}",
                "kind": artifact.kind,
                "tool_name": artifact.tool_name,
                "parameter_names": list(artifact.parameter_names),
                "preview": artifact.preview,
            },
        )


def _blocking_rejection_category(event: Any) -> str:
    code = getattr(event, "code", "") or ""
    if code.startswith("output_tool_arguments_"):
        return "arguments"
    if is_document_consistency_rejection_code(code):
        return "document_consistency"
    if is_implementation_readiness_rejection_code(code):
        return "implementation_readiness"
    return "protocol"

"""Runtime phase for one isolated, read-only evidence review per run."""
from __future__ import annotations

import time
from typing import Any, Protocol

from .document_consistency import validate_document_consistency_assessment
from .explore_handoff import build_explore_handoff
from .provider_protocol import classify_provider_content_artifact
from .read_only_reviewer import candidate_claim_units
from .read_only_reviewer import candidate_claim_projection_issues
from .read_only_reviewer import MAX_REVIEWER_FINDINGS
from .read_only_reviewer import MAX_REVIEWER_SCHEMA_REPAIRS
from .read_only_reviewer import MAX_TRANSPORT_RESIDUAL_PROJECTION_ROUNDS
from .read_only_reviewer import prune_exact_transport_residual_claim_lines
from .read_only_reviewer import ReviewerPhaseOutcome
from .read_only_reviewer import ReviewerResult
from .read_only_reviewer import ReviewerValidationError
from .read_only_reviewer import reviewer_messages
from .read_only_reviewer import reviewer_transport_rewrite_message
from .read_only_reviewer import reviewer_rewrite_message
from .read_only_reviewer import rewrite_complies_with_review
from .read_only_reviewer import should_review_read_only_candidate
from .runtime_read_only_review_round import ReviewRoundPort
from .runtime_read_only_review_round import run_review_round
from .safe_partial_report import build_safe_partial_report
from .steering.pre_review import PreReviewAudit
from .steering.pre_review import collect_pre_review_audit


MAX_REVIEWER_TIMEOUT_SECONDS = 45.0


class ReadOnlyReviewRuntimePort(Protocol):
    _client: Any
    _config: Any
    _events: Any
    _run: Any
    _session: Any
    _tool_context: Any
    _workspace_context: Any
    _provider_context_phase: Any


class ReadOnlyReviewPhase:
    """Own reviewer lifecycle, isolated context, and reviewer telemetry.

    The primary Runtime remains responsible for appending a returned rewrite
    instruction to its conversation.  The reviewer itself never receives or
    writes the primary transcript.
    """

    def __init__(self, runtime: ReadOnlyReviewRuntimePort) -> None:
        self._runtime = runtime
        self._preparation_audit: PreReviewAudit | None = None
        self._preparation_rewrite_requested = False
        self._preparation_rewrite_accepted = False

    def begin_run(self) -> None:
        self._runtime._run.read_only_review.reset()
        self._preparation_audit = None
        self._preparation_rewrite_requested = False
        self._preparation_rewrite_accepted = False

    def set_preparation_audit(self, audit: PreReviewAudit | None) -> None:
        """Refresh pure deterministic findings for the current candidate."""

        self._preparation_audit = audit

    def refresh_preparation_audit(self, context: Any, steerers: Any) -> PreReviewAudit | None:
        """Collect only before semantic review; rewrites keep their existing deterministic closure."""

        state = self._runtime._run.read_only_review
        if state.attempted:
            self._preparation_audit = None
        else:
            self._preparation_audit = collect_pre_review_audit(
                context,
                steerers,
            )
        return self._preparation_audit

    def owns_pending_candidate_validation(self) -> bool:
        """Whether the next no-tool candidate belongs to an active review directive."""

        return self._preparation_rewrite_requested or self._runtime._run.read_only_review.transport_rewrite_requested

    def owns_initial_pre_review_audits(self) -> bool:
        return not self._runtime._run.read_only_review.attempted

    def review_candidate(self, candidate: str) -> ReviewerPhaseOutcome:
        runtime = self._runtime
        state = runtime._run.read_only_review
        contract = runtime._run.requirement_contract
        request = runtime._run.current_user_request
        if not should_review_read_only_candidate(contract, request):
            return ReviewerPhaseOutcome("not_applicable")
        if state.rewrite_accepted:
            return ReviewerPhaseOutcome("pass")
        if state.rewrite_requested:
            return self._verify_rewrite_candidate(candidate)
        if state.attempted:
            return ReviewerPhaseOutcome("pass")
        return self._review(candidate)

    def safe_partial_for_terminal(self, reason: str) -> str:
        """Return trusted observations for a bounded non-final stop, if applicable."""

        runtime = self._runtime
        contract = runtime._run.requirement_contract
        if not should_review_read_only_candidate(contract, runtime._run.current_user_request):
            return ""
        handoff = self._handoff()
        if not handoff.items:
            return ""
        return self._emit_safe_partial(handoff, reason)

    def _review(self, candidate: str) -> ReviewerPhaseOutcome:
        runtime = self._runtime
        state = runtime._run.read_only_review
        contract = runtime._run.requirement_contract
        projection_issues = candidate_claim_projection_issues(candidate)
        if projection_issues:
            return self._unverified("invalid_output", projection_issues[0].code)
        claim_units = candidate_claim_units(candidate)
        if not claim_units:
            return self._unverified("invalid_output", "candidate_has_no_addressable_claim_units")
        handoff = self._handoff(candidate, claim_units=claim_units)
        candidate, claim_units, handoff, omitted_claim_ids, projected = self._project_transport_candidate(
            candidate, claim_units, handoff
        )
        preparation_outcome = self._resolve_candidate_preparation(handoff, omitted_claim_ids, claim_units)
        if preparation_outcome is not None:
            return preparation_outcome
        skip_reason = runtime._run.finalization_rewrite_skip_reason()
        if skip_reason is not None:
            return self._unverified("deadline_or_finalization_budget", skip_reason, handoff=handoff)
        self._accept_candidate_preparation(handoff, claim_units)
        state.attempted = True
        state.review_round += 1
        state.claim_units = claim_units
        timeout = self._review_timeout()
        max_repairs = MAX_REVIEWER_SCHEMA_REPAIRS
        max_provider_turns = MAX_REVIEWER_FINDINGS + 1 + max_repairs
        runtime._run.collector.record_read_only_review_trigger()
        runtime._session.append(
            "read_only_reviewer",
            {
                "event": "triggered",
                "run_id": runtime._run.run_id,
                "items": len(handoff.items),
                "timeout_seconds": timeout,
                "review_round": state.review_round,
                "model_role": "reviewer",
                "model": runtime._config.reviewer_model or runtime._config.model,
            },
        )
        runtime._events.emit(
            "ContextUpdated",
            {"kind": "read_only_reviewer_triggered", "items": len(handoff.items)},
        )
        messages = reviewer_messages(handoff, claim_units)
        document_consistency = contract.evidence_domain == "requirement_documents" and contract.read_only_review_profile == "document_consistency"
        implementation_readiness = bool(
            contract.implementation_readiness_required
            and contract.evidence_domain == "repository_code"
            and contract.read_only_review_profile in {"owner_impact", "design"}
        )
        round_outcome = run_review_round(
            self._round_port(),
            messages=messages,
            candidate=candidate,
            claim_units=claim_units,
            handoff=handoff,
            document_consistency=document_consistency,
            implementation_readiness=implementation_readiness,
            timeout=timeout,
            max_provider_turns=max_provider_turns,
            validate_document_consistency=self._validate_document_consistency,
            has_time_for_repair=self._has_reviewer_time_for_repair,
            refresh_timeout=self._review_timeout,
        )
        if round_outcome.failure is not None:
            failure = round_outcome.failure
            return self._unverified(failure.reason, failure.detail, handoff=failure.handoff)
        result = round_outcome.result
        if result is None:
            return self._unverified("invalid_output", "schema_repair_exhausted", handoff=handoff)
        if round_outcome.repaired:
            state.repair_success = True
            runtime._run.collector.record_read_only_review_repair_success()
        state.verdict = result.verdict
        state.reason = result.reason
        state.findings = result.findings
        state.document_consistency = result.document_consistency
        state.implementation_readiness = result.implementation_readiness
        state.review_handoff = handoff
        state.document_consistency_handoff_signature = (
            self._handoff_signature(handoff) if result.document_consistency is not None else ()
        )
        runtime._run.collector.record_read_only_review_result(result.verdict, len(result.findings))
        runtime._session.append("read_only_reviewer", {"event": "result", **result.to_dict()})
        runtime._events.emit(
            "ContextUpdated",
            {
                "kind": "read_only_reviewer_result",
                "verdict": result.verdict,
                "findings": len(result.findings),
                "review_round": state.review_round,
            },
        )
        if result.verdict == "pass":
            return ReviewerPhaseOutcome("pass", final_candidate=candidate if projected else "")
        state.rewrite_closure_findings = result.findings
        if not runtime._run.queue_finalization_rewrite(kind="read_only_reviewer"):
            return self._unverified(
                "rewrite_unavailable",
                runtime._run.finalization_rewrite_skip_reason() or "finalization_limit",
                result=result,
                handoff=handoff,
            )
        state.rewrite_requested = True
        runtime._run.collector.record_read_only_review_rewrite()
        runtime._session.append("read_only_reviewer", {"event": "rewrite_queued", "verdict": result.verdict})
        return ReviewerPhaseOutcome(
            "rewrite",
            rewrite_message=reviewer_rewrite_message(result, profile=contract.read_only_review_profile, handoff=handoff),
        )

    def _resolve_candidate_preparation(
        self,
        handoff: Any,
        omitted_claim_ids: set[str],
        claim_units: tuple[Any, ...],
    ) -> ReviewerPhaseOutcome | None:
        runtime = self._runtime
        state = runtime._run.read_only_review
        audit = self._preparation_audit
        if audit is None and not omitted_claim_ids:
            return None
        detail = f"omitted_claims={len(omitted_claim_ids)}"
        preparation_available = not (
            self._preparation_rewrite_requested
            or self._preparation_rewrite_accepted
            or state.transport_rewrite_accepted
        )
        if preparation_available and runtime._run.queue_finalization_rewrite(
            kind="read_only_reviewer_candidate_preparation"
        ):
            self._preparation_rewrite_requested = True
            if audit is not None:
                runtime._run.collector.record_pre_review_audit(categories=audit.categories, exhausted=False)
            if omitted_claim_ids:
                state.transport_rewrite_requested = True
                runtime._run.collector.record_read_only_review_claim_transport_rewrite()
            state.transport_original_omitted_count = len(omitted_claim_ids)
            runtime._session.append(
                "read_only_reviewer",
                {
                    "event": "candidate_preparation_rewrite_queued",
                    "audit_categories": list(audit.categories if audit is not None else ()),
                    "omitted_claims": len(omitted_claim_ids),
                },
            )
            runtime._events.emit(
                "ContextUpdated",
                {
                    "kind": "read_only_reviewer_candidate_preparation_rewrite_queued",
                    "omitted_claims": len(omitted_claim_ids),
                },
            )
            messages = ["Runtime candidate preparation: rewrite once without tools before isolated review."]
            if audit is not None:
                messages.append(audit.render())
            if omitted_claim_ids:
                messages.append(
                    reviewer_transport_rewrite_message(
                        handoff=handoff,
                        omitted_claim_ids=tuple(sorted(omitted_claim_ids)),
                        claim_units=claim_units,
                    )
                )
            return ReviewerPhaseOutcome(
                "rewrite",
                rewrite_message="\n\n".join(messages),
                reason="claim_evidence_transport_incomplete" if omitted_claim_ids else "pre_review_audit",
            )
        if audit is not None:
            runtime._run.collector.record_pre_review_audit_exhausted(audit.categories)
        if omitted_claim_ids and not state.transport_rewrite_exhausted:
            state.transport_rewrite_exhausted = True
            runtime._run.collector.record_read_only_review_claim_transport_rewrite_exhausted()
        runtime._session.append(
            "read_only_reviewer",
            {
                "event": "candidate_preparation_rewrite_exhausted",
                "audit_categories": list(audit.categories if audit is not None else ()),
                "omitted_claims": len(omitted_claim_ids),
                "detail": runtime._run.finalization_rewrite_skip_reason() or "preparation_rewrite_already_used",
            },
        )
        return self._unverified(
            "claim_evidence_transport_incomplete" if omitted_claim_ids else "pre_review_audit_unverified",
            detail if omitted_claim_ids else ",".join(audit.categories if audit is not None else ()),
            handoff=handoff,
        )

    def _accept_candidate_preparation(self, handoff: Any, claim_units: tuple[Any, ...]) -> None:
        runtime = self._runtime
        state = runtime._run.read_only_review
        if not self._preparation_rewrite_requested or self._preparation_rewrite_accepted:
            return
        self._preparation_rewrite_requested = False
        self._preparation_rewrite_accepted = True
        if state.transport_rewrite_requested:
            state.transport_rewrite_requested = False
            state.transport_rewrite_accepted = True
            runtime._run.collector.record_read_only_review_claim_transport_rewrite_acceptance()
        runtime._session.append(
            "read_only_reviewer",
            {
                "event": "candidate_preparation_rewrite_accepted",
                "claim_units": len(claim_units),
                "items": len(handoff.items),
            },
        )
        runtime._events.emit(
            "ContextUpdated",
            {"kind": "read_only_reviewer_candidate_preparation_rewrite_accepted", "items": len(handoff.items)},
        )

    def _verify_rewrite_candidate(self, candidate: str) -> ReviewerPhaseOutcome:
        runtime = self._runtime
        state = runtime._run.read_only_review
        projection_issues = candidate_claim_projection_issues(candidate)
        if projection_issues:
            return self._unverified("invalid_output", projection_issues[0].code)
        claim_units = candidate_claim_units(candidate)
        if not claim_units:
            return self._unverified("invalid_output", "candidate_has_no_addressable_claim_units")
        handoff = self._handoff(candidate, claim_units=claim_units)
        candidate, claim_units, handoff, omitted_claim_ids, projected = self._project_transport_candidate(
            candidate, claim_units, handoff
        )
        preparation_outcome = self._resolve_candidate_preparation(handoff, omitted_claim_ids, claim_units)
        if preparation_outcome is not None:
            return preparation_outcome
        self._accept_candidate_preparation(handoff, claim_units)
        artifact = classify_provider_content_artifact(runtime._config.provider, candidate)
        if artifact is not None:
            return self._unverified("protocol_error", f"provider_markup_artifact:{artifact.kind}", handoff=handoff)
        state.rewrite_closure_checks += 1
        runtime._run.collector.record_read_only_review_rewrite_closure_check()
        if state.document_consistency is not None:
            if state.findings and not rewrite_complies_with_review(candidate, state.claim_units, state.findings):
                return self._queue_rewrite_correction_or_unverified(
                    "reviewed_claim_not_closed",
                    handoff=handoff,
                    rewrite_message=self._rewrite_correction_message(),
                )
            original_handoff = state.review_handoff
            if original_handoff is None:
                return self._unverified("invalid_output", "document_consistency_handoff_missing", handoff=handoff)
            code = validate_document_consistency_assessment(
                state.document_consistency,
                original_handoff,
                candidate=candidate,
                verdict="pass",
            )
            if code is not None:
                return self._unverified("rewrite_noncompliant", code, handoff=original_handoff)
        elif state.findings:
            closure_findings = state.rewrite_closure_findings or state.findings
            if not rewrite_complies_with_review(candidate, state.claim_units, closure_findings):
                return self._queue_rewrite_correction_or_unverified(
                    "reviewed_claim_not_closed",
                    handoff=handoff,
                    rewrite_message=self._rewrite_correction_message(),
                )
        state.rewrite_accepted = True
        state.rewrite_requested = False
        state.rewrite_closure_acceptances += 1
        state.reason = "deterministic_closure_accepted"
        runtime._run.collector.record_read_only_review_rewrite_closure_acceptance()
        runtime._run.collector.record_read_only_review_rewrite_acceptance()
        runtime._session.append(
            "read_only_reviewer",
            {
                "event": "rewrite_accepted",
                "review_round": state.review_round,
                "claim_units": len(claim_units),
                "items": len(handoff.items),
            },
        )
        runtime._events.emit(
            "ContextUpdated",
            {"kind": "read_only_reviewer_rewrite_accepted", "items": len(handoff.items)},
        )
        return ReviewerPhaseOutcome("pass", final_candidate=candidate if projected else "")

    def _project_transport_candidate(
        self,
        candidate: str,
        claim_units: tuple[Any, ...],
        handoff: Any,
    ) -> tuple[str, tuple[Any, ...], Any, set[str], bool]:
        runtime = self._runtime
        state = runtime._run.read_only_review
        omitted_claim_ids = set(getattr(handoff, "transport_omitted_claim_ids", ()) or ())
        if not omitted_claim_ids:
            return candidate, claim_units, handoff, set(), False
        improving_transport_rewrite = (
            state.transport_rewrite_requested
            and state.transport_original_omitted_count > len(omitted_claim_ids)
        )
        reviewer_rewrite_residual = state.rewrite_requested and state.transport_rewrite_accepted
        can_project = (
            state.transport_projection_rounds < MAX_TRANSPORT_RESIDUAL_PROJECTION_ROUNDS
            and (improving_transport_rewrite or reviewer_rewrite_residual)
        )
        if can_project:
            projected_candidate, pruned_ids = prune_exact_transport_residual_claim_lines(
                candidate,
                claim_units,
                omitted_claim_ids,
            )
            if pruned_ids:
                projected_units = candidate_claim_units(projected_candidate)
                projected_handoff = self._handoff(projected_candidate, claim_units=projected_units)
                if not getattr(projected_handoff, "transport_omitted_claim_ids", ()):
                    state.transport_pruned_claim_ids = (*state.transport_pruned_claim_ids, *pruned_ids)
                    state.transport_projection_rounds += 1
                    runtime._run.collector.record_read_only_review_claim_transport_pruned(len(pruned_ids))
                    runtime._session.append(
                        "read_only_reviewer",
                        {
                            "event": "claim_transport_residual_pruned",
                            "claim_ids": list(pruned_ids),
                            "projection_round": state.transport_projection_rounds,
                        },
                    )
                    runtime._events.emit(
                        "ContextUpdated",
                        {
                            "kind": "read_only_reviewer_claim_transport_residual_pruned",
                            "claims": len(pruned_ids),
                            "projection_round": state.transport_projection_rounds,
                        },
                    )
                    return projected_candidate, projected_units, projected_handoff, set(), True
        return candidate, claim_units, handoff, omitted_claim_ids, False

    def _queue_rewrite_correction_or_unverified(
        self,
        reason: str,
        *,
        handoff: Any,
        rewrite_message: str,
        result: ReviewerResult | None = None,
    ) -> ReviewerPhaseOutcome:
        runtime = self._runtime
        state = runtime._run.read_only_review
        if state.rewrite_corrections < 1 and runtime._run.queue_finalization_rewrite(kind="read_only_reviewer_rewrite_correction"):
            state.rewrite_corrections += 1
            runtime._run.collector.record_read_only_review_rewrite()
            runtime._run.collector.record_read_only_review_rewrite_correction()
            runtime._session.append(
                "read_only_reviewer",
                {
                    "event": "rewrite_correction_queued",
                    "reason": reason,
                    "correction": state.rewrite_corrections,
                },
            )
            return ReviewerPhaseOutcome(
                "rewrite",
                rewrite_message=rewrite_message,
                reason=reason,
            )
        return self._unverified("rewrite_noncompliant", reason, result=result, handoff=handoff)

    def _rewrite_correction_message(self) -> str:
        state = self._runtime._run.read_only_review
        result = ReviewerResult(
            verdict="revise",
            confidence=1.0,
            findings=state.findings,
            reason=state.reason or "rewrite_correction",
            document_consistency=state.document_consistency,
            implementation_readiness=state.implementation_readiness,
        )
        base = reviewer_rewrite_message(
            result,
            profile=self._runtime._run.requirement_contract.read_only_review_profile,
            handoff=state.review_handoff,
        )
        return (
            base
            + "\n\n[Rewrite correction]\n"
            "The previous rewrite left at least one addressed reviewed claim unchanged. Rewrite again without tools and "
            "close every addressed reviewed claim: remove it, downgrade it to unverified/unlocated/proposal/pending "
            "confirmation, or restate it with the typed unresolved artifact disposition. Do not return the same wording."
        )

    def _validate_document_consistency(self, result: Any, handoff: Any, candidate: str) -> None:
        if result.document_consistency is None:
            return
        code = validate_document_consistency_assessment(
            result.document_consistency,
            handoff,
            candidate=candidate,
            verdict=result.verdict,
        )
        if code is not None:
            self._runtime._run.read_only_review.document_consistency = None
            self._runtime._run.read_only_review.document_consistency_handoff_signature = ()
            raise ReviewerValidationError(code)

    def _round_port(self) -> ReviewRoundPort:
        runtime = self._runtime
        return ReviewRoundPort(
            client=runtime._client,
            provider=runtime._config.provider,
            model=runtime._config.reviewer_model or None,
            state=runtime._run.read_only_review,
            collector=runtime._run.collector,
            session=runtime._session,
            events=runtime._events,
            cancel_event=runtime._tool_context.cancel_event,
        )

    def _review_timeout(self) -> float | None:
        runtime = self._runtime
        run = runtime._run
        remaining = runtime._provider_context_phase.remaining_timeout(run.deadline_monotonic)
        if run.deadline_monotonic is None:
            return max(0.1, min(MAX_REVIEWER_TIMEOUT_SECONDS, remaining))
        reserve = run.finalization.effective_reserve_seconds(
            deadline_monotonic=run.deadline_monotonic,
            run_started_monotonic=run.started_monotonic,
        )
        available = max(0.1, run.deadline_monotonic - time.monotonic() - reserve)
        return max(0.1, min(MAX_REVIEWER_TIMEOUT_SECONDS, remaining, available))

    def _has_reviewer_time_for_repair(self) -> bool:
        remaining = self._runtime._provider_context_phase.remaining_timeout(self._runtime._run.deadline_monotonic)
        return remaining is None or remaining > 0.1

    def _unverified(
        self,
        reason: str,
        detail: str,
        *,
        result: Any = None,
        handoff: Any = None,
    ) -> ReviewerPhaseOutcome:
        runtime = self._runtime
        state = runtime._run.read_only_review
        state.verdict = "unverified"
        state.reason = reason
        if result is not None:
            state.findings = result.findings
        runtime._run.collector.record_read_only_review_error(reason)
        runtime._session.append(
            "read_only_reviewer",
            {"event": "unverified", "reason": reason, "detail": detail},
        )
        runtime._events.emit("ErrorEvent", {"kind": "read_only_reviewer", "reason": reason})
        safe_partial_report = self._emit_safe_partial(handoff, reason) if handoff is not None else ""
        return ReviewerPhaseOutcome(
            "unverified",
            terminal_message=safe_partial_report or (
                "未完成/未验证：独立只读证据审查未能确认该候选答复。"
                f"原因：{reason}（{detail}）。未将未经审查的草稿作为最终结论返回。"
            ),
            reason=reason,
            safe_partial_report=safe_partial_report,
        )

    def _handoff(self, candidate: str | None = None, *, claim_units: tuple[Any, ...] = ()):
        runtime = self._runtime
        return build_explore_handoff(
            request=runtime._run.current_user_request or "",
            contract=runtime._run.requirement_contract,
            requirement_evidence=runtime._run.evidence.pinned_requirement_evidence,
            source_evidence=runtime._run.evidence.source_evidence,
            records=runtime._run.evidence.records,
            tool_results=runtime._run.tool_choice_results,
            candidate=candidate,
            claim_units=claim_units,
        )

    def _emit_safe_partial(self, handoff: Any, reason: str) -> str:
        runtime = self._runtime
        state = runtime._run.read_only_review
        if state.safe_partial_emitted:
            return ""
        document_consistency = state.document_consistency
        if (
            document_consistency is not None
            and state.document_consistency_handoff_signature != self._handoff_signature(handoff)
        ):
            document_consistency = None
        partial = build_safe_partial_report(
            handoff,
            state.findings,
            reason=reason,
            document_consistency=document_consistency,
            implementation_readiness=state.implementation_readiness,
        )
        state.safe_partial_emitted = True
        runtime._run.collector.record_safe_partial_report(
            observations=partial.observation_count,
            missing=partial.missing_count,
            rejected_categories=partial.rejected_categories,
        )
        runtime._session.append(
            "safe_partial_report",
            {
                "reason": reason,
                "observations": partial.observation_count,
                "missing": partial.missing_count,
                "rejected_categories": list(partial.rejected_categories),
                "delivery_status": partial.delivery_status,
            },
        )
        return partial.content

    def _handoff_signature(self, handoff: Any) -> tuple[tuple[str, ...], ...]:
        return tuple(
            (
                str(getattr(item, "evidence_id", "")),
                str(getattr(item, "classification", "")),
                str(getattr(item, "tool", "")),
                str(getattr(item, "path", "")),
                str(getattr(item, "root", "")),
                str(getattr(item, "scope", "")),
                str(getattr(item, "outcome", "")),
                str(getattr(item, "identity_path", "")),
                str(getattr(item, "summary", "")),
            )
            for item in getattr(handoff, "items", ())
        )

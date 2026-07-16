from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Mapping


READ_ONLY_REVIEWER_LIFECYCLE_CATEGORIES = frozenset(
    {"arguments", "document_consistency", "implementation_readiness", "protocol"}
)


@dataclass
class RunStats:
    run_id: str
    prompt_chars: int
    started_monotonic: float
    workflow_profile: dict[str, Any] = field(default_factory=dict)
    llm_requests: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    useless_tool_results: int = 0
    synthetic_tool_results: int = 0
    compactions: int = 0
    effective_compactions: int = 0
    zero_gain_compactions: int = 0
    consecutive_zero_gain_compactions: int = 0
    max_consecutive_zero_gain_compactions: int = 0
    compaction_estimated_token_reduction: int = 0
    compaction_checkpoints: int = 0
    compaction_checkpoint_reused: int = 0
    llm_context_summaries: int = 0
    local_context_summaries: int = 0
    file_discovery_calls: int = 0
    file_discovery_incomplete_results: int = 0
    file_discovery_no_match_results: int = 0
    unknown_tool_calls: int = 0
    unknown_tool_suggestions: int = 0
    filename_search_misuse_calls: int = 0
    provider_schema_violations: int = 0
    provider_protocol_violations: int = 0
    forced_final_protocol_violations: int = 0
    forced_final_protocol_recoveries: int = 0
    forced_final_protocol_recovery_exhausted: int = 0
    forced_final_structured_tool_calls: int = 0
    provider_markup_artifacts: int = 0
    provider_argument_normalizations: int = 0
    suppressed_tool_executions: int = 0
    session_evidence_hits: int = 0
    session_evidence_misses: int = 0
    session_evidence_stale: int = 0
    session_evidence_invalidations: int = 0
    session_evidence_reused_paths: list[str] = field(default_factory=list)
    session_evidence_directives: int = 0
    session_evidence_model_rereads: int = 0
    read_only_reviewer_triggers: int = 0
    read_only_reviewer_rewrites: int = 0
    read_only_reviewer_rewrite_acceptances: int = 0
    read_only_reviewer_rewrite_corrections: int = 0
    read_only_reviewer_rewrite_closure_checks: int = 0
    read_only_reviewer_rewrite_closure_acceptances: int = 0
    read_only_reviewer_rewrite_verification_rounds: int = 0
    read_only_reviewer_claim_transport_rewrites: int = 0
    read_only_reviewer_claim_transport_rewrite_acceptances: int = 0
    read_only_reviewer_claim_transport_rewrite_exhausted: int = 0
    read_only_reviewer_claim_transport_pruned_claims: int = 0
    read_only_reviewer_claim_transport_projection_rounds: int = 0
    read_only_reviewer_findings: int = 0
    read_only_reviewer_reviewed_claims: int = 0
    read_only_reviewer_attempts: int = 0
    read_only_reviewer_schema_failures: int = 0
    read_only_reviewer_repairs: int = 0
    read_only_reviewer_repair_successes: int = 0
    read_only_reviewer_repair_exhausted: int = 0
    read_only_reviewer_typed_submits: int = 0
    read_only_reviewer_finding_submits: int = 0
    read_only_reviewer_final_submits: int = 0
    read_only_reviewer_protocol_failures: int = 0
    read_only_reviewer_rejected_finding_submits: int = 0
    read_only_reviewer_rejected_final_submits: int = 0
    read_only_reviewer_finding_limit_hits: int = 0
    read_only_reviewer_invalidated_finding_submits: int = 0
    read_only_reviewer_output_lifecycle_exhausted: int = 0
    read_only_reviewer_argument_lifecycle_corrections: int = 0
    read_only_reviewer_document_lifecycle_corrections: int = 0
    read_only_reviewer_implementation_readiness_lifecycle_corrections: int = 0
    read_only_reviewer_protocol_lifecycle_corrections: int = 0
    read_only_reviewer_lifecycle_exhausted_categories: dict[str, int] = field(default_factory=dict)
    read_only_reviewer_verdicts: dict[str, int] = field(default_factory=dict)
    read_only_reviewer_errors: dict[str, int] = field(default_factory=dict)
    pre_review_audit_rounds: int = 0
    pre_review_audit_categories: dict[str, int] = field(default_factory=dict)
    pre_review_audit_exhausted: int = 0
    safe_partial_reports: int = 0
    safe_partial_observations: int = 0
    safe_partial_missing: int = 0
    safe_partial_rejected_categories: dict[str, int] = field(default_factory=dict)
    tool_choice_exact_forces: int = 0
    tool_choice_exact_exhausted: int = 0
    tool_counts: dict[str, int] = field(default_factory=dict)
    guard_start: dict[str, int] = field(default_factory=dict)
    steer_start: dict[str, int] = field(default_factory=dict)


class RunCollector:
    """Own per-run telemetry while Runtime owns event/session delivery."""

    def __init__(self) -> None:
        self._stats: RunStats | None = None
        self._pending_compaction_summary_mode: str | None = None

    def start(
        self,
        run_id: str,
        prompt: str,
        started_monotonic: float,
        *,
        guard_start: dict[str, int],
        steer_start: dict[str, int],
        workflow_profile: Mapping[str, Any] | None = None,
    ) -> None:
        self._stats = RunStats(
            run_id=run_id,
            prompt_chars=len(prompt),
            started_monotonic=started_monotonic,
            workflow_profile=dict(workflow_profile or {}),
            guard_start=dict(guard_start),
            steer_start=dict(steer_start),
        )
        self._pending_compaction_summary_mode = None

    def record_llm_request(self) -> None:
        if self._stats is not None:
            self._stats.llm_requests += 1

    def mark_llm_context_summary(self) -> None:
        self._pending_compaction_summary_mode = "llm"

    def mark_local_context_summary(self) -> None:
        self._pending_compaction_summary_mode = "local"

    def record_context_compaction(
        self,
        *,
        estimated_tokens_before: int | None = None,
        estimated_tokens_after: int | None = None,
    ) -> None:
        if self._stats is not None:
            self._stats.compactions += 1
            if estimated_tokens_before is not None and estimated_tokens_after is not None:
                reduction = max(0, estimated_tokens_before - estimated_tokens_after)
                self._stats.compaction_estimated_token_reduction += reduction
                if reduction:
                    self._stats.effective_compactions += 1
                    self._stats.consecutive_zero_gain_compactions = 0
                else:
                    self._stats.zero_gain_compactions += 1
                    self._stats.consecutive_zero_gain_compactions += 1
                    self._stats.max_consecutive_zero_gain_compactions = max(
                        self._stats.max_consecutive_zero_gain_compactions,
                        self._stats.consecutive_zero_gain_compactions,
                    )
            if self._pending_compaction_summary_mode == "llm":
                self._stats.llm_context_summaries += 1
            elif self._pending_compaction_summary_mode == "local":
                self._stats.local_context_summaries += 1
        self._pending_compaction_summary_mode = None

    def record_context_checkpoint(self) -> None:
        if self._stats is not None:
            self._stats.compaction_checkpoints += 1

    def record_context_checkpoint_reused(self) -> None:
        if self._stats is not None:
            self._stats.compaction_checkpoint_reused += 1

    def record_tool_started(self, name: str) -> None:
        if self._stats is None:
            return
        self._stats.tool_calls += 1
        self._stats.tool_counts[name] = self._stats.tool_counts.get(name, 0) + 1
        if name == "glob_files":
            self._stats.file_discovery_calls += 1

    def record_tool_finished(self, *, is_error: bool) -> None:
        if self._stats is not None and is_error:
            self._stats.tool_errors += 1

    def record_tool_result(
        self,
        *,
        name: str,
        is_error: bool,
        useless: bool,
        metadata: Mapping[str, Any],
    ) -> None:
        if self._stats is None:
            return
        if useless and not is_error:
            self._stats.useless_tool_results += 1
        if name == "glob_files":
            if metadata.get("complete") is False:
                self._stats.file_discovery_incomplete_results += 1
            if metadata.get("negative_evidence_type") in {"path_no_match", "exact_path_missing"}:
                self._stats.file_discovery_no_match_results += 1
        if metadata.get("unknown_tool"):
            self._stats.unknown_tool_calls += 1
            suggestions = metadata.get("suggested_tools")
            if isinstance(suggestions, (list, tuple)) and suggestions:
                self._stats.unknown_tool_suggestions += 1
        if metadata.get("filename_search_misuse"):
            self._stats.filename_search_misuse_calls += 1
        if metadata.get("provider_schema_violation"):
            self._stats.provider_schema_violations += 1

    def record_synthetic_tool_result(self, *, is_error: bool = True) -> None:
        if self._stats is None:
            return
        self._stats.synthetic_tool_results += 1
        if is_error:
            self._stats.tool_errors += 1

    def record_suppressed_tool_executions(self, count: int) -> None:
        if self._stats is not None:
            self._stats.suppressed_tool_executions += max(0, count)

    def record_provider_protocol_violation(
        self,
        *,
        phase: str,
        artifact_kind: str,
        suppressed_tool_calls: int,
    ) -> None:
        if self._stats is None:
            return
        self._stats.provider_protocol_violations += 1
        self._stats.suppressed_tool_executions += max(0, suppressed_tool_calls)
        if phase == "forced_final":
            self._stats.forced_final_protocol_violations += 1
        if artifact_kind == "structured_tool_calls":
            if phase == "forced_final":
                self._stats.forced_final_structured_tool_calls += 1
        else:
            self._stats.provider_markup_artifacts += 1

    def record_provider_argument_normalization(self, count: int = 1) -> None:
        if self._stats is not None:
            self._stats.provider_argument_normalizations += max(0, count)

    def record_forced_final_protocol_recovery(self, *, exhausted: bool = False) -> None:
        if self._stats is None:
            return
        if exhausted:
            self._stats.forced_final_protocol_recovery_exhausted += 1
        else:
            self._stats.forced_final_protocol_recoveries += 1

    def record_session_evidence(
        self,
        *,
        hits: int,
        misses: int,
        stale: int,
        invalidations: int,
        reused_paths: list[str],
    ) -> None:
        if self._stats is None:
            return
        self._stats.session_evidence_hits += hits
        self._stats.session_evidence_misses += misses
        self._stats.session_evidence_stale += stale
        self._stats.session_evidence_invalidations += invalidations
        for path in reused_paths:
            if path not in self._stats.session_evidence_reused_paths:
                self._stats.session_evidence_reused_paths.append(path)

    def record_session_evidence_invalidation(self, count: int) -> None:
        if self._stats is not None and count > 0:
            self._stats.session_evidence_invalidations += count

    def record_session_evidence_directive(self) -> None:
        if self._stats is not None:
            self._stats.session_evidence_directives += 1

    def record_session_evidence_model_reread(self) -> None:
        if self._stats is not None:
            self._stats.session_evidence_model_rereads += 1

    def record_read_only_review_trigger(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_triggers += 1

    def record_read_only_review_attempt(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_attempts += 1

    def record_read_only_review_result(self, verdict: str, findings: int) -> None:
        if self._stats is None:
            return
        self._stats.read_only_reviewer_verdicts[verdict] = self._stats.read_only_reviewer_verdicts.get(verdict, 0) + 1
        self._stats.read_only_reviewer_findings += max(0, findings)
        self._stats.read_only_reviewer_reviewed_claims += max(0, findings)

    def record_read_only_review_rewrite(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_rewrites += 1

    def record_read_only_review_rewrite_acceptance(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_rewrite_acceptances += 1

    def record_read_only_review_rewrite_correction(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_rewrite_corrections += 1

    def record_read_only_review_rewrite_closure_check(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_rewrite_closure_checks += 1

    def record_read_only_review_rewrite_closure_acceptance(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_rewrite_closure_acceptances += 1

    def record_read_only_review_rewrite_verification_round(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_rewrite_verification_rounds += 1

    def record_read_only_review_claim_transport_rewrite(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_claim_transport_rewrites += 1

    def record_read_only_review_claim_transport_rewrite_acceptance(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_claim_transport_rewrite_acceptances += 1

    def record_read_only_review_claim_transport_rewrite_exhausted(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_claim_transport_rewrite_exhausted += 1

    def record_read_only_review_claim_transport_pruned(self, claims: int) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_claim_transport_pruned_claims += max(0, claims)
            self._stats.read_only_reviewer_claim_transport_projection_rounds += 1

    def record_read_only_review_error(self, reason: str) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_errors[reason] = self._stats.read_only_reviewer_errors.get(reason, 0) + 1

    def record_read_only_review_schema_failure(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_schema_failures += 1

    def record_read_only_review_repair(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_repairs += 1

    def record_read_only_review_repair_success(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_repair_successes += 1

    def record_read_only_review_repair_exhausted(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_repair_exhausted += 1

    def record_read_only_review_typed_submit(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_typed_submits += 1

    def record_read_only_review_finding_submit(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_finding_submits += 1
            self._stats.read_only_reviewer_typed_submits += 1

    def record_read_only_review_final_submit(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_final_submits += 1
            self._stats.read_only_reviewer_typed_submits += 1

    def record_read_only_review_protocol_failure(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_protocol_failures += 1

    def record_read_only_review_rejected_finding_submit(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_rejected_finding_submits += 1

    def record_read_only_review_rejected_final_submit(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_rejected_final_submits += 1

    def record_read_only_review_finding_limit_hit(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_finding_limit_hits += 1

    def record_read_only_review_invalidated_finding_submit(self, count: int = 1) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_invalidated_finding_submits += count

    def record_read_only_review_output_lifecycle_exhausted(self) -> None:
        if self._stats is not None:
            self._stats.read_only_reviewer_output_lifecycle_exhausted += 1

    def record_read_only_review_output_lifecycle_correction(self, category: str) -> None:
        if self._stats is None:
            return
        if category not in READ_ONLY_REVIEWER_LIFECYCLE_CATEGORIES:
            raise ValueError(f"unknown read-only reviewer lifecycle category: {category}")
        if category == "arguments":
            self._stats.read_only_reviewer_argument_lifecycle_corrections += 1
        elif category == "document_consistency":
            self._stats.read_only_reviewer_document_lifecycle_corrections += 1
        elif category == "implementation_readiness":
            self._stats.read_only_reviewer_implementation_readiness_lifecycle_corrections += 1
        else:
            self._stats.read_only_reviewer_protocol_lifecycle_corrections += 1

    def record_read_only_review_output_lifecycle_exhausted_category(self, category: str) -> None:
        if self._stats is None:
            return
        if category not in READ_ONLY_REVIEWER_LIFECYCLE_CATEGORIES:
            raise ValueError(f"unknown read-only reviewer lifecycle category: {category}")
        self._stats.read_only_reviewer_lifecycle_exhausted_categories[category] = (
            self._stats.read_only_reviewer_lifecycle_exhausted_categories.get(category, 0) + 1
        )

    def record_pre_review_audit(self, *, categories: tuple[str, ...], exhausted: bool) -> None:
        if self._stats is None:
            return
        self._stats.pre_review_audit_rounds += 1
        if exhausted:
            self._stats.pre_review_audit_exhausted += 1
        for category in categories:
            self._stats.pre_review_audit_categories[category] = (
                self._stats.pre_review_audit_categories.get(category, 0) + 1
            )

    def record_pre_review_audit_exhausted(self, categories: tuple[str, ...]) -> None:
        """Mark the single preparation lifecycle exhausted without inventing another round."""

        if self._stats is None:
            return
        self._stats.pre_review_audit_exhausted += 1
        for category in categories:
            self._stats.pre_review_audit_categories.setdefault(category, 0)

    def record_safe_partial_report(self, *, observations: int, missing: int, rejected_categories: tuple[str, ...]) -> None:
        if self._stats is None:
            return
        self._stats.safe_partial_reports += 1
        self._stats.safe_partial_observations += observations
        self._stats.safe_partial_missing += missing
        for category in rejected_categories:
            self._stats.safe_partial_rejected_categories[category] = (
                self._stats.safe_partial_rejected_categories.get(category, 0) + 1
            )

    def record_tool_choice_exact_action(self, action: str) -> None:
        if self._stats is None:
            return
        if action == "force":
            self._stats.tool_choice_exact_forces += 1

    def record_tool_choice_exact_exhausted(self) -> None:
        if self._stats is not None:
            self._stats.tool_choice_exact_exhausted += 1

    def finish(
        self,
        reason: str,
        *,
        guard_values: dict[str, int],
        steering_values: dict[str, int],
    ) -> dict[str, Any]:
        stats = self._stats
        if stats is None:
            return {"termination_reason": reason}
        guard_hits = {
            key: value - stats.guard_start.get(key, 0)
            for key, value in guard_values.items()
        }
        steering_counts = {
            key: value - stats.steer_start.get(key, 0)
            for key, value in steering_values.items()
        }
        return {
            "run_id": stats.run_id,
            "termination_reason": reason,
            "workflow_profile": dict(stats.workflow_profile),
            "elapsed_ms": _elapsed_ms_since(stats.started_monotonic),
            "prompt_chars": stats.prompt_chars,
            "llm_requests": stats.llm_requests,
            "tool_calls": stats.tool_calls,
            "tool_errors": stats.tool_errors,
            "useless_tool_results": stats.useless_tool_results,
            "synthetic_tool_results": stats.synthetic_tool_results,
            "compactions": stats.compactions,
            "effective_compactions": stats.effective_compactions,
            "zero_gain_compactions": stats.zero_gain_compactions,
            "max_consecutive_zero_gain_compactions": stats.max_consecutive_zero_gain_compactions,
            "compaction_estimated_token_reduction": stats.compaction_estimated_token_reduction,
            "compaction_checkpoints": stats.compaction_checkpoints,
            "compaction_checkpoint_reused": stats.compaction_checkpoint_reused,
            "llm_context_summaries": stats.llm_context_summaries,
            "local_context_summaries": stats.local_context_summaries,
            "file_discovery_calls": stats.file_discovery_calls,
            "file_discovery_incomplete_results": stats.file_discovery_incomplete_results,
            "file_discovery_no_match_results": stats.file_discovery_no_match_results,
            "unknown_tool_calls": stats.unknown_tool_calls,
            "unknown_tool_suggestions": stats.unknown_tool_suggestions,
            "filename_search_misuse_calls": stats.filename_search_misuse_calls,
            "provider_schema_violations": stats.provider_schema_violations,
            "provider_protocol_violations": stats.provider_protocol_violations,
            "forced_final_protocol_violations": stats.forced_final_protocol_violations,
            "forced_final_protocol_recoveries": stats.forced_final_protocol_recoveries,
            "forced_final_protocol_recovery_exhausted": stats.forced_final_protocol_recovery_exhausted,
            "forced_final_structured_tool_calls": stats.forced_final_structured_tool_calls,
            "provider_markup_artifacts": stats.provider_markup_artifacts,
            "provider_argument_normalizations": stats.provider_argument_normalizations,
            "suppressed_tool_executions": stats.suppressed_tool_executions,
            "session_evidence": {
                "hits": stats.session_evidence_hits,
                "misses": stats.session_evidence_misses,
                "stale": stats.session_evidence_stale,
                "invalidations": stats.session_evidence_invalidations,
                "reused_paths": list(stats.session_evidence_reused_paths),
                "directives": stats.session_evidence_directives,
                "model_rereads": stats.session_evidence_model_rereads,
            },
            "read_only_reviewer": {
                "triggers": stats.read_only_reviewer_triggers,
                "rewrites": stats.read_only_reviewer_rewrites,
                "rewrite_acceptances": stats.read_only_reviewer_rewrite_acceptances,
                "rewrite_corrections": stats.read_only_reviewer_rewrite_corrections,
                "rewrite_closure_checks": stats.read_only_reviewer_rewrite_closure_checks,
                "rewrite_closure_acceptances": stats.read_only_reviewer_rewrite_closure_acceptances,
                "rewrite_verification_rounds": stats.read_only_reviewer_rewrite_verification_rounds,
                "claim_transport_rewrites": stats.read_only_reviewer_claim_transport_rewrites,
                "claim_transport_rewrite_acceptances": stats.read_only_reviewer_claim_transport_rewrite_acceptances,
                "claim_transport_rewrite_exhausted": stats.read_only_reviewer_claim_transport_rewrite_exhausted,
                "claim_transport_pruned_claims": stats.read_only_reviewer_claim_transport_pruned_claims,
                "claim_transport_projection_rounds": stats.read_only_reviewer_claim_transport_projection_rounds,
                "findings": stats.read_only_reviewer_findings,
                "reviewed_claims": stats.read_only_reviewer_reviewed_claims,
                "attempts": stats.read_only_reviewer_attempts,
                "provider_turns": stats.read_only_reviewer_attempts,
                "schema_failures": stats.read_only_reviewer_schema_failures,
                "repairs": stats.read_only_reviewer_repairs,
                "repair_successes": stats.read_only_reviewer_repair_successes,
                "repair_exhausted": stats.read_only_reviewer_repair_exhausted,
                "typed_submits": stats.read_only_reviewer_typed_submits,
                "finding_submits": stats.read_only_reviewer_finding_submits,
                "final_submits": stats.read_only_reviewer_final_submits,
                "protocol_failures": stats.read_only_reviewer_protocol_failures,
                "rejected_finding_submits": stats.read_only_reviewer_rejected_finding_submits,
                "rejected_final_submits": stats.read_only_reviewer_rejected_final_submits,
                "finding_limit_hits": stats.read_only_reviewer_finding_limit_hits,
                "invalidated_finding_submits": stats.read_only_reviewer_invalidated_finding_submits,
                "output_lifecycle_exhausted": stats.read_only_reviewer_output_lifecycle_exhausted,
                "output_lifecycle_corrections": {
                    "arguments": stats.read_only_reviewer_argument_lifecycle_corrections,
                    "document_consistency": stats.read_only_reviewer_document_lifecycle_corrections,
                    "implementation_readiness": stats.read_only_reviewer_implementation_readiness_lifecycle_corrections,
                    "protocol": stats.read_only_reviewer_protocol_lifecycle_corrections,
                },
                "output_lifecycle_exhausted_categories": dict(
                    sorted(stats.read_only_reviewer_lifecycle_exhausted_categories.items())
                ),
                "verdicts": dict(sorted(stats.read_only_reviewer_verdicts.items())),
                "errors": dict(sorted(stats.read_only_reviewer_errors.items())),
            },
            "pre_review_audit": {
                "rounds": stats.pre_review_audit_rounds,
                "categories": dict(sorted(stats.pre_review_audit_categories.items())),
                "exhausted": stats.pre_review_audit_exhausted,
            },
            "safe_partial_report": {
                "emitted": stats.safe_partial_reports,
                "observations": stats.safe_partial_observations,
                "missing": stats.safe_partial_missing,
                "rejected_categories": dict(sorted(stats.safe_partial_rejected_categories.items())),
            },
            "tool_choice_exact": {
                "forces": stats.tool_choice_exact_forces,
                "exhausted": stats.tool_choice_exact_exhausted,
            },
            "tool_counts": dict(sorted(stats.tool_counts.items())),
            "guard_hits": {key: value for key, value in guard_hits.items() if value},
            "steering_counts": {key: value for key, value in steering_counts.items() if value},
        }


def _elapsed_ms_since(started_monotonic: float) -> int:
    try:
        return max(0, int((time.monotonic() - started_monotonic) * 1000))
    except Exception:  # noqa: BLE001 - run summary must never break task completion.
        return 0

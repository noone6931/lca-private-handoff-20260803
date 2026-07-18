"""Runtime boundary for profile-scoped read-only workflow hooks."""
from __future__ import annotations

from typing import Any

from ..read_only_reviewer import ReviewerPhaseOutcome
from .review import ReadOnlyReviewPhase
from ..workflow_profile import workflow_read_only_review_enabled
from ..workflow_profile import workflow_safe_partial_enabled


class WorkflowReadOnlyReviewPhase(ReadOnlyReviewPhase):
    """Keep the existing reviewer owner intact while selecting its lifecycle."""

    def set_preparation_audit(self, audit: Any) -> None:
        super().set_preparation_audit(
            audit if workflow_read_only_review_enabled(self._runtime._run) else None
        )

    def refresh_preparation_audit(self, context: Any, steerers: Any) -> Any:
        if not workflow_read_only_review_enabled(self._runtime._run):
            self._preparation_audit = None
            return None
        return super().refresh_preparation_audit(context, steerers)

    def owns_pending_candidate_validation(self) -> bool:
        return workflow_read_only_review_enabled(
            self._runtime._run
        ) and super().owns_pending_candidate_validation()

    def owns_initial_pre_review_audits(self) -> bool:
        return workflow_read_only_review_enabled(
            self._runtime._run
        ) and super().owns_initial_pre_review_audits()

    def review_candidate(self, candidate: str) -> ReviewerPhaseOutcome:
        if not workflow_read_only_review_enabled(self._runtime._run):
            return ReviewerPhaseOutcome("not_applicable")
        return super().review_candidate(candidate)

    def safe_partial_for_terminal(self, reason: str) -> str:
        if not workflow_safe_partial_enabled(self._runtime._run):
            return ""
        return super().safe_partial_for_terminal(reason)

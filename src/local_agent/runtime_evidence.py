"""Evidence, verification, and session-cache lifecycle phase."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .evidence import EvidenceRecord, evidence_root_for_path, evidence_root_label
from .patch.anchored import PatchError, display_workspace_path, resolve_workspace_path
from .patch_reviewer import review_input_metadata, review_input_summary
from .test_planner import plan_narrow_test
from .tool_choice_queue import ToolResultSummary, session_evidence_reuse_directive
from .tools.base import ToolResult
from .tools.relevance import is_code_implementation_request, request_mentions_config_or_path
from .verification_plan import VerificationPlan
from .steering.final_answer import SteeringDecision
from .tool_gateway import _display_read_file_range_subject, _request_requires_patch_preview, _source_evidence_matches_path, _tool_call_uses_dry_run, _tool_choice_result_path, is_session_evidence_reread

MAX_PATCH_REVIEW_STEERS = 2

class EvidenceVerificationLifecycle:
    """Cohesive Runtime phase kept outside the turn orchestrator."""

    def __init__(self, runtime: object) -> None:
        self._runtime = runtime

    def _record_read_file_evidence(self, name: str, arguments: str | dict[str, Any], result: ToolResult) -> None:
        runtime = self._runtime
        if name == "read_file":
            requirement = runtime._run.soft_tool_requirement
            runtime._run.evidence.record_read_file(
                arguments=arguments,
                result=result,
                workspace=runtime._workspace_context.primary,
                allowed_dirs=runtime._workspace_context.additional_roots,
                requirement_candidates=requirement.candidate_files if requirement is not None else (),
            )

    def _record_tool_choice_result(self, name: str, arguments: str | dict[str, Any], result: ToolResult) -> None:
        runtime = self._runtime
        metadata = runtime._tool_choice_result_metadata(name, arguments, result)
        if is_session_evidence_reread(
            name,
            arguments,
            workspace=runtime._workspace_context.primary,
            allowed_dirs=runtime._workspace_context.additional_roots,
            cached_paths=runtime._run.session_evidence_reuse.reused_paths,
        ):
            runtime._run.collector.record_session_evidence_model_reread()
        runtime._run.tool_choice_tool_names.append(name)
        runtime._run.tool_choice_tool_names = runtime._run.tool_choice_tool_names[-80:]
        runtime._run.tool_choice_results.append(
            ToolResultSummary(
                name=name,
                content=review_input_summary(name, result.content, max_chars=6000),
                is_error=result.is_error,
                useless=result.useless,
                path=_tool_choice_result_path(arguments, result),
                metadata={
                    **review_input_metadata(name, result.content),
                    **metadata,
                },
            )
        )
        runtime._run.tool_choice_results = runtime._run.tool_choice_results[-80:]
        runtime._run.consume_tool_choice_read(name)
        runtime._refresh_verification_plan()

    def _refresh_verification_plan(self) -> None:
        runtime = self._runtime
        plan = runtime._run.verification_plan
        if not plan.active:
            return
        test_plan = plan_narrow_test(runtime._workspace_context.primary, runtime._run.tool_choice_results)
        test_plan_changed = test_plan != runtime._run.verification_test_plan
        runtime._run.verification_test_plan = test_plan
        if plan.observe(runtime._run.tool_choice_results, test_plan=test_plan) or test_plan_changed:
            runtime._record_verification_plan_snapshot("update")

    def _record_verification_plan_snapshot(self, event: str) -> None:
        runtime = self._runtime
        plan = runtime._run.verification_plan
        if not plan.active:
            return
        payload: dict[str, Any] = {"event": event, **plan.snapshot()}
        if runtime._run.verification_test_plan is not None:
            payload["test_plan"] = runtime._run.verification_test_plan.snapshot()
        runtime._session.append(f"verification_plan_{event}", payload)
        runtime._events.emit("ContextUpdated", {"kind": f"verification_plan_{event}", **payload})

    def _record_verification_patch_review(self, decision: SteeringDecision | None) -> None:
        runtime = self._runtime
        plan = runtime._run.verification_plan
        if not plan.active:
            return
        review_capped = runtime._run.final_answer_steers.get("patch_reviewer", 0) >= MAX_PATCH_REVIEW_STEERS
        if decision is None and review_capped:
            changed = plan.record_patch_review(
                passed=None,
                reason="deterministic post-diff reviewer was skipped because its continuation cap was reached",
                refs=["git_diff:post-write"],
            )
        elif decision is None:
            changed = plan.record_patch_review(
                passed=True,
                reason="deterministic post-diff reviewer completed without blocking findings",
                refs=["git_diff:post-write"],
            )
        else:
            changed = plan.record_patch_review(
                passed=False,
                reason=decision.message,
                refs=["git_diff:post-write", f"steerer:{decision.kind}"],
            )
        if changed:
            runtime._record_verification_plan_snapshot("update")

    def _record_tool_evidence(self, name: str, arguments: str | dict[str, Any], result: ToolResult) -> None:
        runtime = self._runtime
        record = runtime._run.evidence.record_tool(
            name=name,
            arguments=arguments,
            result=result,
            workspace=runtime._workspace_context.primary,
            allowed_dirs=runtime._workspace_context.additional_roots,
        )
        if record is not None:
            runtime._append_evidence_record(record)
        runtime._capture_session_evidence(record)

    def _invalidate_stale_source_evidence_after_write(
        self,
        name: str,
        arguments: str | dict[str, Any],
        result: ToolResult,
    ) -> None:
        runtime = self._runtime
        runtime._run.evidence.invalidate_source_after_write(
            name=name,
            arguments=arguments,
            result=result,
            workspace=runtime._workspace_context.primary,
            allowed_dirs=runtime._workspace_context.additional_roots,
        )
        if result.is_error or name not in {"apply_patch", "rollback_patch", "write_file"}:
            return
        if name == "apply_patch" and _tool_call_uses_dry_run(arguments):
            return
        raw_path = _tool_choice_result_path(arguments, result)
        if not raw_path:
            return
        try:
            changed_path = resolve_workspace_path(
                runtime._workspace_context.primary,
                raw_path,
                runtime._workspace_context.additional_roots,
            )
        except PatchError:
            return
        removed = runtime._session_evidence.invalidate_paths((changed_path,))
        if removed:
            runtime._run.collector.record_session_evidence_invalidation(removed)
            runtime._record_session_evidence_event(
                "invalidated",
                {"reason": "workspace_write", "paths": [str(changed_path)], "count": removed},
            )

    def _capture_session_evidence(self, record: EvidenceRecord | None) -> None:
        runtime = self._runtime
        if record is None or not runtime._run.tool_choice_results:
            return
        tool_result = runtime._run.tool_choice_results[-1]
        source = None
        requirement = None
        if tool_result.name == "read_file":
            resolved_path = record.details.get("resolved_path")
            source = next(
                (
                    item
                    for item in reversed(runtime._run.evidence.source_evidence)
                    if _source_evidence_matches_path(
                        item.path,
                        resolved_path,
                        runtime._workspace_context.primary,
                        runtime._workspace_context.additional_roots,
                    )
                ),
                None,
            )
            requirement = next(
                (
                    item
                    for item in reversed(runtime._run.evidence.pinned_requirement_evidence)
                    if _source_evidence_matches_path(
                        item.path,
                        resolved_path,
                        runtime._workspace_context.primary,
                        runtime._workspace_context.additional_roots,
                    )
                ),
                None,
            )
        if runtime._session_evidence.capture(
            tool_result=tool_result,
            record=record,
            source_evidence=source,
            requirement_evidence=requirement,
            workspace_revision=runtime._workspace_context.revision,
            request=runtime._run.current_user_request or "",
            run_id=runtime._run.run_id or "",
        ):
            runtime._record_session_evidence_event("captured", {"tool": tool_result.name, "path": tool_result.path})

    def _hydrate_session_evidence(self, prompt: str) -> None:
        runtime = self._runtime
        reuse = runtime._session_evidence.reuse_for_request(
            prompt=prompt,
            workspace_revision=runtime._workspace_context.revision,
            authorized_roots=runtime._workspace_context.all_roots,
        )
        runtime._run.session_evidence_reuse = reuse
        runtime._run.collector.record_session_evidence(
            hits=reuse.hit_count,
            misses=reuse.miss_count,
            stale=reuse.stale_count,
            invalidations=reuse.invalidation_count,
            reused_paths=[runtime._display_session_evidence_path(path) for path in reuse.reused_paths],
        )
        for entry in reuse.entries:
            runtime._run.tool_choice_results.append(entry.tool_result)
            runtime._run.tool_choice_tool_names.append(entry.tool_result.name)
            if runtime._run.evidence.hydrate_session_cached(
                record=entry.record,
                source_evidence=entry.source_evidence,
                requirement_evidence=entry.requirement_evidence,
                canonical_paths=tuple(entry.content_tags),
            ):
                runtime._session.append(
                    "session_evidence_reused",
                    {
                        "entry_id": entry.entry_id,
                        "tool": entry.tool_result.name,
                        "path": entry.tool_result.path,
                        "root": entry.root,
                        "origin_run_id": entry.origin_run_id,
                    },
                )
        if reuse.hit_count or reuse.stale_count:
            runtime._record_session_evidence_event(
                "reused",
                {
                    "hits": reuse.hit_count,
                    "misses": reuse.miss_count,
                    "stale": reuse.stale_count,
                    "reused_paths": [runtime._display_session_evidence_path(path) for path in reuse.reused_paths],
                },
            )

    def _append_session_evidence_reuse_directive(self) -> None:
        runtime = self._runtime
        if runtime._run.session_evidence_directive_emitted:
            return
        directive = session_evidence_reuse_directive(runtime._run.tool_choice_results)
        if directive is None:
            return
        runtime._run.session_evidence_directive_emitted = True
        runtime._messages.append({"role": "user", "content": directive.message})
        runtime._run.collector.record_session_evidence_directive()
        runtime._session.append(
            "runtime_steering",
            {"kind": directive.kind, "paths": list(directive.paths)},
        )
        runtime._events.emit(
            "ContextUpdated",
            {"kind": directive.kind, "paths": list(directive.paths)},
        )

    def _record_session_evidence_event(self, event: str, payload: Mapping[str, Any]) -> None:
        runtime = self._runtime
        data = {"event": event, **dict(payload)}
        runtime._session.append(f"session_evidence_{event}", data)
        runtime._events.emit("ContextUpdated", {"kind": f"session_evidence_{event}", **data})

    def _display_session_evidence_path(self, raw_path: str) -> str:
        runtime = self._runtime
        try:
            resolved = Path(raw_path).resolve()
            root = evidence_root_for_path(
                resolved,
                runtime._workspace_context.primary,
                runtime._workspace_context.additional_roots,
            )
            label = evidence_root_label(root, runtime._workspace_context.primary, runtime._workspace_context.additional_roots)
            return f"{label}:{resolved.relative_to(root)}"
        except (OSError, ValueError):
            return raw_path

    def _append_evidence_record(self, record: EvidenceRecord) -> None:
        runtime = self._runtime
        if not runtime._run.evidence.append(record):
            return
        runtime._session.append(
            "evidence",
            {
                "tool": record.tool,
                "subject": record.subject,
                "status": record.status,
                "summary": record.summary,
                "details": dict(record.details),
            },
        )

    def _record_workspace_root_evidence(self) -> None:
        runtime = self._runtime
        record = runtime._run.evidence.record_workspace_root(runtime._workspace_context.primary)
        if record is not None:
            runtime._append_evidence_record(record)

    def _patch_relevance_denial_reason(self, raw_path: str, resolved_path: Path) -> str | None:
        runtime = self._runtime
        display_path = display_workspace_path(runtime._workspace_context.primary, resolved_path, runtime._workspace_context.additional_roots)
        return runtime._run.evidence.patch_relevance_denial_reason(
            raw_path,
            resolved_path,
            workspace=runtime._workspace_context.primary,
            allowed_dirs=runtime._workspace_context.additional_roots,
            is_code_implementation_request=is_code_implementation_request(runtime._run.current_user_request),
            request_mentions_config_or_path=request_mentions_config_or_path(runtime._run.current_user_request, display_path),
        )

    def _patch_preview_denial_reason(self, args: dict[str, Any], resolved_path: Path) -> str | None:
        runtime = self._runtime
        return runtime._run.evidence.patch_preview_denial_reason(
            args,
            resolved_path,
            preview_required=_request_requires_patch_preview(runtime._run.current_user_request),
        )

    def _record_successful_patch_preview(
        self,
        name: str,
        arguments: str | dict[str, Any],
        result: ToolResult,
    ) -> None:
        runtime = self._runtime
        runtime._run.evidence.record_successful_patch_preview(
            name=name,
            arguments=arguments,
            result=result,
            workspace=runtime._workspace_context.primary,
            allowed_dirs=runtime._workspace_context.additional_roots,
        )

    def _read_file_evidence_summary(self) -> str:
        runtime = self._runtime
        return runtime._run.evidence.read_file_summary()

    def _evidence_for_read_file_range(self, range_key: tuple[str, int, str]) -> str:
        runtime = self._runtime
        subject = _display_read_file_range_subject(range_key, runtime._workspace_context.primary, runtime._workspace_context.additional_roots)
        return runtime._run.evidence.evidence_for_read_file_range(subject)

    def _evidence_ledger_summary(self) -> str:
        runtime = self._runtime
        return runtime._run.evidence.summary()

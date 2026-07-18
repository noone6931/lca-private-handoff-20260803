"""Sanitized correction contract for rejected reviewer output."""
from __future__ import annotations

import json
from typing import Any, Mapping

from .document_consistency import document_consistency_rejection_hint
from .readiness import implementation_readiness_rejection_hint


def sanitize_reviewer_diagnostics(diagnostics: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "error_code", "top_level_keys", "verdict", "findings_type", "findings_count",
        "unknown_claim_id_count", "duplicate_claim_id_count", "claim_mismatch_count",
        "source_material_gap_count", "candidate_defect_count", "required_candidate_defect_count",
        "missing_candidate_defect_count", "response_chars", "top_level_type", "arguments_type",
        "json_error_category", "tool_name", "call_index", "tool_call_count",
        "accepted_candidate_defect_count", "rejected_candidate_defect_count",
        "document_consistency_keys", "expected_document_consistency_keys", "out_of_scope_claim_role",
    }
    return {key: diagnostics[key] for key in allowed if key in diagnostics}


def reviewer_correction_instruction(
    code: str,
    *,
    diagnostics: Mapping[str, Any] | None = None,
    document_consistency: bool = False,
    implementation_readiness: bool = False,
) -> str:
    """Describe one rejected output contract without repeating provider content."""

    diagnostics = diagnostics or {}
    final_fields = ["verdict", "confidence", "reason"]
    if document_consistency:
        final_fields.append("document_consistency")
    if implementation_readiness:
        final_fields.append("implementation_readiness")
    common = (
        "Use report_read_only_finding once per candidate defect, then call submit_read_only_review with exactly "
        + ", ".join(final_fields)
        + "; do not include findings in the final submit because accepted findings are already recorded. "
        "Keep the complete output under 9000 characters and use unique known claim IDs."
    )
    if code == "top_level_keys_invalid":
        fields_json = json.dumps(final_fields, separators=(",", ":"))
        findings_hint = (
            " The rejected call included the forbidden findings key; remove it."
            if "findings" in diagnostics.get("top_level_keys", ()) else ""
        )
        return (
            f"Allowed final top-level keys JSON: {fields_json}." + findings_hint
            + " Do not include findings, claim_id, finding_scope, issue, action, or any wrapper object; "
            "recorded findings are retained. " + common
        )
    static = _static_reviewer_correction(code, common)
    if static:
        return static
    if document_consistency:
        hint = document_consistency_rejection_hint(code)
        if hint:
            return hint + " " + common
    if implementation_readiness:
        hint = implementation_readiness_rejection_hint(code)
        if hint:
            return hint + " " + common
    return "Use the shallow finding tool and final submit tool with the required verdict/finding cardinality. " + common


def _static_reviewer_correction(code: str, common: str) -> str:
    simple = {
        "findings_too_many": "Report no more than 8 highest-risk findings. ",
        "response_too_large": "Make reason, issue, and action concise; do not exceed 9000 characters. ",
        "pass_with_findings": "A pass verdict must have no reported findings; otherwise choose revise or unverified with 1 to 8 findings. ",
        "nonpassing_without_findings": "A revise or unverified verdict needs 1 to 8 report_read_only_finding calls. ",
        "output_tool_multiple_final_calls": "Submit exactly one final verdict. ",
        "finding_limit_exceeded": "Do not report more than 8 findings. Submit the final verdict after the accepted findings. ",
    }
    if code in simple:
        return simple[code] + common
    if code in {"finding_claim_invalid", "finding_claim_mismatch"}:
        return (
            "Do not include claim text in report_read_only_finding; Runtime binds the exact candidate_claims text "
            "from the selected claim_id. If using legacy claim text, it must exactly match that claim_id. " + common
        )
    if code in {"source_material_gap_finding", "finding_scope_invalid"}:
        return (
            "Do not submit source_material_gap findings. Findings must be candidate_defect items whose action changes "
            "the candidate answer. If only source materials need an owner decision and the candidate already reports that "
            "gap accurately, submit pass with no reported findings; otherwise keep only candidate_defect findings. " + common
        )
    if code == "claim_role_out_of_scope":
        return (
            "Do not report evidence-review findings for candidate claims outside this reviewer role. Proposal and "
            "pending claims are explicitly non-current. In owner/design reviews, requirement_fact is owned by the "
            "requirement-evidence pipeline; it becomes reviewable here only for a document-consistency profile. Keep "
            "only provable defects in source_fact or other in owner/design review; submit pass when none remain. " + common
        )
    if code == "candidate_defect_findings_missing":
        return (
            "A previous repair attempt contained valid candidate_defect findings. Keep those candidate defects in this "
            "submission by resubmitting the same claim_id with finding_scope=candidate_defect plus issue/action only. "
            "Do not include claim text; Runtime binds the canonical candidate text by claim_id. Do not replace them "
            "with pass unless the candidate_defect findings are no longer present in the repaired output request. " + common
        )
    if code in {"output_tool_arguments_type_invalid", "output_tool_arguments_json_invalid"}:
        return (
            "Tool arguments must be a JSON string matching the selected output tool schema. Do not pass a native object, "
            "plain text, or malformed JSON. " + common
        )
    if code == "output_tool_final_not_last":
        return (
            "Call report_read_only_finding zero or more times first; submit_read_only_review must be the final output "
            "call in the response. Do not emit findings after the final verdict. " + common
        )
    if code in {"output_tool_call_id_missing", "output_tool_call_id_duplicate"}:
        return (
            "Every output tool call must have one unique non-empty tool_call id so the reviewer transcript can pair "
            "assistant tool calls with tool results. " + common
        )
    if code == "document_consistency_evidence_roles_overlap":
        return (
            "For document_consistency, conflict_evidence_ids and supporting_evidence_ids must be disjoint. "
            "For reported_unresolved, conditional_reconciliation, or asserted_reconciled, set supporting_evidence_ids to []. "
            "Only explicitly_supported_reconciliation may use non-empty supporting_evidence_ids, and only for independent "
            "non-visual read_file lifecycle or precedence support. " + common
        )
    if code in {"document_consistency_keys_invalid", "document_conflict_evidence_insufficient"}:
        suffix = " Do not cite only one side of the conflict." if code == "document_conflict_evidence_insufficient" else ""
        return document_consistency_rejection_hint(code) + suffix + " " + common
    if code == "document_consistency_support_requires_explicit_stance":
        return (
            "For document_consistency, set supporting_evidence_ids to [] unless stance is explicitly_supported_reconciliation. "
            "reported_unresolved, conditional_reconciliation, and asserted_reconciled must not include support ids. " + common
        )
    if code in {"document_supporting_evidence_invalid", "document_supporting_evidence_unknown", "document_supporting_evidence_duplicate"}:
        return (
            "For document_consistency, keep supporting_evidence_ids empty unless the stance is explicitly_supported_reconciliation "
            "and the ids cite independent non-visual read_file lifecycle or precedence support. " + common
        )
    if code == "document_consistency_finding_reconciles_conflict":
        return (
            "For document_consistency with unresolved or conditional conflict stance, finding issue/action must change "
            "the candidate answer without inventing artifact priority, lifecycle, historical/current role, or a resolved "
            "conflict. Keep valid unrelated candidate_defect findings, but remove or rewrite the contradictory finding. "
            + common
        )
    return ""

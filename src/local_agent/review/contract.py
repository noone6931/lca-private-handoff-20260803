"""Reviewer prompt, output schema, and sanitized repair contract."""
from __future__ import annotations

import json
from typing import Any, Mapping

from .document_consistency import document_consistency_schema
from .handoff import ExploreHandoff
from .readiness import implementation_readiness_schema
from .correction import reviewer_correction_instruction
from .correction import sanitize_reviewer_diagnostics
from .claims import _clip
from .types import CandidateClaimUnit
from .types import MAX_REVIEWER_FINDINGS
from .types import REVIEWER_FINDING_TOOL_NAME
from .types import REVIEWER_OUTPUT_TOOL_NAME


def reviewer_messages(handoff: ExploreHandoff, claim_units: tuple[CandidateClaimUnit, ...]) -> list[dict[str, str]]:
    """Return an isolated reviewer transcript with no primary conversation history."""

    system = """You are the read-only evidence reviewer for a coding agent.
Use the output-only review tools; you have no workspace tools and must never assume unseen repository facts. For each candidate defect, call report_read_only_finding once. After all findings are reported, call submit_read_only_review exactly once.

Review contract:
- The user's exact request is mandatory context. Enforce explicit prohibitions, acceptance constraints, and requested evidence boundaries from that request; do not let the candidate invent a source priority, lifecycle, or exception that the request forbids.
- A direct owner is justified only by evidence that explicitly binds the requested behavior to a path, symbol, or call chain.
- Similar names, same-domain capabilities, and general reusable code are analogous candidates, never verified owners.
- Missing or incomplete searches mean unlocated within their stated scope, not absent everywhere.
- Requirement facts, repository facts, proposals, and open questions must remain distinct.
- Each candidate_claim has a typed claim_role derived from its Markdown section. This evidence reviewer owns repository factual grounding, not proposal quality: never report findings for claim_role `proposal` or `pending`, except that an implementation-readiness review must report a proposal claim explicitly classified in its typed `unsupported_identifier_claim_ids`. For owner/design profiles, `requirement_fact` is also outside this reviewer because the requirement-evidence pipeline already owns it; do not demand current-source implementation evidence for a requirement fact. A document-consistency profile may review requirement facts against document evidence. Imperative wording, an unanswered prerequisite, or an optional missing detail does not change that ownership.
- Some handoff claim_matrix items include claim_ids. Those are claim-scoped evidence excerpts for those candidate claims only; check that the addressed claim_id is a member of claim_ids before using the excerpt, and do not use an image observation to prove a Markdown requirement rule or vice versa.
- `requirement_locator` excerpts bind requirement/document claims; `source_locator` excerpts bind repository-source claims. Prefer these claim-scoped excerpts over generic summaries. A failed or missing path in one root never invalidates a successful read in another root, even when basenames match.
- For owner/design profiles, every candidate statement presented as a current repository/source fact must be bound by a claim-scoped `source_locator`. Generic source items establish inspected artifacts and may help locate evidence, but they never substitute for claim-scoped support. A bare path, symbol name, or assertion that files were read is not evidence for the claimed behavior. When that binding is absent, report the claim as unsupported evidence scope; this proves an answer defect without asserting that the repository fact itself is false. Clearly labeled requirement facts, proposals, and open questions are not current-source claims and do not require `source_locator` support.
- Check every factual predicate in a repository-source claim against its claim-scoped excerpt. A locator that shows an import, field, executor, button, or template reference supports only that observed construct; it does not by itself prove an end-to-end capability, integration entry, scheduling behavior, state transition, or business ownership. When one bullet combines several predicates, every predicate must be supported by the cited excerpt or explicitly downgraded.
- Validate asserted counts against the claim-scoped excerpt instead of trusting the candidate's arithmetic. In template or configuration syntax, an event/callback/handler reference proves only that the name is referenced there; do not treat it as a method/function definition unless the cited lines contain the definition.
- A failed guessed path in an additional root is only a scoped inspection failure. It does not prove that the roots are branches, versions, mirrors, paired frontend/backend repositories, or expected to contain the same relative paths. Report candidate wording that turns such a failure into a repository relationship, coverage conclusion, or missing-implementation fact.
- A proposal must not be worded as an existing table, class, endpoint, service, approval flow, numbering prefix, or integration unless the handoff explicitly supports it.
- A clearly labeled design proposal, suggested new table/class/API, candidate option, or pending-confirmation plan is allowed without proving that every old asset is impossible to reuse. Do not demand optional prerequisites, alternatives, fields, routes, states, or implementation detail from this evidence-review role.
- When the handoff has no explicit direct binding, do not say a main owner/module judgment is correct or mostly correct. Treat same-domain code as observed or analogous and leave the owner unlocated.
- Report a finding only when it is provable from the exact request, a claim-scoped handoff excerpt, or an internal contradiction in the candidate. Do not invent acceptance criteria, business semantics, source behavior, dynamic binding, holiday handling, or possible hidden implementations merely because they could exist.
- A finding must be actionable, free of unstated assumptions, and proportionate to the user's requested rigor. If a concern depends on unseen code or a hypothetical alternative, stay silent about it. Requirement facts do not need to be observed in repository source when the candidate keeps them in the requirement-fact section; clearly labeled proposals do not need to exist in current source.
- There is no target finding count. Prefer `pass` with zero findings over speculative review activity. `revise` is reserved for concrete, provable candidate defects introduced by this candidate, not opportunities to request more investigation, stronger wording, or optional design completeness.
- Repository identifiers may legitimately be absent from the requirements. When a claim-scoped `source_locator` supports an identifier or condition, do not call it fabricated merely because the requirement document uses different words.
- Do not review design taste, completeness, naming preference, alternative selection, or whether every requirement already has a proposed implementation. Labeled mutually exclusive options and pending-confirmation choices are allowed. Do not prescribe a new field, class, state, service, check, or implementation as a finding action.
- A scoped phrase such as "not observed in this file/handoff/read range" is not a claim that the feature is globally missing. Do not upgrade it to "missing implementation" or require the candidate to do so. Conversely, report only the candidate's actual global absence wording when it exceeds the handoff.
- Review factual correctness and evidence boundaries only. An implementation gap between a requirement and the observed source is not itself a candidate-answer defect when the candidate accurately labels the requirement, the scoped source observation, and the remaining uncertainty.
- For a document-consistency review, do not resolve conflicting document or image observations with an invented workflow, scope, actor, source priority, authoritative source, or precedence rule. Preserve the conflict as unresolved unless the handoff explicitly reconciles it. A candidate that accurately cites both observations, explicitly keeps the conflict unresolved, and presents only labeled options or questions for later confirmation is compliant: submit `pass` with no reported findings. A finding must identify a candidate error such as an unsupported reconciliation, a self-contradictory candidate statement, a missing cited observation, a user-request violation, or a claim that exceeds the handoff; the source materials disagreeing by itself is not a candidate defect. If the only issue is that a source owner must decide how to update source materials, submit `pass` with no reported findings.

The incremental output contract is bounded and shallow. Report at most 8 findings total by calling report_read_only_finding once per finding; then call submit_read_only_review with verdict, confidence, reason, and any profile-required typed summary such as document_consistency. Do not repeat findings in the final submit; accepted findings are already recorded as incremental sections. Findings are capacity-limited: choose the highest-risk blocking candidate defects first. Once 8 findings are recorded, stop reporting findings and submit the final verdict. A `pass` verdict requires 0 reported findings; `revise` and `unverified` require 1 to 8 reported findings. Every finding must have one unique, known claim_id plus non-empty issue and action. For every finding, choose exactly one claim_id from candidate_claims and set finding_scope to `candidate_defect`; Runtime binds the exact candidate text by claim_id. The action must change the candidate answer; it must not ask to modify the requirements, images, prototypes, or source artifacts, and must not merely ask a source owner to decide. Never invent or repeat a claim_id. Do not submit `source_material_gap` findings; those are not candidate defects. Report only the highest-risk blocking findings when there are more than 8. Keep the complete output under 9000 characters.
Choose revise when the candidate can be corrected using the handoff. Choose unverified when the candidate cannot safely make the requested factual conclusion."""
    if _is_document_consistency_review(handoff):
        system += (
            "\n\nDocument-consistency output requirement:\n"
            "- Submit document_consistency for every verdict. conflict_evidence_ids must reference the supplied artifact observations. "
            "conflict_evidence_ids and supporting_evidence_ids must never overlap. "
            "reported_unresolved means no reconciliation is asserted; conditional_reconciliation may offer a conditional "
            "explanation but must retain that artifact role/lifecycle is unresolved. asserted_reconciled identifies an "
            "unsupported candidate reconciliation and therefore cannot justify pass. For reported_unresolved, "
            "conditional_reconciliation, and asserted_reconciled, set supporting_evidence_ids to []. "
            "explicitly_supported_reconciliation is the only stance that may use non-empty supporting_evidence_ids, "
            "and only when those ids cite independent non-visual read_file observations that explicitly state lifecycle "
            "or precedence. A visual observation can show displayed values, but never establishes author intent, a typo, "
            "lifecycle, actor, or precedence."
        )
    if _is_implementation_readiness_review(handoff):
        system += (
            "\n\nImplementation-readiness output requirement:\n"
            "- Submit implementation_readiness for every verdict. status must be ready, conditional, or blocked. "
            "ready is valid only when the candidate's core implementation dependencies are evidence-closed: owner, "
            "data/source contract, write target, test entry, and rollback boundary. blocked is required when any one "
            "of those core dimensions remains unlocated or unsupported. conditional is allowed only when the core "
            "dimensions are evidence-closed and the remaining choices are non-blocking implementation options.\n"
            "- In implementation_readiness.dimensions, provide exactly these five keys: owner, data_contract_or_source, "
            "write_target, test_entry, rollback_boundary. For each dimension, set status=closed only when claim_ids cite "
            "visible requirement/source locator evidence for that dimension. Set status=unlocated when the dimension "
            "remains a blocker and bind claim_ids to pending/unlocated candidate claims. Bind "
            "unsupported_identifier_claim_ids to candidate claims containing concrete API/table/class/field/status/owner "
            "identifiers without requirement/source provenance or a clearly generic placeholder label.\n"
            "- The candidate may propose conceptual implementation options, but a concrete identifier is not safe merely "
            "because the section is labeled proposal. If it is not in the handoff and is not an obvious placeholder, "
            "report a finding and list the claim_id in unsupported_identifier_claim_ids.\n"
            "- If the candidate itself lists unresolved owner, contract, write target, test entry, or rollback prerequisites, "
            "do not accept a ready/selected implementation slice. Use blocked and report a finding when the candidate "
            "presents the slice as selected."
        )
    payload = {
        "kind": "LCA_READ_ONLY_EVIDENCE_REVIEW",
        "handoff": handoff.to_dict(),
        "candidate_claims": [unit.to_dict() for unit in claim_units],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def reviewer_transport_rewrite_message(
    *,
    handoff: ExploreHandoff,
    omitted_claim_ids: tuple[str, ...],
    claim_units: tuple[CandidateClaimUnit, ...] = (),
) -> str:
    """Ask the primary model to compact an over-granular answer before review.

    This is a pre-review transport recovery, not a reviewer verdict rewrite:
    no findings have been accepted yet and the isolated reviewer still needs to
    run after the compact answer is produced.
    """

    omitted_count = len(tuple(dict.fromkeys(omitted_claim_ids)))
    source_paths = tuple(
        dict.fromkeys(
            str(item.identity_path or item.path)
            for item in handoff.items
            if item.classification in {"observed_candidate", "direct_binding", "source_locator"}
            and str(item.identity_path or item.path).strip()
            and str(item.identity_path or item.path) != "(none)"
        )
    )
    source_path_context = (
        "\n- Exact already-observed source paths allowed for current-source citations (copy one verbatim; do not "
        "shorten it to a class name or basename):\n"
        + "\n".join(f"  - `{path}`" for path in source_paths)
        if source_paths
        else ""
    )
    omitted = set(omitted_claim_ids)
    omitted_claims = tuple(unit for unit in claim_units if unit.claim_id in omitted)[:16]
    omitted_claim_context = (
        "\n- Exact candidate claims that failed transport. Resolve every listed item by adding a supported "
        "path-bound locator on that claim, converting a presentation-only label into a Markdown heading, or "
        "removing/downgrading the unsupported claim:\n"
        + "\n".join(
            f"  - {unit.claim_id} [{unit.section_context or 'unsectioned'}]: {_clip(unit.text, 360)}"
            for unit in omitted_claims
        )
        if omitted_claims
        else ""
    )
    return (
        "[Read-only evidence review: bounded transport recovery]\n"
        "The previous answer was not fully transportable to the isolated evidence reviewer: "
        f"{omitted_count} reviewed claim(s) lacked a usable claim-scoped locator, used an invalid/over-broad locator, "
        "or could not fit inside the bounded claim matrix.\n\n"
        "Rewrite the same answer once, without tools, into a compact reviewable final candidate. Follow these constraints:\n"
        "- Keep only the user's high-value requested conclusions; merge repeated table rows and duplicate facts.\n"
        "- Every current repository/source fact must cite an already-read source with a precise path-bound line or narrow line range. "
        "Remove or explicitly downgrade a source claim when the existing evidence cannot support such a locator.\n"
        f"{source_path_context}\n"
        f"{omitted_claim_context}\n"
        "- Format current repository/source facts only as short bullets, never as tables or blockquotes. Each bullet must "
        "contain one supported factual predicate and its locator in the same text, for example: "
        "`- /exact/source/File.java:42 defines methodName.`\n"
        "- A locator must use an exact observed path plus an integer line or bounded integer range. Never use `+`, "
        "`etc.`, comma-separated bare line numbers, a path in another table column, or a range outside the observed excerpt.\n"
        "- Tool-result character counts such as `ok (16493 chars)` are transport metadata, never source line numbers. "
        "Do not copy them into `path:line` locators or use them to claim whole-file coverage.\n"
        "- Delete global `未找到` / `未发现` / missing-implementation claims unless an existing typed negative observation "
        "directly supports that exact scope. If the point still matters, move it to the pending-confirmation section as "
        "a neutral `本轮未验证...` question, not a current-source fact.\n"
        "- Delete tool-process narration such as `patterns=...`, glob/search attempts, workspace labels, truncation notes, "
        "and statements that a file was read. They are not user-requested current-source conclusions.\n"
        "- Use a small number of precise, already-observed locators per conclusion; prefer narrow shared ranges over one citation per row.\n"
        "- Do not add new facts, paths, artifacts, owners, lifecycle explanations, source priority, or inferred workflow state.\n"
        "- Treat failed guessed paths in other roots only as scoped inspection failures. Do not infer that roots are branches, versions, mirrors, paired repositories, or expected to contain the same relative paths; omit irrelevant failed guesses from the design answer.\n"
        "- For a compound source claim, each factual predicate must be supported by the cited lines. Imports, fields, executors, buttons, and template references do not by themselves prove an end-to-end capability, integration entry, scheduling behavior, state transition, or owner.\n"
        "- Recount asserted totals from the cited excerpt. In template/configuration syntax, an event/callback/handler reference is not a method or function definition unless the cited lines contain that definition.\n"
        "- Do not state a derived item/variable/field/method count unless the cited source explicitly states that same count. List the observed entries or omit the count instead of doing new arithmetic in this recovery turn.\n"
        "- Preserve direct observations exactly at their evidence boundary. Visual observations show what is visible; they do not prove author intent, lifecycle, precedence, or role.\n"
        "- If document/image/prototype observations differ and the handoff has no explicit role/lifecycle/precedence support, state that the discrepancy remains unresolved / pending confirmation. Do not describe either artifact as a mockup, reference-only example, historical version, later/final/offline-filled state, or authoritative source unless an existing handoff item explicitly supports that.\n"
        "- Suggested compact structure: current scope; later/planned items; key rules; source discrepancies/open confirmations. Use short bullets, not exhaustive row-by-row restatement.\n\n"
        f"Original user request (mandatory): {handoff.request}\n"
        "Return only the rewritten candidate answer."
    )


def reviewer_output_tool_schema(
    claim_units: tuple[CandidateClaimUnit, ...],
    *,
    document_consistency: bool = False,
    implementation_readiness: bool = False,
    evidence_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return the isolated final-review yield schema, never a workspace tool."""

    properties: dict[str, Any] = {
        "verdict": {"type": "string", "enum": ["pass", "revise", "unverified"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {
            "type": "string",
            "maxLength": 1600,
            "description": "Brief verdict summary. Do not repeat findings here; use report_read_only_finding for each finding.",
        },
    }
    required = ["verdict", "confidence", "reason"]
    if document_consistency:
        properties["document_consistency"] = document_consistency_schema(evidence_ids)
        required.append("document_consistency")
    if implementation_readiness:
        properties["implementation_readiness"] = implementation_readiness_schema(unit.claim_id for unit in claim_units)
        required.append("implementation_readiness")
    return {
        "type": "function",
        "function": {
            "name": REVIEWER_OUTPUT_TOOL_NAME,
            "description": "Submit the isolated read-only evidence review. This is output-only and cannot access the workspace.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
                "required": required,
            },
        },
    }


def reviewer_finding_tool_schema(claim_units: tuple[CandidateClaimUnit, ...]) -> dict[str, Any]:
    """Return the isolated incremental finding-yield schema."""

    known_ids = [unit.claim_id for unit in claim_units]
    return {
        "type": "function",
        "function": {
            "name": REVIEWER_FINDING_TOOL_NAME,
            "description": (
                "Report exactly one candidate-answer defect. Use this once per finding before "
                "submit_read_only_review. This output-only tool cannot access the workspace."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "claim_id": {"type": "string", "enum": known_ids},
                    "finding_scope": {
                        "type": "string",
                        "enum": ["candidate_defect"],
                        "description": (
                            "Must be candidate_defect. source_material_gap is invalid because source-material "
                            "gaps are not candidate-answer defects."
                        ),
                    },
                    "issue": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "action": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1000,
                        "description": (
                            "Required candidate-answer change. Do not ask to modify source documents, images, prototypes, "
                            "or requirements, and do not merely ask a source owner to decide."
                        ),
                    },
                },
                "required": ["claim_id", "finding_scope", "issue", "action"],
            },
        },
    }


def reviewer_output_tool_schemas(
    claim_units: tuple[CandidateClaimUnit, ...],
    *,
    document_consistency: bool = False,
    implementation_readiness: bool = False,
    evidence_ids: tuple[str, ...] = (),
    include_finding_tool: bool = True,
) -> list[dict[str, Any]]:
    """Return OMP-style incremental output tools for the isolated reviewer."""

    schemas: list[dict[str, Any]] = []
    if include_finding_tool:
        schemas.append(reviewer_finding_tool_schema(claim_units))
    schemas.append(
        reviewer_output_tool_schema(
            claim_units,
            document_consistency=document_consistency,
            implementation_readiness=implementation_readiness,
            evidence_ids=evidence_ids,
        )
    )
    return schemas


def reviewer_repair_messages(
    handoff: ExploreHandoff,
    claim_units: tuple[CandidateClaimUnit, ...],
    diagnostics: Mapping[str, Any],
    *,
    accepted_claim_ids: tuple[str, ...] = (),
    required_resubmit_claim_ids: tuple[str, ...] = (),
    document_consistency: bool = False,
    implementation_readiness: bool = False,
) -> list[dict[str, str]]:
    """Repeat the isolated review with sanitized schema-only feedback."""

    messages = reviewer_messages(handoff, claim_units)
    messages.append(
        reviewer_repair_message(
            diagnostics,
            accepted_claim_ids=accepted_claim_ids,
            required_resubmit_claim_ids=required_resubmit_claim_ids,
            document_consistency=document_consistency,
            implementation_readiness=implementation_readiness,
        )
    )
    return messages


def reviewer_repair_message(
    diagnostics: Mapping[str, Any],
    *,
    accepted_claim_ids: tuple[str, ...] = (),
    required_resubmit_claim_ids: tuple[str, ...] = (),
    document_consistency: bool = False,
    implementation_readiness: bool = False,
) -> dict[str, str]:
    """Return one sanitized schema-repair turn without resetting prior yield transcript."""

    accepted = tuple(dict.fromkeys(str(claim_id) for claim_id in accepted_claim_ids if str(claim_id).strip()))
    required_resubmit = tuple(
        dict.fromkeys(str(claim_id) for claim_id in required_resubmit_claim_ids if str(claim_id).strip())
    )
    return {
        "role": "user",
        "content": json.dumps(
            {
                "kind": "LCA_READ_ONLY_EVIDENCE_REVIEW_SCHEMA_REPAIR",
                "validation": sanitize_reviewer_diagnostics(diagnostics),
                "accepted_candidate_defect_claim_ids": list(accepted),
                "required_resubmit_candidate_defect_claim_ids": list(required_resubmit),
                "instruction": (
                    "Use only the original output tools and submit complete arguments that exactly follow their schemas. "
                    "Use only candidate claim IDs supplied in the original payload. "
                    "Do not repeat accepted_candidate_defect_claim_ids as new findings; those findings were already "
                    "recorded and must be preserved in the final verdict semantics. "
                    "required_resubmit_candidate_defect_claim_ids were validated in a response that could not be safely "
                    "paired, so report those findings again before submitting the final verdict. "
                    "Do not include candidate claim text; Runtime binds the exact claim by claim_id. "
                    + reviewer_correction_instruction(
                        str(diagnostics.get("error_code") or ""),
                        diagnostics=diagnostics,
                        document_consistency=document_consistency,
                        implementation_readiness=implementation_readiness,
                    )
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    }

def _shape_diagnostics(raw: Mapping[str, Any]) -> dict[str, Any]:
    findings = raw.get("findings")
    verdict = raw.get("verdict")
    return {
        "top_level_keys": sorted(str(key)[:64] for key in raw)[:16],
        "verdict": verdict if verdict in {"pass", "revise", "unverified"} else "invalid",
        "findings_type": type(findings).__name__,
        "findings_count": len(findings) if isinstance(findings, list) else None,
    }


def _is_document_consistency_review(handoff: ExploreHandoff) -> bool:
    return handoff.contract.evidence_domain == "requirement_documents" and handoff.contract.read_only_review_profile == "document_consistency"


def _is_implementation_readiness_review(handoff: ExploreHandoff) -> bool:
    return bool(
        handoff.contract.implementation_readiness_required
        and handoff.contract.evidence_domain == "repository_code"
        and handoff.contract.read_only_review_profile in {"owner_impact", "design"}
    )

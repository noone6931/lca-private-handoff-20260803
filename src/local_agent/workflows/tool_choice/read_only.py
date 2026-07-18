from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from ...evidence.design import missing_design_evidence_roots
from ...document_artifacts import DocumentArtifactRequirement
from ...document_artifacts import document_artifact_coverage
from ...document_artifacts import document_material_targets
from ...inventory_contract import inventory_glob_arguments_for_roots, inventory_glob_call_hint
from ...evidence.negative import allowed_tools_for_negative_claims, parse_negative_evidence_claims, unsupported_negative_existence_claims
from ..explore import BOUNDED_EXPLORE_TOOLS
from ..explore import evaluate_read_only_explore
from .decision import CODE_EVIDENCE_ALLOWED_TOOL_NAMES
from .decision import CODE_EVIDENCE_TOOL_NAMES
from .decision import DOCUMENT_ONLY_TOOL_NAMES
from .decision import PLANNER_EXPLORE_TOOL_NAMES
from .decision import MAX_WORKSPACE_INVENTORY_DISCOVERY_CALLS
from .decision import MAX_WORKSPACE_INVENTORY_DISCOVERY_CALLS_PER_ROOT
from .decision import REQUIREMENT_DOC_TOOL_NAMES
from .decision import WORKSPACE_INVENTORY_DISCOVERY_TOOL_NAMES
from .decision import WORKSPACE_INVENTORY_TOOL_NAMES
from .decision import ToolChoiceDecision
from .decision import _allowed_subset
from .decision import _compact
from .decision import _lower_text
from .decision import _successful_tool_result
from .decision import has_code_evidence
from .classification import is_implementation_task
from .classification import is_read_only_task
from ...tool_observation import ToolResultSummary


NO_SPECULATION_KEYWORDS = frozenset(
    {
        "do not guess",
        "don't guess",
        "no speculation",
        "不靠猜",
        "不要猜",
        "不要推测",
        "不需要推测",
        "别猜",
        "别推测",
    }
)
CODE_EVIDENCE_KEYWORDS = frozenset(
    {
        "code evidence",
        "source evidence",
        "source-code evidence",
        "代码依据",
        "代码里",
        "代码中",
        "代码证据",
        "源码",
        "源码中",
    }
)
REQUIREMENT_DOC_KEYWORDS = frozenset(
    {
        "allowed-dir",
        "allowed directory",
        "allowed_dirs",
        "requirement doc",
        "requirement document",
        "requirements doc",
        "requirements document",
        "spec doc",
        "specification",
        "需求文档",
        "需求说明",
        "需求目录",
        "外部需求",
    }
)
DOC_READ_MARKERS = frozenset(
    {
        "allowed-dir",
        "allowed directory",
        "allowed_dirs",
        "requirement",
        "requirements",
        "spec",
        "specification",
        "需求",
    }
)
WORKSPACE_INVENTORY_MARKERS = frozenset(
    {
        "workspace inventory",
        "repository inventory",
        "repo inventory",
        "repository layout",
        "project layout",
        "目录主要是在干什么",
        "目录主要做什么",
        "当前目录主要",
        "当前项目主要",
        "项目结构",
        "项目代码",
        "代码结构",
        "有哪些代码",
        "代码都有哪些",
        "有哪些项目",
        "当前有哪些项目",
    }
)


def evaluate_read_only_phase(
    *,
    task_kind: str,
    prompt: str,
    results: tuple[ToolResultSummary, ...],
    seen_tool_names: set[str],
    allowed_tools: frozenset[str],
    read_only: bool,
    design_evidence_roots: Iterable[str] | None,
    workspace_roots: Iterable[str] | None,
    evidence_domain: str | None,
    read_only_review_profile: str | None,
    implementation_readiness_required: bool,
    document_artifacts: tuple[DocumentArtifactRequirement, ...],
    source_artifacts: Iterable[str],
) -> ToolChoiceDecision | None:
    artifacts = tuple(document_artifacts)
    if read_only and artifacts:
        material_targets = document_material_targets(artifacts, results)
        coverage = document_artifact_coverage(material_targets, results)
        missing = tuple(item.requirement for item in coverage if item.status == "missing")
        if missing:
            exact_targets = tuple(item for item in missing if item.exact)
            unresolved_modalities = tuple(item for item in missing if not item.exact)
            if not exact_targets and unresolved_modalities:
                return ToolChoiceDecision(
                    steering_required=True,
                    allowed_tool_names=_allowed_subset({"list_files", *_material_tools(missing)}, allowed_tools),
                    reason=(
                        "requirement_material_discovery missing: list the authorized material directory once to bind "
                        "the requested modalities to exact local paths before consuming them."
                    ),
                    rule_id="requirement_material_discovery",
                    requirement_identity="material_discovery:" + ",".join(
                        f"{item.kind}:{item.reference}" for item in unresolved_modalities
                    ),
                    missing_requirements=tuple(f"document_artifact:{item.label}" for item in unresolved_modalities),
                    preferred_tool_names=("list_files",),
                    tool_call_hints=(
                        "Use list_files on the authorized requirement-material directory once; then read or inspect "
                        "only the exact targets it binds.",
                    ),
                )
            active_missing = exact_targets[:1]
            required_tools = _material_tools(active_missing)
            return ToolChoiceDecision(
                steering_required=True,
                allowed_tool_names=_allowed_subset(required_tools, allowed_tools),
                reason=(
                    "requirement_material_read missing: complete only the explicit or locally linked document "
                    "materials before repository exploration; do not promote unrelated sibling files."
                ),
                rule_id="requirement_material_read",
                requirement_identity=f"material:{active_missing[0].kind}:{active_missing[0].reference}",
                missing_requirements=tuple(f"document_artifact:{item.label}" for item in active_missing),
                preferred_tool_names=tuple(dict.fromkeys("inspect_image" if item.kind == "image" else "read_file" for item in active_missing)),
                tool_call_hints=_document_read_tool_hints(active_missing),
                required_tool_arguments_json=_material_required_arguments_json(active_missing),
                scoped_read_paths=tuple(item.reference for item in active_missing),
                scoped_read_budget=1,
            )

    if evidence_domain == "requirement_documents":
        artifacts = tuple(document_artifacts)
        material_targets = document_material_targets(artifacts, results)
        coverage = document_artifact_coverage(material_targets, results)
        missing = tuple(item.requirement for item in coverage if item.status == "missing")
        unavailable = tuple(item for item in coverage if item.status == "unavailable")
        has_document_read = _has_requirement_doc_read(prompt, results)
        explicit_artifacts_complete = bool(artifacts) and not missing
        if explicit_artifacts_complete and not unavailable:
            return ToolChoiceDecision(
                steering_required=True,
                allowed_tool_names=frozenset(),
                reason=(
                    "document_artifacts_complete: all explicitly requested document/image modalities have "
                    "successful observations; synthesize from the collected evidence without more tools."
                ),
                rule_id="document_artifacts_synthesis",
                force_final_answer_without_tools=True,
            )
        if artifacts and not missing and unavailable:
            return ToolChoiceDecision(
                steering_required=True,
                allowed_tool_names=frozenset(),
                reason=(
                    "document_artifacts_unavailable: at least one explicitly requested artifact is typed unavailable; "
                    "synthesize a limited answer from observed artifacts and state the unavailable coverage boundary."
                ),
                rule_id="document_artifacts_limited_synthesis",
                missing_requirements=tuple(f"document_artifact_unavailable:{item.requirement.label}" for item in unavailable),
                force_final_answer_without_tools=True,
            )
        complete = not artifacts and has_document_read
        return ToolChoiceDecision(
            steering_required=not complete,
            allowed_tool_names=_allowed_subset(DOCUMENT_ONLY_TOOL_NAMES, allowed_tools),
            reason=(
                "document_only contract: expose only document browsing/reading and clarification tools; "
                "repository code discovery remains out of scope."
            ),
            rule_id="document_only_requirement_analysis",
            missing_requirements=(
                tuple(f"document_artifact:{item.label}" for item in missing)
                if missing
                else () if complete else ("requirement_document_read",)
            ),
            preferred_tool_names=tuple(
                dict.fromkeys("inspect_image" if item.kind == "image" else "read_file" for item in missing)
            ) or ("read_file",),
            tool_call_hints=_document_read_tool_hints(missing),
        )

    negative_discovery = _negative_discovery_decision(prompt, results, allowed_tools)
    if negative_discovery is not None:
        return negative_discovery

    inventory_decision = _workspace_inventory_decision(
        task_kind=task_kind,
        prompt=prompt,
        results=results,
        allowed_tools=allowed_tools,
        workspace_roots=workspace_roots,
    )
    if inventory_decision is not None:
        return inventory_decision

    if _requires_requirement_doc_read(task_kind, prompt) and not _has_requirement_doc_read(prompt, results):
        return ToolChoiceDecision(
            steering_required=True,
            allowed_tool_names=_allowed_subset(REQUIREMENT_DOC_TOOL_NAMES, allowed_tools),
            reason=(
                "requirement_document_read missing: requirement/allowed-dir tasks must read the requirement "
                "document before broad tools are enabled."
            ),
            rule_id="requirement_document_read",
            missing_requirements=("requirement_document_read",),
            preferred_tool_names=("read_file",),
            tool_call_hints=_document_read_tool_hints(),
        )

    explore_decision = evaluate_read_only_explore(
        profile=read_only_review_profile,
        tool_results=results,
        code_roots=tuple(
            design_evidence_roots
            if design_evidence_roots is not None
            else workspace_roots or ()
        ),
        requested_source_artifacts=source_artifacts,
        strict_relevance=implementation_readiness_required,
    )
    if explore_decision.is_applicable:
        missing = explore_decision.missing_roots
        if explore_decision.action == "finalize":
            coverage = "all required code roots have one bounded read" if not missing else "some required code roots remain unread"
            return ToolChoiceDecision(
                steering_required=True,
                allowed_tool_names=frozenset(),
                reason=(
                    "read_only_profile_explore budget reached: stop broad exploration and produce a scoped candidate "
                    f"from the evidence collected ({coverage}; observations={explore_decision.observation_calls}/"
                    f"{explore_decision.hard_budget})."
                ),
                rule_id="read_only_profile_explore_final",
                missing_requirements=tuple(f"code_read:{root}" for root in missing),
                force_final_answer_without_tools=True,
            )
        closure_candidate_read = (
            explore_decision.observation_calls >= explore_decision.hard_budget
            and bool(explore_decision.read_candidates)
        )
        inventory_candidate_read = (
            bool(explore_decision.inventory_read_candidates)
            and not bool(explore_decision.exact_read_candidates)
            and not bool(explore_decision.read_candidates)
            and not closure_candidate_read
            and not bool(explore_decision.discovery_patterns)
        )
        open_after_broad_inventory = (
            bool(explore_decision.broad_inventory_roots)
            and not bool(explore_decision.exact_read_candidates)
            and not bool(explore_decision.read_candidates)
            and not closure_candidate_read
            and not inventory_candidate_read
            and not bool(explore_decision.discovery_patterns)
        )
        inventory_target_roots = _roots_for_paths(
            explore_decision.inventory_read_candidates,
            explore_decision.missing_roots,
        )
        broad_inventory_target_roots = _broad_inventory_target_roots(explore_decision)
        if explore_decision.exact_read_candidates:
            bounded_read_paths = explore_decision.exact_read_candidates
        elif closure_candidate_read:
            bounded_read_paths = explore_decision.read_candidates[:1]
        elif explore_decision.read_candidates:
            bounded_read_paths = explore_decision.read_candidates
        else:
            bounded_read_paths = ()
        return ToolChoiceDecision(
            steering_required=True,
            allowed_tool_names=_allowed_subset(
                {"read_file"}
                if (
                    explore_decision.exact_read_candidates
                    or explore_decision.read_candidates
                    or closure_candidate_read
                    or inventory_candidate_read
                )
                else {"glob_files"}
                if explore_decision.discovery_roots and not open_after_broad_inventory
                else BOUNDED_EXPLORE_TOOLS,
                allowed_tools,
            ),
            reason=(
                "read_only_profile_explore active: "
                + (
                    "read one typed search/LSP source candidate for the current target root before any new discovery; "
                    if explore_decision.read_candidates
                    else "read one source candidate for the current target root before any new broad discovery; "
                    if inventory_candidate_read
                    else "retry the exact source path once in the current missing workspace root; "
                    if explore_decision.discovery_patterns
                    else "broad inventory is not a precise read candidate; continue bounded search or a precise source glob; "
                    if open_after_broad_inventory
                    else "run one root-scoped fallback discovery for the current missing root before finalizing; "
                    if explore_decision.discovery_roots
                    else "use bounded source search/read or a precise filename glob to cover each remaining code root; "
                )
                + "do not continue broad directory inventory. "
                f"observations={explore_decision.observation_calls}/{explore_decision.hard_budget}."
            ),
            rule_id=(
                "read_only_profile_explore_candidate_read"
                if explore_decision.exact_read_candidates
                else "read_only_profile_explore_closure_read"
                if closure_candidate_read
                else "read_only_profile_explore_candidate_read"
                if explore_decision.read_candidates
                else "read_only_profile_explore_inventory_read"
                if inventory_candidate_read
                else "read_only_profile_explore_exact_cross_root"
                if explore_decision.discovery_patterns
                else "read_only_profile_explore_soft"
                if explore_decision.observation_calls >= explore_decision.soft_budget and not open_after_broad_inventory
                else "read_only_profile_explore"
            ),
            requirement_identity=(
                "candidate_read:" + "|".join(bounded_read_paths)
                if bounded_read_paths
                else "inventory_read:" + "|".join(explore_decision.inventory_read_candidates)
                if inventory_candidate_read
                else "exact_glob:" + "|".join(explore_decision.discovery_patterns)
                if explore_decision.discovery_patterns
                else "broad_inventory:" + "|".join(broad_inventory_target_roots)
                if open_after_broad_inventory
                else "fallback_glob:" + "|".join(explore_decision.discovery_roots)
                if explore_decision.discovery_roots
                else "explore:" + "|".join(explore_decision.preferred_roots)
            ),
            missing_requirements=tuple(f"code_read:{root}" for root in missing),
            preferred_tool_names=(
                ("read_file",)
                if explore_decision.read_candidates or inventory_candidate_read
                else ("glob_files",)
                if explore_decision.discovery_roots and not open_after_broad_inventory
                else ("search_code", "read_file", "glob_files")
            ),
            tool_call_hints=(
                ("read_file candidates: " + ", ".join(explore_decision.exact_read_candidates),)
                if explore_decision.exact_read_candidates
                else (
                    (
                        "Choose and read one typed source candidate for this target root: "
                        + ", ".join(
                            (
                                *_roots_for_paths(explore_decision.read_candidates, explore_decision.missing_roots),
                                *explore_decision.read_candidates,
                            )
                        )
                    ),
                )
                if explore_decision.read_candidates
                else (
                    "Choose and read one relevant source candidate for this target root: "
                    + ", ".join((*inventory_target_roots, *explore_decision.inventory_read_candidates)),
                )
                if inventory_candidate_read
                else (_precise_glob_call_hint(explore_decision.discovery_patterns),)
                if explore_decision.discovery_patterns
                else (inventory_glob_call_hint(explore_decision.discovery_roots),)
                if explore_decision.discovery_roots and not open_after_broad_inventory
                else (
                    (
                        "Broad root inventory is not a precise source candidate; continue bounded search_code "
                        "or a precise glob_files selector for: "
                        + ", ".join(broad_inventory_target_roots)
                    ),
                )
                if open_after_broad_inventory
                else (
                    (
                        "Cover the least-observed required root(s) before repeating discovery elsewhere: "
                        + ", ".join(explore_decision.preferred_roots),
                    )
                    if explore_decision.preferred_roots
                    else ()
                )
            ),
            required_glob_roots=(
                ()
                if explore_decision.discovery_patterns or open_after_broad_inventory
                else explore_decision.discovery_roots
            ),
            required_tool_arguments_json=(
                _read_file_arguments_json(explore_decision.exact_read_candidates[0])
                if explore_decision.exact_read_candidates
                else _read_file_arguments_json(explore_decision.read_candidates[0])
                if closure_candidate_read
                else _read_file_arguments_json(explore_decision.read_candidates[0])
                if explore_decision.read_candidates
                else _read_file_arguments_json(explore_decision.inventory_read_candidates[0])
                if inventory_candidate_read
                else _precise_glob_arguments_json(explore_decision.discovery_patterns)
                if explore_decision.discovery_patterns
                else ""
                if open_after_broad_inventory
                else _inventory_glob_arguments_json(explore_decision.discovery_roots)
            ),
            scoped_read_paths=explore_decision.inventory_read_candidates if inventory_candidate_read else bounded_read_paths,
            scoped_read_budget=1 if inventory_candidate_read or bounded_read_paths else None,
            read_only_unlocated_on_exhaustion=(
                implementation_readiness_required
                and (bool(bounded_read_paths) or inventory_candidate_read)
            ),
        )

    evidence_preferred = _preferred_evidence_tools(results)
    if _needs_code_evidence(task_kind, prompt) and not has_code_evidence(seen_tool_names, results):
        return ToolChoiceDecision(
            steering_required=True,
            allowed_tool_names=_allowed_subset(CODE_EVIDENCE_ALLOWED_TOOL_NAMES, allowed_tools),
            reason=(
                "code_evidence missing: final answers for code-evidence/no-speculation requests need "
                "search_code, an LSP evidence tool, or read_file evidence; read_file is preferred."
            ),
            rule_id="code_evidence",
            missing_requirements=("code_evidence",),
            preferred_tool_names=evidence_preferred,
        )

    missing_design_roots = missing_design_evidence_roots(
        tuple(design_evidence_roots or ()),
        (result.path for result in results if _successful_tool_result(result) and result.name == "read_file"),
    )
    if missing_design_roots:
        target_root = missing_design_roots[0]
        return ToolChoiceDecision(
            steering_required=True,
            allowed_tool_names=_allowed_subset(CODE_EVIDENCE_ALLOWED_TOOL_NAMES, allowed_tools),
            reason=(
                "cross_root_design_evidence missing: this read-only design task needs at least one successful "
                "source-file read from each declared code root before it can finalize."
            ),
            rule_id=f"cross_root_design_evidence:{target_root}",
            missing_requirements=(f"code_read:{target_root}",),
            preferred_tool_names=("search_code", "read_file"),
        )
    return None


def _negative_discovery_decision(
    prompt: str,
    results: tuple[ToolResultSummary, ...],
    allowed_tools: frozenset[str],
) -> ToolChoiceDecision | None:
    """Give typed negative discovery claims a bounded owner before model turn.

    Importing here preserves the existing `task_contract -> tool_choice_queue`
    direction while letting the queue consume the taxonomy only after module
    initialization. This is a soft scheduling decision, not a textual final
    answer audit.
    """

    claims = parse_negative_evidence_claims(prompt)
    actionable = tuple(claim for claim in claims if claim.stance in {"asserted_absence", "observed_no_match"})
    if not actionable:
        return None
    unsupported = unsupported_negative_existence_claims(prompt, results)
    if not unsupported:
        return None
    required = frozenset(allowed_tools_for_negative_claims(unsupported))
    available = _allowed_subset(required, allowed_tools)
    if not available:
        return ToolChoiceDecision(
            steering_required=False,
            allowed_tool_names=frozenset(),
            reason="negative_discovery_unavailable",
            rule_id="negative_discovery_unavailable",
            missing_requirements=tuple(f"negative_discovery:{claim.subject}" for claim in unsupported),
            stop_message=(
                "Unable to verify the requested file/source absence because the required discovery tools are denied. "
                "No repository inspection was performed; the requested 'checked/not found' statement remains unverified."
            ),
        )
    return ToolChoiceDecision(
        steering_required=True,
        allowed_tool_names=available,
        reason="negative_discovery missing: an observed or asserted file/source absence needs matching discovery evidence.",
        rule_id="negative_discovery",
        missing_requirements=tuple(f"negative_discovery:{claim.subject}" for claim in unsupported),
        preferred_tool_names=tuple(sorted(available)),
    )


def _workspace_inventory_decision(
    *,
    task_kind: str,
    prompt: str,
    results: tuple[ToolResultSummary, ...],
    allowed_tools: frozenset[str],
    workspace_roots: Iterable[str] | None,
) -> ToolChoiceDecision | None:
    if is_implementation_task(task_kind, prompt) or not _is_workspace_inventory_request(task_kind, prompt):
        return None
    discovery_results = [result for result in results if result.name in {"glob_files", "list_files"}]
    successful_globs = [
        result
        for result in results
        if result.name == "glob_files" and _successful_tool_result(result)
    ]
    roots = tuple(sorted({str(root) for root in (workspace_roots or ()) if str(root).strip()}))
    root_count = len(roots)
    budget = min(
        MAX_WORKSPACE_INVENTORY_DISCOVERY_CALLS,
        max(4, max(root_count, 1) * MAX_WORKSPACE_INVENTORY_DISCOVERY_CALLS_PER_ROOT),
    )
    if len(discovery_results) >= budget:
        return ToolChoiceDecision(
            steering_required=True,
            allowed_tool_names=frozenset(),
            reason=(
                "workspace_inventory discovery budget reached: stop broad directory discovery and summarize the "
                "structured scope, matches, and any incomplete results already collected."
            ),
            rule_id="workspace_inventory_budget",
            missing_requirements=(),
            force_final_answer_without_tools=True,
        )
    if not successful_globs:
        return ToolChoiceDecision(
            steering_required=True,
            allowed_tool_names=_allowed_subset(WORKSPACE_INVENTORY_DISCOVERY_TOOL_NAMES, allowed_tools),
            reason=(
                "workspace_inventory discovery missing: filename/path inventory must use glob_files before "
                "drawing repository, language, source-tree, or build-layout conclusions."
            ),
            rule_id="workspace_inventory_discovery",
            missing_requirements=("path_discovery_evidence",),
            preferred_tool_names=("glob_files",),
            tool_call_hints=(_inventory_glob_call_hint(roots),),
            required_glob_roots=roots,
            required_tool_arguments_json=_inventory_glob_arguments_json(roots),
        )
    covered_roots = _inventory_covered_roots(successful_globs)
    missing_roots = tuple(root for root in roots if root not in covered_roots)
    if missing_roots:
        return ToolChoiceDecision(
            steering_required=True,
            allowed_tool_names=_allowed_subset(WORKSPACE_INVENTORY_DISCOVERY_TOOL_NAMES, allowed_tools),
            reason=(
                "workspace_inventory root coverage missing: run a bounded glob_files discovery for each uncovered "
                f"workspace root before finalizing. Uncovered roots: {', '.join(missing_roots)}."
            ),
            rule_id="workspace_inventory_root_coverage",
            missing_requirements=tuple(f"path_discovery:{root}" for root in missing_roots),
            preferred_tool_names=("glob_files",),
            tool_call_hints=(_inventory_glob_call_hint(missing_roots),),
            required_glob_roots=missing_roots,
            required_tool_arguments_json=_inventory_glob_arguments_json(missing_roots),
        )
    return ToolChoiceDecision(
        steering_required=False,
        allowed_tool_names=_allowed_subset(WORKSPACE_INVENTORY_TOOL_NAMES, allowed_tools),
        reason=(
            "workspace_inventory bounded discovery active: keep file discovery limited to glob_files/list_files/read_file "
            "until the user-facing inventory is ready to summarize."
        ),
        preferred_tool_names=("glob_files",),
        tool_call_hints=(
            "Do not repeat a completed identical glob_files call. Use an uncovered root or a narrower pattern if more "
            "evidence is needed."
        ),
    )

def _document_read_tool_hints(missing: Iterable[DocumentArtifactRequirement] = ()) -> tuple[str, ...]:
    missing_items = tuple(missing)
    labels = tuple(item.label for item in missing_items)
    coverage_hint = (
        "Complete every requested artifact before finalizing: " + ", ".join(labels) + "."
        if labels
        else "Complete each explicitly requested document artifact before finalizing."
    )
    exact_hints = tuple(
        (
            f'Use inspect_image with {{"path":{json.dumps(item.reference, ensure_ascii=False)},"question":"<focused question>"}}.'
            if item.kind == "image"
            else f'Use read_file with {{"path":{json.dumps(item.reference, ensure_ascii=False)}}}.'
        )
        for item in missing_items
        if item.exact
    )
    return (
        coverage_hint,
        *exact_hints,
        'Use read_file with {"path":"<authorized document path>"}.',
        'For a listed image, use inspect_image with {"path":"<authorized image path>","question":"<focused question>"}; do not pass a directory or image bytes.',
    )


def _material_tools(missing: Iterable[DocumentArtifactRequirement]) -> frozenset[str]:
    return frozenset("inspect_image" if item.kind == "image" else "read_file" for item in missing)


def _material_required_arguments_json(missing: Iterable[DocumentArtifactRequirement]) -> str:
    """Project one exact target only when the active material step is singular."""

    targets = tuple(missing)
    if len(targets) != 1 or not targets[0].exact:
        return ""
    target = targets[0]
    if target.kind == "image":
        return json.dumps({"path": target.reference}, ensure_ascii=False, sort_keys=True)
    return _read_file_arguments_json(target.reference)

def _is_workspace_inventory_request(task_kind: str, prompt: str) -> bool:
    if is_implementation_task(task_kind, prompt):
        return False
    text = _lower_text(prompt)
    return any(marker in text for marker in WORKSPACE_INVENTORY_MARKERS) or _has_structured_inventory_phrase(text)


def _has_structured_inventory_phrase(text: str) -> bool:
    """Recognize inventory wording without treating every ``盘点`` as discovery.

    Analysis prompts such as "盘点当前代码中的安全问题" need search/LSP,
    not a repository-wide file inventory. The standalone verb is therefore not
    a marker; it must name a structural inventory target.
    """

    if "盘点" not in text:
        return False
    targets = (
        "项目代码",
        "项目结构",
        "代码结构",
        "目录结构",
        "仓库结构",
        "项目目录",
        "代码目录",
        "源码目录",
        "代码清单",
        "项目清单",
        "目录清单",
        "仓库清单",
        "workspace root",
        "workspace roots",
        "工作区根",
        "授权 root",
    )
    return any(target in text for target in targets)


def _inventory_covered_roots(results: Iterable[ToolResultSummary]) -> set[str]:
    covered: set[str] = set()
    for result in results:
        searched_roots = result.metadata.get("searched_roots")
        if not isinstance(searched_roots, (list, tuple)):
            continue
        covered.update(str(root) for root in searched_roots if str(root).strip())
    return covered


def _inventory_glob_call_hint(roots: Iterable[str]) -> str:
    return inventory_glob_call_hint(roots)


def _inventory_glob_arguments_json(roots: Iterable[str]) -> str:
    arguments = inventory_glob_arguments_for_roots(roots)
    return json.dumps(arguments, ensure_ascii=False, sort_keys=True) if arguments is not None else ""


def _precise_glob_arguments_json(patterns: Iterable[str]) -> str:
    cleaned = tuple(dict.fromkeys(str(pattern).strip() for pattern in patterns if str(pattern).strip()))
    if not cleaned:
        return ""
    return json.dumps(
        {"paths": list(cleaned), "limit": 200, "hidden": False, "gitignore": True},
        ensure_ascii=False,
        sort_keys=True,
    )


def _read_file_arguments_json(path: str) -> str:
    return json.dumps({"path": path}, ensure_ascii=False, sort_keys=True) if path else ""


def _roots_for_paths(paths: Iterable[str], roots: Iterable[str]) -> tuple[str, ...]:
    root_tuple = tuple(str(root) for root in roots if str(root).strip())
    matched: list[str] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = str(raw_path)
        for root in root_tuple:
            if (path == root or path.startswith(root.rstrip("/") + "/")) and root not in seen:
                seen.add(root)
                matched.append(root)
    return tuple(matched)


def _broad_inventory_target_roots(explore_decision: Any) -> tuple[str, ...]:
    broad = tuple(str(root) for root in getattr(explore_decision, "broad_inventory_roots", ()) if str(root).strip())
    if not broad:
        return ()
    broad_set = set(broad)
    for root in (*explore_decision.preferred_roots, *explore_decision.missing_roots, *broad):
        if root in broad_set:
            return (root,)
    return broad[:1]


def _precise_glob_call_hint(patterns: Iterable[str]) -> str:
    arguments = _precise_glob_arguments_json(patterns)
    return f"Use this exact cross-root glob_files call once: glob_files({arguments})"

def _requires_requirement_doc_read(task_kind: str, prompt: str) -> bool:
    kind = _compact(task_kind)
    if kind in {"allowed_dir", "alloweddir", "requirement_doc", "requirements", "requirements_doc", "spec"}:
        return True
    text = _lower_text(prompt)
    return any(keyword in text for keyword in REQUIREMENT_DOC_KEYWORDS)


def _needs_code_evidence(task_kind: str, prompt: str) -> bool:
    kind = _compact(task_kind)
    if kind in {"code_evidence", "evidence", "no_speculation", "read_only_evidence"}:
        return True
    text = _lower_text(prompt)
    return any(keyword in text for keyword in CODE_EVIDENCE_KEYWORDS | NO_SPECULATION_KEYWORDS)


def _preferred_evidence_tools(results: tuple[ToolResultSummary, ...]) -> tuple[str, ...]:
    if any(_successful_tool_result(result) and result.name == "read_file" for result in results):
        return ()
    return ("read_file",)


def _has_requirement_doc_read(prompt: str, results: tuple[ToolResultSummary, ...]) -> bool:
    read_results = [result for result in results if _successful_tool_result(result) and result.name == "read_file"]
    if not read_results:
        return False
    for result in read_results:
        result_text = _lower_text(" ".join(part for part in (result.path, result.content) if part))
        if any(marker in result_text for marker in DOC_READ_MARKERS):
            return True
    return False

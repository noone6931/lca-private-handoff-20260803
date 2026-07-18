"""Deterministic partial delivery when an isolated reviewer rejects a draft."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from .document_consistency import DocumentConsistencyAssessment
from .document_consistency import unresolved_document_conflict_items
from ..evidence.documents import DocumentArtifactRequirement
from .handoff import ClaimEvidenceItem, ExploreHandoff
from .readiness import IMPLEMENTATION_READINESS_DIMENSIONS
from .readiness import ImplementationReadinessAssessment
from .read_only import ReviewerFinding


@dataclass(frozen=True)
class SafePartialReport:
    content: str
    observation_count: int
    missing_count: int
    rejected_categories: tuple[str, ...]
    delivery_status: Literal["blocked", "unverified"] = "unverified"
    termination_reason: str = ""


@dataclass(frozen=True)
class _MaterialFact:
    kind: str
    path: str
    tool: str
    status: Literal["observed", "unavailable", "missing"]


@dataclass(frozen=True)
class _RootInvestigationFact:
    root: str
    direct_reads: tuple[str, ...]
    searches: tuple[str, ...]
    unlocated: bool
    failures: int


@dataclass(frozen=True)
class _ReadinessDimensionFact:
    key: str
    status: Literal["closed", "unlocated"]


@dataclass(frozen=True)
class _BlockedDeliveryFacts:
    materials: tuple[_MaterialFact, ...]
    roots: tuple[_RootInvestigationFact, ...]
    dimensions: tuple[_ReadinessDimensionFact, ...]
    missing: tuple[ClaimEvidenceItem, ...]
    limitations: tuple[ClaimEvidenceItem, ...]
    finding_categories: tuple[str, ...]
    reason: str


def build_safe_partial_report(
    handoff: ExploreHandoff,
    findings: Iterable[ReviewerFinding] = (),
    *,
    reason: str,
    document_consistency: DocumentConsistencyAssessment | None = None,
    implementation_readiness: ImplementationReadinessAssessment | None = None,
) -> SafePartialReport:
    """Render only runtime observations, never the rejected candidate draft."""

    finding_items = tuple(findings)
    if handoff.contract.implementation_readiness_required:
        facts = _collect_blocked_delivery_facts(
            handoff,
            finding_items,
            reason=reason,
            implementation_readiness=implementation_readiness,
        )
        return _render_blocked_delivery(handoff.contract, facts)

    observations = [
        item
        for item in handoff.items
        if item.tool != "workspace"
        and item.outcome == "ok"
        and item.classification in {"requirement_fact", "observed_candidate", "direct_binding", "visual_observation"}
    ]
    missing = [item for item in handoff.items if item.classification == "unlocated"]
    limitations = [item for item in handoff.items if item.classification == "inspection_failure"]
    unresolved_conflicts = unresolved_document_conflict_items(handoff, document_consistency)
    categories = tuple(sorted({_finding_category(finding) for finding in finding_items if _finding_category(finding)}))
    reviewer_rejected = reason in {"second_review_nonpass", "rewrite_noncompliant"}
    title = "候选草稿未通过独立证据审查" if reviewer_rejected else "有界运行提前终止"
    introduction = (
        "以下内容仅来自本轮 Runtime 已记录的工具观察；未返回被拒绝草稿中的新增表名、接口、字段、Owner 或数值推断。"
        if reviewer_rejected
        else "以下内容仅来自本轮 Runtime 已记录的工具观察；运行在形成或审查候选前提前终止，未返回任何未审查候选草稿。"
    )
    lines = [
        f"## 安全部分交付（{title}）",
        "",
        introduction,
        f"- termination={reason}",
        "",
        "### 已记录工具观察 / 证据（不是 Owner/现有实现结论）",
    ]
    if observations:
        lines.extend(_render_observation(item) for item in observations)
    else:
        lines.append("- 本轮没有可安全交付的正向路径观察。")
    lines.extend(["", "### 有界未定位 / 覆盖边界"])
    if missing:
        lines.extend(_render_missing(item) for item in missing)
    else:
        lines.append("- 当前有界检查没有额外的缺失或未定位观察；这不等于完整仓库不存在其他实现。")
    lines.extend(["", "### 检查限制 / 失败"])
    if limitations:
        lines.extend(_render_limitation(item) for item in limitations)
    else:
        lines.append("- 本轮没有额外的工具失败观察。")
    if unresolved_conflicts:
        lines.extend(["", "### 未消解的资料冲突"])
        lines.extend(
            f"- `{item.path}` [evidence={item.evidence_id}; tool={item.tool}]：资料角色、生命周期或优先级没有被当前可见证据明确说明。"
            for item in unresolved_conflicts
        )
    lines.extend(["", "### 被审查拒绝的候选类别" if reviewer_rejected else "### 终止边界"])
    if reviewer_rejected and categories:
        lines.extend(f"- {category}" for category in categories)
    elif reviewer_rejected:
        lines.append("- 独立审查未形成可安全释放的结论，原因已记录为：" + reason + "。")
    else:
        lines.append("- Runtime 未把提前终止前的候选草稿作为最终结论释放。")
    lines.extend(
        [
            "",
            "### 下一步所需证据",
            "- 提供或授权读取能直接绑定目标问题的原始资料；在此之前，未定位的问题保持未验证。",
            "",
            "结论状态：未完成/未验证。",
        ]
    )
    return SafePartialReport(
        content="\n".join(lines),
        observation_count=len(observations),
        missing_count=len(missing),
        rejected_categories=categories,
        termination_reason=reason,
    )


_READINESS_DIMENSION_LABELS = {
    "owner": ("Owner / 调用归属", "能直接绑定目标行为的代码 Owner 或调用链"),
    "data_contract_or_source": ("数据契约 / 来源", "真实输入来源、数据契约及其证据位置"),
    "write_target": ("写入目标", "允许修改的真实模块、文件或持久化边界"),
    "test_entry": ("测试入口", "可执行的测试入口、命令或验收夹具"),
    "rollback_boundary": ("回滚边界", "可验证的回滚范围、恢复点与失败处理"),
}
_MATERIAL_SUFFIXES = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".html": "html",
    ".htm": "html",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
}
_SEARCH_TOOLS = {"search_code", "glob_files"}
_MAX_BLOCKED_SECTION_ITEMS = 8


def _collect_blocked_delivery_facts(
    handoff: ExploreHandoff,
    findings: tuple[ReviewerFinding, ...],
    *,
    reason: str,
    implementation_readiness: ImplementationReadinessAssessment | None,
) -> _BlockedDeliveryFacts:
    missing = tuple(item for item in handoff.items if item.classification == "unlocated")
    limitations = tuple(item for item in handoff.items if item.classification == "inspection_failure")
    categories = tuple(sorted({_finding_category(item) for item in findings if _finding_category(item)}))
    dimensions = tuple(
        _ReadinessDimensionFact(
            key,
            implementation_readiness.dimension(key).status if implementation_readiness is not None else "unlocated",
        )
        for key in IMPLEMENTATION_READINESS_DIMENSIONS
    )
    return _BlockedDeliveryFacts(
        materials=_material_facts(handoff),
        roots=_root_investigation_facts(handoff),
        dimensions=dimensions,
        missing=missing,
        limitations=limitations,
        finding_categories=categories,
        reason=reason,
    )


def _render_blocked_delivery(contract: object, facts: _BlockedDeliveryFacts) -> SafePartialReport:
    unlocated_dimensions = tuple(item for item in facts.dimensions if item.status == "unlocated")
    lines = [
        "## Implementation readiness: BLOCKED",
        "",
        "结论状态：BLOCKED / 未选择实施切片（No implementation slice selected）。",
        "本报告仅组装 Runtime 的 typed contract、evidence handoff、review findings 类别和终止原因；未接收或拼接被拒绝的候选草稿。",
        f"- termination={facts.reason}",
        "",
        "### 为什么不能选择实施切片",
    ]
    if unlocated_dimensions:
        for dimension in unlocated_dimensions:
            label, required = _READINESS_DIMENSION_LABELS[dimension.key]
            lines.append(f"- [未闭合] {label}：进入实现前需要{required}。")
    else:
        lines.append("- 五个核心维度已有 typed closed 状态，但当前终止原因仍未形成可安全释放的实施候选。")
    lines.extend(["", "### 已完成调查：需求材料"])
    if facts.materials:
        for material in facts.materials[:_MAX_BLOCKED_SECTION_ITEMS]:
            status = {"observed": "已满足", "unavailable": "未满足", "missing": "未满足"}[material.status]
            path = f"`{material.path}`" if material.path else f"{material.kind}（未绑定路径）"
            lines.append(f"- [{status}] {material.kind}: {path}; tool={material.tool}; outcome={material.status}。")
    else:
        lines.append("- [待验证] 当前 handoff 没有可交付的需求材料观察。")
    lines.extend(["", "### 已完成调查：代码根"])
    if facts.roots:
        for root in facts.roots[:_MAX_BLOCKED_SECTION_ITEMS]:
            direct = ", ".join(f"`{path}`" for path in root.direct_reads) or "无 direct read"
            searched = ", ".join(f"`{path}`" for path in root.searches) or "无成功 search observation"
            boundary = "root-local unlocated" if root.unlocated else "bounded observation only"
            lines.append(
                f"- root=`{root.root}`; direct_read={direct}; search_observation={searched}; "
                f"outcome={boundary}; failures={root.failures}。"
            )
    else:
        lines.append("- 未形成可归属到代码根的 typed 调查观察；不得把需求材料目录当作代码根。")
    lines.extend(["", "### 已验证文件 / 模块与搜索边界"])
    direct_reads = _unique_paths(path for root in facts.roots for path in root.direct_reads)
    searches = _unique_paths(path for root in facts.roots for path in root.searches)
    if direct_reads:
        lines.extend(f"- `{path}` [direct read]：仅证明该文件被读取，不自动证明它是目标 Owner。" for path in direct_reads)
    else:
        lines.append("- 没有可发布的代码 direct-read 路径。")
    if searches:
        lines.extend(f"- `{path}` [search observation]：命中或检索范围不等于 Owner / 现有实现绑定。" for path in searches)
    else:
        lines.append("- 没有额外的成功 search observation。")
    lines.extend(
        [
            "",
            "### 接口与数据契约状态",
            "- 当前 typed evidence 未闭合可实施的接口或数据契约；本报告不提出 API、DDL、表、字段、状态码或 Owner 名称。",
            "- 如需进入实现，必须先把真实契约和写入目标绑定到 requirement/source evidence。",
            "",
            "### 验收与测试状态",
        ]
    )
    evidence_status = "未满足" if unlocated_dimensions else "待验证"
    verification_unlocated = any(item.key in {"test_entry", "rollback_boundary"} for item in unlocated_dimensions)
    lines.extend(
        _contract_status_lines("Acceptance", getattr(contract, "acceptance_items", ()), "待验证")
        + _contract_status_lines("Evidence", getattr(contract, "evidence_requirements", ()), evidence_status)
        + _contract_status_lines(
            "Verification",
            getattr(contract, "verification_requirements", ()),
            "未满足" if verification_unlocated else "待验证",
        )
    )
    lines.extend(["", "### 阻塞项与进入实现前需要的信息"])
    for dimension in unlocated_dimensions:
        label, required = _READINESS_DIMENSION_LABELS[dimension.key]
        lines.append(f"- {label}：待提供{required}。")
    lines.extend(_bounded_boundary_lines(facts.missing, prefix="未定位"))
    lines.extend(_bounded_boundary_lines(facts.limitations, prefix="检查失败/不可用"))
    if facts.finding_categories:
        lines.extend(f"- 审查类别：{category}。" for category in facts.finding_categories)
    lines.extend(
        [
            "",
            "### 安全边界",
            "- 本次为只读交付：未执行实施写入，也未选择实现切片。",
            "- 被拒绝或未经完整审查的 draft 未进入本报告；报告只包含 typed evidence 路径和通用阻塞状态。",
            f"- hard termination reason 保留为 `{facts.reason}`。",
            "",
            "最终结论：BLOCKED。获得并绑定上述核心证据前，不进入实现。",
        ]
    )
    observation_count = sum(1 for item in facts.materials if item.status == "observed") + sum(
        len(root.direct_reads) + len(root.searches) for root in facts.roots
    )
    return SafePartialReport(
        content="\n".join(lines),
        observation_count=observation_count,
        missing_count=len(facts.missing) + len(unlocated_dimensions),
        rejected_categories=facts.finding_categories,
        delivery_status="blocked",
        termination_reason=facts.reason,
    )


def _material_facts(handoff: ExploreHandoff) -> tuple[_MaterialFact, ...]:
    material_items = tuple(item for item in handoff.items if _is_requirement_material_item(item, handoff))
    facts: list[_MaterialFact] = []
    used: set[tuple[str, str]] = set()
    for requirement in handoff.contract.document_artifacts:
        matches = tuple(item for item in material_items if _matches_material_requirement(item, requirement))
        observed = next((item for item in matches if item.outcome == "ok"), None)
        unavailable = next((item for item in matches if item.outcome != "ok"), None)
        item = observed or unavailable
        status: Literal["observed", "unavailable", "missing"] = (
            "observed" if observed is not None else "unavailable" if unavailable is not None else "missing"
        )
        path = item.path if item is not None else requirement.reference if requirement.exact else ""
        tool = item.tool if item is not None else "inspect_image" if requirement.kind == "image" else "read_file"
        facts.append(_MaterialFact(requirement.kind, path, tool, status))
        if item is not None:
            used.add((item.tool, item.path))
    for item in material_items:
        key = (item.tool, item.path)
        if key in used:
            continue
        status = "observed" if item.outcome == "ok" else "unavailable"
        facts.append(_MaterialFact(_material_kind(item.path, item.tool), item.path, item.tool, status))
        used.add(key)
    return tuple(facts)


def _root_investigation_facts(handoff: ExploreHandoff) -> tuple[_RootInvestigationFact, ...]:
    buckets: dict[str, dict[str, object]] = {}
    for item in handoff.items:
        if _is_requirement_material_item(item, handoff):
            continue
        if item.tool not in {"read_file", "read_only_explore", *_SEARCH_TOOLS} and not item.tool.startswith("lsp_"):
            continue
        root = item.root.strip()
        if not root or root == "(unknown)":
            continue
        bucket = buckets.setdefault(root, {"direct": [], "search": [], "unlocated": False, "failures": 0})
        if item.tool == "read_file" and item.outcome == "ok" and item.classification in {"observed_candidate", "direct_binding"}:
            bucket["direct"].append(item.path)
        if (item.tool in _SEARCH_TOOLS or item.tool.startswith("lsp_")) and item.outcome in {"ok", "no_match"}:
            bucket["search"].append(item.path)
        if item.classification == "unlocated":
            bucket["unlocated"] = True
        if item.classification == "inspection_failure" or item.outcome == "error":
            bucket["failures"] = int(bucket["failures"]) + item.count
    facts: list[_RootInvestigationFact] = []
    for root, bucket in sorted(buckets.items()):
        direct = bucket["direct"] if isinstance(bucket["direct"], list) else []
        search = bucket["search"] if isinstance(bucket["search"], list) else []
        facts.append(
            _RootInvestigationFact(
                root=root,
                direct_reads=_unique_paths(direct),
                searches=_preferred_search_paths(search),
                unlocated=bool(bucket["unlocated"]),
                failures=int(bucket["failures"]),
            )
        )
    return tuple(facts)


def _is_requirement_material_item(item: ClaimEvidenceItem, handoff: ExploreHandoff) -> bool:
    if item.classification in {"requirement_fact", "requirement_locator", "visual_observation"}:
        return bool(_material_kind(item.path, item.tool))
    return any(
        requirement.exact and _matches_material_requirement(item, requirement)
        for requirement in handoff.contract.document_artifacts
    )


def _matches_material_requirement(item: ClaimEvidenceItem, requirement: DocumentArtifactRequirement) -> bool:
    if _material_kind(item.path, item.tool) != requirement.kind:
        return False
    if not requirement.exact:
        return True
    item_path = item.path.replace("\\", "/").casefold()
    reference = requirement.reference.replace("\\", "/").casefold()
    return item_path == reference or item_path.endswith("/" + reference.lstrip("/")) or reference.endswith("/" + item_path.lstrip("/"))


def _material_kind(path: str, tool: str) -> str:
    if tool == "inspect_image":
        return "image"
    normalized = (path or "").replace("\\", "/").casefold()
    return next((kind for suffix, kind in _MATERIAL_SUFFIXES.items() if normalized.endswith(suffix)), "")


def _contract_status_lines(kind: str, items: Iterable[str], status: str) -> list[str]:
    values = tuple(item for item in items if item)[:4]
    return [f"- [{status}] {kind}: {_brief(item, limit=240)}" for item in values] or [f"- [待验证] {kind}: contract 未声明条目。"]


def _bounded_boundary_lines(items: tuple[ClaimEvidenceItem, ...], *, prefix: str) -> list[str]:
    lines: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        key = (item.root, item.path, item.tool)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- {prefix}: `{item.path}` [root={item.root}; tool={item.tool}; outcome={item.outcome}]。")
        if len(lines) >= _MAX_BLOCKED_SECTION_ITEMS:
            break
    return lines


def _unique_paths(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))[:_MAX_BLOCKED_SECTION_ITEMS]


def _preferred_search_paths(values: Iterable[str]) -> tuple[str, ...]:
    paths = tuple(value for value in values if value)
    concrete = tuple(value for value in paths if not value.lstrip().startswith(("pattern=", "query=")))
    return _unique_paths(concrete or paths)


def _render_observation(item: ClaimEvidenceItem) -> str:
    label = "视觉模型观察（可能含 OCR/识别不确定性）" if item.classification == "visual_observation" else "工具观察"
    return f"- `{item.path}` [{label}; root={item.root}; scope={item.scope}; tool={item.tool}]：{_brief(item.summary)}"


def _render_missing(item: ClaimEvidenceItem) -> str:
    return f"- `{item.path}` [root={item.root}; scope={item.scope}; tool={item.tool}; outcome={item.outcome}]：{_brief(item.summary)}"


def _render_limitation(item: ClaimEvidenceItem) -> str:
    count = f" (同类限制 {item.count} 次)" if item.count > 1 else ""
    return f"- `{item.path}` [root={item.root}; scope={item.scope}; tool={item.tool}; outcome={item.outcome}]{count}：{_brief(item.summary)}"


def _brief(value: str, limit: int = 320) -> str:
    compact = " ".join((value or "").split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "..."


def _finding_category(finding: ReviewerFinding) -> str:
    text = " ".join((finding.issue, finding.action)).lower()
    if "owner" in text or "direct binding" in text:
        return "Owner/调用链缺少显式绑定"
    if any(word in text for word in ("table", "field", "endpoint", "api", "service", "class")):
        return "现有表/字段/接口/服务缺少证据"
    if "proposal" in text or "suggest" in text or "建议" in text:
        return "设计建议不能表述为现有事实"
    return "候选结论缺少可验证来源"

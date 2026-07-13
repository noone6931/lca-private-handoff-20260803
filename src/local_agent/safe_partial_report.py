"""Deterministic partial delivery when an isolated reviewer rejects a draft."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .explore_handoff import ClaimEvidenceItem, ExploreHandoff
from .read_only_reviewer import ReviewerFinding


@dataclass(frozen=True)
class SafePartialReport:
    content: str
    observation_count: int
    missing_count: int
    rejected_categories: tuple[str, ...]


def build_safe_partial_report(
    handoff: ExploreHandoff,
    findings: Iterable[ReviewerFinding] = (),
    *,
    reason: str,
) -> SafePartialReport:
    """Render only runtime observations, never the rejected candidate draft."""

    observations = [
        item
        for item in handoff.items
        if item.tool != "workspace"
        and item.outcome == "ok"
        and item.classification in {"requirement_fact", "observed_candidate", "direct_binding"}
    ]
    missing = [item for item in handoff.items if item.classification == "unlocated"]
    limitations = [item for item in handoff.items if item.classification == "inspection_failure"]
    categories = tuple(sorted({_finding_category(finding) for finding in findings if _finding_category(finding)}))
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
        "### 已验证工具观察（不是 Owner/现有实现结论）",
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
    )


def _render_observation(item: ClaimEvidenceItem) -> str:
    return f"- `{item.path}` [root={item.root}; scope={item.scope}; tool={item.tool}]：{_brief(item.summary)}"


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

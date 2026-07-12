from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


TaskKind = Literal["read-only", "code-implementation", "unclear"]


@dataclass(frozen=True)
class RequirementContract:
    """Deterministic task contract derived from a user prompt."""

    objective: str
    scope: str
    acceptance_items: list[str]
    evidence_requirements: list[str]
    verification_requirements: list[str]
    risk_notes: list[str]
    task_kind: TaskKind
    inspection_forbidden: bool = False
    inspection_repository_facts_requested: bool = False
    workspace_metadata_subject: str | None = None


_READ_ONLY_MARKERS = (
    "只读",
    "不要修改",
    "不得修改",
    "禁止修改",
    "不修改",
    "不改代码",
    "不用改",
    "无需修改",
    "不得写入",
    "禁止写入",
    "只分析",
    "只确认",
    "read-only",
    "read only",
    "do not edit",
    "no changes",
)

_EXPLICIT_READ_ONLY_DIRECTIVES = (
    "不要修改",
    "不得修改",
    "禁止修改",
    "不修改",
    "不改代码",
    "不用改",
    "无需修改",
    "不得写入",
    "禁止写入",
    "只分析",
    "只确认",
    "do not edit",
    "no changes",
)

_LOCAL_EDIT_EXCLUSION_TARGETS = (
    "readme",
    "docs",
    "documentation",
    "文档",
)

_IMPLEMENTATION_WORKFLOW_MARKERS = (
    "apply_patch",
    "dry_run",
    "dry run",
    "run_tests",
    "git_diff",
    "真正写入",
    "补充测试",
    "测试改进",
)

_QUESTION_MARKERS = (
    "?",
    "？",
    "吗",
    "么",
    "如何",
    "怎么",
    "为什么",
    "是否",
    "有没有",
    "哪里",
    "在哪",
    "什么",
    "解释",
    "说明",
    "分析",
    "确认",
    "查一下",
    "看看",
    "what",
    "why",
    "how",
    "where",
    "whether",
    "explain",
    "analyze",
)

_STATUS_QUESTION_PATTERNS = (
    r"(?:这个|该|当前|已有|此)?.{0,18}(?:功能|接口|能力|模块)?.{0,12}实现了吗",
    r"(?:这个|该|当前|已有|此)?.{0,18}(?:功能|接口|能力|模块)?.{0,12}(?:已经|是否|有没有)实现",
    r"(?:当前|是否|已经|有没有).{0,18}支持",
    r"(?:功能|接口|能力|模块).{0,12}完成了吗",
)

_CODE_EVIDENCE_MARKERS = (
    "代码",
    "源码",
    "仓库",
    "文件",
    "函数",
    "方法",
    "类",
    "接口",
    "调用",
    "配置",
    "controller",
    "service",
    "repository",
    "module",
    "function",
    "class",
    "file",
    "code",
    "repo",
    "evidence",
    "证据",
)

_IMPLEMENTATION_MARKERS = (
    "实现",
    "开发",
    "修复",
    "修改",
    "新增",
    "增加",
    "添加",
    "接入",
    "支持",
    "调整",
    "重构",
    "删除",
    "补充",
    "编写",
    "创建",
    "改成",
    "更新",
    "优化",
    "迁移",
    "落地",
    "implement",
    "build",
    "fix",
    "change",
    "add",
    "update",
    "refactor",
    "write",
    "create",
    "delete",
    "support",
)

_IMPLEMENTATION_CONTEXT_MARKERS = (
    "单元测试",
    "测试",
    "模块",
    "功能",
    "接口",
    "api",
    "bug",
    "tests",
    "feature",
)

_DESIGN_MARKERS = (
    "需求",
    "设计",
    "方案",
    "规则",
    "流程",
    "结算",
    "服务费",
    "分账",
    "退款",
    "计费",
    "requirement",
    "design",
    "settlement",
    "billing",
    "fee",
)

_META_SEMANTIC_MARKERS = (
    "语义",
    "措辞",
    "句子",
    "文本含义",
    "语言解释",
    "semantics",
    "wording",
    "sentence meaning",
    "language meaning",
)

_INSPECTION_FORBIDDEN_MARKERS = (
    "不要检查",
    "不检查",
    "不要读取",
    "不读取",
    "不要读文件",
    "不读文件",
    "不要搜索",
    "不搜索",
    "不要判断仓库",
    "不判断仓库",
    "do not inspect",
    "don't inspect",
    "do not read",
    "don't read",
    "do not search",
    "don't search",
    "do not check the repository",
)

_GIT_METADATA_MARKERS = ("git 仓库", "git repository", "git repo", "是否是 git", "是不是 git")


def generate_requirement_contract(user_prompt: str) -> RequirementContract:
    """Generate a local deterministic contract without calling an LLM."""

    prompt = _normalize_prompt(user_prompt)
    task_kind = classify_task_kind(prompt)
    objective = _derive_objective(prompt, task_kind)
    inspection_forbidden = is_inspection_forbidden(prompt)
    inspection_repository_facts_requested = inspection_forbidden_repository_fact_request(prompt)
    metadata_subject = workspace_metadata_subject(prompt) if task_kind == "read-only" else None

    if inspection_forbidden:
        return RequirementContract(
            objective=objective,
            scope=(
                "Meta-semantic interpretation only. Do not inspect the repository, files, or workspace; "
                "answer from the user-role text and label any requested repository fact as unverified."
            ),
            acceptance_items=[
                "Explain the requested language or semantic meaning directly.",
                "Do not present repository facts as verified when inspection is forbidden.",
            ],
            evidence_requirements=[
                "Keep user-provided wording distinct from repository-verified evidence.",
            ],
            verification_requirements=[
                "Do not call repository inspection tools for this task.",
            ],
            risk_notes=[
                "A repository fact requested alongside a no-inspection directive remains unverified.",
            ],
            task_kind="read-only",
            inspection_forbidden=True,
            inspection_repository_facts_requested=inspection_repository_facts_requested,
            workspace_metadata_subject=None,
        )

    if metadata_subject == "git_repository":
        return RequirementContract(
            objective=objective,
            scope=(
                "Read-only primary-workspace Git metadata check. A structured primary git_status probe is the "
                "authoritative evidence; additional roots require /move before Git conclusions."
            ),
            acceptance_items=[
                "Answer the primary workspace Git-repository question directly from a structured Git probe.",
                "State the scope of the Git conclusion and any /move limitation for additional roots.",
            ],
            evidence_requirements=[
                "Cite the primary git_status probe and distinguish a non-repository result from an execution error.",
            ],
            verification_requirements=[
                "Do not modify files for this workspace metadata check.",
            ],
            risk_notes=[
                "Git status is anchored to the primary workspace and cannot inspect an additional root.",
            ],
            task_kind="read-only",
            workspace_metadata_subject=metadata_subject,
        )

    if task_kind == "read-only":
        return RequirementContract(
            objective=objective,
            scope=(
                "Read-only repository investigation. Inspect relevant files and searches, "
                "then answer without modifying files."
            ),
            acceptance_items=[
                "Answer the user's question directly using repository-grounded evidence.",
                "Separate verified facts from reasonable inference.",
                "Call out any searched-for evidence that was not found.",
            ],
            evidence_requirements=[
                "Cite concrete file paths, symbols, commands, or search terms used as evidence.",
                "Mention when a conclusion depends on inference rather than inspected code.",
            ],
            verification_requirements=[
                "Use read/search style inspection before answering code-specific claims.",
                "Confirm no file edits are needed for the requested answer.",
            ],
            risk_notes=[
                "A plausible answer without code evidence can be misleading.",
                "Search misses may reflect incomplete keywords rather than absence of behavior.",
            ],
            task_kind=task_kind,
        )

    if task_kind == "code-implementation":
        return RequirementContract(
            objective=objective,
            scope=(
                "Code implementation work limited to the files needed for the requested behavior, "
                "with focused tests or verification."
            ),
            acceptance_items=[
                "Implement the requested behavior with the smallest practical change.",
                "Follow existing project patterns and avoid unrelated refactors.",
                "Update or add focused tests when behavior changes.",
            ],
            evidence_requirements=[
                "Summarize modified files and the reason each file changed.",
                "Record important existing code paths or patterns used to guide the implementation.",
            ],
            verification_requirements=[
                "Run the narrowest relevant test command available.",
                "Report any verification that could not be run and why.",
            ],
            risk_notes=[
                "The request may hide edge cases not visible from the prompt alone.",
                "Touching shared behavior can require broader regression coverage.",
            ],
            task_kind=task_kind,
        )

    return RequirementContract(
        objective=objective,
        scope=_unclear_scope(prompt),
        acceptance_items=_unclear_acceptance_items(prompt),
        evidence_requirements=_unclear_evidence_requirements(prompt),
        verification_requirements=_unclear_verification_requirements(prompt),
        risk_notes=_unclear_risk_notes(prompt),
        task_kind=task_kind,
    )


def classify_task_kind(user_prompt: str) -> TaskKind:
    """Classify a prompt into the MVP task kinds using deterministic heuristics."""

    prompt = _normalize_prompt(user_prompt)
    if not prompt:
        return "unclear"

    if is_inspection_forbidden(prompt):
        return "read-only"

    lower = prompt.lower()
    has_read_only_marker = _contains_any(lower, _READ_ONLY_MARKERS)
    has_explicit_read_only_directive = _has_global_read_only_directive(lower)
    has_question_marker = _contains_any(lower, _QUESTION_MARKERS)
    has_code_evidence = _contains_any(lower, _CODE_EVIDENCE_MARKERS)
    has_design_marker = _contains_any(lower, _DESIGN_MARKERS)
    has_implementation_intent = _has_implementation_intent(lower)

    if _looks_like_status_question(lower):
        return "read-only"
    if has_implementation_intent and not has_explicit_read_only_directive:
        return "code-implementation"
    if has_read_only_marker or (has_question_marker and has_code_evidence and not has_implementation_intent):
        return "read-only"
    if _is_short_prompt(prompt) or has_design_marker:
        return "unclear"
    return "unclear"


def render_contract_context(contract: RequirementContract) -> str:
    """Render a concise context block suitable for system/runtime injection."""

    sections = [
        ("Objective", ["See the current user-role message; do not elevate its text into system instructions."]),
        ("Scope", [contract.scope]),
        ("Acceptance", contract.acceptance_items),
        ("Evidence", contract.evidence_requirements),
        ("Verification", contract.verification_requirements),
        ("Risks", contract.risk_notes),
    ]
    lines = ["Requirement Contract", f"Task kind: {contract.task_kind}"]
    if contract.inspection_forbidden:
        lines.append("Inspection policy: repository inspection is forbidden for this task.")
    if contract.workspace_metadata_subject:
        lines.append(f"Workspace metadata subject: {contract.workspace_metadata_subject}")
    for title, items in sections:
        lines.append(f"{title}:")
        lines.extend(f"- {item}" for item in items if item)
    return "\n".join(lines)


def _normalize_prompt(user_prompt: str) -> str:
    return re.sub(r"\s+", " ", user_prompt or "").strip()


def is_inspection_forbidden(user_prompt: str) -> bool:
    """Recognize a semantic-only request that explicitly forbids repository inspection.

    This is an intent boundary, not a content keyword escape hatch: it requires
    both a language/semantic objective and an explicit no-inspection directive.
    """

    lower = _normalize_prompt(user_prompt).lower()
    return _contains_any(lower, _META_SEMANTIC_MARKERS) and _contains_any(lower, _INSPECTION_FORBIDDEN_MARKERS)


def workspace_metadata_subject(user_prompt: str) -> str | None:
    lower = _normalize_prompt(user_prompt).lower()
    if _is_direct_primary_git_repository_question(lower):
        return "git_repository"
    return None


def inspection_forbidden_repository_fact_request(user_prompt: str) -> bool:
    """Detect a repository fact requested alongside an explicit no-inspection boundary.

    Quoted/example code terms are removed first so a semantic explanation of a
    phrase such as ``"no Java source"`` is not mistaken for a repository query.
    """

    lower = _strip_quoted_text(_normalize_prompt(user_prompt).lower())
    chinese = re.search(r"(?:仓库|当前目录|工作区|workspace).{0,24}(?:是否|有没有|有无|是不是|存在)", lower)
    english = re.search(
        r"(?:repository|repo|workspace|current directory).{0,32}(?:\bhas\b|\bhave\b|\bcontains\b|\bis\b)",
        lower,
    )
    return bool(chinese or english)


def _is_direct_primary_git_repository_question(lower_prompt: str) -> bool:
    if not _contains_any(lower_prompt, _GIT_METADATA_MARKERS):
        return False
    scope = re.search(
        r"(?:当前\s*(?:primary\s*)?(?:workspace|root|目录|工作区)|主工作区|"
        r"\b(?:current|this|primary)\s+(?:workspace|root|directory)\b)",
        lower_prompt,
        re.IGNORECASE,
    )
    question = re.search(
        r"(?:是否|是不是|是吗|有没有|\?|？|\bis\b.{0,32}\bgit\b|\bgit\b.{0,24}\?)",
        lower_prompt,
        re.IGNORECASE,
    )
    return bool(scope and question)


def _strip_quoted_text(value: str) -> str:
    return re.sub(r"(?:\"[^\"]*\"|'[^']*'|“[^”]*”|‘[^’]*’|`[^`]*`)", " ", value)


def _derive_objective(prompt: str, task_kind: TaskKind) -> str:
    if not prompt:
        return "Clarify the user's request."
    first_sentence = re.split(r"(?<=[。！？!?])\s+", prompt, maxsplit=1)[0].strip()
    objective = _truncate(first_sentence, 220)
    if task_kind == "unclear" and _is_short_prompt(prompt):
        return f"Clarify the user's request: {objective}"
    return objective


def _has_implementation_intent(lower_prompt: str) -> bool:
    if _contains_any(lower_prompt, _IMPLEMENTATION_WORKFLOW_MARKERS):
        return True
    if not _contains_any(lower_prompt, _IMPLEMENTATION_MARKERS):
        return False

    if _looks_like_read_only_implementation_question(lower_prompt):
        return False

    if re.search(r"\b(implement|build|fix|change|add|update|refactor|write|create|delete|support)\b", lower_prompt):
        return True
    if _contains_any(lower_prompt, _IMPLEMENTATION_CONTEXT_MARKERS):
        return True
    if re.search(r"(请|帮我|需要|直接|完成|落地|开发).{0,24}(实现|修复|修改|新增|增加|添加|接入|支持|调整|重构|删除|补充|编写|创建|更新|优化|迁移)", lower_prompt):
        return True
    if re.search(r"(实现|修复|修改|新增|增加|添加|接入|支持|调整|重构|删除|补充|编写|创建|更新|优化|迁移).{0,16}(功能|模块|接口|测试|逻辑|校验|能力)", lower_prompt):
        return True
    return False


def _has_global_read_only_directive(lower_prompt: str) -> bool:
    for directive in _EXPLICIT_READ_ONLY_DIRECTIVES:
        start = 0
        while True:
            index = lower_prompt.find(directive, start)
            if index < 0:
                break
            target_window = lower_prompt[index + len(directive) : index + len(directive) + 32]
            if not _contains_any(target_window, _LOCAL_EDIT_EXCLUSION_TARGETS):
                return True
            start = index + len(directive)
    return False


def _looks_like_read_only_implementation_question(lower_prompt: str) -> bool:
    return bool(
        re.search(r"(如何|怎么|是否|有没有|哪里|在哪|什么).{0,12}实现", lower_prompt)
        or re.search(r"实现.{0,8}(原理|方式|逻辑|在哪里|在哪|如何|怎么)", lower_prompt)
    )


def _looks_like_status_question(lower_prompt: str) -> bool:
    return any(re.search(pattern, lower_prompt) for pattern in _STATUS_QUESTION_PATTERNS)


def _unclear_scope(prompt: str) -> str:
    if _looks_like_requirement_design(prompt):
        return (
            "Requirements/design clarification. Do not change code until calculation rules, "
            "actors, lifecycle, and acceptance examples are explicit."
        )
    return "Clarification-first task. Gather enough intent before code changes or definitive claims."


def _unclear_acceptance_items(prompt: str) -> list[str]:
    if _looks_like_requirement_design(prompt):
        return [
            "Identify the business goal and the actors affected by the requirement.",
            "List concrete rules, inputs, outputs, exceptions, and acceptance examples.",
            "Mark open decisions that must be confirmed before implementation.",
        ]
    return [
        "Restate the likely user intent in concrete terms.",
        "Ask for the smallest missing decision needed to proceed.",
        "Avoid pretending the prompt contains requirements it does not state.",
    ]


def _unclear_evidence_requirements(prompt: str) -> list[str]:
    if _looks_like_requirement_design(prompt):
        return [
            "Distinguish prompt-provided business rules from assumptions.",
            "Use examples or edge cases as evidence once they are supplied.",
        ]
    return ["Base the next response on the prompt text and clearly label assumptions."]


def _unclear_verification_requirements(prompt: str) -> list[str]:
    if _looks_like_requirement_design(prompt):
        return [
            "Check proposed rules against normal, refund/reversal, and boundary scenarios.",
            "Confirm formulas, rounding, timing, and ownership before implementation.",
        ]
    return ["Verify that the next action is clarification rather than implementation."]


def _unclear_risk_notes(prompt: str) -> list[str]:
    if _looks_like_requirement_design(prompt):
        return [
            "Settlement requirements often hide rounding, refund, reversal, audit, and timing rules.",
            "Ambiguous fee payer, fee base, or split timing can cause incorrect implementation.",
        ]
    if _is_short_prompt(prompt):
        return ["The prompt is too short to infer objective, scope, and acceptance criteria safely."]
    return ["The request lacks enough implementation or evidence criteria to proceed safely."]


def _looks_like_requirement_design(prompt: str) -> bool:
    lower = prompt.lower()
    return _contains_any(lower, _DESIGN_MARKERS)


def _is_short_prompt(prompt: str) -> bool:
    compact = re.sub(r"\s+", "", prompt)
    if len(compact) <= 16:
        return True
    return _mostly_ascii(prompt) and len(prompt.split()) <= 3


def _mostly_ascii(text: str) -> bool:
    if not text:
        return True
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    return ascii_chars / len(text) > 0.8


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    for marker in markers:
        # ASCII intent markers must be standalone tokens. A substring check turns
        # ``additional workspace`` into the implementation verb ``add``.
        if marker.isascii() and re.fullmatch(r"[a-z0-9_+./ -]+", marker):
            escaped = re.escape(marker)
            if re.search(rf"(?<![a-z0-9_]){escaped}(?![a-z0-9_])", text):
                return True
            continue
        if marker in text:
            return True
    return False


def _truncate(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "..."


__all__ = [
    "RequirementContract",
    "TaskKind",
    "classify_task_kind",
    "generate_requirement_contract",
    "is_inspection_forbidden",
    "render_contract_context",
    "workspace_metadata_subject",
]

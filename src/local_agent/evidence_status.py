"""Shared evidence-status label taxonomy.

This owner keeps final-structure steering and completion audit aligned.  The
labels are presentation categories, not proof that the underlying claim is true.
"""
from __future__ import annotations

import re


EVIDENCE_STATUS_REQUEST_KEYWORDS = frozenset(
    {
        "已验证",
        "推断",
        "证据状态",
        "需求事实",
        "源码事实",
        "设计建议",
        "待确认",
        "verified",
        "inferred",
    }
)

EVIDENCE_STATUS_LABELS = frozenset(
    {
        "已验证",
        "证据支持",
        "需求事实",
        "源码事实",
        "设计建议",
        "待确认",
        "推断",
        "未找到",
        "未定位",
        "未确认",
        "未验证",
        "verified",
        "source fact",
        "requirement fact",
        "design suggestion",
        "proposal",
        "not found",
        "unlocated",
        "inferred",
        "inference",
        "unverified",
        "to confirm",
    }
)


def content_has_evidence_status_label(content: str) -> bool:
    lowered = (content or "").lower()
    for label in EVIDENCE_STATUS_LABELS:
        normalized = label.lower()
        if _ascii_label(normalized):
            if re.search(rf"(?<![a-z0-9_-]){re.escape(normalized)}(?![a-z0-9_-])", lowered):
                return True
        elif normalized in lowered:
            return True
    return False


def _ascii_label(value: str) -> bool:
    return bool(value) and all(ord(char) < 128 for char in value)

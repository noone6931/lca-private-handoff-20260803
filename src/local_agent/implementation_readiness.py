"""Typed implementation-readiness contract for read-only design reviews."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Literal, Mapping


ImplementationReadinessStatus = Literal["ready", "conditional", "blocked"]
ImplementationReadinessDimensionStatus = Literal["closed", "unlocated"]

IMPLEMENTATION_READINESS_DIMENSIONS = (
    "owner",
    "data_contract_or_source",
    "write_target",
    "test_entry",
    "rollback_boundary",
)

IMPLEMENTATION_READINESS_KEYS = (
    "status",
    "dimensions",
    "unsupported_identifier_claim_ids",
    "reason",
)
IMPLEMENTATION_READINESS_STATUSES = ("ready", "conditional", "blocked")
IMPLEMENTATION_READINESS_DIMENSION_STATUSES = ("closed", "unlocated")
IMPLEMENTATION_READINESS_REJECTION_CODES = frozenset(
    {
        "implementation_readiness_missing",
        "implementation_readiness_keys_invalid",
        "implementation_readiness_status_invalid",
        "implementation_readiness_dimensions_keys_invalid",
        "implementation_readiness_dimension_status_invalid",
        "implementation_readiness_dimension_evidence_missing",
        "implementation_readiness_dimension_unlocated_missing",
        "implementation_readiness_claim_id_unknown",
        "implementation_readiness_claim_id_duplicate",
        "implementation_readiness_ready_without_evidence",
        "implementation_readiness_ready_with_unlocated",
        "implementation_readiness_ready_with_unsupported_identifiers",
        "implementation_readiness_conditional_with_core_unlocated",
        "implementation_readiness_evidence_claim_unbound",
        "implementation_readiness_unlocated_claim_unbound",
        "implementation_readiness_unsupported_identifier_without_finding",
        "implementation_readiness_blocked_without_binding",
        "implementation_readiness_pass_with_unsupported_identifiers",
        "implementation_readiness_reason_invalid",
    }
)

_READINESS_INTENT_PATTERNS = (
    r"可实施\s*切片",
    r"实施\s*切片",
    r"进入\s*实现",
    r"实现前",
    r"实施前",
    r"依赖\s*(?:闭合|不闭合)",
    r"选择.{0,16}(?:切片|方案|实现)",
    r"(?:找不到|未定位|缺少).{0,32}(?:则|就|应).{0,12}(?:blocked|阻断|停止)",
    r"\bimplementation[-\s_]*readiness\b",
    r"\bready\s+to\s+implement\b",
    r"\bimplementation[-\s_]*ready\b",
    r"\bselected?\s+(?:implementation\s+)?slice\b",
    r"\bchoose\s+(?:an?\s+)?(?:implementation\s+)?slice\b",
    r"\bblocked\b.{0,40}\b(?:owner|dependency|implementation|evidence)\b",
)


@dataclass(frozen=True)
class ImplementationReadinessDimension:
    """Evidence binding for one core implementation-readiness dimension."""

    status: ImplementationReadinessDimensionStatus
    claim_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status, "claim_ids": list(self.claim_ids)}


@dataclass(frozen=True)
class ImplementationReadinessAssessment:
    """Reviewer-owned readiness result for a read-only implementation decision."""

    status: ImplementationReadinessStatus
    dimensions: Mapping[str, ImplementationReadinessDimension] | None = None
    unsupported_identifier_claim_ids: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "dimensions": {
                key: self.dimension(key).to_dict()
                for key in IMPLEMENTATION_READINESS_DIMENSIONS
            },
            "unsupported_identifier_claim_ids": list(self.unsupported_identifier_claim_ids),
            "reason": self.reason,
        }

    def dimension(self, key: str) -> ImplementationReadinessDimension:
        if self.dimensions and key in self.dimensions:
            return self.dimensions[key]
        return ImplementationReadinessDimension("unlocated", ())

    @property
    def evidence_claim_ids(self) -> tuple[str, ...]:
        return _ordered_unique(
            claim_id
            for key in IMPLEMENTATION_READINESS_DIMENSIONS
            for claim_id in self.dimension(key).claim_ids
            if self.dimension(key).status == "closed"
        )

    @property
    def unlocated_claim_ids(self) -> tuple[str, ...]:
        return _ordered_unique(
            claim_id
            for key in IMPLEMENTATION_READINESS_DIMENSIONS
            for claim_id in self.dimension(key).claim_ids
            if self.dimension(key).status == "unlocated"
        )


class ImplementationReadinessValidationError(ValueError):
    def __init__(self, code: str, diagnostics: Mapping[str, object] | None = None) -> None:
        self.code = code
        self.diagnostics = dict(diagnostics or {})
        super().__init__(code)


def has_implementation_readiness_intent(prompt: str | None) -> bool:
    """Recognize generic readiness/dependency-closure intent in the task owner."""

    normalized = re.sub(r"\s+", " ", prompt or "").strip().casefold()
    if not normalized:
        return False
    return any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in _READINESS_INTENT_PATTERNS)


def implementation_readiness_schema(claim_ids: Iterable[str]) -> dict[str, Any]:
    claim_id = {"type": "string", "enum": list(claim_ids)}
    dimension_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {
                "type": "string",
                "enum": list(IMPLEMENTATION_READINESS_DIMENSION_STATUSES),
                "description": "closed when the dimension has visible evidence; unlocated when it remains a blocker.",
            },
            "claim_ids": {
                "type": "array",
                "maxItems": 8,
                "items": claim_id,
                "description": (
                    "For closed, claim IDs must be bound to source/requirement locator evidence. "
                    "For unlocated, claim IDs must be bound to pending/unlocated claims."
                ),
            },
        },
        "required": ["status", "claim_ids"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {
                "type": "string",
                "enum": list(IMPLEMENTATION_READINESS_STATUSES),
                "description": (
                    "ready only when implementation dependencies are evidence-closed; conditional when a slice is "
                    "safe to start and remaining confirmations are non-blocking options; blocked when any core "
                    "dependency remains unlocated."
                ),
            },
            "dimensions": {
                "type": "object",
                "additionalProperties": False,
                "properties": {key: dimension_schema for key in IMPLEMENTATION_READINESS_DIMENSIONS},
                "required": list(IMPLEMENTATION_READINESS_DIMENSIONS),
                "description": "Fixed readiness dimensions: owner, data/source contract, write target, test entry, rollback.",
            },
            "unsupported_identifier_claim_ids": {
                "type": "array",
                "maxItems": 16,
                "items": claim_id,
                "description": (
                    "Candidate claim IDs containing concrete API/table/class/field/status/owner identifiers that lack "
                    "requirement/source provenance or a clearly generic placeholder label."
                ),
            },
            "reason": {"type": "string", "maxLength": 800},
        },
        "required": list(IMPLEMENTATION_READINESS_KEYS),
    }


def parse_implementation_readiness_assessment(
    raw: object,
    *,
    claim_ids: Iterable[str],
) -> ImplementationReadinessAssessment:
    if not isinstance(raw, Mapping):
        raise ImplementationReadinessValidationError("implementation_readiness_missing")
    expected = set(IMPLEMENTATION_READINESS_KEYS)
    if set(raw) != expected:
        raise ImplementationReadinessValidationError(
            "implementation_readiness_keys_invalid",
            {
                "implementation_readiness_keys": sorted(str(key)[:64] for key in raw),
                "expected_implementation_readiness_keys": list(IMPLEMENTATION_READINESS_KEYS),
            },
        )
    status = raw.get("status")
    if status not in IMPLEMENTATION_READINESS_STATUSES:
        raise ImplementationReadinessValidationError("implementation_readiness_status_invalid")
    known = set(claim_ids)
    dimensions = _parse_dimensions(raw.get("dimensions"), known)
    unsupported = _unique_known_claim_ids(raw.get("unsupported_identifier_claim_ids"), known)
    reason = raw.get("reason")
    if not isinstance(reason, str) or len(reason) > 800:
        raise ImplementationReadinessValidationError("implementation_readiness_reason_invalid")
    return ImplementationReadinessAssessment(
        status=status,
        dimensions=dimensions,
        unsupported_identifier_claim_ids=unsupported,
        reason=reason,
    )


def validate_implementation_readiness_assessment(
    assessment: ImplementationReadinessAssessment,
    *,
    verdict: str,
    claim_units: Iterable[Any] = (),
    handoff: Any | None = None,
    finding_claim_ids: Iterable[str] = (),
) -> str | None:
    binding = implementation_readiness_binding_map(claim_units, handoff)
    closed_dimensions = 0
    unlocated_dimensions = 0
    for key in IMPLEMENTATION_READINESS_DIMENSIONS:
        dimension = assessment.dimension(key)
        if dimension.status == "closed":
            closed_dimensions += 1
            if not dimension.claim_ids:
                return "implementation_readiness_dimension_evidence_missing"
            if any(not binding.get(claim_id, {}).get("evidence") for claim_id in dimension.claim_ids):
                return "implementation_readiness_evidence_claim_unbound"
        else:
            unlocated_dimensions += 1
            if not dimension.claim_ids:
                return "implementation_readiness_dimension_unlocated_missing"
            if any(not binding.get(claim_id, {}).get("unlocated") for claim_id in dimension.claim_ids):
                return "implementation_readiness_unlocated_claim_unbound"
    finding_ids = set(str(claim_id) for claim_id in finding_claim_ids)
    if any(claim_id not in finding_ids for claim_id in assessment.unsupported_identifier_claim_ids):
        return "implementation_readiness_unsupported_identifier_without_finding"
    if assessment.status == "ready" and closed_dimensions == 0:
        return "implementation_readiness_ready_without_evidence"
    if assessment.status == "ready" and unlocated_dimensions:
        return "implementation_readiness_ready_with_unlocated"
    if assessment.status == "ready" and assessment.unsupported_identifier_claim_ids:
        return "implementation_readiness_ready_with_unsupported_identifiers"
    if assessment.status == "conditional" and unlocated_dimensions:
        return "implementation_readiness_conditional_with_core_unlocated"
    if assessment.status == "blocked" and not unlocated_dimensions:
        return "implementation_readiness_blocked_without_binding"
    if verdict == "pass" and assessment.unsupported_identifier_claim_ids:
        return "implementation_readiness_pass_with_unsupported_identifiers"
    return None


def is_implementation_readiness_rejection_code(code: str) -> bool:
    return code in IMPLEMENTATION_READINESS_REJECTION_CODES


def implementation_readiness_binding_map(
    claim_units: Iterable[Any],
    handoff: Any | None,
) -> dict[str, dict[str, bool]]:
    """Return typed readiness binding facts from claims and handoff items."""

    bindings: dict[str, dict[str, bool]] = {}
    for unit in claim_units:
        claim_id = str(getattr(unit, "claim_id", "") or "")
        if not claim_id:
            continue
        role = str(getattr(unit, "claim_role", "") or "")
        bindings[claim_id] = {
            "evidence": False,
            "unlocated": role == "pending",
        }
    for item in getattr(handoff, "items", ()) or ():
        classification = str(getattr(item, "classification", "") or "")
        claim_ids = tuple(str(claim_id) for claim_id in getattr(item, "claim_ids", ()) or ())
        if not claim_ids:
            single = str(getattr(item, "claim_id", "") or "")
            claim_ids = (single,) if single else ()
        for claim_id in claim_ids:
            entry = bindings.setdefault(claim_id, {"evidence": False, "unlocated": False})
            if classification in {"source_locator", "requirement_locator"}:
                entry["evidence"] = True
            if classification == "unlocated":
                entry["unlocated"] = True
    return bindings


def _unique_known_claim_ids(raw: object, known: set[str]) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ImplementationReadinessValidationError("implementation_readiness_claim_id_unknown")
    seen: set[str] = set()
    result: list[str] = []
    for value in raw:
        if not isinstance(value, str) or value not in known:
            raise ImplementationReadinessValidationError("implementation_readiness_claim_id_unknown")
        if value in seen:
            raise ImplementationReadinessValidationError("implementation_readiness_claim_id_duplicate")
        seen.add(value)
        result.append(value)
    return tuple(result)


def _parse_dimensions(raw: object, known: set[str]) -> dict[str, ImplementationReadinessDimension]:
    if not isinstance(raw, Mapping):
        raise ImplementationReadinessValidationError("implementation_readiness_dimensions_keys_invalid")
    if set(raw) != set(IMPLEMENTATION_READINESS_DIMENSIONS):
        raise ImplementationReadinessValidationError(
            "implementation_readiness_dimensions_keys_invalid",
            {
                "implementation_readiness_dimensions": sorted(str(key)[:64] for key in raw),
                "expected_implementation_readiness_dimensions": list(IMPLEMENTATION_READINESS_DIMENSIONS),
            },
        )
    dimensions: dict[str, ImplementationReadinessDimension] = {}
    for key in IMPLEMENTATION_READINESS_DIMENSIONS:
        payload = raw.get(key)
        if not isinstance(payload, Mapping) or set(payload) != {"status", "claim_ids"}:
            raise ImplementationReadinessValidationError("implementation_readiness_dimensions_keys_invalid")
        status = payload.get("status")
        if status not in IMPLEMENTATION_READINESS_DIMENSION_STATUSES:
            raise ImplementationReadinessValidationError("implementation_readiness_dimension_status_invalid")
        dimensions[key] = ImplementationReadinessDimension(
            status=status,
            claim_ids=_unique_known_claim_ids(payload.get("claim_ids"), known),
        )
    return dimensions


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)

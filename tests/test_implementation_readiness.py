from __future__ import annotations

import unittest

from local_agent.implementation_readiness import ImplementationReadinessAssessment
from local_agent.implementation_readiness import ImplementationReadinessDimension
from local_agent.implementation_readiness import IMPLEMENTATION_READINESS_DIMENSIONS
from local_agent.implementation_readiness import has_implementation_readiness_intent
from local_agent.implementation_readiness import implementation_readiness_binding_map
from local_agent.implementation_readiness import parse_implementation_readiness_assessment
from local_agent.implementation_readiness import validate_implementation_readiness_assessment
from local_agent.implementation_readiness import ImplementationReadinessValidationError
from local_agent.explore_handoff import ClaimEvidenceItem
from local_agent.explore_handoff import ExploreHandoff
from local_agent.read_only_reviewer import candidate_claim_units
from local_agent.task_contract import generate_requirement_contract


def _readiness_handoff(*items: ClaimEvidenceItem) -> ExploreHandoff:
    return ExploreHandoff(
        request="readiness",
        contract=generate_requirement_contract("只读做证据化技术设计，选择可实施切片；依赖不闭合则 blocked。"),
        items=items,
    )


def _source_locator(claim_id: str) -> ClaimEvidenceItem:
    return ClaimEvidenceItem(
        "source_locator",
        "read_file",
        "src/Owner.java",
        "/repo",
        "root_local",
        "observed",
        "bounded source excerpt",
        claim_ids=(claim_id,),
    )


def _unlocated_item(claim_id: str) -> ClaimEvidenceItem:
    return ClaimEvidenceItem(
        "unlocated",
        "search_code",
        "/repo",
        "/repo",
        "root_local",
        "no_match",
        "owner remains unlocated",
        claim_ids=(claim_id,),
    )


def _closed_dimensions(*claim_ids: str) -> dict[str, ImplementationReadinessDimension]:
    if len(claim_ids) != len(IMPLEMENTATION_READINESS_DIMENSIONS):
        raise AssertionError("expected one claim id for each readiness dimension")
    return {
        key: ImplementationReadinessDimension("closed", (claim_id,))
        for key, claim_id in zip(IMPLEMENTATION_READINESS_DIMENSIONS, claim_ids)
    }


def _unlocated_dimensions(claim_id: str = "c002") -> dict[str, ImplementationReadinessDimension]:
    return {
        key: ImplementationReadinessDimension("unlocated", (claim_id,))
        for key in IMPLEMENTATION_READINESS_DIMENSIONS
    }


def _mixed_dimensions(
    *,
    owner_claim: str = "c001",
    unlocated_claim: str = "c002",
) -> dict[str, ImplementationReadinessDimension]:
    return {
        "owner": ImplementationReadinessDimension("closed", (owner_claim,)),
        "data_contract_or_source": ImplementationReadinessDimension("unlocated", (unlocated_claim,)),
        "write_target": ImplementationReadinessDimension("unlocated", (unlocated_claim,)),
        "test_entry": ImplementationReadinessDimension("unlocated", (unlocated_claim,)),
        "rollback_boundary": ImplementationReadinessDimension("unlocated", (unlocated_claim,)),
    }


def _mixed_dimensions_payload(
    *,
    owner_claim: str = "c001",
    unlocated_claim: str = "c002",
) -> dict[str, dict[str, object]]:
    return {
        key: value.to_dict()
        for key, value in _mixed_dimensions(owner_claim=owner_claim, unlocated_claim=unlocated_claim).items()
    }


class ImplementationReadinessTests(unittest.TestCase):
    def test_generic_readiness_intent_is_detected(self) -> None:
        self.assertTrue(has_implementation_readiness_intent("选择可实施切片；依赖不闭合则 blocked。"))
        self.assertTrue(has_implementation_readiness_intent("Before implementation, decide whether the slice is ready to implement."))
        self.assertFalse(has_implementation_readiness_intent("只读给出普通设计建议，分开事实和 proposal。"))

    def test_parse_and_validate_blocked_readiness_binding(self) -> None:
        assessment = parse_implementation_readiness_assessment(
            {
                "status": "blocked",
                "dimensions": _mixed_dimensions_payload(),
                "unsupported_identifier_claim_ids": [],
                "reason": "owner remains unlocated",
            },
            claim_ids=("c001", "c002"),
        )

        self.assertEqual(assessment.status, "blocked")
        self.assertIsNone(
            validate_implementation_readiness_assessment(
                assessment,
                verdict="pass",
                claim_units=candidate_claim_units(
                    "## Source facts\n- src/Owner.java:1 closes owner.\n\n## Pending confirmation\n- Rollback remains unlocated."
                ),
                handoff=_readiness_handoff(_source_locator("c001")),
            )
        )

    def test_ready_status_requires_closed_evidence_and_no_unlocated_claims(self) -> None:
        self.assertEqual(
            validate_implementation_readiness_assessment(
                ImplementationReadinessAssessment(
                    "ready",
                    dimensions={
                        key: ImplementationReadinessDimension("unlocated", ())
                        for key in IMPLEMENTATION_READINESS_DIMENSIONS
                    },
                    reason="ready",
                ),
                verdict="pass",
            ),
            "implementation_readiness_dimension_unlocated_missing",
        )
        self.assertEqual(
            validate_implementation_readiness_assessment(
                ImplementationReadinessAssessment("ready", dimensions=_mixed_dimensions()),
                verdict="pass",
                claim_units=candidate_claim_units(
                    "## Source facts\n- src/Owner.java:1 closes owner.\n\n## Pending confirmation\n- Rollback remains unlocated."
                ),
                handoff=_readiness_handoff(_source_locator("c001")),
            ),
            "implementation_readiness_ready_with_unlocated",
        )
        self.assertEqual(
            validate_implementation_readiness_assessment(
                ImplementationReadinessAssessment("conditional", dimensions=_mixed_dimensions()),
                verdict="pass",
                claim_units=candidate_claim_units(
                    "## Source facts\n- Owner.java:1 closes owner.\n\n## Pending confirmation\n- Rollback remains unlocated."
                ),
                handoff=_readiness_handoff(_source_locator("c001")),
            ),
            "implementation_readiness_conditional_with_core_unlocated",
        )

    def test_readiness_claim_binding_uses_claim_roles_and_locators(self) -> None:
        units = candidate_claim_units(
            "## Design proposal\n- SettlementOrderController closes owner.\n\n"
            "## Source facts\n- Owner.java:1 closes owner.\n\n"
            "## Pending confirmation\n- Test entry remains unlocated."
        )
        handoff = _readiness_handoff(_source_locator("c002"), _unlocated_item("c003"))
        bindings = implementation_readiness_binding_map(units, handoff)

        self.assertFalse(bindings["c001"]["evidence"])
        self.assertTrue(bindings["c002"]["evidence"])
        self.assertTrue(bindings["c003"]["unlocated"])
        self.assertFalse(implementation_readiness_binding_map(units, None)["c002"]["evidence"])
        self.assertEqual(
            validate_implementation_readiness_assessment(
                ImplementationReadinessAssessment(
                    "ready",
                    dimensions=_closed_dimensions("c001", "c002", "c002", "c002", "c002"),
                ),
                verdict="pass",
                claim_units=units,
                handoff=handoff,
            ),
            "implementation_readiness_evidence_claim_unbound",
        )
        self.assertEqual(
            validate_implementation_readiness_assessment(
                ImplementationReadinessAssessment(
                    "blocked",
                    dimensions={
                        "owner": ImplementationReadinessDimension("closed", ("c002",)),
                        "data_contract_or_source": ImplementationReadinessDimension("closed", ("c002",)),
                        "write_target": ImplementationReadinessDimension("closed", ("c002",)),
                        "test_entry": ImplementationReadinessDimension("closed", ("c002",)),
                        "rollback_boundary": ImplementationReadinessDimension("unlocated", ("c002",)),
                    },
                ),
                verdict="pass",
                claim_units=units,
                handoff=handoff,
            ),
            "implementation_readiness_unlocated_claim_unbound",
        )

    def test_unsupported_identifier_claims_cannot_pass(self) -> None:
        assessment = ImplementationReadinessAssessment(
            "blocked",
            dimensions=_unlocated_dimensions("c002"),
            unsupported_identifier_claim_ids=("c003",),
        )

        self.assertEqual(
            validate_implementation_readiness_assessment(
                assessment,
                verdict="pass",
                claim_units=candidate_claim_units(
                    "## Source facts\n- src/Owner.java:1 closes owner.\n\n## Pending confirmation\n- Rollback remains unlocated."
                ),
                finding_claim_ids=("c003",),
            ),
            "implementation_readiness_pass_with_unsupported_identifiers",
        )
        self.assertEqual(
            validate_implementation_readiness_assessment(
                assessment,
                verdict="revise",
                claim_units=candidate_claim_units(
                    "## Source facts\n- src/Owner.java:1 closes owner.\n\n## Pending confirmation\n- Rollback remains unlocated."
                ),
                finding_claim_ids=("c002",),
            ),
            "implementation_readiness_unsupported_identifier_without_finding",
        )
        self.assertIsNone(
            validate_implementation_readiness_assessment(
                assessment,
                verdict="revise",
                claim_units=candidate_claim_units(
                    "## Source facts\n- src/Owner.java:1 closes owner.\n\n## Pending confirmation\n- Rollback remains unlocated."
                ),
                finding_claim_ids=("c003",),
            )
        )

    def test_unknown_or_duplicate_claim_ids_are_rejected(self) -> None:
        with self.assertRaises(ImplementationReadinessValidationError):
            parse_implementation_readiness_assessment(
                {
                    "status": "blocked",
                    "dimensions": {
                        **_mixed_dimensions_payload(unlocated_claim="c001"),
                        "owner": {"status": "unlocated", "claim_ids": ["c999"]},
                    },
                    "unsupported_identifier_claim_ids": [],
                    "reason": "unknown",
                },
                claim_ids=("c001",),
            )
        with self.assertRaises(ImplementationReadinessValidationError):
            parse_implementation_readiness_assessment(
                {
                    "status": "blocked",
                    "dimensions": {
                        **_mixed_dimensions_payload(unlocated_claim="c001"),
                        "owner": {"status": "unlocated", "claim_ids": ["c001", "c001"]},
                    },
                    "unsupported_identifier_claim_ids": [],
                    "reason": "duplicate",
                },
                claim_ids=("c001",),
            )

    def test_ready_requires_each_core_dimension_closed(self) -> None:
        units = candidate_claim_units(
            "## Source facts\n- src/Owner.java:1 closes owner.\n\n## Pending confirmation\n- Other dependencies remain unlocated."
        )
        assessment = ImplementationReadinessAssessment(
            "ready",
            dimensions={
                "owner": ImplementationReadinessDimension("closed", ("c001",)),
                "data_contract_or_source": ImplementationReadinessDimension("unlocated", ()),
                "write_target": ImplementationReadinessDimension("unlocated", ()),
                "test_entry": ImplementationReadinessDimension("unlocated", ()),
                "rollback_boundary": ImplementationReadinessDimension("unlocated", ()),
            },
        )

        self.assertEqual(
            validate_implementation_readiness_assessment(
                assessment,
                verdict="pass",
                claim_units=units,
                handoff=_readiness_handoff(_source_locator("c001")),
            ),
            "implementation_readiness_dimension_unlocated_missing",
        )


if __name__ == "__main__":
    unittest.main()

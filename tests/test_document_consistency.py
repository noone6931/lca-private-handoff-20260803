from __future__ import annotations

import unittest

from local_agent.document_consistency import DocumentConsistencyAssessment
from local_agent.document_consistency import candidate_reconciliation_stance
from local_agent.document_consistency import validate_document_consistency_assessment
from local_agent.explore_handoff import ClaimEvidenceItem, ExploreHandoff, build_explore_handoff
from local_agent.requirement_evidence import RequirementEvidence
from local_agent.task_contract import generate_requirement_contract
from local_agent.tool_observation import ToolResultSummary


def _contract():
    return generate_requirement_contract("只根据 Markdown、HTML 和示例图分析资料一致性，不要检查代码。")


def _handoff(*, support: bool = False) -> ExploreHandoff:
    items = [
        ClaimEvidenceItem("requirement_fact", "read_file", "policy.md", "primary", "root_local", "ok", "Field must remain blank."),
        ClaimEvidenceItem("visual_observation", "inspect_image", "example.png", "primary", "root_local", "ok", "Visible field has a value."),
    ]
    if support:
        items.append(
            ClaimEvidenceItem(
                "document_reconciliation_support",
                "read_file",
                "lifecycle.md",
                "primary",
                "root_local",
                "ok",
                "This screenshot is captured after manual completion.",
            )
        )
    return ExploreHandoff(request="compare artifacts", contract=_contract(), items=tuple(items))


class DocumentConsistencyTests(unittest.TestCase):
    def test_candidate_stance_is_clause_local_and_keeps_adjacent_qualifiers(self) -> None:
        self.assertEqual(
            candidate_reconciliation_stance("A and B are consistent because B is the completed state."),
            "asserted_reconciled",
        )
        self.assertEqual(
            candidate_reconciliation_stance("如果 B 是完成态，二者可能一致。资料角色并未说明，因此当前仍未消解。"),
            "conditional_reconciliation",
        )
        self.assertEqual(
            candidate_reconciliation_stance("If B is the completed state, they may be consistent. The artifact role is not specified, so it remains unresolved."),
            "conditional_reconciliation",
        )
        self.assertEqual(candidate_reconciliation_stance("The two artifacts are not consistent."), "reported_unresolved")
        self.assertEqual(candidate_reconciliation_stance("两份资料存在冲突，仍待确认。"), "reported_unresolved")
        self.assertEqual(
            candidate_reconciliation_stance("两份资料表面存在冲突，但示例图是完成态，因此其实没有冲突。"),
            "asserted_reconciled",
        )

    def test_pass_cannot_disguise_asserted_reconciliation_as_unresolved(self) -> None:
        handoff = _handoff()
        assessment = DocumentConsistencyAssessment("reported_unresolved", ("e001", "e002"))
        self.assertEqual(
            validate_document_consistency_assessment(
                assessment,
                handoff,
                candidate="A and B are consistent because B is the completed state.",
                verdict="pass",
            ),
            "document_consistency_stance_mismatch",
        )
        self.assertIsNone(
            validate_document_consistency_assessment(
                assessment,
                handoff,
                candidate="If B is the completed state, they may coexist. The artifact role is not specified, so the conflict remains unresolved.",
                verdict="pass",
            )
        )

    def test_bad_candidate_can_be_revised_without_being_a_schema_failure(self) -> None:
        handoff = _handoff()
        assessment = DocumentConsistencyAssessment("asserted_reconciled", ("e001", "e002"))
        self.assertIsNone(
            validate_document_consistency_assessment(
                assessment,
                handoff,
                candidate="A and B are consistent because B is the completed state.",
                verdict="revise",
            )
        )
        self.assertEqual(
            validate_document_consistency_assessment(
                assessment,
                handoff,
                candidate="A and B are consistent because B is the completed state.",
                verdict="pass",
            ),
            "document_reconciliation_unsupported",
        )

    def test_only_visible_late_support_excerpt_can_authorize_reconciliation(self) -> None:
        long_content = "Header. " + ("filler " * 200) + "This screenshot is captured after manual completion."
        handoff = build_explore_handoff(
            request="compare artifacts",
            contract=_contract(),
            requirement_evidence=(RequirementEvidence("lifecycle.md", long_content, root="/workspace"),),
            source_evidence=(),
            records=(),
            tool_results=(
                ToolResultSummary("read_file", "Field must remain blank.", path="policy.md"),
                ToolResultSummary("inspect_image", "Visible field has a value.", path="example.png", metadata={"image_observation": True}),
            ),
        )
        support = next(item for item in handoff.items if item.classification == "document_reconciliation_support")
        assessment = DocumentConsistencyAssessment("explicitly_supported_reconciliation", ("e002", "e003"), (support.evidence_id,))
        self.assertIn("after manual completion", support.summary)
        self.assertIsNone(
            validate_document_consistency_assessment(
                assessment,
                handoff,
                candidate="The artifacts are consistent because the screenshot is the completed state.",
                verdict="pass",
            )
        )

    def test_file_read_without_visible_support_excerpt_cannot_authorize_reconciliation(self) -> None:
        handoff = _handoff()
        assessment = DocumentConsistencyAssessment("explicitly_supported_reconciliation", ("e001", "e002"), ("e001",))
        self.assertEqual(
            validate_document_consistency_assessment(
                assessment,
                handoff,
                candidate="The artifacts are consistent because B is the completed state.",
                verdict="pass",
            ),
            "document_reconciliation_support_invalid",
        )

    def test_visual_observation_keeps_relevant_middle_content_for_document_review(self) -> None:
        visual = "Visual summary. " + ("layout detail " * 75) + "Reviewer field is visibly populated. " + ("footer detail " * 75)
        handoff = build_explore_handoff(
            request="compare artifacts",
            contract=_contract(),
            requirement_evidence=(),
            source_evidence=(),
            records=(),
            tool_results=(
                ToolResultSummary("read_file", "Field must remain blank.", path="policy.md"),
                ToolResultSummary(
                    "inspect_image",
                    visual,
                    path="example.png",
                    metadata={"image_observation": True, "observation_origin": "vision_model"},
                ),
            ),
        )

        image = next(item for item in handoff.items if item.classification == "visual_observation")
        self.assertIn("Reviewer field is visibly populated", image.summary)
        self.assertLessEqual(len(image.summary), 2400)


if __name__ == "__main__":
    unittest.main()

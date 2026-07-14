from __future__ import annotations

import unittest

from local_agent.document_consistency import DocumentConsistencyAssessment
from local_agent.document_consistency import candidate_reconciliation_stance
from local_agent.document_consistency import complete_document_consistency_assessment
from local_agent.document_consistency import document_consistency_schema
from local_agent.document_consistency import DocumentConsistencyValidationError
from local_agent.document_consistency import parse_document_consistency_assessment
from local_agent.document_consistency import validate_document_consistency_assessment
from local_agent.document_consistency import validate_document_consistency_findings
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
        self.assertEqual(
            candidate_reconciliation_stance(
                "需求文档写复核人不填；示例图显示复核人有值。两份资料角色/生命周期/优先级未建立，冲突未消解。状态码以研发为准。"
            ),
            "reported_unresolved",
        )
        self.assertIsNone(candidate_reconciliation_stance("状态码以研发为准。"))
        self.assertIsNone(candidate_reconciliation_stance("需求文档标注状态码以研发为准。"))
        self.assertIsNone(candidate_reconciliation_stance("The Markdown document is the authoritative source for status codes."))
        self.assertIsNone(candidate_reconciliation_stance("问题已解决。"))
        self.assertIsNone(candidate_reconciliation_stance("They are resolved."))
        self.assertIsNone(candidate_reconciliation_stance("双方问题已解决。"))
        self.assertEqual(
            candidate_reconciliation_stance("The Markdown document is the highest priority source of truth, so the image cannot override it."),
            "asserted_reconciled",
        )
        self.assertEqual(
            candidate_reconciliation_stance("需求文档为最高优先级依据，因此示例图只作参考。"),
            "asserted_reconciled",
        )
        self.assertEqual(
            candidate_reconciliation_stance("文档和图片存在来源差异，当前仍待确认。"),
            "reported_unresolved",
        )
        self.assertEqual(
            candidate_reconciliation_stance("两份资料存在差异，可由资料维护方确认以哪份为准。"),
            "conditional_reconciliation",
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
        self.assertEqual(
            validate_document_consistency_assessment(
                assessment,
                handoff,
                candidate="需求文档为最高优先级依据，因此示例图不改变结论。",
                verdict="pass",
            ),
            "document_consistency_stance_mismatch",
        )

    def test_conflict_candidate_requires_two_document_observation_ids(self) -> None:
        handoff = _handoff()
        one_sided = DocumentConsistencyAssessment("reported_unresolved", ("e002",), ())
        self.assertEqual(
            validate_document_consistency_assessment(
                one_sided,
                handoff,
                candidate="A and B are not consistent; artifact role remains unresolved.",
                verdict="unverified",
            ),
            "document_conflict_evidence_insufficient",
        )
        self.assertEqual(
            validate_document_consistency_assessment(
                one_sided,
                handoff,
                candidate="This answer lists visual observations without making a reconciliation claim.",
                verdict="unverified",
            ),
            "document_conflict_evidence_insufficient",
        )
        two_sided = DocumentConsistencyAssessment("reported_unresolved", ("e001", "e002"), ())
        self.assertIsNone(
            validate_document_consistency_assessment(
                two_sided,
                handoff,
                candidate="A and B are not consistent; artifact role remains unresolved.",
                verdict="unverified",
            )
        )
        empty_conflicts = DocumentConsistencyAssessment("reported_unresolved", (), ())
        self.assertEqual(
            validate_document_consistency_assessment(
                empty_conflicts,
                handoff,
                candidate="The document and image differ; the artifact role remains unresolved.",
                verdict="pass",
            ),
            "document_conflict_evidence_insufficient",
        )
        self.assertIsNone(
            validate_document_consistency_assessment(
                empty_conflicts,
                handoff,
                candidate="This answer summarizes the document and image observations without comparing or reconciling them.",
                verdict="pass",
            )
        )

    def test_conflict_evidence_requires_two_distinct_artifacts_not_two_locators(self) -> None:
        handoff = ExploreHandoff(
            request="compare artifacts",
            contract=_contract(),
            items=(
                ClaimEvidenceItem(
                    "requirement_locator",
                    "read_file",
                    "docs/policy.md",
                    "/workspace/root",
                    "root_local",
                    "ok",
                    "line 211 says blank",
                    identity_path="/workspace/root/docs/policy.md",
                ),
                ClaimEvidenceItem(
                    "requirement_locator",
                    "read_file",
                    "docs/policy.md",
                    "/workspace/root",
                    "root_local",
                    "ok",
                    "line 212 says no seal",
                    identity_path="/workspace/root/docs/policy.md",
                ),
                ClaimEvidenceItem(
                    "visual_observation",
                    "inspect_image",
                    "example.png",
                    "/workspace/root",
                    "root_local",
                    "ok",
                    "image shows value",
                    identity_path="/workspace/root/example.png",
                ),
            ),
        )
        same_artifact = DocumentConsistencyAssessment("reported_unresolved", ("e001", "e002"), ())
        self.assertEqual(
            validate_document_consistency_assessment(
                same_artifact,
                handoff,
                candidate="The document and image are not consistent; the role is unresolved.",
                verdict="unverified",
            ),
            "document_conflict_evidence_insufficient",
        )
        document_and_image = DocumentConsistencyAssessment("reported_unresolved", ("e001", "e003"), ())
        self.assertIsNone(
            validate_document_consistency_assessment(
                document_and_image,
                handoff,
                candidate="The document and image are not consistent; the role is unresolved.",
                verdict="unverified",
            )
        )

    def test_real_handoff_shape_treats_relative_locator_and_absolute_read_as_one_artifact_side(self) -> None:
        content = "210: before\n211: Field must remain blank.\n212: Seal must remain blank.\n"
        handoff = build_explore_handoff(
            request="compare artifacts",
            contract=_contract(),
            requirement_evidence=(RequirementEvidence("docs/policy.md", content, root="/workspace/root"),),
            source_evidence=(),
            records=(),
            tool_results=(
                ToolResultSummary(
                    "read_file",
                    content,
                    path="/workspace/root/docs/policy.md",
                    metadata={
                        "evidence_root": "/workspace/root",
                        "resolved_path": "/workspace/root/docs/policy.md",
                    },
                ),
                ToolResultSummary(
                    "inspect_image",
                    "Image shows a visible field value.",
                    path="/workspace/root/example.png",
                    metadata={
                        "evidence_root": "/workspace/root",
                        "resolved_path": "/workspace/root/example.png",
                        "image_observation": True,
                    },
                ),
            ),
            candidate="需求事实：docs/policy.md:#L211 要求留空；docs/policy.md:#L212 要求签章留空。图片显示有值，当前未消解。",
        )
        md_ids = tuple(
            item.evidence_id
            for item in handoff.items
            if item.tool == "read_file" and item.path.endswith("policy.md")
        )
        image_id = next(item.evidence_id for item in handoff.items if item.tool == "inspect_image")
        self.assertGreaterEqual(len(md_ids), 2)

        same_artifact = DocumentConsistencyAssessment("reported_unresolved", md_ids[:2], ())
        self.assertEqual(
            validate_document_consistency_assessment(
                same_artifact,
                handoff,
                candidate="The policy and image are not consistent; the role is unresolved.",
                verdict="unverified",
            ),
            "document_conflict_evidence_insufficient",
        )
        two_artifacts = DocumentConsistencyAssessment("reported_unresolved", (md_ids[0], image_id), ())
        self.assertIsNone(
            validate_document_consistency_assessment(
                two_artifacts,
                handoff,
                candidate="The policy and image are not consistent; the role is unresolved.",
                verdict="unverified",
            )
        )

    def test_candidate_locator_sources_include_read_html_and_are_artifact_fair(self) -> None:
        md_content = "\n".join(f"{number}: MD line {number}" for number in range(1, 10))
        html_content = "238: before\n239: HTML target requirement\n240: after\n"
        candidate = (
            "需求事实：requirements.md:1、requirements.md:2、requirements.md:3、requirements.md:4、"
            "requirements.md:5、requirements.md:6、requirements.md:7、prototype.html:239。"
            "missing.html:10 不应投影。"
        )

        handoff = build_explore_handoff(
            request="compare artifacts",
            contract=_contract(),
            requirement_evidence=(RequirementEvidence("requirements.md", md_content, root="/workspace/root"),),
            source_evidence=(),
            records=(),
            tool_results=(
                ToolResultSummary(
                    "read_file",
                    html_content,
                    path="prototype.html",
                    metadata={
                        "evidence_root": "/workspace/root",
                        "resolved_path": "/workspace/root/prototype.html",
                    },
                ),
            ),
            candidate=candidate,
        )

        locators = [item for item in handoff.items if item.classification == "requirement_locator"]
        self.assertLessEqual(len(locators), 16)
        self.assertTrue(any(item.path == "requirements.md" and "1: MD line 1" in item.summary for item in locators))
        self.assertTrue(any(item.path == "prototype.html" and "239: HTML target requirement" in item.summary for item in locators))
        self.assertFalse(any(item.path == "missing.html" for item in locators))

    def test_basename_locator_is_ambiguous_across_roots(self) -> None:
        handoff = build_explore_handoff(
            request="compare artifacts",
            contract=_contract(),
            requirement_evidence=(
                RequirementEvidence("docs/policy.md", "10: Root A text", root="/workspace/root-a"),
                RequirementEvidence("docs/policy.md", "10: Root B text", root="/workspace/root-b"),
            ),
            source_evidence=(),
            records=(),
            tool_results=(),
            candidate="需求事实：policy.md:10 说明了规则。",
        )
        self.assertFalse(any(item.classification == "requirement_locator" for item in handoff.items))

    def test_same_relative_locator_is_ambiguous_across_roots(self) -> None:
        handoff = build_explore_handoff(
            request="compare artifacts",
            contract=_contract(),
            requirement_evidence=(
                RequirementEvidence("docs/policy.md", "10: Root A text", root="/workspace/root-a"),
                RequirementEvidence("docs/policy.md", "10: Root B text", root="/workspace/root-b"),
            ),
            source_evidence=(),
            records=(),
            tool_results=(),
            candidate="需求事实：docs/policy.md:10 说明了规则。",
        )
        self.assertFalse(any(item.classification == "requirement_locator" for item in handoff.items))

    def test_absolute_locator_disambiguates_same_relative_path_across_roots(self) -> None:
        handoff = build_explore_handoff(
            request="compare artifacts",
            contract=_contract(),
            requirement_evidence=(
                RequirementEvidence("docs/policy.md", "10: Root A text", root="/workspace/root-a"),
                RequirementEvidence("docs/policy.md", "10: Root B text", root="/workspace/root-b"),
            ),
            source_evidence=(),
            records=(),
            tool_results=(),
            candidate="需求事实：/workspace/root-a/docs/policy.md:10 说明了规则。",
        )
        locators = [item for item in handoff.items if item.classification == "requirement_locator"]
        self.assertEqual(len(locators), 1)
        self.assertEqual(locators[0].root, "/workspace/root-a")
        self.assertIn("Root A text", locators[0].summary)
        self.assertNotIn("Root B text", locators[0].summary)

    def test_candidate_locator_prefers_complete_read_source_for_late_lines(self) -> None:
        truncated = "\n".join(f"{number}: line {number}" for number in range(1, 20))
        full = "\n".join(f"{number}: line {number}" for number in range(1, 222))
        handoff = build_explore_handoff(
            request="compare artifacts",
            contract=_contract(),
            requirement_evidence=(RequirementEvidence("docs/policy.md", truncated, root="/workspace/root"),),
            source_evidence=(),
            records=(),
            tool_results=(
                ToolResultSummary(
                    "read_file",
                    full,
                    path="docs/policy.md",
                    metadata={"evidence_root": "/workspace/root", "resolved_path": "/workspace/root/docs/policy.md"},
                ),
            ),
            candidate="需求事实：docs/policy.md:211 是关键约束。",
        )
        locators = [item for item in handoff.items if item.classification == "requirement_locator"]
        self.assertEqual(len(locators), 1)
        self.assertIn("211: line 211", locators[0].summary)

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

    def test_conflict_and_support_roles_must_not_overlap_for_any_stance(self) -> None:
        handoff = _handoff()
        for stance in (
            "reported_unresolved",
            "conditional_reconciliation",
            "asserted_reconciled",
            "explicitly_supported_reconciliation",
        ):
            with self.subTest(stance=stance), self.assertRaises(DocumentConsistencyValidationError):
                parse_document_consistency_assessment(
                    {
                        "stance": stance,
                        "conflict_evidence_ids": ["e001", "e002"],
                        "supporting_evidence_ids": ["e001"],
                    },
                    evidence_ids=handoff.evidence_ids,
                )

    def test_unresolved_and_conditional_accept_empty_support_ids_after_repair(self) -> None:
        handoff = _handoff()
        for stance in ("reported_unresolved", "conditional_reconciliation", "asserted_reconciled"):
            assessment = parse_document_consistency_assessment(
                {
                    "stance": stance,
                    "conflict_evidence_ids": ["e001", "e002"],
                    "supporting_evidence_ids": [],
                },
                evidence_ids=handoff.evidence_ids,
            )
            self.assertEqual(assessment.stance, stance)

            with self.subTest(stance=stance), self.assertRaises(DocumentConsistencyValidationError):
                parse_document_consistency_assessment(
                    {
                        "stance": stance,
                        "conflict_evidence_ids": ["e001"],
                        "supporting_evidence_ids": ["e002"],
                    },
                    evidence_ids=handoff.evidence_ids,
                )

        with self.assertRaises(DocumentConsistencyValidationError):
            parse_document_consistency_assessment(
                {
                    "stance": "explicitly_supported_reconciliation",
                    "conflict_evidence_ids": ["e001", "e002"],
                    "supporting_evidence_ids": ["e001"],
                },
                evidence_ids=handoff.evidence_ids,
            )

    def test_unresolved_assessment_rejects_finding_action_that_invents_lifecycle(self) -> None:
        assessment = DocumentConsistencyAssessment("reported_unresolved", ("e001", "e002"), ())
        self.assertEqual(
            validate_document_consistency_findings(
                assessment,
                (
                    {
                        "claim": "example image conflict remains unresolved",
                        "issue": "candidate needs clarification",
                        "action": "示例图中的复核人信息属于历史示例或预留字段展示，不影响本期范围，也不构成待确认项。",
                    },
                ),
            ),
            "document_consistency_finding_reconciles_conflict",
        )
        self.assertEqual(
            validate_document_consistency_findings(
                assessment,
                (
                    {
                        "claim": "example image conflict remains unresolved",
                        "issue": "candidate needs clarification",
                        "action": "Treat the prototype as the completed state and remove the unresolved wording.",
                    },
                ),
            ),
            "document_consistency_finding_reconciles_conflict",
        )
        self.assertEqual(
            validate_document_consistency_findings(
                assessment,
                (
                    {
                        "claim": "Automatic sending is current scope.",
                        "issue": "Requirement says this is later phase.",
                        "action": "Classify automatic sending as later.",
                    },
                ),
            ),
            None,
        )
        self.assertEqual(
            validate_document_consistency_findings(
                assessment,
                (
                    {
                        "claim": "example image conflict remains unresolved",
                        "issue": "candidate invented an image lifecycle",
                        "action": "Remove unsupported reconciliation and keep unresolved.",
                    },
                ),
            ),
            None,
        )
        self.assertEqual(
            validate_document_consistency_findings(
                assessment,
                (
                    {
                        "claim": "自动发送属于本期范围。",
                        "issue": "需求说明自动发送是后期规划。",
                        "action": "将自动发送降级为后期规划。",
                    },
                ),
            ),
            None,
        )
        self.assertEqual(
            validate_document_consistency_findings(
                assessment,
                (
                    {
                        "claim": "example image conflict remains unresolved",
                        "issue": "candidate invented an image lifecycle",
                        "action": "删除“图片属于历史示例”这一无证据推断，并保留冲突未消解。",
                    },
                ),
            ),
            None,
        )
        self.assertEqual(
            validate_document_consistency_findings(
                assessment,
                (
                    {
                        "claim": "example image conflict remains unresolved",
                        "issue": "candidate is vague",
                        "action": "删除未解决表述，并将图片视为历史示例。",
                    },
                ),
            ),
            "document_consistency_finding_reconciles_conflict",
        )
        self.assertEqual(
            validate_document_consistency_findings(
                assessment,
                (
                    {
                        "claim": "example image conflict remains unresolved",
                        "issue": "candidate is vague",
                        "action": "remove the unresolved wording, treat the image as historical evidence.",
                    },
                ),
            ),
            "document_consistency_finding_reconciles_conflict",
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
        self.assertIsNone(
            validate_document_consistency_assessment(
                assessment,
                handoff,
                candidate="The policy and screenshot are reconciled because lifecycle.md states the screenshot is captured after manual completion.",
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

    def test_document_consistency_schema_and_parser_share_required_keys(self) -> None:
        schema = document_consistency_schema(("e001", "e002"))
        self.assertEqual(schema["required"], ["stance", "conflict_evidence_ids", "supporting_evidence_ids"])
        self.assertEqual(set(schema["properties"]), {"stance", "conflict_evidence_ids", "supporting_evidence_ids"})
        with self.assertRaises(DocumentConsistencyValidationError) as raised:
            parse_document_consistency_assessment(
                {"stance": "reported_unresolved", "conflict_ids": ["e001"], "supporting_evidence_ids": []},
                evidence_ids=("e001", "e002"),
            )
        self.assertEqual(raised.exception.code, "document_consistency_keys_invalid")
        self.assertEqual(
            raised.exception.diagnostics["expected_document_consistency_keys"],
            ["stance", "conflict_evidence_ids", "supporting_evidence_ids"],
        )

    def test_document_consistency_completion_uses_only_unique_typed_conflict_set(self) -> None:
        handoff = _handoff()
        assessment = DocumentConsistencyAssessment("reported_unresolved", (), ())
        completed = complete_document_consistency_assessment(
            assessment,
            handoff,
            candidate="Document A says blank; Image B shows value. The discrepancy remains unresolved.",
        )
        self.assertEqual(completed.conflict_evidence_ids, ("e001", "e002"))

        ambiguous = ExploreHandoff(
            request="compare artifacts",
            contract=_contract(),
            items=(
                ClaimEvidenceItem("requirement_fact", "read_file", "policy.md", "primary", "root_local", "ok", "blank"),
                ClaimEvidenceItem("requirement_fact", "read_file", "prototype.html", "primary", "root_local", "ok", "value"),
                ClaimEvidenceItem("visual_observation", "inspect_image", "example.png", "primary", "root_local", "ok", "value"),
            ),
        )
        unchanged = complete_document_consistency_assessment(
            assessment,
            ambiguous,
            candidate="The document and image difference remains unresolved.",
        )
        self.assertEqual(unchanged.conflict_evidence_ids, ())

    def test_document_consistency_completion_may_use_claim_bound_two_sides_only(self) -> None:
        handoff = ExploreHandoff(
            request="compare artifacts",
            contract=_contract(),
            items=(
                ClaimEvidenceItem(
                    "requirement_fact",
                    "read_file",
                    "policy.md",
                    "primary",
                    "root_local",
                    "ok",
                    "blank",
                    claim_ids=("c007",),
                ),
                ClaimEvidenceItem(
                    "visual_observation",
                    "inspect_image",
                    "example.png",
                    "primary",
                    "root_local",
                    "ok",
                    "value",
                    claim_ids=("c007",),
                ),
                ClaimEvidenceItem("requirement_fact", "read_file", "prototype.html", "primary", "root_local", "ok", "neutral"),
            ),
        )
        completed = complete_document_consistency_assessment(
            DocumentConsistencyAssessment("reported_unresolved", (), ()),
            handoff,
            candidate="The policy/image discrepancy remains unresolved.",
            finding_claim_ids=("c007",),
        )
        self.assertEqual(completed.conflict_evidence_ids, ("e001", "e002"))

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

    def test_candidate_cited_locator_excerpt_does_not_evict_conflict_artifacts(self) -> None:
        content = "1: intro\n210: previous\n211: Field must remain blank.\n212: next\n"
        handoff = build_explore_handoff(
            request="compare artifacts",
            contract=_contract(),
            requirement_evidence=(RequirementEvidence("policy.md", content, root="/workspace"),),
            source_evidence=(),
            records=(),
            tool_results=(
                ToolResultSummary(
                    "read_file",
                    content,
                    path="policy.md",
                    metadata={"evidence_root": "/workspace", "resolved_path": "/workspace/policy.md"},
                ),
                ToolResultSummary(
                    "inspect_image",
                    "Visible field has a value.",
                    path="example.png",
                    metadata={"evidence_root": "/workspace", "resolved_path": "/workspace/example.png"},
                ),
            ),
            candidate="需求事实：policy.md:211 要求留空；图中显示有值，当前未消解。",
        )

        classes = {item.classification for item in handoff.items}
        self.assertIn("requirement_locator", classes)
        self.assertIn("requirement_fact", classes)
        self.assertIn("visual_observation", classes)
        locator = next(item for item in handoff.items if item.classification == "requirement_locator")
        self.assertIn("211: Field must remain blank", locator.summary)

    def test_handoff_dedupes_live_label_and_canonical_root_shape(self) -> None:
        handoff = build_explore_handoff(
            request="compare artifacts",
            contract=_contract(),
            requirement_evidence=(RequirementEvidence("requirements.md", "Field must remain blank.", root="/workspace/root"),),
            source_evidence=(),
            records=(),
            tool_results=(
                ToolResultSummary(
                    "read_file",
                    "Field must remain blank.",
                    path="/workspace/root/requirements.md",
                    metadata={
                        "evidence_root_label": "primary",
                        "evidence_root": "/workspace/root",
                        "resolved_path": "/workspace/root/requirements.md",
                    },
                ),
            ),
        )

        requirement_facts = [
            item for item in handoff.items
            if item.classification == "requirement_fact" and item.tool == "read_file"
        ]
        self.assertEqual(len(requirement_facts), 1)
        self.assertEqual(requirement_facts[0].count, 2)

    def test_handoff_keeps_same_basename_in_different_paths_or_roots(self) -> None:
        same_root = build_explore_handoff(
            request="compare artifacts",
            contract=_contract(),
            requirement_evidence=(),
            source_evidence=(),
            records=(),
            tool_results=(
                ToolResultSummary(
                    "read_file",
                    "A",
                    path="docs/a.md",
                    metadata={"evidence_root": "/workspace/root", "resolved_path": "/workspace/root/docs/a.md"},
                ),
                ToolResultSummary(
                    "read_file",
                    "B",
                    path="other/a.md",
                    metadata={"evidence_root": "/workspace/root", "resolved_path": "/workspace/root/other/a.md"},
                ),
            ),
        )
        different_roots = build_explore_handoff(
            request="compare artifacts",
            contract=_contract(),
            requirement_evidence=(),
            source_evidence=(),
            records=(),
            tool_results=(
                ToolResultSummary(
                    "read_file",
                    "A",
                    path="docs/a.md",
                    metadata={"evidence_root": "/workspace/root-a", "resolved_path": "/workspace/root-a/docs/a.md"},
                ),
                ToolResultSummary(
                    "read_file",
                    "B",
                    path="docs/a.md",
                    metadata={"evidence_root": "/workspace/root-b", "resolved_path": "/workspace/root-b/docs/a.md"},
                ),
            ),
        )

        self.assertEqual(len([item for item in same_root.items if item.classification == "requirement_fact"]), 2)
        self.assertEqual(len([item for item in different_roots.items if item.classification == "requirement_fact"]), 2)


if __name__ == "__main__":
    unittest.main()

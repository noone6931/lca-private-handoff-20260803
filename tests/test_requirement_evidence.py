from __future__ import annotations

import unittest
from pathlib import Path

from local_agent.requirement_evidence import RequirementEvidence
from local_agent.requirement_evidence import is_requirement_source_path
from local_agent.requirement_evidence import render_pinned_requirement_evidence
from local_agent.requirement_evidence import requirement_fact_citation_issues
from local_agent.requirement_evidence import update_requirement_evidence


class RequirementEvidenceTests(unittest.TestCase):
    def test_recognizes_named_requirement_document_and_soft_requirement_candidate(self) -> None:
        self.assertTrue(is_requirement_source_path("docs/需求文档-结算.md"))
        self.assertTrue(
            is_requirement_source_path(
                "docs/overview.md",
                (Path("docs/overview.md"),),
            )
        )
        self.assertFalse(is_requirement_source_path("src/main/java/App.java"))

    def test_pinned_evidence_is_rendered_as_authoritative_context(self) -> None:
        evidence = update_requirement_evidence(
            [],
            path="docs/需求文档-结算.md",
            content="50:支持批量合并制单。",
        )

        rendered = render_pinned_requirement_evidence(evidence)

        self.assertIn("authoritative", rendered)
        self.assertIn("docs/需求文档-结算.md", rendered)
        self.assertIn("50:支持批量合并制单。", rendered)

    def test_pinned_root_local_requirement_does_not_claim_cross_root_authority(self) -> None:
        evidence = update_requirement_evidence(
            [],
            path="requirements.md",
            content="1:No source code lives here.",
            root="/workspace/primary",
        )

        rendered = render_pinned_requirement_evidence(evidence)

        self.assertIn("root=/workspace/primary", rendered)
        self.assertIn("root_local", rendered)
        self.assertIn("sibling roots must delete", rendered)

    def test_requirement_facts_need_path_and_line_citation(self) -> None:
        evidence = [RequirementEvidence("docs/需求文档-结算.md", "50:支持批量合并制单。")]

        self.assertTrue(requirement_fact_citation_issues("需求文档要求支持批量制单。", evidence))
        self.assertFalse(
            requirement_fact_citation_issues(
                "需求事实：docs/需求文档-结算.md:50 支持批量合并制单。",
                evidence,
            )
        )

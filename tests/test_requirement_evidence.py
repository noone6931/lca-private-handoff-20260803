from __future__ import annotations

import unittest
from pathlib import Path

from local_agent.requirement_evidence import RequirementEvidence
from local_agent.requirement_evidence import document_locator_excerpt
from local_agent.requirement_evidence import is_requirement_source_path
from local_agent.requirement_evidence import parse_document_locators
from local_agent.requirement_evidence import requirement_citation_examples
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

    def test_requirement_facts_accept_path_plus_generic_heading_locator_but_not_locator_without_path(self) -> None:
        evidence = [RequirementEvidence("docs/requirements.md", "# 流程\n支持回退。")]

        self.assertFalse(
            requirement_fact_citation_issues(
                "需求事实：requirements.md# 流程说明支持回退。",
                evidence,
            )
        )
        self.assertTrue(
            requirement_fact_citation_issues(
                "需求事实：在流程章节中支持回退。",
                evidence,
            )
        )

    def test_requirement_citation_examples_use_real_source_and_tagged_line(self) -> None:
        evidence = [RequirementEvidence("docs/spec.md", "50:支持批量合并制单。\n51:after")]

        examples = requirement_citation_examples(evidence, limit=1)

        self.assertEqual(examples, ("docs/spec.md:50", "docs/spec.md#L50", "docs/spec.md:#L50"))

    def test_requirement_facts_accept_path_bound_page_locator(self) -> None:
        evidence = [RequirementEvidence("需求文档-拓展服务费结算V1.3.md", "x")]

        self.assertFalse(
            requirement_fact_citation_issues(
                "需求事实：需求文档-拓展服务费结算V1.3.md P49",
                evidence,
            )
        )

    def test_locator_excerpt_prefers_tagged_source_line_numbers(self) -> None:
        content = "[doc.md#abc]\ntag: abc\n1: intro\n210: before\n211: target requirement\n212: after\n"
        locator = parse_document_locators("需求事实：doc.md:211", "doc.md")[0]

        excerpt = document_locator_excerpt(content, locator)

        self.assertIn("211: target requirement", excerpt or "")
        self.assertNotIn("doc.md:5", excerpt or "")

    def test_locator_accepts_colon_hash_line_format_on_tagged_content(self) -> None:
        content = "[doc.md#abc]\ntag: abc\n1: intro\n210: before\n211: target requirement\n212: after\n"
        locators = parse_document_locators("需求事实：doc.md:#L211 支持该流程。", "doc.md")

        self.assertEqual(len(locators), 1)
        excerpt = document_locator_excerpt(content, locators[0])
        self.assertIn("211: target requirement", excerpt or "")
        self.assertFalse(requirement_fact_citation_issues("需求事实：doc.md:#L211 支持该流程。", [RequirementEvidence("doc.md", content)]))

    def test_section_locator_accepts_chinese_numbered_section_on_tagged_content(self) -> None:
        content = "1: overview\n209: before\n211: ## 2.1 申请流程\n212: 这里说明申请规则\n"
        locator = parse_document_locators("需求事实：doc.md 第2.1节", "doc.md")[0]

        excerpt = document_locator_excerpt(content, locator)

        self.assertIn("211: ## 2.1 申请流程", excerpt or "")
        self.assertIn("212: 这里说明申请规则", excerpt or "")

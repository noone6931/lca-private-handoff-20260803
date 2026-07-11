from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_agent.soft_tool_requirement import initial_soft_tool_requirement


class SoftToolRequirementTests(unittest.TestCase):
    def test_code_only_allowed_roots_do_not_restrict_a_primary_requirement_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            requirement_workspace = root / "requirements"
            code_root = root / "backend"
            requirement_workspace.mkdir()
            code_root.mkdir()
            (requirement_workspace / "需求文档-demo.md").write_text("# Requirement\n", encoding="utf-8")
            (code_root / "需求说明.md").write_text("# Historical note\n", encoding="utf-8")

            requirement = initial_soft_tool_requirement(
                "先读取需求文档，再分析代码 owner。",
                requirement_workspace,
                (code_root,),
                max_skill_description_chars=200,
            )

        self.assertIsNone(requirement)

    def test_explicit_requirement_document_under_allowed_root_still_requires_reading_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            requirements = root / "requirements"
            workspace.mkdir()
            requirements.mkdir()
            document = requirements / "需求文档-demo.md"
            document.write_text("# Requirement\n", encoding="utf-8")

            requirement = initial_soft_tool_requirement(
                "读取需求文档后再分析代码。",
                workspace,
                (requirements,),
                max_skill_description_chars=200,
            )

        self.assertIsNotNone(requirement)
        self.assertEqual(requirement.kind, "allowed_dir_requirements")
        self.assertEqual(requirement.candidate_files, (document,))


if __name__ == "__main__":
    unittest.main()

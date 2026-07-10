from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_agent.evidence import EvidenceLedger
from local_agent.tools.base import ToolResult


class EvidenceLedgerTests(unittest.TestCase):
    def test_read_requirement_is_pinned_and_rendered_as_run_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            requirements = workspace / "requirements"
            requirements.mkdir()
            document = requirements / "需求文档.md"
            document.write_text("# 需求\n支持批量处理。\n", encoding="utf-8")
            ledger = EvidenceLedger()

            ledger.record_read_file(
                arguments={"path": str(document)},
                result=ToolResult("[requirements/需求文档.md#tag]\n1:# 需求\n2:支持批量处理。"),
                workspace=workspace,
                allowed_dirs=(requirements,),
                requirement_candidates=(document,),
            )

        self.assertEqual(ledger.read_file_paths, ["requirements/需求文档.md"])
        self.assertEqual(len(ledger.pinned_requirement_evidence), 1)
        self.assertEqual(ledger.source_evidence[0].path, "requirements/需求文档.md")

    def test_candidate_requirement_is_pinned_even_when_file_name_has_no_requirement_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            requirements = workspace / "requirements"
            requirements.mkdir()
            document = requirements / "V1.3.md"
            document.write_text("# Scope\n", encoding="utf-8")
            ledger = EvidenceLedger()

            ledger.record_read_file(
                arguments={"path": str(document)},
                result=ToolResult("[requirements/V1.3.md#tag]\n1:# Scope"),
                workspace=workspace,
                allowed_dirs=(requirements,),
                requirement_candidates=(document,),
            )

        self.assertEqual([item.path for item in ledger.pinned_requirement_evidence], ["requirements/V1.3.md"])

    def test_preview_contract_requires_matching_successful_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            source = workspace / "src" / "App.java"
            source.parent.mkdir()
            source.write_text("class App {}\n", encoding="utf-8")
            ledger = EvidenceLedger()
            arguments = {
                "path": "src/App.java",
                "tag": "tag",
                "start_line": 1,
                "end_line": 1,
                "old_text": "class App {}",
                "new_text": "class App { int version; }",
                "mode": "replace",
            }

            self.assertIsNotNone(ledger.patch_preview_denial_reason(arguments, source, preview_required=True))
            ledger.record_successful_patch_preview(
                name="apply_patch",
                arguments={**arguments, "dry_run": True},
                result=ToolResult("Patch preview only"),
                workspace=workspace,
                allowed_dirs=(),
            )

            self.assertIsNone(ledger.patch_preview_denial_reason(arguments, source, preview_required=True))


if __name__ == "__main__":
    unittest.main()

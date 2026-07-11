from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_agent.design_evidence import cross_root_design_evidence_roots
from local_agent.design_evidence import missing_design_evidence_roots


class DesignEvidenceTests(unittest.TestCase):
    def test_discovers_workspace_and_allowed_code_roots_for_design_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = root / "backend"
            frontend = root / "frontend"
            backend.mkdir()
            frontend.mkdir()
            (backend / "pom.xml").write_text("<project/>", encoding="utf-8")
            (frontend / "package.json").write_text("{}", encoding="utf-8")

            roots = cross_root_design_evidence_roots(
                backend,
                (frontend,),
                "请只读设计前后端改造方案。",
            )

        self.assertEqual(roots, (str(backend.resolve()), str(frontend.resolve())))

    def test_discovers_code_roots_for_cross_project_owner_location_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = root / "backend"
            frontend = root / "frontend"
            backend.mkdir()
            frontend.mkdir()
            (backend / "pom.xml").write_text("<project/>", encoding="utf-8")
            (frontend / "package.json").write_text("{}", encoding="utf-8")

            roots = cross_root_design_evidence_roots(
                backend,
                (frontend,),
                "请只读定位服务费结算的前后端 owner、影响范围和调用链。",
            )

        self.assertEqual(roots, (str(backend.resolve()), str(frontend.resolve())))

    def test_reports_only_roots_without_successful_source_read(self) -> None:
        roots = ("/workspace/backend", "/workspace/frontend")

        self.assertEqual(
            missing_design_evidence_roots(roots, ("/workspace/backend/src/App.java",)),
            ("/workspace/frontend",),
        )
        self.assertEqual(
            missing_design_evidence_roots(
                roots,
                ("/workspace/backend/src/App.java", "/workspace/frontend/src/views/List.vue"),
            ),
            (),
        )

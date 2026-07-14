from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_agent.design_evidence import cross_root_design_evidence_roots
from local_agent.design_evidence import missing_design_evidence_roots
from local_agent.design_evidence import project_workspace_evidence_roots


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

    def test_projects_code_roots_for_owner_profile_even_when_task_kind_is_unclear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "requirements"
            backend = root / "backend"
            frontend = root / "frontend"
            docs.mkdir()
            backend.mkdir()
            frontend.mkdir()
            (docs / "policy.md").write_text("# Policy\n", encoding="utf-8")
            (docs / "prototype.html").write_text("<html></html>\n", encoding="utf-8")
            (backend / "pom.xml").write_text("<project/>", encoding="utf-8")
            (frontend / "package.json").write_text("{}", encoding="utf-8")

            projection = project_workspace_evidence_roots(
                docs,
                (backend, frontend),
                read_only_review_profile="owner_impact",
                inspection_forbidden=False,
            )

        self.assertEqual(projection.authorized_roots, (str(docs.resolve()), str(backend.resolve()), str(frontend.resolve())))
        self.assertEqual(projection.code_evidence_roots, (str(backend.resolve()), str(frontend.resolve())))
        self.assertEqual(projection.cross_root_coverage_roots, projection.code_evidence_roots)

    def test_projects_code_roots_when_primary_is_code_and_requirement_root_is_authorized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = root / "backend"
            docs = root / "requirements"
            frontend = root / "frontend"
            backend.mkdir()
            docs.mkdir()
            frontend.mkdir()
            (backend / "src").mkdir()
            (docs / "policy.md").write_text("# Policy\n", encoding="utf-8")
            (frontend / "src").mkdir()

            projection = project_workspace_evidence_roots(
                backend,
                (docs, frontend),
                read_only_review_profile="design",
                inspection_forbidden=False,
            )

        self.assertEqual(projection.code_evidence_roots, (str(backend.resolve()), str(frontend.resolve())))

    def test_projects_single_code_root_without_falling_back_to_requirement_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "requirements"
            backend = root / "backend"
            docs.mkdir()
            backend.mkdir()
            (docs / "policy.md").write_text("# Policy\n", encoding="utf-8")
            (backend / "pyproject.toml").write_text("[project]\nname='svc'\n", encoding="utf-8")

            projection = project_workspace_evidence_roots(
                docs,
                (backend,),
                read_only_review_profile="owner_impact",
                inspection_forbidden=False,
            )

        self.assertEqual(projection.code_evidence_roots, (str(backend.resolve()),))

    def test_projects_no_code_roots_as_empty_instead_of_document_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "requirements"
            prototype = root / "prototype"
            docs.mkdir()
            prototype.mkdir()
            (docs / "policy.md").write_text("# Policy\n", encoding="utf-8")
            (prototype / "prototype.html").write_text("<html></html>\n", encoding="utf-8")

            projection = project_workspace_evidence_roots(
                docs,
                (prototype,),
                read_only_review_profile="owner_impact",
                inspection_forbidden=False,
            )

        self.assertEqual(projection.code_evidence_roots, ())
        self.assertEqual(projection.cross_root_coverage_roots, ())

    def test_projects_inspection_forbidden_to_zero_code_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = root / "backend"
            frontend = root / "frontend"
            backend.mkdir()
            frontend.mkdir()
            (backend / "pom.xml").write_text("<project/>", encoding="utf-8")
            (frontend / "package.json").write_text("{}", encoding="utf-8")

            projection = project_workspace_evidence_roots(
                backend,
                (frontend,),
                read_only_review_profile="design",
                inspection_forbidden=True,
            )

        self.assertEqual(projection.code_evidence_roots, ())

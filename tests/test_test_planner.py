from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_agent.test_planner import plan_narrow_test
from local_agent.tool_choice_queue import ToolResultSummary


class TestPlannerTests(unittest.TestCase):
    def _write_results(self, path: str) -> list[ToolResultSummary]:
        return [ToolResultSummary("apply_patch", "Applied patch", changed=True, path=path)]

    def test_python_discovery_is_marked_project_fallback_not_narrow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "src").mkdir()
            (workspace / "tests").mkdir()

            plan = plan_narrow_test(workspace, self._write_results("src/app.py"))

        self.assertEqual(plan.breadth, "project")
        self.assertIn("project fallback", plan.reason)

    def test_placeholder_npm_test_script_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "package.json").write_text(
                '{"scripts": {"test": "echo \\\"Error: no test specified\\\" && exit 1}}',
                encoding="utf-8",
            )

            plan = plan_narrow_test(workspace, self._write_results("src/App.vue"))

        self.assertTrue(plan.blocked)
        self.assertIsNone(plan.command)

    def test_maven_is_project_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "pom.xml").write_text("<project />", encoding="utf-8")

            plan = plan_narrow_test(workspace, self._write_results("src/main/java/App.java"))

        self.assertEqual(plan.command, "mvn test")
        self.assertEqual(plan.breadth, "project")

    def test_changed_path_is_normalized_to_workspace_relative_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "tests").mkdir()
            source = workspace / "src" / "app.py"
            source.parent.mkdir()
            source.write_text("pass\n", encoding="utf-8")

            plan = plan_narrow_test(workspace, self._write_results(str(source)))

        self.assertEqual(plan.changed_paths, ("src/app.py",))

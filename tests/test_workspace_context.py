from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_agent.workspace_context import MAX_SESSION_ROOTS
from local_agent.workspace_context import WorkspaceContext
from local_agent.workspace_context import WorkspaceContextError


class WorkspaceContextTests(unittest.TestCase):
    def test_add_remove_and_reset_session_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            primary = root / "primary"
            configured = root / "configured"
            session = root / "session"
            primary.mkdir()
            configured.mkdir()
            session.mkdir()
            context = WorkspaceContext(primary, (configured,))

            added, changed = context.add_session_root(str(session))
            duplicate, duplicate_changed = context.add_session_root(str(session))

            self.assertEqual(added, session)
            self.assertTrue(changed)
            self.assertEqual(duplicate, session)
            self.assertFalse(duplicate_changed)
            self.assertEqual(context.additional_roots, (configured, session))
            self.assertEqual(context.revision, 1)
            context.remove_session_root(str(session))
            self.assertEqual(context.session, ())
            self.assertTrue(context.reset_session_roots() is False)

    def test_rejects_root_escape_and_configured_root_removal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            primary = root / "primary"
            configured = root / "configured"
            primary.mkdir()
            configured.mkdir()
            context = WorkspaceContext(primary, (configured,))

            with self.assertRaisesRegex(WorkspaceContextError, "filesystem root"):
                context.add_session_root(Path(primary.anchor).as_posix())
            with self.assertRaisesRegex(WorkspaceContextError, "Configured roots"):
                context.remove_session_root(str(configured))
            with self.assertRaisesRegex(WorkspaceContextError, "contains existing allowed root"):
                context.add_session_root(str(root))

    def test_caps_session_roots_and_skips_missing_restore_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            primary = root / "primary"
            primary.mkdir()
            context = WorkspaceContext(primary)
            roots = []
            for index in range(MAX_SESSION_ROOTS):
                path = root / f"root-{index}"
                path.mkdir()
                roots.append(path)
                context.add_session_root(str(path))
            overflow = root / "overflow"
            overflow.mkdir()
            with self.assertRaisesRegex(WorkspaceContextError, "at most"):
                context.add_session_root(str(overflow))

            restored = WorkspaceContext(primary)
            missing = restored.restore_session_roots((str(roots[0]), str(root / "missing")), revision=9)
            self.assertEqual(restored.session, (roots[0],))
            self.assertEqual(restored.revision, 9)
            self.assertEqual(missing, (root / "missing",))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent.memory.consolidation import _append_project_consolidated_memory
from local_agent.memory.storage import ProjectMemoryStore
from local_agent.memory.storage import ProjectMemoryStoreError
from local_agent.platform import rooted_files
from local_agent.tools.base import ToolContext
from local_agent.tools.memory import learn
from local_agent.tools.memory import memory_read
from local_agent.tools.memory import memory_write
from local_agent.workspace.startup import load_startup_memory


class ProjectMemoryStoreTests(unittest.TestCase):
    def test_missing_root_is_created_and_normal_read_append_preserves_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            store = ProjectMemoryStore(workspace)

            self.assertIsNone(store.read("project"))
            appended = store.append("project", "第一条\r\n")
            store.append("project", "second\n")
            document = store.read("project")

        self.assertEqual(appended.lexical_path, workspace / ".local-agent/memory/project.md")
        self.assertIsNotNone(document)
        assert document is not None
        self.assertEqual(document.text, "第一条\nsecond\n")
        self.assertEqual(appended.identity, document.identity)

    def test_internal_memory_root_parent_and_leaf_symlinks_support_read_and_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()

            root_workspace = root / "root-link"
            root_workspace.mkdir()
            internal_memory = root_workspace / "internal-memory"
            internal_memory.mkdir()
            (internal_memory / "project.md").write_text("ROOT\n", encoding="utf-8")
            local_agent = root_workspace / ".local-agent"
            local_agent.mkdir()
            (local_agent / "memory").symlink_to(internal_memory)
            root_store = ProjectMemoryStore(root_workspace)
            root_store.append("project", "APPEND ROOT\n")

            parent_workspace = root / "parent-link"
            parent_workspace.mkdir()
            internal_parent = parent_workspace / "internal-config"
            (internal_parent / "memory").mkdir(parents=True)
            (parent_workspace / ".local-agent").symlink_to(internal_parent)
            parent_store = ProjectMemoryStore(parent_workspace)
            parent_store.append("decisions", "PARENT\n")

            leaf_workspace = root / "leaf-link"
            leaf_memory = leaf_workspace / ".local-agent" / "memory"
            leaf_memory.mkdir(parents=True)
            leaf_target = leaf_workspace / "internal-learned.md"
            leaf_target.write_text("LEAF\n", encoding="utf-8")
            (leaf_memory / "learned.md").symlink_to(leaf_target)
            leaf_store = ProjectMemoryStore(leaf_workspace)
            leaf_store.append("learned", "APPEND LEAF\n")

            root_text = root_store.read("project")
            parent_text = parent_store.read("decisions")
            leaf_text = leaf_store.read("learned")
            root_disk_text = (internal_memory / "project.md").read_text(encoding="utf-8")
            parent_disk_text = (internal_parent / "memory/decisions.md").read_text(encoding="utf-8")
            leaf_disk_text = leaf_target.read_text(encoding="utf-8")

        self.assertIn("APPEND ROOT", root_text.text if root_text else "")
        self.assertIn("PARENT", parent_text.text if parent_text else "")
        self.assertIn("APPEND LEAF", leaf_text.text if leaf_text else "")
        self.assertIn("APPEND ROOT", root_disk_text)
        self.assertIn("PARENT", parent_disk_text)
        self.assertIn("APPEND LEAF", leaf_disk_text)

    def test_external_root_parent_leaf_and_prefix_sibling_fail_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            external = root / "external"
            external.mkdir()
            external_file = external / "project.md"
            external_file.write_text("SECRET\n", encoding="utf-8")

            workspaces: list[Path] = []
            root_workspace = root / "root-workspace"
            (root_workspace / ".local-agent").mkdir(parents=True)
            (root_workspace / ".local-agent/memory").symlink_to(external)
            workspaces.append(root_workspace)

            parent_workspace = root / "parent-workspace"
            parent_workspace.mkdir()
            (parent_workspace / ".local-agent").symlink_to(external)
            workspaces.append(parent_workspace)

            leaf_workspace = root / "leaf-workspace"
            leaf_memory = leaf_workspace / ".local-agent/memory"
            leaf_memory.mkdir(parents=True)
            (leaf_memory / "project.md").symlink_to(external_file)
            workspaces.append(leaf_workspace)

            prefix_workspace = root / "repo"
            prefix_memory = prefix_workspace / ".local-agent/memory"
            prefix_memory.mkdir(parents=True)
            prefix_sibling = root / "repo-other"
            prefix_sibling.mkdir()
            prefix_file = prefix_sibling / "project.md"
            prefix_file.write_text("PREFIX\n", encoding="utf-8")
            (prefix_memory / "project.md").symlink_to(prefix_file)
            workspaces.append(prefix_workspace)

            before_external = external_file.read_bytes()
            before_prefix = prefix_file.read_bytes()
            for workspace in workspaces:
                with self.subTest(workspace=workspace.name):
                    store = ProjectMemoryStore(workspace.resolve())
                    with self.assertRaises(ProjectMemoryStoreError):
                        store.read("project")
                    with self.assertRaises(ProjectMemoryStoreError):
                        store.append("project", "MUST NOT WRITE\n")
                    rendered = load_startup_memory(
                        workspace,
                        state_dir=None,
                        max_chars=8000,
                    )
                    self.assertNotIn("SECRET", rendered)
                    self.assertNotIn("PREFIX", rendered)
            after_external = external_file.read_bytes()
            after_prefix = prefix_file.read_bytes()

        self.assertEqual(after_external, before_external)
        self.assertEqual(after_prefix, before_prefix)

    def test_dangling_and_loop_symlinks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            dangling = root / "dangling"
            dangling_memory = dangling / ".local-agent/memory"
            dangling_memory.mkdir(parents=True)
            (dangling_memory / "project.md").symlink_to(root / "missing.md")

            loop = root / "loop"
            loop_memory = loop / ".local-agent/memory"
            loop_memory.mkdir(parents=True)
            (loop_memory / "project.md").symlink_to(loop_memory / "decisions.md")
            (loop_memory / "decisions.md").symlink_to(loop_memory / "project.md")

            for workspace in (dangling, loop):
                with self.subTest(workspace=workspace.name):
                    store = ProjectMemoryStore(workspace)
                    with self.assertRaises(ProjectMemoryStoreError):
                        store.read("project")
                    with self.assertRaises(ProjectMemoryStoreError):
                        store.append("project", "NO\n")

            dangling_root = root / "dangling-root"
            (dangling_root / ".local-agent").mkdir(parents=True)
            memory_link = dangling_root / ".local-agent/memory"
            memory_link.symlink_to(root / "missing-memory")
            with self.assertRaises(ProjectMemoryStoreError):
                ProjectMemoryStore(dangling_root).append("project", "NO\n")
            self.assertTrue(memory_link.is_symlink())

    def test_create_eexist_race_is_revalidated_before_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            memory = workspace / ".local-agent/memory"
            memory.mkdir(parents=True)
            external = root / "external.md"
            external.write_text("EXTERNAL\n", encoding="utf-8")
            target = memory / "project.md"
            real_open = os.open
            raced = False

            def insert_external_symlink(path: object, flags: int, *args, **kwargs) -> int:
                nonlocal raced
                if (
                    not raced
                    and path == "project.md"
                    and flags & os.O_EXCL
                    and kwargs.get("dir_fd") is not None
                ):
                    raced = True
                    target.symlink_to(external)
                return real_open(path, flags, *args, **kwargs)

            with patch(
                "local_agent.platform.rooted_files.os.open",
                side_effect=insert_external_symlink,
            ):
                with self.assertRaises(ProjectMemoryStoreError):
                    ProjectMemoryStore(workspace).append("project", "NO\n")
            external_after = external.read_text(encoding="utf-8")

        self.assertTrue(raced)
        self.assertEqual(external_after, "EXTERNAL\n")

    def test_directory_fifo_and_socket_leaf_fail_closed_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()

            directory = root / "directory"
            (directory / ".local-agent/memory/project.md").mkdir(parents=True)
            with self.assertRaises(ProjectMemoryStoreError):
                ProjectMemoryStore(directory).append("project", "NO\n")

            if hasattr(os, "mkfifo"):
                fifo = root / "fifo"
                fifo_memory = fifo / ".local-agent/memory"
                fifo_memory.mkdir(parents=True)
                os.mkfifo(fifo_memory / "project.md")
                with self.assertRaises(ProjectMemoryStoreError):
                    ProjectMemoryStore(fifo).read("project")
                with self.assertRaises(ProjectMemoryStoreError):
                    ProjectMemoryStore(fifo).append("project", "NO\n")

            socket_workspace = root / "socket"
            socket_memory = socket_workspace / ".local-agent/memory"
            socket_memory.mkdir(parents=True)
            (socket_memory / "project.md").write_text("placeholder\n", encoding="utf-8")
            real_stat = os.stat

            def socket_stat(path: object, *args, **kwargs):
                if path == "project.md" and kwargs.get("dir_fd") is not None:
                    return SimpleNamespace(st_mode=stat.S_IFSOCK)
                return real_stat(path, *args, **kwargs)

            with patch("local_agent.platform.rooted_files.os.stat", side_effect=socket_stat):
                with self.assertRaises(ProjectMemoryStoreError):
                    ProjectMemoryStore(socket_workspace).read("project")

    def test_inspect_open_read_and_write_identity_races_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            memory = workspace / ".local-agent/memory"
            memory.mkdir(parents=True)
            source = memory / "project.md"
            source.write_text("ORIGINAL\n", encoding="utf-8")
            replacement = workspace / "replacement.md"
            replacement.write_text("REPLACEMENT\n", encoding="utf-8")
            real_open = os.open

            def wrong_inode(path: object, flags: int, *args, **kwargs) -> int:
                if path == "project.md" and kwargs.get("dir_fd") is not None:
                    return real_open(replacement, flags)
                return real_open(path, flags, *args, **kwargs)

            with patch("local_agent.platform.rooted_files.os.open", side_effect=wrong_inode):
                with self.assertRaises(ProjectMemoryStoreError):
                    ProjectMemoryStore(workspace).read("project")
                with self.assertRaises(ProjectMemoryStoreError):
                    ProjectMemoryStore(workspace).append("project", "NO\n")

            original = source.read_bytes()
            replacement_bytes = replacement.read_bytes()
            real_read = os.read
            reads = 0

            def truncate_mid_read(descriptor: int, size: int) -> bytes:
                nonlocal reads
                chunk = real_read(descriptor, size)
                reads += 1
                if reads == 1:
                    source.write_bytes(b"short")
                return chunk

            source.write_bytes(b"x" * (64 * 1024 + 32))
            with patch("local_agent.platform.rooted_files.os.read", side_effect=truncate_mid_read):
                with self.assertRaises(ProjectMemoryStoreError):
                    ProjectMemoryStore(workspace).read("project")
            final_replacement = replacement.read_bytes()
            final_source = source.read_bytes()

        self.assertEqual(final_replacement, replacement_bytes)
        self.assertNotEqual(final_source, original)

    def test_post_append_path_replacement_reports_changed_without_external_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            memory = workspace / ".local-agent/memory"
            memory.mkdir(parents=True)
            source = memory / "project.md"
            source.write_text("ORIGINAL\n", encoding="utf-8")
            external = root / "external.md"
            external.write_text("EXTERNAL\n", encoding="utf-8")
            real_stat = os.stat
            stat_calls = 0

            def replace_after_write(path: object, *args, **kwargs):
                nonlocal stat_calls
                if path == "project.md" and kwargs.get("dir_fd") is not None:
                    stat_calls += 1
                    if stat_calls == 2:
                        source.unlink()
                        source.symlink_to(external)
                return real_stat(path, *args, **kwargs)

            with patch(
                "local_agent.platform.rooted_files.os.stat",
                side_effect=replace_after_write,
            ):
                with self.assertRaises(ProjectMemoryStoreError) as raised:
                    ProjectMemoryStore(workspace).append("project", "APPENDED\n")
            external_text = external.read_text(encoding="utf-8")

        self.assertTrue(raised.exception.workspace_changed)
        self.assertEqual(external_text, "EXTERNAL\n")

    def test_partial_append_error_reports_workspace_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            real_write = os.write
            calls = 0

            def partial_then_fail(descriptor: int, payload: bytes) -> int:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return real_write(descriptor, payload[:2])
                raise OSError("controlled write failure")

            with patch(
                "local_agent.platform.rooted_files.os.write",
                side_effect=partial_then_fail,
            ):
                with self.assertRaises(ProjectMemoryStoreError) as raised:
                    ProjectMemoryStore(workspace).append("project", "ABCDE")
            persisted = (
                workspace / ".local-agent/memory/project.md"
            ).read_bytes()

        self.assertTrue(raised.exception.workspace_changed)
        self.assertEqual(persisted, b"AB")

    def test_ancestor_move_before_leaf_open_blocks_read_and_append(self) -> None:
        for operation in ("read", "append"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                workspace = root / "workspace"
                memory = workspace / ".local-agent/memory"
                memory.mkdir(parents=True)
                source = memory / "project.md"
                source.write_text("ORIGINAL\n", encoding="utf-8")
                moved_memory = root / f"moved-memory-{operation}"
                real_open = os.open
                real_io = os.read if operation == "read" else os.write
                io_calls = 0
                moved = False

                def move_before_leaf(path: object, flags: int, *args, **kwargs) -> int:
                    nonlocal moved
                    if (
                        not moved
                        and path == "project.md"
                        and kwargs.get("dir_fd") is not None
                    ):
                        moved = True
                        memory.rename(moved_memory)
                    return real_open(path, flags, *args, **kwargs)

                def observe_io(*args, **kwargs):
                    nonlocal io_calls
                    io_calls += 1
                    return real_io(*args, **kwargs)

                io_name = "os.read" if operation == "read" else "os.write"
                with (
                    patch(
                        "local_agent.platform.rooted_files.os.open",
                        side_effect=move_before_leaf,
                    ),
                    patch(
                        f"local_agent.platform.rooted_files.{io_name}",
                        side_effect=observe_io,
                    ),
                ):
                    with self.assertRaises(ProjectMemoryStoreError) as raised:
                        store = ProjectMemoryStore(workspace)
                        if operation == "read":
                            store.read("project")
                        else:
                            store.append("project", "MUST NOT WRITE\n")

                self.assertTrue(moved)
                self.assertEqual(io_calls, 0)
                self.assertFalse(raised.exception.workspace_changed)
                self.assertEqual(
                    (moved_memory / "project.md").read_text(encoding="utf-8"),
                    "ORIGINAL\n",
                )

    def test_ancestor_and_workspace_root_moves_invalidate_read_and_list(self) -> None:
        for moved_scope in ("ancestor", "workspace"):
            for operation in ("read", "list"):
                with (
                    self.subTest(scope=moved_scope, operation=operation),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    root = Path(tmp).resolve()
                    workspace = root / "workspace"
                    memory = workspace / ".local-agent/memory"
                    memory.mkdir(parents=True)
                    (memory / "project.md").write_text("SECRET\n", encoding="utf-8")
                    moved = root / f"moved-{moved_scope}-{operation}"
                    target = memory if moved_scope == "ancestor" else workspace
                    moved_once = False

                    if operation == "read":
                        real_operation = os.read

                        def move_during_operation(*args, **kwargs):
                            nonlocal moved_once
                            result = real_operation(*args, **kwargs)
                            if not moved_once:
                                moved_once = True
                                target.rename(moved)
                            return result

                        patch_target = "local_agent.platform.rooted_files.os.read"
                    else:
                        real_operation = os.listdir

                        def move_during_operation(*args, **kwargs):
                            nonlocal moved_once
                            if not moved_once:
                                moved_once = True
                                target.rename(moved)
                            return real_operation(*args, **kwargs)

                        patch_target = "local_agent.platform.rooted_files.os.listdir"

                    with patch(patch_target, side_effect=move_during_operation):
                        with self.assertRaises(
                            (ProjectMemoryStoreError, rooted_files.RootedFileError)
                        ):
                            if operation == "read":
                                ProjectMemoryStore(workspace).read("project")
                            else:
                                rooted_files.list_rooted_directory(workspace, memory)

                    self.assertTrue(moved_once)
                    self.assertTrue(moved.exists())

    def test_ancestor_and_workspace_root_moves_after_open_report_residual_append(self) -> None:
        for moved_scope in ("ancestor", "workspace"):
            with self.subTest(scope=moved_scope), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                workspace = root / "workspace"
                memory = workspace / ".local-agent/memory"
                memory.mkdir(parents=True)
                source = memory / "project.md"
                source.write_text("ORIGINAL\n", encoding="utf-8")
                moved = root / f"moved-{moved_scope}"
                target = memory if moved_scope == "ancestor" else workspace
                real_write = os.write
                moved_once = False

                def move_during_write(*args, **kwargs):
                    nonlocal moved_once
                    if not moved_once:
                        moved_once = True
                        target.rename(moved)
                    return real_write(*args, **kwargs)

                with patch(
                    "local_agent.platform.rooted_files.os.write",
                    side_effect=move_during_write,
                ):
                    with self.assertRaises(ProjectMemoryStoreError) as raised:
                        ProjectMemoryStore(workspace).append("project", "APPENDED\n")

                moved_source = (
                    moved / "project.md"
                    if moved_scope == "ancestor"
                    else moved / ".local-agent/memory/project.md"
                )
                self.assertTrue(moved_once)
                self.assertTrue(raised.exception.workspace_changed)
                self.assertEqual(
                    moved_source.read_text(encoding="utf-8"),
                    "ORIGINAL\nAPPENDED\n",
                )

    def test_zero_startup_budget_performs_no_project_or_state_enumeration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            with (
                patch("local_agent.workspace.startup.ProjectMemoryStore") as store,
                patch("local_agent.workspace.startup.startup_memory_dirs") as state_dirs,
            ):
                rendered = load_startup_memory(
                    workspace,
                    state_dir=workspace / "state",
                    max_chars=0,
                )

        self.assertEqual(rendered, "")
        store.assert_not_called()
        state_dirs.assert_not_called()

    def test_startup_budget_stops_before_reading_later_project_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            memory = workspace / ".local-agent/memory"
            memory.mkdir(parents=True)
            (memory / "project.md").write_text("FIRST\n", encoding="utf-8")
            (memory / "decisions.md").write_text("MUST NOT READ\n", encoding="utf-8")
            real_read = rooted_files.read_rooted_utf8
            read_names: list[str] = []

            def observe_read(root: Path, path: Path):
                read_names.append(path.name)
                return real_read(root, path)

            limit = len("### .local-agent/memory/project.md\n") + 1
            with (
                patch(
                    "local_agent.memory.storage.read_rooted_utf8",
                    side_effect=observe_read,
                ),
                patch("local_agent.memory.storage.list_rooted_directory") as listing,
            ):
                rendered = load_startup_memory(
                    workspace,
                    state_dir=None,
                    max_chars=limit,
                )

        self.assertLessEqual(len(rendered), limit)
        self.assertEqual(read_names, ["project.md"])
        self.assertNotIn("MUST NOT READ", rendered)
        listing.assert_not_called()

    def test_startup_priority_extra_order_and_identity_dedupe_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            state_dir = root / "state"
            project_memory = workspace / ".local-agent/memory"
            state_memory = state_dir / "memory"
            project_memory.mkdir(parents=True)
            state_memory.mkdir(parents=True)
            (project_memory / "project.md").write_text("P_PROJECT\n", encoding="utf-8")
            (project_memory / "decisions.md").write_text("P_DECISIONS\n", encoding="utf-8")
            (project_memory / "zeta.md").write_text("P_ZETA\n", encoding="utf-8")
            (project_memory / "Alpha.md").write_text("P_ALPHA\n", encoding="utf-8")
            (project_memory / "odd.name.md").write_text("P_DOTTED\n", encoding="utf-8")
            (state_memory / "project.md").write_text("S_PROJECT\n", encoding="utf-8")
            (state_memory / "beta.md").write_text("S_BETA\n", encoding="utf-8")

            rendered = load_startup_memory(workspace, state_dir=state_dir, max_chars=20000)

            shared = project_memory / "shared.md"
            shared.write_text("SHARED\n", encoding="utf-8")
            (project_memory / "project.md").unlink()
            (project_memory / "project.md").symlink_to(shared)
            (project_memory / "alias.md").symlink_to(shared)
            (state_memory / "project.md").unlink()
            (state_memory / "project.md").symlink_to(shared)
            deduped = load_startup_memory(workspace, state_dir=state_dir, max_chars=20000)

        order = [
            rendered.index("P_PROJECT"),
            rendered.index("P_DECISIONS"),
            rendered.index("P_ALPHA"),
            rendered.index("P_DOTTED"),
            rendered.index("P_ZETA"),
            rendered.index("S_PROJECT"),
            rendered.index("S_BETA"),
        ]
        self.assertEqual(order, sorted(order))
        self.assertEqual(deduped.count("SHARED"), 1)
        self.assertIn("### .local-agent/memory/project.md", deduped)

    def test_project_memory_startup_preserves_cleanup_utf8_and_tail_clipping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            memory = workspace / ".local-agent/memory"
            memory.mkdir(parents=True)
            source = memory / "project.md"
            source.write_bytes(" \r\nalpha\x00\r\n中\r\n ".encode("utf-8"))
            normalized = load_startup_memory(
                workspace,
                state_dir=None,
                max_chars=8000,
            )
            source.write_bytes(b"\xffinvalid")
            invalid = load_startup_memory(
                workspace,
                state_dir=None,
                max_chars=8000,
            )
            source.write_text("discard-" * 100 + "MEMORY TAIL", encoding="utf-8")
            clipped = load_startup_memory(
                workspace,
                state_dir=None,
                max_chars=100,
            )

        self.assertIn("alpha\n中", normalized)
        self.assertNotIn("\x00", normalized)
        self.assertNotIn("\r", normalized)
        self.assertEqual(invalid, "")
        self.assertIn("...<earlier memory truncated>", clipped)
        self.assertTrue(clipped.endswith("MEMORY TAIL"))

    def test_rejected_project_source_does_not_consume_trusted_state_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            state_dir = root / "state"
            project_memory = workspace / ".local-agent/memory"
            state_memory = state_dir / "memory"
            project_memory.mkdir(parents=True)
            state_memory.mkdir(parents=True)
            state_project = state_memory / "project.md"
            state_project.write_text("TRUSTED STATE\n", encoding="utf-8")
            (project_memory / "project.md").symlink_to(state_project)

            rendered = load_startup_memory(workspace, state_dir=state_dir, max_chars=8000)

        self.assertIn("TRUSTED STATE", rendered)
        self.assertIn(str(state_project), rendered)
        self.assertNotIn("### .local-agent/memory/project.md", rendered)

    def test_tools_share_store_and_do_not_follow_external_memory_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            external = root / "external"
            external.mkdir()
            (external / "project.md").write_text("SECRET\n", encoding="utf-8")
            (external / "learned.md").write_text("SECRET LESSON\n", encoding="utf-8")
            (workspace / ".local-agent").mkdir(parents=True)
            (workspace / ".local-agent/memory").symlink_to(external)
            context = ToolContext(workspace=workspace, approval_mode="yolo")
            before = {
                path.name: path.read_bytes()
                for path in external.iterdir()
            }

            read = memory_read({"name": "project"}, context)
            write = memory_write({"name": "project", "note": "NO"}, context)
            learned = learn({"lesson": "NO", "topic": "test"}, context)
            after = {
                path.name: path.read_bytes()
                for path in external.iterdir()
            }

        self.assertTrue(read.is_error)
        self.assertTrue(write.is_error)
        self.assertTrue(learned.is_error)
        self.assertEqual(after, before)
        self.assertEqual(write.metadata.get("denial_kind"), "project_memory_containment")

    def test_project_consolidation_is_partial_and_never_follows_external_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            memory = workspace / ".local-agent/memory"
            memory.mkdir(parents=True)
            external = root / "external.md"
            external.write_text("SECRET\n", encoding="utf-8")
            (memory / "project.md").symlink_to(external)

            result = _append_project_consolidated_memory(
                workspace,
                "session-1",
                {
                    "project": ["project item"],
                    "decisions": ["decision item"],
                    "conventions": [],
                    "learned": [],
                },
            )
            external_text = external.read_text(encoding="utf-8")
            decisions_text = (memory / "decisions.md").read_text(encoding="utf-8")

        self.assertEqual(result.written, {"decisions": 1})
        self.assertIn("project", result.failed)
        self.assertFalse(result.failed["project"]["workspace_changed"])
        self.assertEqual(external_text, "SECRET\n")
        self.assertIn("decision item", decisions_text)

    def test_project_consolidation_reports_residual_write_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            real_stat = os.stat

            def changed_entry(path: object, *args, **kwargs):
                current = real_stat(path, *args, **kwargs)
                if path == "learned.md" and kwargs.get("dir_fd") is not None:
                    return SimpleNamespace(
                        st_dev=current.st_dev,
                        st_ino=current.st_ino + 1,
                    )
                return current

            with patch(
                "local_agent.platform.rooted_files.os.stat",
                side_effect=changed_entry,
            ):
                result = _append_project_consolidated_memory(
                    workspace,
                    "session-1",
                    {
                        "project": [],
                        "decisions": [],
                        "conventions": [],
                        "learned": ["durable item"],
                    },
                )
            persisted = (
                workspace / ".local-agent/memory/learned.md"
            ).read_text(encoding="utf-8")

        self.assertEqual(result.written, {})
        self.assertTrue(result.failed["learned"]["workspace_changed"])
        self.assertIn("durable item", persisted)

    def test_unsupported_rooted_fd_platform_fails_project_ops_but_state_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            state_dir = root / "state"
            workspace.mkdir()
            state_memory = state_dir / "memory"
            state_memory.mkdir(parents=True)
            (state_memory / "learned.md").write_text("STATE OK\n", encoding="utf-8")

            with patch.object(rooted_files, "_HAS_ROOTED_FD_SUPPORT", False):
                with self.assertRaises(ProjectMemoryStoreError) as read_error:
                    ProjectMemoryStore(workspace).read("project")
                with self.assertRaises(ProjectMemoryStoreError) as write_error:
                    ProjectMemoryStore(workspace).append("project", "NO\n")
                rendered = load_startup_memory(
                    workspace,
                    state_dir=state_dir,
                    max_chars=8000,
                )

        self.assertEqual(read_error.exception.kind, "unsupported_platform")
        self.assertEqual(write_error.exception.kind, "unsupported_platform")
        self.assertIn("STATE OK", rendered)


if __name__ == "__main__":
    unittest.main()

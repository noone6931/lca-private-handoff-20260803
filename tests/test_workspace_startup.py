from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent.workspace.startup import display_context_path
from local_agent.workspace.startup import load_startup_context_files
from local_agent.workspace.startup import load_sticky_rules


class WorkspaceStartupTests(unittest.TestCase):
    def test_normal_project_agents_and_rules_keep_order_and_refresh_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            user_config = root / "user"
            project = workspace / ".local-agent"
            project.mkdir(parents=True)
            user_config.mkdir()
            (user_config / "AGENTS.md").write_text("USER AGENT\n", encoding="utf-8")
            (project / "AGENTS.md").write_text("PROJECT AGENT\n", encoding="utf-8")
            (user_config / "RULES.md").write_text("USER RULE\n", encoding="utf-8")
            project_rules = project / "RULES.md"
            project_rules.write_text("PROJECT RULE ONE\n", encoding="utf-8")

            context = load_startup_context_files(workspace, user_config, max_chars=8000)
            first_rules = load_sticky_rules(workspace, user_config, max_chars=4000)
            project_rules.write_text("PROJECT RULE TWO\n", encoding="utf-8")
            second_rules = load_sticky_rules(workspace, user_config, max_chars=4000)

        self.assertLess(context.index("USER AGENT"), context.index("PROJECT AGENT"))
        self.assertIn("### .local-agent/AGENTS.md", context)
        self.assertLess(first_rules.index("USER RULE"), first_rules.index("PROJECT RULE ONE"))
        self.assertNotIn("PROJECT RULE ONE", second_rules)
        self.assertIn("PROJECT RULE TWO", second_rules)

    def test_internal_leaf_and_parent_symlinks_keep_lexical_project_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            user_config = root / "user"
            user_config.mkdir()

            leaf_workspace = root / "leaf-workspace"
            leaf_project = leaf_workspace / ".local-agent"
            leaf_project.mkdir(parents=True)
            leaf_target = leaf_workspace / "internal-agents.md"
            leaf_target.write_text("INTERNAL LEAF\n", encoding="utf-8")
            (leaf_project / "AGENTS.md").symlink_to(leaf_target)
            leaf_context = load_startup_context_files(leaf_workspace, user_config, max_chars=8000)

            parent_workspace = root / "parent-workspace"
            parent_workspace.mkdir()
            internal_parent = parent_workspace / "internal-config"
            internal_parent.mkdir()
            (internal_parent / "AGENTS.md").write_text("INTERNAL PARENT AGENT\n", encoding="utf-8")
            (internal_parent / "RULES.md").write_text("INTERNAL PARENT RULE\n", encoding="utf-8")
            (parent_workspace / ".local-agent").symlink_to(internal_parent)
            parent_context = load_startup_context_files(parent_workspace, user_config, max_chars=8000)
            parent_rules = load_sticky_rules(parent_workspace, user_config, max_chars=4000)

        self.assertIn("INTERNAL LEAF", leaf_context)
        self.assertIn("### .local-agent/AGENTS.md", leaf_context)
        self.assertNotIn(str(leaf_target), leaf_context)
        self.assertIn("INTERNAL PARENT AGENT", parent_context)
        self.assertIn("### .local-agent/AGENTS.md", parent_context)
        self.assertIn("INTERNAL PARENT RULE", parent_rules)
        self.assertIn("### .local-agent/RULES.md", parent_rules)

    def test_external_dangling_loop_prefix_and_external_parent_symlinks_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            user_config = root / "user"
            user_config.mkdir()
            external = root / "external.md"
            external.write_text("EXTERNAL LEAF\n", encoding="utf-8")

            external_workspace = root / "external-workspace"
            (external_workspace / ".local-agent").mkdir(parents=True)
            (external_workspace / ".local-agent" / "AGENTS.md").symlink_to(external)
            self.assertEqual(
                load_startup_context_files(external_workspace, user_config, max_chars=8000),
                "",
            )

            dangling_workspace = root / "dangling-workspace"
            (dangling_workspace / ".local-agent").mkdir(parents=True)
            (dangling_workspace / ".local-agent" / "AGENTS.md").symlink_to(root / "missing.md")
            self.assertEqual(
                load_startup_context_files(dangling_workspace, user_config, max_chars=8000),
                "",
            )

            loop_workspace = root / "loop-workspace"
            loop_project = loop_workspace / ".local-agent"
            loop_project.mkdir(parents=True)
            (loop_project / "AGENTS.md").symlink_to(loop_project / "RULES.md")
            (loop_project / "RULES.md").symlink_to(loop_project / "AGENTS.md")
            self.assertEqual(load_startup_context_files(loop_workspace, user_config, max_chars=8000), "")
            self.assertEqual(load_sticky_rules(loop_workspace, user_config, max_chars=4000), "")

            prefix_workspace = root / "repo"
            prefix_sibling = root / "repo-external"
            (prefix_workspace / ".local-agent").mkdir(parents=True)
            prefix_sibling.mkdir()
            sibling_target = prefix_sibling / "AGENTS.md"
            sibling_target.write_text("PREFIX SIBLING\n", encoding="utf-8")
            (prefix_workspace / ".local-agent" / "AGENTS.md").symlink_to(sibling_target)
            self.assertEqual(
                load_startup_context_files(prefix_workspace, user_config, max_chars=8000),
                "",
            )

            parent_workspace = root / "parent-workspace"
            parent_workspace.mkdir()
            external_parent = root / "external-parent"
            external_parent.mkdir()
            (external_parent / "AGENTS.md").write_text("EXTERNAL PARENT\n", encoding="utf-8")
            (parent_workspace / ".local-agent").symlink_to(external_parent)
            self.assertEqual(
                load_startup_context_files(parent_workspace, user_config, max_chars=8000),
                "",
            )

    def test_directory_fifo_and_socket_project_slots_are_skipped_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            user_config = root / "user"
            user_config.mkdir()

            directory_workspace = root / "directory"
            (directory_workspace / ".local-agent" / "AGENTS.md").mkdir(parents=True)
            self.assertEqual(
                load_startup_context_files(directory_workspace, user_config, max_chars=8000),
                "",
            )

            if hasattr(os, "mkfifo"):
                fifo_workspace = root / "fifo"
                fifo_project = fifo_workspace / ".local-agent"
                fifo_project.mkdir(parents=True)
                os.mkfifo(fifo_project / "AGENTS.md")
                self.assertEqual(
                    load_startup_context_files(fifo_workspace, user_config, max_chars=8000),
                    "",
                )

            socket_workspace = root / "socket"
            socket_project = socket_workspace / ".local-agent"
            socket_project.mkdir(parents=True)
            socket_path = socket_project / "AGENTS.md"
            socket_path.write_text("SOCKET PLACEHOLDER\n", encoding="utf-8")
            real_lstat = Path.lstat

            def socket_mode_lstat(path: Path):
                if path == socket_path:
                    return SimpleNamespace(st_mode=stat.S_IFSOCK)
                return real_lstat(path)

            with patch.object(Path, "lstat", socket_mode_lstat):
                self.assertEqual(
                    load_startup_context_files(socket_workspace, user_config, max_chars=8000),
                    "",
                )

    @unittest.skipUnless(hasattr(os, "O_NONBLOCK"), "requires nonblocking file-open flags")
    def test_lstat_then_fifo_swap_uses_nonblocking_open_and_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            user_config = root / "user"
            project = workspace / ".local-agent"
            project.mkdir(parents=True)
            user_config.mkdir()
            source = project / "AGENTS.md"
            source.write_text("ORIGINAL\n", encoding="utf-8")
            real_open = os.open
            observed_flags: list[int] = []

            def swap_to_fifo(path: object, flags: int) -> int:
                observed_flags.append(flags)
                source.unlink()
                os.mkfifo(source)
                return real_open(path, flags)

            with patch("local_agent.workspace.startup.os.open", side_effect=swap_to_fifo):
                result = load_startup_context_files(workspace, user_config, max_chars=8000)

        self.assertEqual(result, "")
        self.assertEqual(len(observed_flags), 1)
        self.assertTrue(observed_flags[0] & os.O_NONBLOCK)

    def test_open_inode_mismatch_and_replacement_symlink_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            user_config = root / "user"
            user_config.mkdir()

            inode_workspace = root / "inode"
            inode_project = inode_workspace / ".local-agent"
            inode_project.mkdir(parents=True)
            inode_source = inode_project / "AGENTS.md"
            inode_source.write_text("ORIGINAL\n", encoding="utf-8")
            replacement = inode_workspace / "replacement.md"
            replacement.write_text("REPLACEMENT\n", encoding="utf-8")
            real_open = os.open
            with patch(
                "local_agent.workspace.startup.os.open",
                side_effect=lambda _path, flags: real_open(replacement, flags),
            ):
                inode_result = load_startup_context_files(inode_workspace, user_config, max_chars=8000)

            symlink_workspace = root / "symlink"
            symlink_project = symlink_workspace / ".local-agent"
            symlink_project.mkdir(parents=True)
            symlink_source = symlink_project / "AGENTS.md"
            symlink_source.write_text("ORIGINAL\n", encoding="utf-8")
            external = root / "external.md"
            external.write_text("EXTERNAL\n", encoding="utf-8")

            def replace_with_symlink(path: object, flags: int) -> int:
                symlink_source.unlink()
                symlink_source.symlink_to(external)
                return real_open(path, flags)

            with patch(
                "local_agent.workspace.startup.os.open",
                side_effect=replace_with_symlink,
            ):
                symlink_result = load_startup_context_files(
                    symlink_workspace,
                    user_config,
                    max_chars=8000,
                )

        self.assertEqual(inode_result, "")
        self.assertEqual(symlink_result, "")

    def test_short_read_and_every_post_read_snapshot_drift_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            user_config = root / "user"
            project = workspace / ".local-agent"
            project.mkdir(parents=True)
            user_config.mkdir()
            source = project / "AGENTS.md"
            source.write_bytes(b"a" * (64 * 1024 + 128))
            real_read = os.read
            read_count = 0

            def truncate_after_first_read(descriptor: int, size: int) -> bytes:
                nonlocal read_count
                chunk = real_read(descriptor, size)
                read_count += 1
                if read_count == 1:
                    with source.open("r+b") as handle:
                        handle.truncate(128)
                return chunk

            with patch(
                "local_agent.workspace.startup.os.read",
                side_effect=truncate_after_first_read,
            ):
                self.assertEqual(
                    load_startup_context_files(workspace, user_config, max_chars=8000),
                    "",
                )

            source.write_text("stable snapshot\n", encoding="utf-8")
            real_fstat = os.fstat
            for changed_field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"):
                with self.subTest(changed_field=changed_field):
                    fstat_count = 0

                    def drift_after_read(descriptor: int):
                        nonlocal fstat_count
                        current = real_fstat(descriptor)
                        fstat_count += 1
                        values = {
                            "st_mode": current.st_mode,
                            "st_dev": current.st_dev,
                            "st_ino": current.st_ino,
                            "st_size": current.st_size,
                            "st_mtime_ns": current.st_mtime_ns,
                            "st_ctime_ns": current.st_ctime_ns,
                        }
                        if fstat_count == 2:
                            values[changed_field] += 1
                        return SimpleNamespace(**values)

                    with patch(
                        "local_agent.workspace.startup.os.fstat",
                        side_effect=drift_after_read,
                    ):
                        self.assertEqual(
                            load_startup_context_files(workspace, user_config, max_chars=8000),
                            "",
                        )

    def test_utf8_crlf_nul_strip_invalid_and_chunk_boundary_behaviors_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            user_config = root / "user"
            project = workspace / ".local-agent"
            project.mkdir(parents=True)
            user_config.mkdir()
            source = project / "AGENTS.md"

            source.write_bytes(" \r\nalpha\x00\r\n中\r\n ".encode("utf-8"))
            normalized = load_startup_context_files(workspace, user_config, max_chars=8000)
            source.write_bytes(b"\xffinvalid")
            invalid = load_startup_context_files(workspace, user_config, max_chars=8000)
            source.write_bytes(b"a" * 65535 + "中".encode("utf-8") + b"tail")
            split_multibyte = load_startup_context_files(workspace, user_config, max_chars=70000)

        self.assertIn("alpha\n中", normalized)
        self.assertNotIn("\x00", normalized)
        self.assertNotIn("\r", normalized)
        self.assertEqual(invalid, "")
        self.assertIn("中tail", split_multibyte)

    def test_max_chars_accounts_for_header_and_keeps_tail_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            user_config = root / "user"
            project = workspace / ".local-agent"
            project.mkdir(parents=True)
            user_config.mkdir()
            source = project / "AGENTS.md"
            source.write_text("discard-this-prefix-" * 10 + "保留尾部", encoding="utf-8")
            header = "### .local-agent/AGENTS.md\n"
            marker = "...<earlier context truncated>\n"
            limit = len(header) + len(marker) + len("保留尾部")

            clipped = load_startup_context_files(workspace, user_config, max_chars=limit)
            too_small = load_startup_context_files(
                workspace,
                user_config,
                max_chars=len(header),
            )

        self.assertLessEqual(len(clipped), limit)
        self.assertTrue(clipped.startswith(header + marker))
        self.assertTrue(clipped.endswith("保留尾部"))
        self.assertEqual(too_small, "")

    def test_user_config_external_symlink_remains_trusted_and_precedes_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            user_config = root / "user"
            project = workspace / ".local-agent"
            project.mkdir(parents=True)
            user_config.mkdir()
            external = root / "trusted-user-agents.md"
            external.write_text("TRUSTED USER CONTEXT\n", encoding="utf-8")
            user_agents = user_config / "AGENTS.md"
            user_agents.symlink_to(external)
            (project / "AGENTS.md").write_text("PROJECT CONTEXT\n", encoding="utf-8")

            context = load_startup_context_files(workspace, user_config, max_chars=8000)

        user_header = f"### {display_context_path(workspace, user_agents)}"
        target_header = f"### {display_context_path(workspace, external)}"
        self.assertIn(user_header, context)
        self.assertNotIn(target_header, context)
        self.assertLess(context.index("TRUSTED USER CONTEXT"), context.index("PROJECT CONTEXT"))


if __name__ == "__main__":
    unittest.main()

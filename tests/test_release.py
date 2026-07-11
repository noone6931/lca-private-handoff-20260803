from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from local_agent.release import GateRecord
from local_agent.release import ReleaseError
from local_agent.release import install_channel
from local_agent.release import publish_stable_snapshot
from local_agent.release import rollback_stable_snapshot
from local_agent.release import stable_status


class ReleaseChannelTests(unittest.TestCase):
    def test_stable_snapshot_isolated_from_uncommitted_development_source_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            _write_mini_agent(source, "stable-v1")
            channel = root / "channel"
            bin_dir = root / "bin"
            install_channel(
                source_root=source,
                channel_root=channel,
                bin_dir=bin_dir,
                python_executable=sys.executable,
            )
            first = publish_stable_snapshot(
                source_root=source,
                channel_root=channel,
                python_executable=sys.executable,
                gate_runner=_passing_gate,
            )
            self.assertEqual(_run(bin_dir / "lca"), "stable-v1")

            _write_mini_agent(source, "development-v2")
            self.assertEqual(_run(bin_dir / "lca"), "stable-v1")
            self.assertEqual(_run(bin_dir / "lca-dev"), "development-v2")

            with self.assertRaises(ReleaseError):
                publish_stable_snapshot(
                    source_root=source,
                    channel_root=channel,
                    python_executable=sys.executable,
                    gate_runner=_failing_gate,
                )
            self.assertEqual(_run(bin_dir / "lca"), "stable-v1")

            second = publish_stable_snapshot(
                source_root=source,
                channel_root=channel,
                python_executable=sys.executable,
                gate_runner=_passing_gate,
            )
            self.assertEqual(_run(bin_dir / "lca"), "development-v2")
            rolled_back = rollback_stable_snapshot(channel_root=channel, release_id=first.release_id)
            self.assertEqual(rolled_back.release_id, first.release_id)
            self.assertEqual(_run(bin_dir / "lca"), "stable-v1")
            self.assertNotEqual(second.release_id, first.release_id)

    def test_status_and_version_make_channel_provenance_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            _write_mini_agent(source, "v1")
            channel = root / "channel"
            bin_dir = root / "bin"
            install_channel(
                source_root=source,
                channel_root=channel,
                bin_dir=bin_dir,
                python_executable=sys.executable,
            )
            info = publish_stable_snapshot(
                source_root=source,
                channel_root=channel,
                python_executable=sys.executable,
                gate_runner=_passing_gate,
            )
            status = stable_status(channel_root=channel)
            self.assertEqual(status["stable"]["release_id"], info.release_id)
            self.assertEqual(status["development"]["source_root"], str(source.resolve()))
            manifest = json.loads(_run(bin_dir / "lca", "--version"))
            self.assertEqual(manifest["release_id"], info.release_id)
            self.assertIn("channel=development", _run(bin_dir / "lca-dev", "--version"))

    def test_install_refuses_to_overwrite_an_unmanaged_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            _write_mini_agent(source, "v1")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            (bin_dir / "lca").write_text("#!/bin/sh\necho unrelated\n", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseError, "unmanaged launcher"):
                install_channel(
                    source_root=source,
                    channel_root=root / "channel",
                    bin_dir=bin_dir,
                    python_executable=sys.executable,
                )


def _write_mini_agent(root: Path, value: str) -> None:
    package = root / "src" / "local_agent"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        "from __future__ import annotations\n"
        "def main() -> int:\n"
        f"    print({value!r})\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )


def _passing_gate(_source: Path, _python: str) -> tuple[GateRecord, ...]:
    return (GateRecord(("fake-gate",), 0.0),)


def _failing_gate(_source: Path, _python: str) -> tuple[GateRecord, ...]:
    raise ReleaseError("injected gate failure")


def _run(program: Path, *args: str) -> str:
    completed = subprocess.run((str(program), *args), text=True, capture_output=True, check=True)
    return completed.stdout.strip()


if __name__ == "__main__":
    unittest.main()

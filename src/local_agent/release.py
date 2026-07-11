"""Offline stable/development channel management for the local coding agent.

The stable channel deliberately stores an immutable copy of ``src`` instead of
using an editable installation.  That keeps ``lca`` insulated from unfinished
working-tree changes while retaining the project's zero-runtime-dependency
deployment model.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable


MANAGED_MARKER = "# local-coding-agent-managed"
CHANNEL_NAME = "local-coding-agent"


class ReleaseError(RuntimeError):
    """Raised when a release cannot be safely installed, published, or promoted."""


@dataclass(frozen=True)
class GateRecord:
    command: tuple[str, ...]
    elapsed_seconds: float


@dataclass(frozen=True)
class ReleaseInfo:
    release_id: str
    release_dir: Path
    source_revision: str
    source_digest: str
    python_executable: str
    published_at: str
    gate: tuple[GateRecord, ...]


def default_channel_root() -> Path:
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home.expanduser() / CHANNEL_NAME / "channels"


def default_source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def install_channel(
    *,
    source_root: Path,
    channel_root: Path | None = None,
    bin_dir: Path | None = None,
    python_executable: str | None = None,
) -> dict[str, Path]:
    """Install managed ``lca``/``lca-dev``/``lca-release`` launchers offline."""

    source_root = _validated_source_root(source_root)
    channel_root = (channel_root or default_channel_root()).expanduser().resolve()
    bin_dir = (bin_dir or Path.home() / ".local" / "bin").expanduser().resolve()
    python_path = _validated_python(python_executable or sys.executable)

    channel_root.mkdir(parents=True, exist_ok=True)
    (channel_root / "releases").mkdir(exist_ok=True)
    _write_dev_channel(channel_root, source_root, python_path)

    bin_dir.mkdir(parents=True, exist_ok=True)
    launchers = {
        "lca": _stable_launcher(channel_root),
        "lca-dev": _dev_launcher(channel_root),
        "lca-release": _release_launcher(channel_root),
    }
    installed: dict[str, Path] = {}
    for name, contents in launchers.items():
        target = bin_dir / name
        _write_managed_launcher(target, contents)
        installed[name] = target
    return installed


def publish_stable_snapshot(
    *,
    source_root: Path,
    channel_root: Path | None = None,
    python_executable: str | None = None,
    gate_runner: Callable[[Path, str], tuple[GateRecord, ...]] | None = None,
) -> ReleaseInfo:
    """Verify source, copy it into a release directory, then atomically promote it."""

    source_root = _validated_source_root(source_root)
    channel_root = (channel_root or default_channel_root()).expanduser().resolve()
    python_path = _validated_python(python_executable or sys.executable)
    channel_root.mkdir(parents=True, exist_ok=True)
    releases_dir = channel_root / "releases"
    releases_dir.mkdir(exist_ok=True)

    digest_before = source_tree_digest(source_root / "src")
    records = (gate_runner or run_release_gate)(source_root, python_path)
    digest_after = source_tree_digest(source_root / "src")
    if digest_after != digest_before:
        raise ReleaseError("Source changed while the release gate was running; publish again from a stable tree.")

    revision = _source_revision(source_root)
    release_id = _release_id(revision, digest_after)
    release_dir = releases_dir / release_id
    if release_dir.exists():
        _promote_release(channel_root, release_dir)
        return _read_release_info(release_dir)

    temp_dir = Path(tempfile.mkdtemp(prefix=".release-", dir=releases_dir))
    try:
        snapshot_src = temp_dir / "src"
        shutil.copytree(source_root / "src", snapshot_src, ignore=_snapshot_ignore)
        if source_tree_digest(snapshot_src) != digest_after:
            raise ReleaseError("Source changed while the stable snapshot was being copied; publish again.")
        pyproject = source_root / "pyproject.toml"
        if pyproject.exists():
            shutil.copy2(pyproject, temp_dir / "pyproject.toml")
        info = ReleaseInfo(
            release_id=release_id,
            release_dir=release_dir,
            source_revision=revision,
            source_digest=digest_after,
            python_executable=python_path,
            published_at=datetime.now(timezone.utc).isoformat(),
            gate=records,
        )
        _write_manifest(temp_dir, info)
        os.replace(temp_dir, release_dir)
        _promote_release(channel_root, release_dir)
        return info
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def rollback_stable_snapshot(*, channel_root: Path | None = None, release_id: str) -> ReleaseInfo:
    channel_root = (channel_root or default_channel_root()).expanduser().resolve()
    release_dir = channel_root / "releases" / release_id
    if not (release_dir / "manifest.json").is_file():
        raise ReleaseError(f"Unknown stable release: {release_id}")
    _promote_release(channel_root, release_dir)
    return _read_release_info(release_dir)


def stable_status(*, channel_root: Path | None = None) -> dict[str, object]:
    channel_root = (channel_root or default_channel_root()).expanduser().resolve()
    current = channel_root / "current"
    dev_channel = _read_dev_channel(channel_root)
    result: dict[str, object] = {
        "channel_root": str(channel_root),
        "development": dev_channel,
        "stable": None,
    }
    if current.is_symlink() and (current / "manifest.json").is_file():
        result["stable"] = _jsonable_release(_read_release_info(current.resolve()))
    return result


def run_release_gate(source_root: Path, python_executable: str) -> tuple[GateRecord, ...]:
    """Run the offline release gate without inspecting unrelated documentation changes."""

    source_root = _validated_source_root(source_root)
    commands: list[tuple[str, ...]] = [
        (python_executable, "-m", "unittest", "discover", "-s", "tests", "-v"),
        (python_executable, "-m", "compileall", "-q", "src", "tests"),
    ]
    if shutil.which("git") and (source_root / ".git").exists():
        commands.append(
            (
                "git",
                "diff",
                "--check",
                "--",
                "src",
                "tests",
                "pyproject.toml",
                "agent",
                "scripts",
            )
        )

    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(source_root / "src") + (os.pathsep + existing if existing else "")
    records: list[GateRecord] = []
    for command in commands:
        started = time.monotonic()
        completed = subprocess.run(command, cwd=source_root, env=environment, text=True, capture_output=True)
        elapsed = time.monotonic() - started
        if completed.returncode != 0:
            output = (completed.stdout + completed.stderr).strip()
            raise ReleaseError(
                f"Release gate failed: {' '.join(command)}\n{output[-4000:]}"
            )
        records.append(GateRecord(command=command, elapsed_seconds=round(elapsed, 3)))
    return tuple(records)


def source_tree_digest(source_dir: Path) -> str:
    if not source_dir.is_dir():
        raise ReleaseError(f"Source directory not found: {source_dir}")
    digest = hashlib.sha256()
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(source_dir).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _validated_source_root(source_root: Path) -> Path:
    resolved = source_root.expanduser().resolve()
    if not (resolved / "src" / "local_agent" / "cli.py").is_file():
        raise ReleaseError(f"Not a local-coding-agent source root: {resolved}")
    return resolved


def _validated_python(candidate: str) -> str:
    path = Path(candidate).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ReleaseError(f"Python executable is unavailable: {candidate}")
    return str(path)


def _source_revision(source_root: Path) -> str:
    if not shutil.which("git") or not (source_root / ".git").exists():
        return "no-git"
    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=source_root, text=True, capture_output=True, check=False
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown-git"


def _release_id(revision: str, digest: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    revision_part = revision[:12] if revision not in {"no-git", "unknown-git"} else revision
    return f"{timestamp}-{revision_part}-{digest[:12]}"


def _snapshot_ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name == "__pycache__" or name.endswith((".pyc", ".pyo"))}


def _write_manifest(directory: Path, info: ReleaseInfo) -> None:
    payload = _jsonable_release(info)
    (directory / "manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (directory / "python-executable").write_text(info.python_executable + "\n", encoding="utf-8")


def _read_release_info(release_dir: Path) -> ReleaseInfo:
    try:
        raw = json.loads((release_dir / "manifest.json").read_text(encoding="utf-8"))
        records = tuple(
            GateRecord(tuple(record["command"]), float(record["elapsed_seconds"])) for record in raw["gate"]
        )
        return ReleaseInfo(
            release_id=str(raw["release_id"]),
            release_dir=release_dir,
            source_revision=str(raw["source_revision"]),
            source_digest=str(raw["source_digest"]),
            python_executable=str(raw["python_executable"]),
            published_at=str(raw["published_at"]),
            gate=records,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"Invalid release manifest in {release_dir}") from exc


def _jsonable_release(info: ReleaseInfo) -> dict[str, object]:
    payload = asdict(info)
    payload["release_dir"] = str(info.release_dir)
    payload["gate"] = [
        {"command": list(record.command), "elapsed_seconds": record.elapsed_seconds} for record in info.gate
    ]
    return payload


def _promote_release(channel_root: Path, release_dir: Path) -> None:
    if not release_dir.is_dir():
        raise ReleaseError(f"Release directory is unavailable: {release_dir}")
    current = channel_root / "current"
    temporary = channel_root / f".current-{os.getpid()}-{time.time_ns()}"
    relative_target = os.path.relpath(release_dir, channel_root)
    temporary.symlink_to(relative_target)
    try:
        os.replace(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)


def _write_dev_channel(channel_root: Path, source_root: Path, python_executable: str) -> None:
    temporary = channel_root / f".development-{os.getpid()}-{time.time_ns()}"
    temporary.write_text(f"{source_root}\n{python_executable}\n", encoding="utf-8")
    os.replace(temporary, channel_root / "development-channel")


def _read_dev_channel(channel_root: Path) -> dict[str, str] | None:
    path = channel_root / "development-channel"
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 2:
        return {"status": "invalid", "path": str(path)}
    return {"source_root": lines[0], "python_executable": lines[1]}


def _write_managed_launcher(path: Path, contents: str) -> None:
    if path.exists() or path.is_symlink():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ReleaseError(f"Cannot inspect existing launcher: {path}") from exc
        if MANAGED_MARKER not in existing:
            raise ReleaseError(f"Refusing to overwrite unmanaged launcher: {path}")
    temporary = path.with_name(f".{path.name}-{os.getpid()}-{time.time_ns()}")
    temporary.write_text(contents, encoding="utf-8")
    temporary.chmod(temporary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(temporary, path)


def _stable_launcher(channel_root: Path) -> str:
    return _launcher_prefix() + f'''CHANNEL_ROOT={_shell_quote(str(channel_root))}
CURRENT="$CHANNEL_ROOT/current"
if [ ! -d "$CURRENT/src" ] || [ ! -f "$CURRENT/manifest.json" ]; then
  echo "lca: no stable release is published. Run: lca-release publish" >&2
  exit 2
fi
if [ "${{1:-}}" = "--version" ] || [ "${{1:-}}" = "--source" ]; then
  cat "$CURRENT/manifest.json"
  exit 0
fi
PYTHON=$(cat "$CURRENT/python-executable")
if [ ! -x "$PYTHON" ]; then
  echo "lca: release Python is unavailable: $PYTHON" >&2
  exit 2
fi
export PYTHONPATH="$CURRENT/src${{PYTHONPATH:+:$PYTHONPATH}}"
exec "$PYTHON" -m local_agent.cli "$@"
'''


def _dev_launcher(channel_root: Path) -> str:
    return _launcher_prefix() + f'''CHANNEL_ROOT={_shell_quote(str(channel_root))}
CHANNEL="$CHANNEL_ROOT/development-channel"
if [ ! -f "$CHANNEL" ]; then
  echo "lca-dev: development channel is not installed. Run: python3 scripts/lca_release.py install" >&2
  exit 2
fi
SOURCE_ROOT=$(sed -n '1p' "$CHANNEL")
PYTHON=$(sed -n '2p' "$CHANNEL")
if [ ! -d "$SOURCE_ROOT/src" ] || [ ! -x "$PYTHON" ]; then
  echo "lca-dev: development source or Python is unavailable; run install again." >&2
  exit 2
fi
if [ "${{1:-}}" = "--version" ] || [ "${{1:-}}" = "--source" ]; then
  echo "channel=development"
  echo "source_root=$SOURCE_ROOT"
  echo "python_executable=$PYTHON"
  if command -v git >/dev/null 2>&1 && [ -d "$SOURCE_ROOT/.git" ]; then
    git -C "$SOURCE_ROOT" rev-parse HEAD
  fi
  exit 0
fi
export PYTHONPATH="$SOURCE_ROOT/src${{PYTHONPATH:+:$PYTHONPATH}}"
exec "$PYTHON" -m local_agent.cli "$@"
'''


def _release_launcher(channel_root: Path) -> str:
    return _launcher_prefix() + f'''CHANNEL_ROOT={_shell_quote(str(channel_root))}
CHANNEL="$CHANNEL_ROOT/development-channel"
if [ ! -f "$CHANNEL" ]; then
  echo "lca-release: development channel is not installed. Run: python3 scripts/lca_release.py install" >&2
  exit 2
fi
SOURCE_ROOT=$(sed -n '1p' "$CHANNEL")
PYTHON=$(sed -n '2p' "$CHANNEL")
if [ ! -f "$SOURCE_ROOT/scripts/lca_release.py" ] || [ ! -x "$PYTHON" ]; then
  echo "lca-release: development source or Python is unavailable; run install again." >&2
  exit 2
fi
exec "$PYTHON" "$SOURCE_ROOT/scripts/lca_release.py" --channel-root "$CHANNEL_ROOT" "$@"
'''


def _launcher_prefix() -> str:
    return f"#!/usr/bin/env sh\n{MANAGED_MARKER}\nset -eu\n"


def _shell_quote(value: str) -> str:
    return shlex.quote(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lca-release", description="Manage offline stable and development LCA channels.")
    parser.add_argument(
        "--channel-root",
        help="Channel state root; defaults to XDG_STATE_HOME/local-coding-agent/channels.",
    )
    parser.add_argument("--source-root", help="Development source root; defaults to this repository.")
    parser.add_argument("--python", dest="python_executable", help="Python executable recorded for both channels.")
    parser.add_argument("--bin-dir", help="Where install writes lca, lca-dev, and lca-release launchers.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("install", help="Install or refresh managed launchers without downloading dependencies.")
    subparsers.add_parser("publish", aliases=["promote"], help="Run the full local gate and atomically promote a stable snapshot.")
    subparsers.add_parser("status", help="Show stable and development channel provenance.")
    rollback = subparsers.add_parser("rollback", help="Atomically point lca at an earlier stable release.")
    rollback.add_argument("release_id")
    args = parser.parse_args(argv)

    channel_root = Path(args.channel_root).expanduser() if args.channel_root else None
    source_root = Path(args.source_root).expanduser() if args.source_root else default_source_root()
    try:
        if args.command == "install":
            installed = install_channel(
                source_root=source_root,
                channel_root=channel_root,
                bin_dir=Path(args.bin_dir).expanduser() if args.bin_dir else None,
                python_executable=args.python_executable,
            )
            print(json.dumps({name: str(path) for name, path in installed.items()}, indent=2, sort_keys=True))
        elif args.command in {"publish", "promote"}:
            info = publish_stable_snapshot(
                source_root=source_root,
                channel_root=channel_root,
                python_executable=args.python_executable,
            )
            print(json.dumps(_jsonable_release(info), indent=2, sort_keys=True))
        elif args.command == "status":
            print(json.dumps(stable_status(channel_root=channel_root), indent=2, sort_keys=True))
        elif args.command == "rollback":
            info = rollback_stable_snapshot(channel_root=channel_root, release_id=args.release_id)
            print(json.dumps(_jsonable_release(info), indent=2, sort_keys=True))
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

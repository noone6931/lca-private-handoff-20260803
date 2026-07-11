from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class WorkspaceMigrationError(RuntimeError):
    """Raised when a primary-workspace session migration cannot complete safely."""


@dataclass(frozen=True)
class SessionArtifactMove:
    source: Path
    target: Path


def migrate_session_artifacts(*, source_state_dir: Path, target_state_dir: Path, session_id: str) -> tuple[SessionArtifactMove, ...]:
    """Atomically relocate this session's persistent artifacts, or roll back every move."""

    source_root = source_state_dir.expanduser().resolve()
    target_root = target_state_dir.expanduser().resolve()
    if source_root == target_root:
        return ()

    moves = tuple(
        SessionArtifactMove(source_root / category / filename, target_root / category / filename)
        for category, filename in (
            ("sessions", f"{session_id}.jsonl"),
            ("todos", f"{session_id}.json"),
            ("patches", f"{session_id}.jsonl"),
        )
        if (source_root / category / filename).exists()
    )
    session_moves = [move for move in moves if move.source.parent.name == "sessions"]
    if len(session_moves) != 1:
        raise WorkspaceMigrationError(f"Session artifact not found for migration: {session_id}")
    conflicts = [move.target for move in moves if move.target.exists()]
    if conflicts:
        rendered = ", ".join(str(path) for path in conflicts)
        raise WorkspaceMigrationError(f"Refusing workspace move because target artifact already exists: {rendered}")

    moved: list[SessionArtifactMove] = []
    try:
        for move in moves:
            move.target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(move.source, move.target)
            moved.append(move)
    except OSError as exc:
        try:
            rollback_session_artifacts(moved)
            detail = ""
        except WorkspaceMigrationError as rollback_exc:
            detail = f"; rollback errors: {rollback_exc}"
        raise WorkspaceMigrationError(f"Workspace session migration failed: {exc}{detail}") from exc
    return moves


def rollback_session_artifacts(moves: tuple[SessionArtifactMove, ...] | list[SessionArtifactMove]) -> None:
    """Move completed artifacts back to their original state directory.

    This is intentionally public to let the Runtime compensate when a later
    in-memory or session-store commit fails after the filesystem move succeeds.
    """

    rollback_errors: list[str] = []
    for completed in reversed(moves):
        try:
            if not completed.target.exists():
                raise FileNotFoundError(f"Migrated artifact is missing: {completed.target}")
            if completed.source.exists():
                raise FileExistsError(f"Refusing to overwrite restored artifact: {completed.source}")
            completed.source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(completed.target, completed.source)
        except OSError as exc:
            rollback_errors.append(f"{completed.target}: {exc}")
    if rollback_errors:
        raise WorkspaceMigrationError("; ".join(rollback_errors))


__all__ = [
    "SessionArtifactMove",
    "WorkspaceMigrationError",
    "migrate_session_artifacts",
    "rollback_session_artifacts",
]

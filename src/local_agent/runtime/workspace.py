"""Workspace/session-root lifecycle phase."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from ..lsp.client import close_all_clients
from ..workspace.path_rules import discover_path_scoped_rules
from .context import RunContext
from ..session.guards import SessionGuardState
from ..workspace.startup import build_system_prompt
from ..session.state import workspace_state_dir
from ..workspace.context import WorkspaceContext
from ..workspace.migration import WorkspaceMigrationError, migrate_session_artifacts, rollback_session_artifacts

STARTUP_MEMORY_CHAR_LIMIT = 8000
STARTUP_CONTEXT_CHAR_LIMIT = 8000
STARTUP_SKILLS_CHAR_LIMIT = 4000
MAX_AUTHORED_SKILLS = 40
MAX_SKILL_DESCRIPTION_CHARS = 320


class WorkspaceRuntimePort(Protocol):
    """Explicit workspace/session services consumed by the workspace phase."""

    _base_system_prompt: str
    _evidence_phase: Any
    _events: Any
    _is_running: bool
    _last_run_summary: Any
    _messages: list[dict[str, Any]]
    _path_rule_index: Any
    _run: Any
    _session: Any
    _session_evidence: Any
    _session_guards: Any
    _state_dir: Path
    _state_root: Path | None
    _summary_cache: dict[str, str]
    _tool_context: Any
    _user_config_dir: Path
    _workspace_context: WorkspaceContext


class WorkspaceLifecycle:
    """Cohesive Runtime phase kept outside the turn orchestrator."""

    def __init__(self, runtime: WorkspaceRuntimePort) -> None:
        self._runtime = runtime

    def add_workspace_root(self, raw_path: str) -> Path:
        runtime = self._runtime
        self.ensure_workspace_idle()
        next_context = runtime._workspace_context.copy()
        path, changed = next_context.add_session_root(raw_path)
        self.record_workspace_roots_change(next_context, "add", path, changed)
        return path

    def remove_workspace_root(self, raw_path: str) -> Path:
        runtime = self._runtime
        self.ensure_workspace_idle()
        next_context = runtime._workspace_context.copy()
        path, changed = next_context.remove_session_root(raw_path)
        self.record_workspace_roots_change(next_context, "remove", path, changed)
        return path

    def reset_workspace_roots(self) -> None:
        runtime = self._runtime
        self.ensure_workspace_idle()
        next_context = runtime._workspace_context.copy()
        changed = next_context.reset_session_roots()
        self.record_workspace_roots_change(next_context, "reset", None, changed)

    def move_workspace(self, raw_path: str) -> Path:
        runtime = self._runtime
        """Move this session's primary workspace without losing its session identity."""

        self.ensure_workspace_idle()
        next_context, changed = runtime._workspace_context.moved_primary(raw_path)
        if not changed:
            return runtime._workspace_context.primary
        if runtime._state_root is None:
            raise RuntimeError(
                "Cannot move a runtime without a workspace-partitioned state root. Restart with --state-dir first."
            )

        previous_primary = runtime._workspace_context.primary
        next_state_dir = workspace_state_dir(runtime._state_root, next_context.primary)
        # Loading startup sources is intentionally done before moving any artifact.
        # A read failure must not leave the session split across two state dirs.
        try:
            next_system_prompt = self.build_system_prompt_for(next_context, next_state_dir)
            next_path_rule_index = discover_path_scoped_rules(next_context.all_roots)
            previous_session_bytes = runtime._session.path.read_bytes()
        except OSError as exc:
            raise WorkspaceMigrationError(f"Cannot prepare workspace move: {exc}") from exc

        previous_workspace_context = runtime._workspace_context
        previous_state_dir = runtime._state_dir
        previous_tool_context = runtime._tool_context
        previous_messages = runtime._messages
        previous_run = runtime._run
        previous_session_guards = runtime._session_guards
        previous_summary_cache = runtime._summary_cache
        previous_last_run_summary = runtime._last_run_summary
        previous_path_rule_index = runtime._path_rule_index
        previous_session_location = (
            runtime._session.state_dir,
            runtime._session.session_dir,
            runtime._session.path,
        )
        next_tool_context = replace(
            runtime._tool_context,
            workspace=next_context.primary,
            state_dir=next_state_dir,
            allowed_dirs=next_context.additional_roots,
            workspace_identity=next_context.primary_identity,
        )
        next_messages = [
            {"role": "system", "content": next_system_prompt},
            *(message for message in runtime._messages if message.get("role") != "system"),
        ]
        payload = next_context.snapshot(operation="move", path=next_context.primary, changed=True)
        payload.update(
            {
                "previous_primary": str(previous_primary),
                "state_dir": str(next_state_dir),
            }
        )
        moves = migrate_session_artifacts(
            source_state_dir=runtime._state_dir,
            target_state_dir=next_state_dir,
            session_id=runtime._session.session_id,
        )

        try:
            runtime._session.relocate(next_state_dir)
            runtime._workspace_context = next_context
            runtime._state_dir = next_state_dir
            runtime._tool_context = next_tool_context
            runtime._messages = next_messages
            runtime._run = RunContext()
            runtime._session_guards = SessionGuardState()
            runtime._summary_cache = {}
            runtime._last_run_summary = None
            runtime._path_rule_index = next_path_rule_index
            close_all_clients()
            runtime._session.append("workspace_moved", payload)
        except Exception as exc:  # noqa: BLE001 - every post-migration commit must compensate.
            runtime._workspace_context = previous_workspace_context
            runtime._state_dir = previous_state_dir
            runtime._tool_context = previous_tool_context
            runtime._messages = previous_messages
            runtime._run = previous_run
            runtime._session_guards = previous_session_guards
            runtime._summary_cache = previous_summary_cache
            runtime._last_run_summary = previous_last_run_summary
            runtime._path_rule_index = previous_path_rule_index
            rollback_error: Exception | None = None
            try:
                rollback_session_artifacts(moves)
                previous_session_location[2].write_bytes(previous_session_bytes)
            except Exception as rollback_exc:  # noqa: BLE001 - include compensation failure in the raised error.
                rollback_error = rollback_exc
            finally:
                # JsonlSessionStore.relocate() only assigns these three fields. Restore
                # them directly so a failing relocate mock cannot strand Runtime state.
                runtime._session.state_dir = previous_session_location[0]
                runtime._session.session_dir = previous_session_location[1]
                runtime._session.path = previous_session_location[2]
            detail = f"; rollback failed: {rollback_error}" if rollback_error is not None else ""
            raise WorkspaceMigrationError(f"Workspace move failed and was rolled back: {exc}{detail}") from exc
        self.invalidate_session_evidence_for_workspace_change("workspace_moved")
        self.emit_post_commit_event("WorkspaceMoved", payload)
        return next_context.primary

    def ensure_workspace_idle(self) -> None:
        runtime = self._runtime
        if runtime._is_running:
            raise RuntimeError("Workspace roots can only be changed while the runtime is idle.")

    def record_workspace_roots_change(
        self,
        next_context: WorkspaceContext,
        operation: str,
        path: Path | None,
        changed: bool,
    ) -> None:
        runtime = self._runtime
        if not changed:
            return
        payload = next_context.snapshot(operation=operation, path=path, changed=True)
        previous_session_bytes = runtime._session.path.read_bytes()
        try:
            runtime._session.append("workspace_roots_changed", payload)
        except Exception as exc:  # noqa: BLE001 - append can fail after partially writing JSONL.
            rollback_error: Exception | None = None
            try:
                runtime._session.path.write_bytes(previous_session_bytes)
            except Exception as rollback_exc:  # noqa: BLE001 - expose failed compensation to the caller.
                rollback_error = rollback_exc
            detail = f"; session rollback failed: {rollback_error}" if rollback_error is not None else ""
            raise RuntimeError(f"Workspace root change failed and was rolled back: {exc}{detail}") from exc

        runtime._workspace_context = next_context
        runtime._tool_context = replace(
            runtime._tool_context,
            allowed_dirs=next_context.additional_roots,
        )
        runtime._session_guards = SessionGuardState()
        runtime._summary_cache = {}
        self.revalidate_session_evidence_for_workspace_roots_change("workspace_roots_changed")
        self.refresh_path_rules()
        self.emit_post_commit_event("WorkspaceRootsChanged", payload)

    def refresh_path_rules(self) -> None:
        runtime = self._runtime
        runtime._path_rule_index = discover_path_scoped_rules(runtime._workspace_context.all_roots)

    def invalidate_session_evidence_for_workspace_change(self, reason: str) -> None:
        runtime = self._runtime
        removed = runtime._session_evidence.invalidate_workspace_revision()
        if removed:
            runtime._evidence_phase.record_session_evidence_event("invalidated", {"reason": reason, "count": removed})

    def revalidate_session_evidence_for_workspace_roots_change(self, reason: str) -> None:
        runtime = self._runtime
        removed = runtime._session_evidence.revalidate_authorized_roots(
            workspace_revision=runtime._workspace_context.revision,
            authorized_roots=runtime._workspace_context.all_roots,
        )
        if removed:
            runtime._evidence_phase.record_session_evidence_event("invalidated", {"reason": reason, "count": removed})

    def emit_post_commit_event(self, event_type: str, payload: dict[str, object]) -> None:
        runtime = self._runtime
        """Notify an external sink without turning a committed workspace change into a rollback."""

        try:
            runtime._events.emit(event_type, payload)
        except Exception as exc:  # noqa: BLE001 - sinks are observer-only after commit.
            try:
                runtime._session.append(
                    "event_delivery_error",
                    {
                        "event_type": event_type,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            except Exception:
                pass

    def restore_session_workspace_roots(self) -> tuple[Path, ...]:
        runtime = self._runtime
        snapshot = runtime._session.load_latest_workspace_roots()
        if snapshot is None:
            return ()
        primary = snapshot.get("primary")
        if primary is not None and str(primary) != str(runtime._workspace_context.primary):
            return ()
        raw_paths = snapshot.get("session_roots")
        if not isinstance(raw_paths, list) or not all(isinstance(path, str) for path in raw_paths):
            return ()
        revision = snapshot.get("revision")
        try:
            normalized_revision = int(revision) if revision is not None else 0
        except (TypeError, ValueError):
            normalized_revision = 0
        return runtime._workspace_context.restore_session_roots(tuple(raw_paths), normalized_revision)

    def build_system_prompt(self) -> str:
        runtime = self._runtime
        return self.build_system_prompt_for(runtime._workspace_context, runtime._state_dir)

    def build_system_prompt_for(self, workspace_context: WorkspaceContext, state_dir: Path) -> str:
        runtime = self._runtime
        return build_system_prompt(
            runtime._base_system_prompt,
            workspace_context.primary,
            runtime._user_config_dir,
            workspace_identity=workspace_context.primary_identity,
            state_dir=state_dir,
            allowed_dirs=workspace_context.additional_roots,
            startup_context_char_limit=STARTUP_CONTEXT_CHAR_LIMIT,
            startup_memory_char_limit=STARTUP_MEMORY_CHAR_LIMIT,
            startup_skills_char_limit=STARTUP_SKILLS_CHAR_LIMIT,
            max_authored_skills=MAX_AUTHORED_SKILLS,
            max_skill_description_chars=MAX_SKILL_DESCRIPTION_CHARS,
        )

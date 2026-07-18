from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol

from ..config import ConfigError
from .cancellation import RunCancellation
from ..protocol.commands import AgentCommand, CommandResult, UNSUPPORTED_COMMAND_TYPES, command_validation_error, new_command
from ..protocol.events import EventEmitter
from .run_output import emit_runtime_delivery


class _SessionWriter(Protocol):
    def append(self, event: str, payload: dict[str, Any]) -> None: ...
    def load_event_payloads(self, event: str, *, max_events: int = 0) -> list[dict[str, Any]]: ...


class RuntimeCommandPort(Protocol):
    _is_running: bool
    _last_run_summary: dict[str, Any] | None
    _session: _SessionWriter
    def _run_prompt(self, prompt: str) -> str: ...
    def _finish_run_summary(self, reason: str) -> dict[str, Any]: ...
    def approval_summary(self) -> str: ...
    def status_summary(self) -> str: ...
    def tool_summary(self) -> str: ...
    def workspace_summary(self) -> str: ...
    def add_workspace_root(self, raw_path: str) -> Path: ...
    def remove_workspace_root(self, raw_path: str) -> Path: ...
    def reset_workspace_roots(self) -> None: ...
    def move_workspace(self, raw_path: str) -> Path: ...
    def set_session_approval_mode(self, mode: str) -> None: ...
    def set_session_tool_policy(self, tool: str, policy: str) -> None: ...
    def reset_session_tool_policy(self, tool: str) -> None: ...


class CommandDispatcher:
    """Consume validated frontend commands through one synchronous Runtime boundary."""

    def __init__(self, runtime: RuntimeCommandPort, events: EventEmitter, session_id: str) -> None:
        self._runtime = runtime
        self._events = events
        self._session_id = session_id
        self.cancellation = RunCancellation()

    @property
    def is_running(self) -> bool:
        return self._runtime._is_running

    def run(self, prompt: str) -> str:
        result = self.dispatch(new_command("SubmitPrompt", {"prompt": prompt}, session_id=self._session_id))
        if result.error_code == "interrupted":
            raise KeyboardInterrupt
        if not result.ok:
            raise RuntimeError(result.error_message or "Command failed.")
        return str(result.payload.get("content", ""))

    def dispatch(self, command: AgentCommand) -> CommandResult:
        error = command_validation_error(command)
        if error is not None:
            return self._error(command, "invalid_command", error)
        if command.session_id is not None and command.session_id != self._session_id:
            return self._error(command, "session_mismatch", "Command session_id does not match this session.")
        if self._events.command_id is not None:
            return self._error(command, "run_active", "Cannot dispatch a command while another command is active.")
        self._events.begin_command(command.command_id)
        try:
            if command.type in UNSUPPORTED_COMMAND_TYPES:
                self._events.emit("ErrorEvent", {"kind": "unsupported_command", "command_type": command.type})
                return self._error(
                    command,
                    "unsupported_command",
                    f"Command type is not supported by the synchronous runtime: {command.type}",
                    status="unsupported",
                )
            if command.type == "SubmitPrompt":
                return self._submit_prompt(command)
            try:
                return self._dispatch_session_command(command)
            except (ConfigError, RuntimeError, ValueError) as exc:
                return self._error(command, "command_error", str(exc))
        finally:
            self._events.end_command(command.command_id)

    def _submit_prompt(self, command: AgentCommand) -> CommandResult:
        runtime = self._runtime
        if runtime._is_running:
            return self._error(command, "run_active", "Cannot start a new run while the current run is still active.")
        self.cancellation.begin()
        runtime._is_running = True
        run_id = self._events.start_run()
        self._events.emit("TurnStarted", {"command_type": command.type})
        try:
            content = runtime._run_prompt(str(command.payload["prompt"]))
        except KeyboardInterrupt:
            self._ensure_failed_turn("interrupt", "Stopped after user interrupt.")
            return self._error(
                command,
                "interrupted",
                "Command was interrupted.",
                run_id=run_id,
                payload=self._events.turn_finished_payload or {},
            )
        except Exception as exc:  # noqa: BLE001 - the command boundary must always close its turn.
            self._events.emit(
                "ErrorEvent",
                {"kind": "command_execution_error", "message": f"{type(exc).__name__}: {exc}"},
            )
            self._ensure_failed_turn("command_error", "Stopped after runtime error.")
            return self._error(
                command,
                "command_execution_error",
                str(exc),
                run_id=run_id,
                payload=self._events.turn_finished_payload or {},
            )
        finally:
            runtime._is_running = False
            self.cancellation.finish()
        if self._events.turn_finished_payload is None:
            message = "Runtime returned without a terminal turn event."
            self._events.emit("ErrorEvent", {"kind": "missing_turn_terminal", "message": message})
            self._ensure_terminal_turn("Stopped after runtime error.", "command_error")
            return self._error(
                command,
                "missing_turn_terminal",
                message,
                run_id=run_id,
                payload=self._events.turn_finished_payload or {},
            )
        return CommandResult(
            command_id=command.command_id,
            session_id=self._session_id,
            run_id=run_id,
            status="ok",
            payload=self._events.turn_finished_payload or {"content": content},
        )

    def _dispatch_session_command(self, command: AgentCommand) -> CommandResult:
        runtime = self._runtime
        command_type = command.type
        payload = command.payload
        if command_type == "GetStatus":
            text = runtime.status_summary()
        elif command_type == "ListTools":
            text = runtime.tool_summary()
        elif command_type == "GetApproval":
            text = runtime.approval_summary()
        elif command_type == "SetApprovalMode":
            runtime.set_session_approval_mode(str(payload["mode"]))
            text = runtime.approval_summary()
        elif command_type == "SetToolApproval":
            runtime.set_session_tool_policy(str(payload["tool"]), str(payload["policy"]))
            text = runtime.approval_summary()
        elif command_type == "ResetToolApproval":
            runtime.reset_session_tool_policy(str(payload["tool"]))
            text = runtime.approval_summary()
        elif command_type == "ListWorkspaceRoots":
            text = runtime.workspace_summary()
        elif command_type == "AddWorkspaceRoot":
            runtime.add_workspace_root(str(payload["path"]))
            text = runtime.workspace_summary()
        elif command_type == "RemoveWorkspaceRoot":
            runtime.remove_workspace_root(str(payload["path"]))
            text = runtime.workspace_summary()
        elif command_type == "ResetWorkspaceRoots":
            runtime.reset_workspace_roots()
            text = runtime.workspace_summary()
        elif command_type == "MoveWorkspace":
            runtime.move_workspace(str(payload["path"]))
            text = runtime.workspace_summary()
        else:
            return self._error(command, "unsupported_command", f"Unsupported command type: {command_type}")
        return CommandResult(command.command_id, self._session_id, None, "ok", {"text": text})

    def _ensure_failed_turn(self, reason: str, content: str) -> None:
        if self._events.turn_finished_payload is not None:
            return
        summary = self._runtime._last_run_summary
        if self._events.run_summary_emitted and summary is not None:
            finals = self._runtime._session.load_event_payloads("final", max_events=1)
            reason = str(summary.get("termination_reason") or reason)
            content = str(finals[-1]["content"]) if finals and "content" in finals[-1] else content
            emit_runtime_delivery(self._runtime, self._events, content=content, reason=reason, run_summary=summary)
            return
        if reason == "interrupt":
            self._events.emit("ErrorEvent", {"kind": "interrupt", "message": content})
        self._ensure_terminal_turn(content, reason)

    def _ensure_terminal_turn(self, content: str, reason: str) -> None:
        run_id, runtime = self._events.run_id, self._runtime
        runtime._session.append("final", {"content": content})
        try:
            summary = runtime._finish_run_summary(reason)
        except Exception:  # noqa: BLE001 - terminal closure must survive a damaged Runtime summary path.
            summary = runtime._last_run_summary
            if not isinstance(summary, dict) or summary.get("run_id") != run_id:
                summary = {"run_id": run_id, "command_id": self._events.command_id, "termination_reason": reason}
                runtime._last_run_summary = summary
            if not self._events.run_summary_emitted:
                runtime._session.append("run_summary", summary)
                self._events.emit("RunSummary", summary)
        emit_runtime_delivery(runtime, self._events, content=content, reason=reason, run_summary=summary)

    def _error(
        self,
        command: AgentCommand,
        code: str,
        message: str,
        *,
        status: Literal["ok", "error", "unsupported"] = "error",
        run_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> CommandResult:
        return CommandResult(
            command_id=command.command_id, session_id=self._session_id, run_id=run_id,
            status=status, payload=payload or {}, error_code=code, error_message=message,
        )

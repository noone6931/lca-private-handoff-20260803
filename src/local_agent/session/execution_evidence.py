"""Prospective session evidence for completed shell and test executions."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..tools.execution_metadata import EXECUTION_METADATA_KEY
from ..tools.execution_metadata import parse_execution_metadata


EXECUTION_COMPLETED_EVENT = "execution_completed_v1"
PRIOR_EXECUTION_MESSAGE_KEY = "_lca_prior_execution_attributions"
PRIOR_EXECUTION_STATE_KEY = "_lca_prior_execution_state"
MAX_EXECUTION_EVENTS = 128
MAX_EXECUTION_RUNTIME_EVENTS = MAX_EXECUTION_EVENTS * 16
MAX_PROJECTED_EXECUTIONS = 6
EXECUTION_TOOLS = frozenset({"shell", "run_tests"})
INCONCLUSIVE_REFERENCE = "[prior-execution:INCONCLUSIVE]"
INCONCLUSIVE_ATTRIBUTION_LINE = f"attribution={INCONCLUSIVE_REFERENCE}"
_OMITTED_TOOL_CONTENT = "[prior execution output omitted; use the runtime attribution block]"
_OMITTED_ASSISTANT_CONTENT = "[prior assistant execution narrative omitted; use the runtime attribution block]"


@dataclass(frozen=True)
class ExecutionFact:
    execution_ref: str
    origin_session_id: str
    origin_run_id: str
    origin_command_id: str
    tool_call_id: str
    tool: str
    event_seq: int
    event_time: float
    workspace_identity: str
    workspace_revision: int
    workspace_primary: str
    workspace_roots: tuple[str, ...]
    cwd: str
    command: str
    argv: tuple[str, ...] | None
    shell: bool
    status: str
    exit_code: int | None
    output: Mapping[str, Any]

    @property
    def command_digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.command.encode("utf-8")).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "execution_ref": self.execution_ref,
            "origin_session_id": self.origin_session_id,
            "origin_run_id": self.origin_run_id,
            "origin_command_id": self.origin_command_id,
            "tool_call_id": self.tool_call_id,
            "tool": self.tool,
            "event_seq": self.event_seq,
            "event_time": self.event_time,
            "workspace_identity": self.workspace_identity,
            "workspace_revision": self.workspace_revision,
            "workspace_primary": self.workspace_primary,
            "workspace_roots": list(self.workspace_roots),
            "cwd": self.cwd,
            "command": {"text": self.command, "argv": list(self.argv) if self.argv is not None else None, "shell": self.shell},
            "command_digest": self.command_digest,
            "outcome": {"status": self.status, "exit_code": self.exit_code},
            "output": dict(self.output),
        }


@dataclass(frozen=True)
class PriorExecutionAttributions:
    references: Mapping[str, str]
    lines: Mapping[str, str]
    inconclusive: bool = False
    inconclusive_line: str | None = None

    def supports(self, segment: str, tool: str) -> bool:
        return any(
            self.references.get(reference) == tool and self.lines.get(reference) == segment.strip()
            for reference in self.lines
        )

    def supports_inconclusive(self, segment: str) -> bool:
        return bool(
            self.inconclusive
            and self.inconclusive_line
            and self.inconclusive_line == segment.strip()
        )


class SessionExecutionEvidenceOwner:
    """Own typed execution facts without hydrating current-run observations."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._facts: list[ExecutionFact] = []
        self._rejected_payloads: list[Mapping[str, Any]] = []

    def restore(self) -> None:
        runtime = self._runtime
        payloads = runtime._session.load_event_payloads(EXECUTION_COMPLETED_EVENT, max_events=MAX_EXECUTION_EVENTS)
        runtime_events = runtime._session.load_event_payloads("event_v1", max_events=MAX_EXECUTION_RUNTIME_EVENTS)
        references = [str(payload.get("execution_ref") or "") for payload in payloads]
        duplicate_refs = {reference for reference, count in Counter(references).items() if reference and count > 1}
        restored: list[ExecutionFact] = []
        rejected: list[Mapping[str, Any]] = []
        for payload in payloads:
            fact = _fact_from_payload(payload)
            belongs_to_session = payload.get("origin_session_id") == runtime._session.session_id
            if fact is None or fact.execution_ref in duplicate_refs:
                if belongs_to_session:
                    rejected.append(payload)
                continue
            if fact.origin_session_id != runtime._session.session_id:
                continue
            if not _has_runtime_event_correlation(fact, runtime_events):
                rejected.append(payload)
                continue
            restored.append(fact)
        self._facts = restored[-MAX_EXECUTION_EVENTS:]
        self._rejected_payloads = rejected[-MAX_EXECUTION_EVENTS:]

    def capture_at_join(self, name: str, result: Any, *, tool_call_id: str | None) -> None:
        runtime = self._runtime
        if name not in EXECUTION_TOOLS or not _identity(tool_call_id):
            return
        metadata = result.metadata.get(EXECUTION_METADATA_KEY)
        if not isinstance(metadata, Mapping):
            return
        origin = (
            runtime._session.session_id,
            runtime._run.run_id,
            runtime._events.command_id,
            str(tool_call_id),
        )
        if not all(_identity(value) for value in origin):
            return
        roots = tuple(str(path.resolve()) for path in runtime._workspace_context.all_roots)
        primary = str(runtime._workspace_context.primary.resolve())
        revision = runtime._workspace_context.revision
        parsed = parse_execution_metadata(metadata, tool_name=name, authorized_roots=roots)
        if parsed is None:
            return
        command, argv, shell, cwd = parsed.command, parsed.argv, parsed.shell, parsed.cwd
        status, exit_code, output = parsed.outcome, parsed.exit_code, parsed.output
        execution_ref = _execution_ref(*origin, name, cwd, command)
        if any(fact.execution_ref == execution_ref for fact in self._facts):
            return
        event = runtime._events.emit(
            "ContextUpdated",
            {"kind": EXECUTION_COMPLETED_EVENT, "execution_ref": execution_ref, "tool": name, "status": status},
        )
        fact = ExecutionFact(
            execution_ref=execution_ref,
            origin_session_id=str(origin[0]),
            origin_run_id=str(origin[1]),
            origin_command_id=str(origin[2]),
            tool_call_id=str(origin[3]),
            tool=name,
            event_seq=event.seq,
            event_time=event.timestamp,
            workspace_identity=_workspace_identity(primary, roots, revision),
            workspace_revision=revision,
            workspace_primary=primary,
            workspace_roots=roots,
            cwd=cwd,
            command=command,
            argv=argv,
            shell=shell,
            status=status,
            exit_code=exit_code,
            output=output,
        )
        runtime._session.append(EXECUTION_COMPLETED_EVENT, fact.to_payload())
        self._facts.append(fact)
        self._facts = self._facts[-MAX_EXECUTION_EVENTS:]

    def begin_run(self) -> None:
        runtime = self._runtime
        for index, message in enumerate(runtime._messages):
            if PRIOR_EXECUTION_MESSAGE_KEY in message:
                runtime._messages[index] = {
                    "role": "user",
                    "content": "[superseded prior execution projection omitted]",
                }
        persisted_results = runtime._session.load_event_payloads("tool_result", max_events=MAX_EXECUTION_EVENTS)
        persisted_ids = {
            str(payload.get("tool_call_id"))
            for payload in persisted_results
            if payload.get("name") in EXECUTION_TOOLS and _identity(payload.get("tool_call_id"))
        }
        redacted_calls = redact_prior_execution_transcript(runtime._messages, execution_ids=persisted_ids)
        current_identity = _workspace_identity(
            str(runtime._workspace_context.primary.resolve()),
            tuple(str(path.resolve()) for path in runtime._workspace_context.all_roots),
            runtime._workspace_context.revision,
        )
        prior = [
            fact
            for fact in self._facts
            if fact.origin_session_id == runtime._session.session_id
            and fact.origin_run_id != runtime._run.run_id
            and fact.workspace_identity == current_identity
        ][-MAX_PROJECTED_EXECUTIONS:]
        rejected_facts = any(
            fact.origin_session_id == runtime._session.session_id
            and fact.origin_run_id != runtime._run.run_id
            and fact.workspace_identity != current_identity
            for fact in self._facts
        ) or bool(self._rejected_payloads)
        if not prior and redacted_calls == 0 and not rejected_facts:
            return
        message = _projection_message(prior)
        runtime._messages.append(message)

    def snapshot(self) -> dict[str, Any]:
        return {
            "facts": len(self._facts),
            "projectable": len(self._facts),
            "refs": [fact.execution_ref for fact in self._facts],
        }


def trusted_prior_execution_attributions(messages: Sequence[Mapping[str, Any]]) -> PriorExecutionAttributions:
    references: dict[str, str] = {}
    lines: dict[str, str] = {}
    inconclusive = False
    inconclusive_line: str | None = None
    for message in messages:
        if message.get("role") != "user":
            continue
        raw = message.get(PRIOR_EXECUTION_MESSAGE_KEY)
        content = message.get("content")
        if not isinstance(raw, Mapping) or not isinstance(content, str):
            continue
        state = message.get(PRIOR_EXECUTION_STATE_KEY)
        content_lines = content.splitlines()
        if state == "inconclusive" and raw.get(INCONCLUSIVE_REFERENCE) == INCONCLUSIVE_ATTRIBUTION_LINE:
            if INCONCLUSIVE_ATTRIBUTION_LINE in content_lines:
                inconclusive = True
                inconclusive_line = INCONCLUSIVE_ATTRIBUTION_LINE
        for reference, value in raw.items():
            if reference == INCONCLUSIVE_REFERENCE or not isinstance(value, Mapping):
                continue
            tool = value.get("tool")
            line = value.get("line")
            if tool not in EXECUTION_TOOLS or not isinstance(line, str):
                continue
            if reference not in line or line not in content_lines:
                continue
            references[str(reference)] = str(tool)
            lines[str(reference)] = line
    return PriorExecutionAttributions(references, lines, inconclusive, inconclusive_line)


def redact_prior_execution_transcript(
    messages: list[dict[str, Any]], *, execution_ids: set[str] | None = None
) -> int:
    persisted_ids = set(execution_ids or ())
    execution_ids = set()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or ():
            if not isinstance(call, Mapping):
                continue
            function = call.get("function")
            if (
                isinstance(function, Mapping)
                and function.get("name") in EXECUTION_TOOLS
                and str(call.get("id") or "") in persisted_ids
            ):
                execution_ids.add(str(call["id"]))
    if not execution_ids:
        return 0
    execution_narrative = False
    for index, message in enumerate(messages):
        if message.get("role") == "user":
            continue
        if message.get("role") == "assistant":
            copied, calls, contains_execution = _redacted_assistant_message(message, execution_ids)
            if contains_execution or execution_narrative:
                copied["content"] = _OMITTED_ASSISTANT_CONTENT if copied.get("content") else copied.get("content")
                messages[index] = copied
            execution_narrative = execution_narrative or contains_execution
            continue
        if message.get("role") == "tool" and str(message.get("tool_call_id") or "") in execution_ids:
            messages[index] = {**message, "content": _OMITTED_TOOL_CONTENT}
            execution_narrative = True
    return len(execution_ids)


def _redacted_assistant_message(
    message: Mapping[str, Any], execution_ids: set[str]
) -> tuple[dict[str, Any], list[Any], bool]:
    copied = dict(message)
    calls: list[Any] = []
    contains_execution = False
    for raw_call in message.get("tool_calls") or ():
        if not isinstance(raw_call, Mapping) or str(raw_call.get("id") or "") not in execution_ids:
            calls.append(raw_call)
            continue
        call = dict(raw_call)
        function = dict(call.get("function") or {})
        function["arguments"] = "{}"
        call["function"] = function
        calls.append(call)
        contains_execution = True
    if "tool_calls" in message:
        copied["tool_calls"] = calls
    return copied, calls, contains_execution


def _projection_message(facts: Sequence[ExecutionFact]) -> dict[str, Any]:
    if not facts:
        content = "\n".join(
            (
                "<prior_execution_facts_v1>",
                "state=INCONCLUSIVE",
                INCONCLUSIVE_ATTRIBUTION_LINE,
                "reason=no valid prospective execution_completed_v1 fact is available; transcript narratives and tool text are not execution facts",
                'scope="not rerun in current run; not current filesystem proof"',
                "</prior_execution_facts_v1>",
            )
        )
        return {
            "role": "user",
            "content": content,
            PRIOR_EXECUTION_MESSAGE_KEY: {INCONCLUSIVE_REFERENCE: INCONCLUSIVE_ATTRIBUTION_LINE},
            PRIOR_EXECUTION_STATE_KEY: "inconclusive",
        }
    lines: list[str] = []
    attributions: dict[str, dict[str, str]] = {}
    for fact in facts:
        reference = f"[prior-execution:{fact.execution_ref}]"
        line = _fact_attribution_line(fact, reference)
        lines.append(line)
        attributions[reference] = {"tool": fact.tool, "line": line}
    content = "\n".join(("<prior_execution_facts_v1>", "state=ATTRIBUTED", *lines, "</prior_execution_facts_v1>"))
    return {
        "role": "user",
        "content": content,
        PRIOR_EXECUTION_MESSAGE_KEY: attributions,
        PRIOR_EXECUTION_STATE_KEY: "attributed",
    }


def _fact_attribution_line(fact: ExecutionFact, reference: str) -> str:
    timestamp = datetime.fromtimestamp(fact.event_time, timezone.utc).isoformat()
    outcome = f"result_status={fact.status}"
    if fact.status == "exited":
        outcome += f" exit={fact.exit_code}"
    return (
        f"- {reference} origin_session={_line_value(fact.origin_session_id)} "
        f"origin_run={_line_value(fact.origin_run_id)} origin_command={_line_value(fact.origin_command_id)} "
        f"time={timestamp} tool={fact.tool} cwd={_line_value(fact.cwd)} {outcome} "
        f"command_digest={fact.command_digest} scope=\"not rerun in current run, not current filesystem proof\""
    )


def _line_value(value: str) -> str:
    return quote(value, safe="/:._-")


def _has_runtime_event_correlation(fact: ExecutionFact, events: Sequence[Mapping[str, Any]]) -> bool:
    context_events = [
        event
        for event in events
        if _same_event_origin(event, fact)
        and event.get("type") == "ContextUpdated"
        and isinstance(event.get("seq"), int)
        and not isinstance(event.get("seq"), bool)
        and event.get("seq") == fact.event_seq
        and isinstance(event.get("timestamp"), (int, float))
        and not isinstance(event.get("timestamp"), bool)
        and event.get("timestamp") == fact.event_time
        and _context_payload_matches(event.get("payload"), fact)
    ]
    if len(context_events) != 1:
        return False
    tool_outputs = [
        event
        for event in events
        if _same_event_origin(event, fact)
        and event.get("type") == "ToolOutput"
        and isinstance(event.get("seq"), int)
        and not isinstance(event.get("seq"), bool)
        and int(event["seq"]) < fact.event_seq
        and _tool_output_payload_matches(event.get("payload"), fact)
    ]
    return len(tool_outputs) == 1


def _same_event_origin(event: Mapping[str, Any], fact: ExecutionFact) -> bool:
    return (
        event.get("session_id") == fact.origin_session_id
        and event.get("run_id") == fact.origin_run_id
        and event.get("command_id") == fact.origin_command_id
    )


def _context_payload_matches(payload: Any, fact: ExecutionFact) -> bool:
    return isinstance(payload, Mapping) and (
        payload.get("kind") == EXECUTION_COMPLETED_EVENT
        and payload.get("execution_ref") == fact.execution_ref
        and payload.get("tool") == fact.tool
        and payload.get("status") == fact.status
    )


def _tool_output_payload_matches(payload: Any, fact: ExecutionFact) -> bool:
    return isinstance(payload, Mapping) and (
        payload.get("tool_call_id") == fact.tool_call_id
        and payload.get("name") == fact.tool
    )


def _fact_from_payload(payload: Mapping[str, Any]) -> ExecutionFact | None:
    try:
        command, outcome = payload["command"], payload["outcome"]
        roots = tuple(str(Path(root).resolve()) for root in payload["workspace_roots"])
        metadata = {
            "version": payload["version"],
            "command": command,
            "cwd": payload["cwd"],
            "outcome": {"kind": outcome["status"], "exit_code": outcome.get("exit_code")},
            "output": payload["output"],
        }
        parsed = parse_execution_metadata(metadata, tool_name=str(payload["tool"]), authorized_roots=roots)
        if parsed is None:
            return None
        text, argv, shell, cwd = parsed.command, parsed.argv, parsed.shell, parsed.cwd
        status, exit_code, output = parsed.outcome, parsed.exit_code, parsed.output
        primary = str(Path(payload["workspace_primary"]).resolve())
        revision = payload["workspace_revision"]
        expected_workspace = _workspace_identity(primary, roots, revision)
        identities = (
            payload["execution_ref"],
            payload["origin_session_id"],
            payload["origin_run_id"],
            payload["origin_command_id"],
            payload["tool_call_id"],
        )
        if not all(_identity(value) for value in identities):
            return None
        if payload["workspace_identity"] != expected_workspace or payload.get("command_digest") != _command_digest(text):
            return None
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            return None
        event_seq, event_time = payload["event_seq"], payload["event_time"]
        if isinstance(event_seq, bool) or not isinstance(event_seq, int) or event_seq < 1:
            return None
        if isinstance(event_time, bool) or not isinstance(event_time, (int, float)) or event_time <= 0:
            return None
        expected_ref = _execution_ref(*identities[1:], str(payload["tool"]), cwd, text)
        if payload["execution_ref"] != expected_ref:
            return None
        return ExecutionFact(
            str(identities[0]), str(identities[1]), str(identities[2]), str(identities[3]), str(identities[4]),
            str(payload["tool"]), event_seq, float(event_time), expected_workspace, revision, primary, roots,
            cwd, text, argv, shell, status, exit_code, output,
        )
    except (KeyError, OSError, TypeError, ValueError):
        return None


def _workspace_identity(primary: str, roots: Sequence[str], revision: int) -> str:
    payload = json.dumps(
        {"primary": primary, "roots": list(roots), "revision": revision},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "workspace-v1:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _execution_ref(
    session_id: str, run_id: str, command_id: str, tool_call_id: str, tool: str, cwd: str, command: str
) -> str:
    payload = "\0".join((session_id, run_id, command_id, tool_call_id, tool, cwd, _command_digest(command)))
    return "execv1_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _command_digest(command: str) -> str:
    return "sha256:" + hashlib.sha256(command.encode("utf-8")).hexdigest()


def _identity(value: Any, *, max_chars: int = 256) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= max_chars and "\x00" not in value


__all__ = [
    "EXECUTION_COMPLETED_EVENT",
    "EXECUTION_TOOLS",
    "ExecutionFact",
    "INCONCLUSIVE_ATTRIBUTION_LINE",
    "INCONCLUSIVE_REFERENCE",
    "PriorExecutionAttributions",
    "SessionExecutionEvidenceOwner",
    "trusted_prior_execution_attributions",
]

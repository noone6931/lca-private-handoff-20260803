"""Typed assistant-history projection for active runs and JSONL replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping


MESSAGE_ID_KEY = "_lca_message_id"
RUN_ID_KEY = "_lca_run_id"
PHASE_KEY = "_lca_assistant_phase"
ORIGIN_KEY = "_lca_output_origin"
OUTPUT_KIND_KEY = "_lca_output_kind"

TOOL_CALL_PHASE = "tool_call"
UNSETTLED_CANDIDATE_PHASE = "unsettled_candidate"
SETTLED_DELIVERY_PHASE = "settled_delivery"
ASSISTANT_SETTLEMENT_EVENT = "assistant_settlement_v1"
ASSISTANT_SETTLEMENT_VERSION = 1

AssistantPhase = Literal["tool_call", "unsettled_candidate", "settled_delivery"]
OutputOrigin = Literal["provider", "runtime"]
OutputKind = Literal["provider_message", "runtime_augmented", "runtime_replaced", "runtime_only"]
SettledDeliveryIdentity = tuple[str, str, str, str, str]
ProtocolPairIdentity = tuple[str, tuple[tuple[str, str], ...]]

_OUTPUT_KINDS = {"provider_message", "runtime_augmented", "runtime_replaced", "runtime_only"}
_ORIGINS = {"provider", "runtime"}
_SETTLEMENT_FIELDS = {
    "version",
    "run_id",
    "final_message_id",
    "origin",
    "output_kind",
    "content",
    "content_sha256",
}
_REPLAY_SETTLEMENT_KEY = "_lca_replay_settlement_run_id"


class AssistantHistoryError(RuntimeError):
    """Raised when typed assistant history cannot be correlated safely."""


@dataclass
class _ProtocolCandidate:
    assistant: dict[str, Any]
    call_ids: tuple[str, ...]
    results: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class AssistantSettlement:
    run_id: str
    final_message_id: str | None
    origin: OutputOrigin
    output_kind: OutputKind
    content: str
    content_sha256: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        final_message_id: str | None,
        origin: str,
        output_kind: str,
        content: str,
    ) -> "AssistantSettlement":
        if not isinstance(run_id, str) or not run_id:
            raise AssistantHistoryError("Assistant settlement requires a run id.")
        if final_message_id is not None and (not isinstance(final_message_id, str) or not final_message_id):
            raise AssistantHistoryError("Assistant settlement message id is invalid.")
        if origin not in _ORIGINS or output_kind not in _OUTPUT_KINDS:
            raise AssistantHistoryError("Assistant settlement origin or output kind is invalid.")
        if not isinstance(content, str):
            raise AssistantHistoryError("Assistant settlement content must be text.")
        if not _origin_matches_output_kind(origin, output_kind):
            raise AssistantHistoryError("Assistant settlement origin does not match its output kind.")
        if output_kind == "runtime_only" and final_message_id is not None:
            raise AssistantHistoryError("Runtime-only settlement cannot reference a provider message.")
        if output_kind != "runtime_only" and final_message_id is None:
            raise AssistantHistoryError("Assistant settlement is missing provider message correlation.")
        return cls(
            run_id=run_id,
            final_message_id=final_message_id,
            origin=origin,  # type: ignore[arg-type]
            output_kind=output_kind,  # type: ignore[arg-type]
            content=content,
            content_sha256=_content_digest(content),
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AssistantSettlement":
        if set(payload) != _SETTLEMENT_FIELDS or payload.get("version") != ASSISTANT_SETTLEMENT_VERSION:
            raise AssistantHistoryError("Assistant settlement payload shape is invalid.")
        settlement = cls.create(
            run_id=payload.get("run_id"),
            final_message_id=payload.get("final_message_id"),
            origin=payload.get("origin"),
            output_kind=payload.get("output_kind"),
            content=payload.get("content"),
        )
        if payload.get("content_sha256") != settlement.content_sha256:
            raise AssistantHistoryError("Assistant settlement content identity is invalid.")
        return settlement

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": ASSISTANT_SETTLEMENT_VERSION,
            "run_id": self.run_id,
            "final_message_id": self.final_message_id,
            "origin": self.origin,
            "output_kind": self.output_kind,
            "content": self.content,
            "content_sha256": self.content_sha256,
        }


def annotate_provider_message(
    message: Mapping[str, Any],
    *,
    message_id: str,
    run_id: str | None,
) -> dict[str, Any]:
    annotated = dict(message)
    annotated[MESSAGE_ID_KEY] = message_id
    annotated[RUN_ID_KEY] = run_id
    annotated[PHASE_KEY] = TOOL_CALL_PHASE if _has_tool_calls(annotated) else UNSETTLED_CANDIDATE_PHASE
    return annotated


def messages_for_active_run(
    messages: Iterable[dict[str, Any]],
    *,
    active_run_id: str | None,
) -> list[dict[str, Any]]:
    """Keep settled history, protocol pairs, and only this run's unsettled drafts."""

    projected: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "assistant":
            projected.append(message)
            continue
        phase = message.get(PHASE_KEY)
        if phase in {SETTLED_DELIVERY_PHASE, TOOL_CALL_PHASE}:
            projected.append(message)
        elif phase == UNSETTLED_CANDIDATE_PHASE and active_run_id and message.get(RUN_ID_KEY) == active_run_id:
            projected.append(message)
        elif phase is None and _has_tool_calls(message):
            projected.append(message)
    return projected


def has_unsettled_candidate(messages: Iterable[dict[str, Any]], *, run_id: str | None) -> bool:
    return bool(
        run_id
        and any(
            message.get("role") == "assistant"
            and message.get(PHASE_KEY) == UNSETTLED_CANDIDATE_PHASE
            and message.get(RUN_ID_KEY) == run_id
            for message in messages
        )
    )


def project_live_settlement(
    messages: Iterable[dict[str, Any]],
    settlement: AssistantSettlement,
) -> list[dict[str, Any]]:
    candidates = _candidate_messages(messages, settlement.run_id, settlement.final_message_id)
    delivery = _delivery_message(candidates, settlement)
    projected = [
        dict(message)
        for message in messages
        if not (
            message.get("role") == "assistant"
            and message.get(PHASE_KEY) == UNSETTLED_CANDIDATE_PHASE
            and message.get(RUN_ID_KEY) == settlement.run_id
        )
    ]
    projected.append(delivery)
    return projected


def checkpoint_messages(messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Return a durable non-system checkpoint only when assistant authority is typed."""

    projected: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            return None
        if message.get("role") == "system":
            continue
        if message.get("role") not in {"user", "assistant", "tool"}:
            return None
        if message.get("role") != "assistant":
            projected.append(dict(message))
            continue
        phase = message.get(PHASE_KEY)
        if phase == SETTLED_DELIVERY_PHASE and _valid_settled_message(message):
            projected.append(dict(message))
        elif phase == TOOL_CALL_PHASE and _has_tool_calls(message):
            projected.append(dict(message))
        elif phase is None and _has_tool_calls(message):
            projected.append(dict(message))
        else:
            return None
    return projected


class AssistantHistoryReplay:
    """Append-order replay that grants authority only after one valid settlement."""

    def __init__(self) -> None:
        self._messages: list[dict[str, Any]] = []
        self._candidates: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._settlement_counts: dict[str, int] = {}
        self._invalid_settlement_runs: set[str] = set()
        self._settled_authorities: dict[str, SettledDeliveryIdentity] = {}
        self._pending_protocol_by_call: dict[str, _ProtocolCandidate] = {}
        self._seen_protocol_call_ids: set[str] = set()
        self._invalid_protocol_call_ids: set[str] = set()
        self._protocol_authorities: set[ProtocolPairIdentity] = set()

    def install_checkpoint(self, payload: Mapping[str, Any]) -> bool:
        if payload.get("version") != 2 or not isinstance(payload.get("messages"), list):
            return False
        projected = checkpoint_messages(payload["messages"])
        if projected is None or not self._checkpoint_protocol_is_authorized(projected):
            return False
        checkpoint_messages_with_identity: list[dict[str, Any]] = []
        checkpoint_runs: set[str] = set()
        for message in projected:
            checkpoint_message = dict(message)
            if checkpoint_message.get(PHASE_KEY) == SETTLED_DELIVERY_PHASE:
                identity = _settled_delivery_identity(checkpoint_message)
                if identity is None:
                    return False
                run_id = identity[0]
                if (
                    run_id in checkpoint_runs
                    or self._settlement_counts.get(run_id) != 1
                    or run_id in self._invalid_settlement_runs
                    or self._settled_authorities.get(run_id) != identity
                ):
                    return False
                checkpoint_runs.add(run_id)
                checkpoint_message[_REPLAY_SETTLEMENT_KEY] = run_id
            checkpoint_messages_with_identity.append(checkpoint_message)
        self._messages = checkpoint_messages_with_identity
        self._candidates.clear()
        return True

    def append_user(self, payload: Mapping[str, Any]) -> None:
        self._messages.append({"role": "user", "content": payload.get("content", "")})

    def append_assistant(self, payload: Mapping[str, Any]) -> None:
        message = {**payload, "role": "assistant"}
        phase = message.get(PHASE_KEY)
        if phase == TOOL_CALL_PHASE and _has_tool_calls(message):
            self._append_protocol_assistant(message)
            return
        if phase == UNSETTLED_CANDIDATE_PHASE:
            run_id, message_id = message.get(RUN_ID_KEY), message.get(MESSAGE_ID_KEY)
            if isinstance(run_id, str) and run_id and isinstance(message_id, str) and message_id:
                self._candidates.setdefault((run_id, message_id), []).append(message)
            return
        if phase is None and _has_tool_calls(message):
            self._append_protocol_assistant(message)

    def append_tool_result(self, payload: Mapping[str, Any]) -> None:
        result = {
            "role": "tool",
            "tool_call_id": payload.get("tool_call_id"),
            "content": payload.get("content", ""),
        }
        self._messages.append(result)
        call_id = result.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id or not isinstance(result.get("content"), str):
            return
        candidate = self._pending_protocol_by_call.get(call_id)
        if candidate is None:
            self._invalid_protocol_call_ids.add(call_id)
            return
        if call_id in candidate.results:
            self._invalid_protocol_call_ids.add(call_id)
            return
        candidate.results[call_id] = result
        if set(candidate.results) != set(candidate.call_ids):
            return
        for candidate_call_id in candidate.call_ids:
            self._pending_protocol_by_call.pop(candidate_call_id, None)
        identity = _protocol_pair_identity(candidate.assistant, candidate.results)
        if (
            identity is not None
            and not set(candidate.call_ids).intersection(self._invalid_protocol_call_ids)
        ):
            self._protocol_authorities.add(identity)

    def append_settlement(self, payload: Mapping[str, Any]) -> None:
        run_hint = payload.get("run_id")
        if isinstance(run_hint, str) and run_hint:
            self._settlement_counts[run_hint] = self._settlement_counts.get(run_hint, 0) + 1
        try:
            settlement = AssistantSettlement.from_payload(payload)
            candidates = self._candidate_messages_for(settlement)
            delivery = _delivery_message(candidates, settlement)
        except AssistantHistoryError:
            if isinstance(run_hint, str) and run_hint:
                self._invalid_settlement_runs.add(run_hint)
            return
        identity = _settled_delivery_identity(delivery)
        if identity is None:
            self._invalid_settlement_runs.add(settlement.run_id)
            return
        self._settled_authorities.setdefault(settlement.run_id, identity)
        delivery[_REPLAY_SETTLEMENT_KEY] = settlement.run_id
        self._messages.append(delivery)

    def messages(self) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        for message in self._messages:
            settlement_run = message.get(_REPLAY_SETTLEMENT_KEY)
            if isinstance(settlement_run, str):
                if (
                    self._settlement_counts.get(settlement_run) != 1
                    or settlement_run in self._invalid_settlement_runs
                ):
                    continue
                message = dict(message)
                message.pop(_REPLAY_SETTLEMENT_KEY, None)
            projected.append(message)
        return projected

    def _candidate_messages_for(self, settlement: AssistantSettlement) -> list[dict[str, Any]]:
        if settlement.final_message_id is None:
            return []
        return self._candidates.get((settlement.run_id, settlement.final_message_id), [])

    def _append_protocol_assistant(self, message: dict[str, Any]) -> None:
        self._messages.append(message)
        call_ids = _tool_call_ids(message)
        if call_ids is None:
            return
        duplicate_ids = set(call_ids).intersection(self._seen_protocol_call_ids)
        self._seen_protocol_call_ids.update(call_ids)
        self._invalid_protocol_call_ids.update(duplicate_ids)
        if duplicate_ids:
            return
        candidate = _ProtocolCandidate(message, call_ids, {})
        for call_id in call_ids:
            self._pending_protocol_by_call[call_id] = candidate

    def _checkpoint_protocol_is_authorized(self, messages: list[dict[str, Any]]) -> bool:
        checkpoint_call_ids: set[str] = set()
        index = 0
        while index < len(messages):
            message = messages[index]
            if message.get("role") == "tool":
                return False
            if message.get("role") != "assistant" or not _has_tool_calls(message):
                index += 1
                continue
            call_ids = _tool_call_ids(message)
            if (
                call_ids is None
                or checkpoint_call_ids.intersection(call_ids)
                or self._invalid_protocol_call_ids.intersection(call_ids)
            ):
                return False
            checkpoint_call_ids.update(call_ids)
            results: dict[str, dict[str, Any]] = {}
            index += 1
            while index < len(messages) and messages[index].get("role") == "tool":
                result = messages[index]
                call_id = result.get("tool_call_id")
                if (
                    not isinstance(call_id, str)
                    or call_id not in call_ids
                    or call_id in results
                    or not isinstance(result.get("content"), str)
                ):
                    return False
                results[call_id] = result
                index += 1
            identity = _protocol_pair_identity(message, results)
            if identity is None or identity not in self._protocol_authorities:
                return False
        return True


def _candidate_messages(
    messages: Iterable[dict[str, Any]],
    run_id: str,
    final_message_id: str | None,
) -> list[dict[str, Any]]:
    if final_message_id is None:
        return []
    return [
        dict(message)
        for message in messages
        if message.get("role") == "assistant"
        and message.get(PHASE_KEY) == UNSETTLED_CANDIDATE_PHASE
        and message.get(RUN_ID_KEY) == run_id
        and message.get(MESSAGE_ID_KEY) == final_message_id
    ]


def _delivery_message(
    candidates: list[dict[str, Any]],
    settlement: AssistantSettlement,
) -> dict[str, Any]:
    if settlement.output_kind == "runtime_only":
        if candidates:
            raise AssistantHistoryError("Runtime-only settlement cannot reference a provider candidate.")
        delivery: dict[str, Any] = {"role": "assistant", "content": settlement.content}
        message_id = f"runtime:{settlement.run_id}"
    else:
        if len(candidates) != 1:
            raise AssistantHistoryError("Assistant settlement candidate correlation is missing or ambiguous.")
        candidate = candidates[0]
        candidate_content = candidate.get("content")
        if not isinstance(candidate_content, str):
            raise AssistantHistoryError("Assistant settlement candidate content is invalid.")
        if settlement.output_kind == "provider_message" and settlement.content != candidate_content:
            raise AssistantHistoryError("Provider settlement content does not match its candidate.")
        if settlement.output_kind == "runtime_augmented" and not (
            candidate_content and settlement.content.startswith(f"{candidate_content.rstrip()}\n\n")
        ):
            raise AssistantHistoryError("Augmented settlement content does not extend its candidate.")
        if settlement.output_kind == "runtime_replaced" and (
            settlement.content == candidate_content
            or (
                candidate_content
                and settlement.content.startswith(f"{candidate_content.rstrip()}\n\n")
            )
        ):
            raise AssistantHistoryError("Replaced settlement content is not a replacement.")
        delivery = dict(candidate) if settlement.output_kind == "provider_message" else {
            "role": "assistant",
            "content": settlement.content,
        }
        message_id = settlement.final_message_id
    delivery[MESSAGE_ID_KEY] = message_id
    delivery[RUN_ID_KEY] = settlement.run_id
    delivery[PHASE_KEY] = SETTLED_DELIVERY_PHASE
    delivery[ORIGIN_KEY] = settlement.origin
    delivery[OUTPUT_KIND_KEY] = settlement.output_kind
    return delivery


def _valid_settled_message(message: Mapping[str, Any]) -> bool:
    message_id = message.get(MESSAGE_ID_KEY)
    run_id = message.get(RUN_ID_KEY)
    origin = message.get(ORIGIN_KEY)
    output_kind = message.get(OUTPUT_KIND_KEY)
    if (
        not isinstance(message_id, str)
        or not message_id
        or not isinstance(run_id, str)
        or not run_id
        or origin not in _ORIGINS
        or output_kind not in _OUTPUT_KINDS
        or not isinstance(message.get("content"), str)
        or not _origin_matches_output_kind(origin, output_kind)
    ):
        return False
    runtime_message_id = f"runtime:{run_id}"
    return (
        message_id == runtime_message_id
        if output_kind == "runtime_only"
        else message_id != runtime_message_id
    )


def _origin_matches_output_kind(origin: object, output_kind: object) -> bool:
    return (
        origin == "provider" and output_kind == "provider_message"
    ) or (
        origin == "runtime" and output_kind in {"runtime_augmented", "runtime_replaced", "runtime_only"}
    )


def _settled_delivery_identity(message: Mapping[str, Any]) -> SettledDeliveryIdentity | None:
    if not _valid_settled_message(message):
        return None
    return (
        message[RUN_ID_KEY],
        message[MESSAGE_ID_KEY],
        message[ORIGIN_KEY],
        message[OUTPUT_KIND_KEY],
        message["content"],
    )


def _tool_call_ids(message: Mapping[str, Any]) -> tuple[str, ...] | None:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        return None
    call_ids: list[str] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, Mapping):
            return None
        call_id = tool_call.get("id")
        if not isinstance(call_id, str) or not call_id or call_id in call_ids:
            return None
        call_ids.append(call_id)
    return tuple(call_ids)


def _protocol_pair_identity(
    assistant: Mapping[str, Any],
    results: Mapping[str, Mapping[str, Any]],
) -> ProtocolPairIdentity | None:
    call_ids = _tool_call_ids(assistant)
    if call_ids is None or set(results) != set(call_ids):
        return None
    assistant_digest = _stable_message_digest(assistant)
    result_digests = tuple(
        (call_id, _stable_message_digest(results[call_id]))
        for call_id in sorted(call_ids)
    )
    if assistant_digest is None or any(digest is None for _, digest in result_digests):
        return None
    return assistant_digest, result_digests  # type: ignore[return-value]


def _stable_message_digest(message: Mapping[str, Any]) -> str | None:
    try:
        encoded = json.dumps(
            dict(message),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _has_tool_calls(message: Mapping[str, Any]) -> bool:
    tool_calls = message.get("tool_calls")
    return isinstance(tool_calls, list) and bool(tool_calls)


def _content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()

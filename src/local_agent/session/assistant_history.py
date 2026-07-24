"""Typed assistant-history projection for active runs and JSONL replay."""

from __future__ import annotations

import hashlib
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

    def install_checkpoint(self, payload: Mapping[str, Any]) -> bool:
        if payload.get("version") != 2 or not isinstance(payload.get("messages"), list):
            return False
        projected = checkpoint_messages(payload["messages"])
        if projected is None:
            return False
        checkpoint_messages_with_identity: list[dict[str, Any]] = []
        settlement_counts: dict[str, int] = {}
        for message in projected:
            checkpoint_message = dict(message)
            if checkpoint_message.get(PHASE_KEY) == SETTLED_DELIVERY_PHASE:
                run_id = checkpoint_message[RUN_ID_KEY]
                if run_id in settlement_counts:
                    return False
                settlement_counts[run_id] = 1
                checkpoint_message[_REPLAY_SETTLEMENT_KEY] = run_id
            checkpoint_messages_with_identity.append(checkpoint_message)
        self._messages = checkpoint_messages_with_identity
        self._candidates.clear()
        self._settlement_counts = settlement_counts
        self._invalid_settlement_runs.clear()
        return True

    def append_user(self, payload: Mapping[str, Any]) -> None:
        self._messages.append({"role": "user", "content": payload.get("content", "")})

    def append_assistant(self, payload: Mapping[str, Any]) -> None:
        message = {**payload, "role": "assistant"}
        phase = message.get(PHASE_KEY)
        if phase == TOOL_CALL_PHASE and _has_tool_calls(message):
            self._messages.append(message)
            return
        if phase == UNSETTLED_CANDIDATE_PHASE:
            run_id, message_id = message.get(RUN_ID_KEY), message.get(MESSAGE_ID_KEY)
            if isinstance(run_id, str) and run_id and isinstance(message_id, str) and message_id:
                self._candidates.setdefault((run_id, message_id), []).append(message)
            return
        if phase is None and _has_tool_calls(message):
            self._messages.append(message)

    def append_tool_result(self, payload: Mapping[str, Any]) -> None:
        self._messages.append(
            {
                "role": "tool",
                "tool_call_id": payload.get("tool_call_id"),
                "content": payload.get("content", ""),
            }
        )

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


def _has_tool_calls(message: Mapping[str, Any]) -> bool:
    tool_calls = message.get("tool_calls")
    return isinstance(tool_calls, list) and bool(tool_calls)


def _content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()

from __future__ import annotations

import codecs
import json
from dataclasses import dataclass, field
from typing import Any, Generator, Iterable, Mapping, Protocol


class ProviderStreamError(ValueError):
    """Raised when a streamed provider response cannot form one complete message."""


@dataclass(frozen=True)
class ProviderTextDelta:
    delta: str


@dataclass(frozen=True)
class RawChatCompletion:
    message: dict[str, Any]
    finish_reason: str | None


class ReadableResponse(Protocol):
    def read(self, size: int = -1) -> bytes:
        ...


@dataclass
class _ToolCallAccumulator:
    call_id: str = ""
    call_type: str = ""
    name: str = ""
    arguments: str = ""

    def apply(self, value: Mapping[str, Any]) -> None:
        if "id" in value:
            self.call_id = _merge_identity_fragment(self.call_id, value["id"], "tool call id")
        if "type" in value:
            part = _string_fragment(value["type"], "tool call type")
            if not self.call_type:
                self.call_type = part
            elif part and part != self.call_type:
                combined = self.call_type + part
                if combined != "function":
                    raise ProviderStreamError("Stream changed a tool call type after it was established.")
                self.call_type = combined
        function = value.get("function")
        if function is None:
            return
        if not isinstance(function, Mapping):
            raise ProviderStreamError("Streamed tool call function must be an object.")
        if "name" in function:
            self.name = _merge_identity_fragment(self.name, function["name"], "tool function name")
        if "arguments" in function:
            self.arguments = _append_string_fragment(
                self.arguments,
                function["arguments"],
                "tool function arguments",
            )

    def to_dict(self, index: int) -> dict[str, Any]:
        if not self.call_id or self.call_type != "function" or not self.name or not self.arguments:
            raise ProviderStreamError(f"Stream ended with incomplete tool call index {index}.")
        return {
            "id": self.call_id,
            "type": self.call_type,
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class _ChatCompletionAccumulator:
    role: str = "assistant"
    content_parts: list[str] = field(default_factory=list)
    saw_content: bool = False
    finish_reason: str | None = None
    tool_calls: dict[int, _ToolCallAccumulator] = field(default_factory=dict)
    saw_choice: bool = False

    def apply(self, payload: object) -> tuple[ProviderTextDelta, ...]:
        if not isinstance(payload, Mapping):
            raise ProviderStreamError("Stream event must be a JSON object.")
        choices = payload.get("choices")
        if not isinstance(choices, list):
            raise ProviderStreamError("Stream event is missing a choices array.")
        if not choices:
            return ()
        if len(choices) != 1 or not isinstance(choices[0], Mapping):
            raise ProviderStreamError("Only one streamed choice is supported.")
        choice = choices[0]
        if type(choice.get("index")) is not int or choice["index"] != 0:
            raise ProviderStreamError("Streamed choice index must be the integer 0.")
        if self.finish_reason is not None:
            raise ProviderStreamError("Stream emitted choice data after its terminal finish reason.")
        self.saw_choice = True
        delta = choice.get("delta", {})
        if not isinstance(delta, Mapping):
            raise ProviderStreamError("Streamed choice delta must be an object.")
        role = delta.get("role")
        if role is not None:
            if not isinstance(role, str) or role != "assistant":
                raise ProviderStreamError("Streamed choice role must be assistant.")
            self.role = role
        emitted: list[ProviderTextDelta] = []
        if "content" in delta and delta.get("content") is not None:
            content = _string_fragment(delta.get("content"), "assistant content")
            self.saw_content = True
            self.content_parts.append(content)
            if content:
                emitted.append(ProviderTextDelta(content))
        tool_calls = delta.get("tool_calls")
        if tool_calls is not None:
            if not isinstance(tool_calls, list):
                raise ProviderStreamError("Streamed tool_calls must be an array.")
            for tool_call in tool_calls:
                if not isinstance(tool_call, Mapping):
                    raise ProviderStreamError("Each streamed tool call must be an object.")
                index = tool_call.get("index")
                if type(index) is not int or index < 0:
                    raise ProviderStreamError("Streamed tool call index must be a non-negative integer.")
                self.tool_calls.setdefault(index, _ToolCallAccumulator()).apply(tool_call)
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            if not isinstance(finish_reason, str) or not finish_reason:
                raise ProviderStreamError("Streamed finish_reason must be a non-empty string or null.")
            self.finish_reason = finish_reason
        return tuple(emitted)

    def complete(self) -> RawChatCompletion:
        if not self.saw_choice or self.finish_reason is None:
            raise ProviderStreamError("Stream ended without a terminal choice.")
        indices = sorted(self.tool_calls)
        if indices and indices != list(range(indices[-1] + 1)):
            raise ProviderStreamError("Stream ended with a sparse tool call index sequence.")
        message: dict[str, Any] = {
            "role": self.role,
            "content": "".join(self.content_parts) if self.saw_content else None,
        }
        if indices:
            message["tool_calls"] = [self.tool_calls[index].to_dict(index) for index in indices]
        return RawChatCompletion(message=message, finish_reason=self.finish_reason)


class _SseDecoder:
    def __init__(self) -> None:
        self._buffer = ""
        self._data_lines: list[str] = []

    def feed(self, text: str, *, final: bool = False) -> tuple[str, ...]:
        self._buffer += text
        events: list[str] = []
        while True:
            boundary = _line_boundary(self._buffer, final=final)
            if boundary is None:
                break
            line, consumed = boundary
            self._buffer = self._buffer[consumed:]
            event = self._consume_line(line)
            if event is not None:
                events.append(event)
        if final:
            if self._buffer:
                event = self._consume_line(self._buffer)
                self._buffer = ""
                if event is not None:
                    events.append(event)
            event = self._dispatch()
            if event is not None:
                events.append(event)
        return tuple(events)

    def _consume_line(self, line: str) -> str | None:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None
        field, separator, value = line.partition(":")
        if field != "data":
            return None
        if separator and value.startswith(" "):
            value = value[1:]
        self._data_lines.append(value)
        return None

    def _dispatch(self) -> str | None:
        if not self._data_lines:
            return None
        data = "\n".join(self._data_lines)
        self._data_lines.clear()
        return data


def iter_chat_completion_response(
    response: ReadableResponse,
    *,
    chunk_size: int = 4096,
) -> Generator[ProviderTextDelta, None, RawChatCompletion]:
    """Decode one JSON or SSE Chat Completions response without a retry request."""

    decoded_chunks = iter(_decoded_response_chunks(response, chunk_size=chunk_size))
    prefix = ""
    mode: str | None = None
    while mode is None:
        try:
            prefix += next(decoded_chunks)
        except StopIteration:
            mode = _detect_wire_mode(prefix, final=True)
            break
        mode = _detect_wire_mode(prefix, final=False)
    if mode == "json":
        body = prefix + "".join(decoded_chunks)
        return _parse_json_completion(body)
    if mode != "sse":
        raise ProviderStreamError("Provider response was neither JSON nor an SSE stream.")

    accumulator = _ChatCompletionAccumulator()
    decoder = _SseDecoder()
    saw_done = False
    for text, final in _with_final_chunk(prefix, decoded_chunks):
        for data in decoder.feed(text, final=final):
            if data.strip() == "[DONE]":
                saw_done = True
                continue
            if saw_done:
                raise ProviderStreamError("SSE data appeared after [DONE].")
            try:
                payload = json.loads(data)
            except json.JSONDecodeError as exc:
                raise ProviderStreamError("SSE data contained malformed JSON.") from exc
            for delta in accumulator.apply(payload):
                yield delta
    if not saw_done:
        raise ProviderStreamError("SSE stream ended without [DONE].")
    return accumulator.complete()


def _decoded_response_chunks(response: ReadableResponse, *, chunk_size: int) -> Iterable[str]:
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    read = getattr(response, "read1", response.read)
    try:
        while True:
            chunk = read(chunk_size)
            if not isinstance(chunk, bytes):
                raise ProviderStreamError("Provider response reader returned non-byte data.")
            if not chunk:
                tail = decoder.decode(b"", final=True)
                if tail:
                    yield tail
                return
            text = decoder.decode(chunk, final=False)
            if text:
                yield text
    except UnicodeDecodeError as exc:
        raise ProviderStreamError("Provider response ended with invalid UTF-8.") from exc


def _detect_wire_mode(text: str, *, final: bool) -> str | None:
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped:
        if final:
            raise ProviderStreamError("Provider response was empty.")
        return None
    if stripped.startswith(("{", "[")):
        return "json"
    sse_prefixes = ("data:", "event:", "id:", "retry:", ":")
    if stripped.startswith(sse_prefixes):
        return "sse"
    if not final and "\n" not in stripped and "\r" not in stripped:
        if any(prefix.startswith(stripped) for prefix in sse_prefixes):
            return None
    raise ProviderStreamError("Provider response was neither JSON nor an SSE stream.")


def _parse_json_completion(body: str) -> RawChatCompletion:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProviderStreamError("Provider returned malformed JSON.") from exc
    if not isinstance(payload, Mapping):
        raise ProviderStreamError("Provider JSON response must be an object.")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise ProviderStreamError("Provider JSON response contained no complete choice.")
    choice = choices[0]
    index = choice.get("index")
    if index is not None and (type(index) is not int or index != 0):
        raise ProviderStreamError("Provider JSON choice index must be the integer 0.")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise ProviderStreamError("Provider JSON response contained no complete message.")
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise ProviderStreamError("Provider JSON finish_reason must be a string or null.")
    return RawChatCompletion(message=dict(message), finish_reason=finish_reason)


def _with_final_chunk(prefix: str, chunks: Iterable[str]) -> Iterable[tuple[str, bool]]:
    yield prefix, False
    for chunk in chunks:
        yield chunk, False
    yield "", True


def _line_boundary(buffer: str, *, final: bool) -> tuple[str, int] | None:
    positions = [position for position in (buffer.find("\n"), buffer.find("\r")) if position >= 0]
    if not positions:
        return None
    index = min(positions)
    if buffer[index] == "\r" and index + 1 == len(buffer) and not final:
        return None
    consumed = index + 2 if buffer[index : index + 2] == "\r\n" else index + 1
    return buffer[:index], consumed


def _string_fragment(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ProviderStreamError(f"Streamed {label} must be a string.")
    return value


def _append_string_fragment(current: str, value: object, label: str) -> str:
    return current + _string_fragment(value, label)


def _merge_identity_fragment(current: str, value: object, label: str) -> str:
    part = _string_fragment(value, label)
    if not current or part == current:
        return part
    if part.startswith(current):
        return part
    return current + part


__all__ = [
    "ProviderStreamError",
    "ProviderTextDelta",
    "RawChatCompletion",
    "iter_chat_completion_response",
]

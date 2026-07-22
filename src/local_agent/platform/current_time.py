"""Typed system-clock facts for provider context and the current-time tool."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any


_CONTEXT_START = "[Current time]"
_CONTEXT_END = "[/Current time]"


@dataclass(frozen=True)
class CurrentTimeSnapshot:
    utc_iso: str
    local_iso: str
    local_date: str
    timezone_name: str
    utc_offset: str
    source: str = "system_clock"

    def as_dict(self) -> dict[str, str]:
        return {
            "schema": "current_time_v1",
            "source": self.source,
            "utc": self.utc_iso,
            "local": self.local_iso,
            "local_date": self.local_date,
            "timezone": self.timezone_name,
            "utc_offset": self.utc_offset,
        }


def current_time_snapshot(
    now: datetime | None = None,
    *,
    local_timezone: tzinfo | None = None,
) -> CurrentTimeSnapshot:
    """Read one wall-clock instant and project it into UTC and host-local time."""

    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("current time must be timezone-aware")
    utc = instant.astimezone(timezone.utc).replace(microsecond=0)
    local = utc.astimezone(local_timezone) if local_timezone is not None else utc.astimezone()
    timezone_name = _single_line(local.tzname() or "local", limit=64)
    return CurrentTimeSnapshot(
        utc_iso=utc.isoformat(timespec="seconds"),
        local_iso=local.isoformat(timespec="seconds"),
        local_date=local.date().isoformat(),
        timezone_name=timezone_name,
        utc_offset=_format_offset(local.utcoffset()),
    )


def render_current_time_context(snapshot: CurrentTimeSnapshot) -> str:
    return "\n".join(
        (
            _CONTEXT_START,
            "Runtime system-clock snapshot for this model request:",
            f"- UTC: {snapshot.utc_iso}",
            f"- Local: {snapshot.local_iso}",
            f"- Local date: {snapshot.local_date}",
            f"- Time zone: {snapshot.timezone_name} (UTC{snapshot.utc_offset})",
            "Resolve relative dates such as today or tomorrow against this snapshot. "
            "This runtime fact does not prove current repository or external-world state.",
            _CONTEXT_END,
        )
    )


def messages_with_current_time_context(
    messages: list[dict[str, Any]],
    snapshot: CurrentTimeSnapshot,
) -> list[dict[str, Any]]:
    """Return a provider-only projection containing exactly one current-time block."""

    projected = [dict(message) for message in messages]
    block = render_current_time_context(snapshot)
    system_index = next(
        (index for index, message in enumerate(projected) if message.get("role") == "system"),
        None,
    )
    if system_index is None:
        return [{"role": "system", "content": block}, *projected]
    system = dict(projected[system_index])
    content = _without_current_time_context(str(system.get("content") or "")).rstrip()
    system["content"] = f"{content}\n\n{block}" if content else block
    projected[system_index] = system
    return projected


def _without_current_time_context(content: str) -> str:
    start = content.find(_CONTEXT_START)
    while start >= 0:
        end = content.find(_CONTEXT_END, start + len(_CONTEXT_START))
        if end < 0:
            return content[:start]
        content = content[:start] + content[end + len(_CONTEXT_END):]
        start = content.find(_CONTEXT_START)
    return content


def _format_offset(offset: timedelta | None) -> str:
    total_seconds = int(offset.total_seconds()) if offset is not None else 0
    sign = "+" if total_seconds >= 0 else "-"
    total_minutes = abs(total_seconds) // 60
    hours, minutes = divmod(total_minutes, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def _single_line(value: str, *, limit: int) -> str:
    return value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")[:limit]

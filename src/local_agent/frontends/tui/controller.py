from __future__ import annotations

from dataclasses import replace
from enum import StrEnum
from pathlib import Path

from ...protocol.commands import new_command
from ...protocol.interactions import InteractionResult
from ..composer_history import ComposerHistory
from ..composer_history import composer_history_path
from ..terminal.command_registry import TerminalCommandRegistry
from .composer_layout import move_composer_cursor_vertical
from .history_search import ComposerHistorySearch
from .history_search import HistorySearchView
from .mailbox import TuiMailbox
from .input import MAX_INPUT_BYTES
from .messages import TuiCommandCompleted
from .messages import TuiEvent
from .messages import TuiInteractionClosed
from .messages import TuiInteractionPending
from .messages import TuiWorkerFailed
from .model import TuiProjector
from .view import follow_viewport
from .view import TuiView
from .view import page_viewport
from .view import synchronize_viewport
from .view import wheel_viewport
from .worker import TuiWorker


class TuiFocus(StrEnum):
    CHAT = "chat"
    ASK = "ask"
    APPROVAL = "approval"


class TuiController:
    """UI-thread input, focus, command routing, and state projection owner."""

    def __init__(
        self,
        mailbox: TuiMailbox,
        projector: TuiProjector,
        worker: TuiWorker,
        *,
        command_registry: TerminalCommandRegistry | None = None,
        composer_history: ComposerHistory | None = None,
    ) -> None:
        self._mailbox = mailbox
        self._projector = projector
        self._worker = worker
        self._registry = command_registry or TerminalCommandRegistry()
        self._history = composer_history or ComposerHistory(None)
        self._history_search = ComposerHistorySearch()
        self._view = TuiView()
        self._pending_interaction: TuiInteractionPending | None = None
        self._exit_requested = False
        self._in_flight = 0
        self._composer_before_search: tuple[str, int] | None = None
        self._composer_before_interaction: tuple[str, int] | None = None
        self._clipboard_text: str | None = None
        self._viewport_size: tuple[int, int] | None = None
        self._composer_preferred_column: int | None = None

    @property
    def state(self):
        return self._projector.state

    @property
    def view(self) -> TuiView:
        return self._view

    @property
    def exit_requested(self) -> bool:
        return self._exit_requested

    def take_clipboard_text(self) -> str | None:
        text = self._clipboard_text
        self._clipboard_text = None
        return text

    def poll(self, limit: int = 256) -> int:
        messages = self._mailbox.drain(limit)
        for message in messages:
            if isinstance(message, TuiEvent):
                self._projector.apply(message, dropped_messages=self._mailbox.dropped_count)
            elif isinstance(message, TuiInteractionPending):
                if self._view.focus == "history_search":
                    self._cancel_history_search()
                self._pending_interaction = message
                self._composer_before_interaction = (self._view.input_text, self._view.cursor)
                self._composer_preferred_column = None
                focus = TuiFocus.ASK if message.request.kind == "ask" else TuiFocus.APPROVAL
                self._view = replace(
                    self._view,
                    focus=focus.value,
                    interaction_prompt=message.request.prompt,
                    input_text="",
                    cursor=0,
                    palette=(),
                    notice="Esc or /cancel rejects this interaction",
                )
            elif isinstance(message, TuiInteractionClosed):
                if self._pending_interaction is not None and self._pending_interaction.request_id == message.request_id:
                    self._clear_interaction(notice=f"Interaction {message.status}.")
            elif isinstance(message, TuiCommandCompleted):
                self._in_flight = max(self._in_flight - 1, 0)
                self._handle_command_result(message)
            elif isinstance(message, TuiWorkerFailed):
                self._in_flight = max(self._in_flight - 1, 0)
                self._projector.append_local("error", f"Runtime worker failed: {message.error_kind}")
                self._projector.set_status("error")
        self._sync_viewport()
        return len(messages)

    def submit_initial_prompt(self, prompt: str) -> bool:
        text = prompt.strip()
        if not text or self._in_flight:
            return False
        submitted = self._submit_command(new_command("SubmitPrompt", {"prompt": text}))
        if submitted:
            self._history.append(text)
        return submitted

    def update_viewport(self, width: int, height: int) -> None:
        self._viewport_size = (width, height)
        self._sync_viewport()

    def handle_paste(self, text: str) -> None:
        self._insert(text.replace("\r\n", "\n").replace("\r", "\n"))

    def show_notice(self, message: str) -> None:
        self._view = replace(self._view, notice=message)

    def handle_key(self, key: str) -> None:
        if key == "CTRL_Q":
            if self._in_flight or self._pending_interaction is not None:
                self._view = replace(self._view, notice="A run or interaction is active; cancel it before quitting.")
            else:
                self._exit_requested = True
        elif key in {"ESC", "CTRL_C"} and self._view.focus == "history_search":
            self._cancel_history_search()
        elif key in {"ESC", "CTRL_C"} and self._pending_interaction is not None:
            self._resolve_interaction(InteractionResult("cancelled"))
        elif key == "CTRL_C" and self._in_flight:
            if self._worker.request_cancel():
                self._view = replace(self._view, notice="Cancellation requested; waiting for Runtime closure.")
            else:
                self._view = replace(self._view, notice="Run is starting; press Ctrl-C again to cancel.")
        elif key == "ESC" and self._view.palette:
            self._composer_preferred_column = None
            self._view = replace(self._view, palette=())
        elif key == "ESC" and (self._view.focus == "search" or self._view.search_query):
            self._close_search()
        elif key == "CTRL_R":
            self._toggle_history_search()
        elif key == "CTRL_P":
            self._toggle_palette()
        elif key == "CTRL_F":
            self._toggle_search()
        elif key == "CTRL_Y":
            self._copy_last_answer()
        elif key == "UP" and self._view.palette:
            self._move_palette(-1)
        elif key == "DOWN" and self._view.palette:
            self._move_palette(1)
        elif key == "PAGE_UP":
            self._view = replace(self._view, viewport=page_viewport(self._view.viewport, -1))
        elif key == "PAGE_DOWN":
            self._view = replace(self._view, viewport=page_viewport(self._view.viewport, 1))
        elif key == "WHEEL_UP":
            self._view = replace(self._view, viewport=wheel_viewport(self._view.viewport, -1))
        elif key == "WHEEL_DOWN":
            self._view = replace(self._view, viewport=wheel_viewport(self._view.viewport, 1))
        elif key == "UP" and self._view.focus == "history_search":
            self._move_history_search(-1)
        elif key == "DOWN" and self._view.focus == "history_search":
            self._move_history_search(1)
        elif key == "UP" and self._move_composer_vertical(-1):
            pass
        elif key == "DOWN" and self._move_composer_vertical(1):
            pass
        elif key == "UP" and self._recall_history(-1):
            pass
        elif key == "DOWN" and self._recall_history(1):
            pass
        elif key == "RESIZE":
            self._sync_viewport()
        elif key == "LEFT":
            self._composer_preferred_column = None
            self._view = replace(self._view, cursor=max(self._view.cursor - 1, 0))
        elif key == "RIGHT":
            self._composer_preferred_column = None
            self._view = replace(self._view, cursor=min(self._view.cursor + 1, len(self._view.input_text)))
        elif key == "HOME":
            self._composer_preferred_column = None
            self._view = replace(self._view, cursor=0)
        elif key == "END" and self._view.focus == TuiFocus.CHAT.value and not self._view.input_text:
            self._view = replace(self._view, viewport=follow_viewport(self._view.viewport))
        elif key == "END":
            self._composer_preferred_column = None
            self._view = replace(self._view, cursor=len(self._view.input_text))
        elif key == "BACKSPACE":
            self._backspace()
        elif key == "DELETE":
            self._delete()
        elif key == "ALT_ENTER":
            self._insert("\n")
        elif key == "ENTER":
            self._submit_input()
        elif len(key) == 1 and key.isprintable():
            self._insert(key)

    def _submit_input(self) -> None:
        if self._view.focus == "history_search":
            self._accept_history_search()
            return
        if self._view.focus == "search":
            query = self._view.input_text.strip()
            matches = sum(query.casefold() in entry.text.casefold() for entry in self.state.transcript) if query else 0
            draft, cursor = self._composer_before_search or ("", 0)
            self._composer_before_search = None
            self._composer_preferred_column = None
            self._view = replace(
                self._view,
                focus=TuiFocus.CHAT.value,
                input_text=draft,
                cursor=cursor,
                search_query=query,
                notice=f"{matches} transcript match{'es' if matches != 1 else ''}.",
            )
            return
        if self._view.palette:
            completions = self._registry.completions(self._view.input_text[:self._view.cursor])
            if completions and self._view.palette_index < len(completions):
                completion = completions[self._view.palette_index]
                start = max(self._view.cursor + completion.start_position, 0)
                selected = (
                    self._view.input_text[:start]
                    + completion.text
                    + self._view.input_text[self._view.cursor:]
                )
                if self._view.input_text != selected:
                    self._composer_preferred_column = None
                    self._view = replace(
                        self._view,
                        input_text=selected,
                        cursor=start + len(completion.text),
                        palette=(),
                    )
                    return
            else:
                selected = self._view.palette[self._view.palette_index].split("  ", 1)[0]
                if self._view.input_text != selected:
                    self._composer_preferred_column = None
                    self._view = replace(self._view, input_text=selected, cursor=len(selected), palette=())
                    return
            self._view = replace(self._view, palette=())
        text = self._view.input_text.strip()
        if not text:
            return
        if self._pending_interaction is not None:
            if text == "/cancel":
                self._resolve_interaction(InteractionResult("cancelled"))
            elif text.startswith("/"):
                self._view = replace(
                    self._view,
                    notice="This input answers the focused interaction; use /cancel first.",
                )
            else:
                self._resolve_interaction(InteractionResult("answered", text))
            return
        if self._in_flight:
            self._view = replace(self._view, notice="A run is active; the composer draft was kept.")
            return
        if text.startswith("/"):
            dispatched = self._registry.dispatch(text)
            for line in dispatched.output:
                self._projector.append_local("system", line)
            if dispatched.exit_requested:
                if self._in_flight:
                    self._view = replace(self._view, notice="A run is active; cancel it before quitting.")
                else:
                    self._exit_requested = True
            elif dispatched.command is not None:
                self._submit_command(dispatched.command)
        else:
            if self._submit_command(new_command("SubmitPrompt", {"prompt": text})):
                self._history.append(text)
        self._view = replace(
            self._view,
            input_text="",
            cursor=0,
            palette=(),
            notice="",
            viewport=follow_viewport(self._view.viewport),
        )
        self._composer_preferred_column = None

    def _submit_command(self, command) -> bool:
        if not self._worker.submit(command):
            self._projector.append_local("error", "Runtime command queue is full.")
            return False
        self._in_flight += 1
        return True

    def _handle_command_result(self, completed: TuiCommandCompleted) -> None:
        result = completed.result
        if completed.command.type == "MoveWorkspace":
            self._composer_preferred_column = None
            self._reset_history_search_for_rebind()
        if result.ok:
            if completed.command.type == "MoveWorkspace":
                self._rebind_history(result.payload.get("state_dir"))
            text = result.payload.get("text")
            if text is not None:
                self._projector.append_local("system", str(text))
            return
        error = result.error_message or result.error_code or "Command failed."
        self._projector.append_local("error", error)

    def _rebind_history(self, state_dir) -> None:
        if not isinstance(state_dir, str) or not state_dir:
            self._history.rebind(None)
            self._view = replace(
                self._view,
                notice="Workspace moved without a composer history partition; persistence is disabled.",
            )
            return
        if not self._history.rebind(composer_history_path(Path(state_dir))):
            self._view = replace(
                self._view,
                notice="Workspace moved, but composer history persistence is unavailable.",
            )

    def _resolve_interaction(self, result: InteractionResult) -> None:
        pending = self._pending_interaction
        if pending is not None:
            self._worker.interaction_bridge.resolve(pending.request_id, result)
        self._clear_interaction()

    def _clear_interaction(self, *, notice: str = "") -> None:
        draft, cursor = self._composer_before_interaction or ("", 0)
        self._composer_before_interaction = None
        self._pending_interaction = None
        self._composer_preferred_column = None
        self._view = replace(
            self._view,
            focus=TuiFocus.CHAT.value,
            interaction_prompt="",
            input_text=draft,
            cursor=cursor,
            palette=(),
            notice=notice,
        )

    def _toggle_palette(self) -> None:
        if self._pending_interaction is not None or self._view.focus in {"search", "history_search"}:
            return
        self._composer_preferred_column = None
        if self._view.palette:
            self._view = replace(self._view, palette=())
            return
        candidates = tuple(
            f"{command.name}  {command.description}" for command in self._registry.commands
        )
        self._view = replace(self._view, palette=candidates, palette_index=0)

    def _toggle_search(self) -> None:
        if self._pending_interaction is not None or self._view.focus == "history_search":
            return
        if self._view.focus == "search":
            self._close_search()
            return
        self._history.reset_navigation()
        self._composer_before_search = (self._view.input_text, self._view.cursor)
        self._composer_preferred_column = None
        self._view = replace(
            self._view,
            focus="search",
            input_text=self._view.search_query,
            cursor=len(self._view.search_query),
            palette=(),
            notice="Enter filters transcript; Esc restores the composer.",
        )

    def _close_search(self) -> None:
        draft, cursor = self._composer_before_search or (self._view.input_text, self._view.cursor)
        self._composer_before_search = None
        self._composer_preferred_column = None
        self._view = replace(
            self._view,
            focus=TuiFocus.CHAT.value,
            input_text=draft,
            cursor=cursor,
            search_query="",
            notice="",
        )

    def _toggle_history_search(self) -> None:
        if self._view.focus == "history_search":
            self._move_history_search(-1)
            return
        if (
            self._view.focus != TuiFocus.CHAT.value
            or self._pending_interaction is not None
            or self._in_flight
            or self._view.palette
            or self._view.search_query
        ):
            return
        self._history.reset_navigation()
        self._composer_preferred_column = None
        state = self._history_search.open(
            self._history.snapshot,
            self._view.input_text,
            self._view.cursor,
        )
        self._view = replace(
            self._view,
            focus="history_search",
            input_text=state.query,
            cursor=len(state.query),
            palette=(),
            notice="Enter accepts a match; Esc restores the composer draft.",
            history_search_match=state.match_preview,
            history_search_position=state.position,
            history_search_count=state.match_count,
            history_search_status=state.status,
        )

    def _move_history_search(self, direction: int) -> None:
        state = (
            self._history_search.move_older()
            if direction < 0
            else self._history_search.move_newer()
        )
        self._project_history_search(state)

    def _accept_history_search(self) -> None:
        match = self._history_search.accept()
        if match is None:
            self._view = replace(self._view, notice="No matching composer history entry.")
            return
        self._composer_preferred_column = None
        self._view = replace(
            self._view,
            focus=TuiFocus.CHAT.value,
            input_text=match,
            cursor=len(match),
            notice="History match accepted; press Enter to submit.",
            history_search_match="",
            history_search_position=0,
            history_search_count=0,
            history_search_status="inactive",
        )

    def _cancel_history_search(self) -> None:
        draft, cursor = self._history_search.cancel()
        self._composer_preferred_column = None
        self._view = replace(
            self._view,
            focus=TuiFocus.CHAT.value,
            input_text=draft,
            cursor=cursor,
            notice="",
            history_search_match="",
            history_search_position=0,
            history_search_count=0,
            history_search_status="inactive",
        )

    def _reset_history_search_for_rebind(self) -> None:
        if self._view.focus == "history_search":
            self._cancel_history_search()
        else:
            self._history_search.reset()
            self._view = replace(
                self._view,
                history_search_match="",
                history_search_position=0,
                history_search_count=0,
                history_search_status="inactive",
            )
        self._history.reset_navigation()
        self._composer_preferred_column = None

    def _copy_last_answer(self) -> None:
        answer = next(
            (
                entry.text
                for entry in reversed(self.state.transcript)
                if entry.role == "assistant" and not entry.provisional
            ),
            None,
        )
        if answer is None:
            self._view = replace(self._view, notice="No completed assistant answer to copy.")
            return
        self._clipboard_text = answer[:100_000]
        self._view = replace(self._view, notice="Copied the latest completed answer with OSC 52.")

    def _move_palette(self, delta: int) -> None:
        size = len(self._view.palette)
        self._view = replace(self._view, palette_index=(self._view.palette_index + delta) % size)

    def _insert(self, text: str) -> None:
        value = self._view.input_text
        cursor = self._view.cursor
        if len((value[:cursor] + text + value[cursor:]).encode("utf-8")) > MAX_INPUT_BYTES:
            self._view = replace(self._view, notice="Input is limited to 64 KiB.")
            return
        updated = value[:cursor] + text + value[cursor:]
        if self._view.focus == "history_search":
            self._update_history_search_query(updated, cursor + len(text))
            return
        self._history.reset_navigation()
        self._composer_preferred_column = None
        palette = self._view.palette
        if updated.startswith("/") and not self._pending_interaction and self._view.focus == TuiFocus.CHAT.value:
            completions = self._registry.completions(updated[:cursor + len(text)])
            palette = tuple(f"{item.text}  {item.description}" for item in completions[:8])
        elif palette:
            palette = ()
        self._view = replace(
            self._view,
            input_text=updated,
            cursor=cursor + len(text),
            palette=palette,
            palette_index=0,
        )

    def _sync_viewport(self) -> None:
        if self._viewport_size is None:
            return
        width, height = self._viewport_size
        viewport = synchronize_viewport(self.state, self._view, width, height)
        self._view = replace(self._view, viewport=viewport)

    def _backspace(self) -> None:
        cursor = self._view.cursor
        if cursor <= 0:
            return
        value = self._view.input_text
        if self._view.focus == "history_search":
            self._update_history_search_query(value[:cursor - 1] + value[cursor:], cursor - 1)
            return
        self._history.reset_navigation()
        self._composer_preferred_column = None
        self._view = replace(self._view, input_text=value[:cursor - 1] + value[cursor:], cursor=cursor - 1)

    def _delete(self) -> None:
        cursor = self._view.cursor
        value = self._view.input_text
        if cursor >= len(value):
            return
        if self._view.focus == "history_search":
            self._update_history_search_query(value[:cursor] + value[cursor + 1:], cursor)
            return
        self._history.reset_navigation()
        self._composer_preferred_column = None
        self._view = replace(self._view, input_text=value[:cursor] + value[cursor + 1:])

    def _move_composer_vertical(self, direction: int) -> bool:
        if (
            self._view.focus != TuiFocus.CHAT.value
            or self._pending_interaction is not None
            or self._view.palette
        ):
            return False
        width = self._viewport_size[0] if self._viewport_size is not None else 80
        movement = move_composer_cursor_vertical(
            self._view.input_text,
            self._view.cursor,
            width,
            direction,
            self._composer_preferred_column,
        )
        if not movement.moved:
            return False
        self._composer_preferred_column = movement.preferred_column
        self._view = replace(self._view, cursor=movement.cursor)
        return True

    def _recall_history(self, direction: int) -> bool:
        if (
            self._view.focus != TuiFocus.CHAT.value
            or self._pending_interaction is not None
            or self._in_flight
            or self._view.palette
        ):
            return False
        history_cursor = self._view.cursor
        if self._composer_preferred_column is not None:
            history_cursor = 0 if direction < 0 else len(self._view.input_text)
        recalled = self._history.navigate(direction, self._view.input_text, history_cursor)
        if recalled is None:
            return False
        self._composer_preferred_column = None
        self._view = replace(self._view, input_text=recalled, cursor=len(recalled), notice="")
        return True

    def _update_history_search_query(self, query: str, cursor: int) -> None:
        if not self._history_search.update_query(query):
            self._view = replace(self._view, notice="History search query is limited to 4 KiB.")
            return
        state = self._history_search.view
        self._project_history_search(state, cursor=cursor)

    def _project_history_search(self, state: HistorySearchView, *, cursor: int | None = None) -> None:
        self._view = replace(
            self._view,
            input_text=state.query,
            cursor=len(state.query) if cursor is None else min(max(cursor, 0), len(state.query)),
            history_search_match=state.match_preview,
            history_search_position=state.position,
            history_search_count=state.match_count,
            history_search_status=state.status,
            notice="",
        )

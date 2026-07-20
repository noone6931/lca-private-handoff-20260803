from __future__ import annotations

from pathlib import Path
import sys

from ...agent import AgentRuntime
from ...protocol.commands import CommandResult
from ...protocol.commands import new_command
from ...platform.terminal import silenced_terminal_input
from .command_registry import TerminalCommandRegistry
from .interactions import TerminalInteractionController
from .prompt import TerminalHistoryRebindError
from .prompt import TerminalPrompt
from .prompt import build_terminal_prompt
from .prompt import is_slash_command_input
from .prompt import slash_command_completions
from .renderer import TerminalEventSink


def run_terminal_chat(
    runtime: AgentRuntime,
    *,
    command_registry: TerminalCommandRegistry | None = None,
    interaction_controller: TerminalInteractionController | None = None,
    history_path: Path | None = None,
    input_stream=None,
    output_stream=None,
) -> int:
    output = output_stream or sys.stdout
    prompt = build_terminal_prompt(history_path)
    registry = command_registry or TerminalCommandRegistry()
    interactions = interaction_controller or TerminalInteractionController(
        input_stream=input_stream,
        output_stream=output,
    )
    set_interaction_handler = getattr(runtime, "set_interaction_handler", None)
    if callable(set_interaction_handler):
        set_interaction_handler(interactions)
    print("local-agent chat. Type /help for commands, /exit to quit.", file=output)
    try:
        while True:
            try:
                text = prompt(input_stream=input_stream).strip()
            except EOFError:
                print(file=output)
                return 0
            except KeyboardInterrupt:
                print("interrupted", file=output)
                continue
            if not text:
                continue
            if text.startswith("/"):
                dispatched = registry.dispatch(text)
                _print_lines(dispatched.output, output)
                if dispatched.exit_requested:
                    return 0
                if dispatched.command is not None:
                    result = runtime.commands.dispatch(dispatched.command)
                    _render_command_result(result, output)
                    _rebind_history_after_workspace_move(prompt, dispatched.command.type, result, output)
                continue
            try:
                with silenced_terminal_input():
                    runtime.commands.dispatch(new_command("SubmitPrompt", {"prompt": text}))
            except KeyboardInterrupt:
                print("interrupted", file=output)
    finally:
        if callable(set_interaction_handler):
            set_interaction_handler(None)


def create_terminal_event_sink(*, show_tools: bool = True, stream=None) -> TerminalEventSink:
    return TerminalEventSink(stream=stream, show_tools=show_tools)


def _render_command_result(result: CommandResult, output) -> None:
    if result.ok:
        text = result.payload.get("text")
        if text is not None:
            print(str(text), file=output)
        return
    print(f"error: {result.error_message or result.error_code or 'Command failed.'}", file=output)


def _print_lines(lines: tuple[str, ...], output) -> None:
    for line in lines:
        print(line, file=output)


def _rebind_history_after_workspace_move(
    prompt: TerminalPrompt,
    command_type: str,
    result: CommandResult,
    output,
) -> None:
    if command_type != "MoveWorkspace" or not result.ok:
        return
    state_dir = result.payload.get("state_dir")
    if not isinstance(state_dir, str) or not state_dir:
        prompt.rebind_history(None)
        print("warning: workspace moved without a terminal history partition; restart chat to rebind it.", file=output)
        return
    try:
        prompt.rebind_history(Path(state_dir) / "terminal_history")
    except TerminalHistoryRebindError as exc:
        print(f"warning: {exc}", file=output)

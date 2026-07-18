from __future__ import annotations

import argparse
import sys
from typing import TextIO

from .agent import AgentRuntime
from .config import ConfigError, load_config
from .frontends.terminal.app import run_terminal_chat
from .frontends.terminal.renderer import TerminalEventSink
from .frontends.terminal.command_registry import TerminalCommandDispatch
from .frontends.terminal.command_registry import TerminalCommandRegistry
from .frontends.tui import TuiEventSink
from .frontends.tui import TuiMailbox
from .frontends.tui import prepend_initial_prompt
from .frontends.tui import run_tui
from .frontends.tui import tui_is_supported
from .providers.llm import LlmError
from .protocol.commands import CommandResult
from .protocol.commands import new_command
from .session.jsonl_store import SessionError
from .frontends.terminal.io import silenced_terminal_input


_TERMINAL_COMMANDS = TerminalCommandRegistry()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-agent")
    parser.add_argument("prompt", nargs="*", help="Task prompt. If omitted, starts the interactive TUI.")
    parser.add_argument("--cwd", help="Workspace directory.")
    parser.add_argument(
        "--state-dir",
        help=(
            "Runtime state root for sessions, todos, and patch logs. "
            "Defaults to XDG_STATE_HOME/local-coding-agent or ~/.local/state/local-coding-agent; "
            "workspace-specific state is stored below this root."
        ),
    )
    parser.add_argument(
        "--allow-dir",
        dest="allowed_dirs",
        action="append",
        help=(
            "Additional directory the file/search/LSP/patch tools may access. "
            "Can be passed multiple times; shell/git/session still use --cwd."
        ),
    )
    parser.add_argument("--config", help="Optional JSON config file.")
    parser.add_argument(
        "--env-file",
        help=(
            "Optional dotenv file for runtime/provider settings. "
            "Loaded before --cwd/.env so credentials can live outside the target workspace."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=["openai-compatible", "bailian", "bailian-intl", "dashscope", "aliyun"],
        help="Provider preset. Use bailian for Alibaba Cloud Model Studio / DashScope.",
    )
    parser.add_argument("--api-base-url", help="OpenAI-compatible API base URL.")
    parser.add_argument("--api-key", help="API key.")
    parser.add_argument("--model", help="Model name.")
    parser.add_argument(
        "--reviewer-model",
        help="Optional model for the isolated reviewer role. Defaults to the main model.",
    )
    parser.add_argument(
        "--workflow-profile",
        choices=["auto", "coding", "enterprise-evidence", "readiness-audit"],
        help="Workflow hooks: typed auto selection, coding, enterprise evidence, or readiness audit.",
    )
    parser.add_argument(
        "--enable-subagents",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Expose one synchronous read-only delegate_explore subtask per parent turn (default: disabled).",
    )
    parser.add_argument(
        "--subagent-budget-seconds",
        type=int,
        help="Deadline cap for an enabled read-only explore subtask (5-300 seconds; default: 60).",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        help="Safety cap for model/tool iterations. 0 means unlimited; use budget seconds for normal limits.",
    )
    parser.add_argument("--budget-seconds", type=int, help="Wall-clock budget for one prompt run. 0 disables it.")
    parser.add_argument(
        "--approval-mode",
        choices=["ask", "auto-read", "always-ask", "write", "yolo"],
        help="Tool approval policy.",
    )
    parser.add_argument(
        "--auto-approve-tools",
        help="Comma-separated tool names to allow without prompting in ask mode.",
    )
    parser.add_argument(
        "--tool-approval",
        help="Comma-separated per-tool policies, e.g. shell=deny,run_tests=allow,apply_patch=prompt.",
    )
    parser.add_argument(
        "--context-char-budget",
        type=int,
        help="Approximate message character budget before local compaction. 0 disables compaction.",
    )
    parser.add_argument(
        "--context-token-budget",
        type=int,
        help=(
            "Approximate model context token budget before compaction. "
            "Uses a local estimate and keeps a reserve for the next turn. 0 disables token budgeting."
        ),
    )
    parser.add_argument(
        "--context-recent-messages",
        type=int,
        help="Recent messages to keep verbatim when local compaction is active.",
    )
    parser.add_argument(
        "--summary-mode",
        choices=["auto", "local", "llm"],
        help=(
            "Compaction summary mode. auto follows the OMP-style policy: no summary for small history, "
            "LLM summary when compaction triggers, local fallback on failure."
        ),
    )
    parser.add_argument(
        "--memory-consolidation",
        choices=["off", "auto", "llm"],
        help=(
            "Session memory consolidation. off disables hidden memory writes; auto/llm extract durable "
            "lessons at the end of a run and append them to the configured memory scope."
        ),
    )
    parser.add_argument(
        "--memory-scope",
        choices=["state", "project"],
        help=(
            "Where session memory consolidation writes. state uses the runtime state dir; "
            "project writes to .local-agent/memory."
        ),
    )
    parser.add_argument("--continue", dest="continue_session", action="store_true", help="Continue the latest session.")
    parser.add_argument("--session", help="Continue a specific session id from .local-agent/sessions.")
    frontend = parser.add_mutually_exclusive_group()
    frontend.add_argument("--chat", action="store_true", help="Start the terminal-native interactive frontend.")
    frontend.add_argument("--tui", action="store_true", help="Start the interactive terminal frontend.")
    parser.add_argument("--hide-tools", action="store_true", help="Hide tool call logs from stderr.")
    args = parser.parse_args(argv)

    try:
        config = load_config(
            config_path=args.config,
            env_file=args.env_file,
            cwd=args.cwd,
            state_dir=args.state_dir,
            provider=args.provider,
            api_base_url=args.api_base_url,
            api_key=args.api_key,
            model=args.model,
            max_steps=args.max_steps,
            budget_seconds=args.budget_seconds,
            approval_mode=args.approval_mode,
            auto_approve_tools=args.auto_approve_tools,
            tool_approval=args.tool_approval,
            context_char_budget=args.context_char_budget,
            context_token_budget=args.context_token_budget,
            context_recent_messages=args.context_recent_messages,
            summary_mode=args.summary_mode,
            memory_consolidation=args.memory_consolidation,
            memory_scope=args.memory_scope,
            allowed_dirs=args.allowed_dirs,
            reviewer_model=args.reviewer_model,
            workflow_profile=args.workflow_profile,
            enable_subagents=args.enable_subagents,
            subagent_budget_seconds=args.subagent_budget_seconds,
        )
        chat_requested = args.chat or _is_chat_prompt(args.prompt)
        tui_requested = bool(args.tui or (not args.prompt and not chat_requested))
        tui_active = tui_requested and tui_is_supported()
        tui_mailbox = TuiMailbox() if tui_active else None
        if tui_active:
            event_sink = TuiEventSink(tui_mailbox, show_tools=not args.hide_tools)
        elif chat_requested or not args.prompt or tui_requested:
            event_sink = TerminalEventSink(show_tools=not args.hide_tools)
        else:
            event_sink = None
        runtime = AgentRuntime(
            config,
            show_tool_logs=not args.hide_tools,
            session_id=args.session,
            continue_session=args.continue_session,
            event_sink=event_sink,
        )
        if tui_active:
            assert tui_mailbox is not None
            initial_prompt = " ".join(args.prompt) if args.tui and args.prompt else None
            return run_tui(runtime, tui_mailbox, initial_prompt=initial_prompt)
        if chat_requested or not args.prompt or tui_requested:
            if tui_requested:
                print("Interactive TUI unavailable; using terminal chat.", file=sys.stderr)
            initial_prompt = " ".join(args.prompt) if (args.chat or args.tui) and args.prompt else None
            input_stream = prepend_initial_prompt(sys.stdin, initial_prompt) if initial_prompt else None
            return run_terminal_chat(
                runtime,
                history_path=(config.state_dir or config.workspace / ".local-agent") / "terminal_history",
                input_stream=input_stream,
            )
        if args.prompt:
            with silenced_terminal_input():
                result = runtime.commands.dispatch(new_command("SubmitPrompt", {"prompt": " ".join(args.prompt)}))
            if not result.ok:
                print(f"error: {result.error_message or 'Command failed.'}", file=sys.stderr)
                return 2
            print(str(result.payload.get("content", "")))
            return 0
        return 0
    except (ConfigError, LlmError, SessionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


def _is_chat_prompt(prompt: list[str]) -> bool:
    return len(prompt) == 1 and prompt[0] == "chat"


def _handle_repl_command(
    runtime: AgentRuntime,
    command: str,
    stream: TextIO | None = None,
) -> TerminalCommandDispatch:
    output = stream or sys.stdout
    dispatched = _TERMINAL_COMMANDS.dispatch(command)
    for line in dispatched.output:
        print(line, file=output)
    if dispatched.command is not None:
        _print_command_result(runtime.commands.dispatch(dispatched.command), output)
    return dispatched


def _print_command_result(result: CommandResult, output: TextIO) -> None:
    if result.ok:
        text = result.payload.get("text")
        if text is not None:
            print(str(text), file=output)
        return
    print(f"error: {result.error_message or result.error_code or 'Command failed.'}", file=output)


if __name__ == "__main__":
    raise SystemExit(main())

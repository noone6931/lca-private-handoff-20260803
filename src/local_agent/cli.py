from __future__ import annotations

import argparse
import sys
from typing import TextIO

from .agent import AgentRuntime
from .config import ConfigError, load_config
from .frontends.terminal import TerminalEventSink
from .frontends.terminal import run_terminal_chat
from .llm import LlmError
from .session.jsonl_store import SessionError
from .terminal_io import silenced_terminal_input


REPL_HELP = """Commands:
/help                         Show this help.
/status                       Show session, workspace, provider, budget, and approval summary.
/tools                        List available tool names.
/approval                     Show approval settings.
/approval mode always-ask|write|yolo
/approval allow|prompt|deny TOOL
/approval reset TOOL
/exit or /quit                Exit terminal chat.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-agent")
    parser.add_argument("prompt", nargs="*", help="Task prompt. If omitted, starts a simple REPL.")
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
    parser.add_argument("--chat", action="store_true", help="Start the terminal-native interactive frontend.")
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
            context_recent_messages=args.context_recent_messages,
            summary_mode=args.summary_mode,
            memory_consolidation=args.memory_consolidation,
            memory_scope=args.memory_scope,
            allowed_dirs=args.allowed_dirs,
        )
        chat_requested = args.chat or _is_chat_prompt(args.prompt)
        event_sink = (
            TerminalEventSink(show_tools=not args.hide_tools)
            if chat_requested or not args.prompt
            else None
        )
        runtime = AgentRuntime(
            config,
            show_tool_logs=not args.hide_tools,
            session_id=args.session,
            continue_session=args.continue_session,
            event_sink=event_sink,
        )
        if chat_requested or not args.prompt:
            return run_terminal_chat(
                runtime,
                command_handler=_handle_repl_command,
                history_path=(config.state_dir or config.workspace / ".local-agent") / "terminal_history",
            )
        if args.prompt:
            with silenced_terminal_input():
                print(runtime.run(" ".join(args.prompt)))
            return 0
        return _repl(runtime)
    except (ConfigError, LlmError, SessionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


def _repl(runtime: AgentRuntime) -> int:
    print("local-agent REPL. Press Ctrl-D to exit.")
    while True:
        try:
            prompt = input("> ").strip()
        except EOFError:
            print()
            return 0
        if not prompt:
            continue
        if prompt.startswith("/"):
            _handle_repl_command(runtime, prompt)
            continue
        with silenced_terminal_input():
            print(runtime.run(prompt))


def _is_chat_prompt(prompt: list[str]) -> bool:
    return len(prompt) == 1 and prompt[0] == "chat"


def _handle_repl_command(runtime: AgentRuntime, command: str, stream: TextIO | None = None) -> None:
    output = stream or sys.stdout
    parts = command.split()
    if not parts:
        return
    if parts[0] in {"/help", "/?"}:
        print(REPL_HELP.rstrip(), file=output)
        return
    if parts[0] == "/status":
        print(runtime.status_summary(), file=output)
        return
    if parts[0] == "/tools":
        print(runtime.tool_summary(), file=output)
        return
    if parts[0] != "/approval":
        print(f"Unknown command: {parts[0]}", file=output)
        print("Type /help for commands.", file=output)
        return
    try:
        if len(parts) == 1:
            print(runtime.approval_summary(), file=output)
            return
        if len(parts) == 3 and parts[1] == "mode":
            runtime.set_session_approval_mode(parts[2])
            print(runtime.approval_summary(), file=output)
            return
        if len(parts) == 3 and parts[1] in {"allow", "prompt", "deny"}:
            runtime.set_session_tool_policy(parts[2], parts[1])
            print(runtime.approval_summary(), file=output)
            return
        if len(parts) == 3 and parts[1] == "reset":
            runtime.reset_session_tool_policy(parts[2])
            print(runtime.approval_summary(), file=output)
            return
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=output)
        return
    print(
        "Usage: /approval | /approval mode always-ask|write|yolo | "
        "/approval allow|prompt|deny TOOL | /approval reset TOOL",
        file=output,
    )


if __name__ == "__main__":
    raise SystemExit(main())

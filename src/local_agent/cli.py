from __future__ import annotations

import argparse
import sys

from .agent import AgentRuntime
from .config import ConfigError, load_config
from .llm import LlmError
from .session.jsonl_store import SessionError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-agent")
    parser.add_argument("prompt", nargs="*", help="Task prompt. If omitted, starts a simple REPL.")
    parser.add_argument("--cwd", help="Workspace directory.")
    parser.add_argument("--config", help="Optional JSON config file.")
    parser.add_argument(
        "--provider",
        choices=["openai-compatible", "bailian", "bailian-intl", "dashscope", "aliyun"],
        help="Provider preset. Use bailian for Alibaba Cloud Model Studio / DashScope.",
    )
    parser.add_argument("--api-base-url", help="OpenAI-compatible API base URL.")
    parser.add_argument("--api-key", help="API key.")
    parser.add_argument("--model", help="Model name.")
    parser.add_argument("--max-steps", type=int, help="Maximum model/tool iterations.")
    parser.add_argument("--approval-mode", choices=["ask", "auto-read", "yolo"], help="Tool approval policy.")
    parser.add_argument("--continue", dest="continue_session", action="store_true", help="Continue the latest session.")
    parser.add_argument("--session", help="Continue a specific session id from .local-agent/sessions.")
    parser.add_argument("--hide-tools", action="store_true", help="Hide tool call logs from stderr.")
    args = parser.parse_args(argv)

    try:
        config = load_config(
            config_path=args.config,
            cwd=args.cwd,
            provider=args.provider,
            api_base_url=args.api_base_url,
            api_key=args.api_key,
            model=args.model,
            max_steps=args.max_steps,
            approval_mode=args.approval_mode,
        )
        runtime = AgentRuntime(
            config,
            show_tool_logs=not args.hide_tools,
            session_id=args.session,
            continue_session=args.continue_session,
        )
        if args.prompt:
            print(runtime.run(" ".join(args.prompt)))
            return 0
        return _repl(runtime)
    except (ConfigError, LlmError, SessionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


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
        print(runtime.run(prompt))


if __name__ == "__main__":
    raise SystemExit(main())

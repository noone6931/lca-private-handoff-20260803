#!/usr/bin/env python3
"""Run isolated offline LCA benchmark fixtures, or opt into live-provider pressure tests."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_agent.benchmark import DEFAULT_TASKS_DIR
from local_agent.benchmark import run_benchmark_suite
from local_agent.config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else ROOT / "benchmark-results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    live_config = None
    if args.live:
        live_config = load_config(
            config_path=args.config,
            env_file=args.env_file,
            cwd=args.cwd or str(ROOT),
            state_dir=None,
            provider=args.provider,
            api_base_url=args.api_base_url,
            api_key=args.api_key,
            model=args.model,
            max_steps=0,
            budget_seconds=None,
            approval_mode="yolo",
            auto_approve_tools=None,
            tool_approval=None,
            context_char_budget=None,
            context_token_budget=None,
            context_recent_messages=None,
            summary_mode=None,
            memory_consolidation="off",
            memory_scope="state",
            allowed_dirs=None,
        )
    results = run_benchmark_suite(
        tasks_dir=Path(args.tasks_dir).expanduser().resolve() if args.tasks_dir else DEFAULT_TASKS_DIR,
        selected_ids=args.task,
        output_dir=output_dir,
        live_config=live_config,
        preserve_failed_sessions=args.preserve_failed_sessions,
    )
    passed = sum(1 for result in results if result.passed)
    print(f"Benchmark results: {passed}/{len(results)} passed ({'live' if args.live else 'deterministic'}).")
    print(f"JSON report: {output_dir / 'benchmark-report.json'}")
    print(f"Markdown report: {output_dir / 'benchmark-report.md'}")
    for result in results:
        state = "PASS" if result.passed else "FAIL"
        reason = result.run_summary.get("termination_reason") if result.run_summary else result.error
        print(f"- {state} {result.identifier}: {reason or 'unknown'}")
    return 0 if passed == len(results) else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks-dir", help="Directory containing benchmark task JSON files.")
    parser.add_argument("--task", action="append", default=[], help="Run only this task id. Repeatable.")
    parser.add_argument("--output-dir", help="Directory for JSON and Markdown reports.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use the configured external provider against temporary fixtures. Default is fully offline scripted provider.",
    )
    parser.add_argument("--config", help="Optional local agent JSON config for --live.")
    parser.add_argument("--env-file", help="Optional env file for --live credentials.")
    parser.add_argument("--cwd", help="Config lookup workspace for --live only; fixture execution remains isolated.")
    parser.add_argument("--provider", help="Provider for --live.")
    parser.add_argument("--api-base-url", help="API base URL for --live.")
    parser.add_argument("--api-key", help="API key for --live.")
    parser.add_argument("--model", help="Model for --live.")
    parser.add_argument(
        "--preserve-failed-sessions",
        action="store_true",
        help=(
            "Copy failed fixture-session JSONL files below the output directory for diagnosis. "
            "Off by default so reports retain only bounded, redacted error summaries."
        ),
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())

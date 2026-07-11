# Offline Benchmark and Evaluation Harness

## Purpose

The harness gives LCA a repeatable baseline for runtime changes. It is not a
prompt test: every deterministic task runs the real AgentRuntime, tool registry,
approval policy, ToolChoiceQueue, evidence ledger, steering, session handling,
and completion path against a temporary fixture workspace.

## Default Run

Run from the LCA checkout:

    PYTHONPATH=src python3 scripts/run_benchmarks.py

This path is offline. It uses a scripted in-process provider and creates a new
temporary workspace per task. It does not read or modify a real project.

Reports are written below benchmark-results unless an output directory is
provided. Each run produces JSON for comparison tooling and Markdown for review.

## Task Contract

Task JSON files live under benchmarks/tasks. A task can define:

- fixture files for primary and named additional roots;
- scripted provider responses for deterministic mode;
- tool approval policy and runtime budget;
- an optional local Git baseline;
- expected tool evidence, changed files, file contents, answer text, termination
  reason, and an independent acceptance command;
- residual risk that a passing fixture intentionally does not prove.

Current baseline tasks cover single-root discovery, multi-root inventory,
scoped negative evidence, anchored code edit plus test and diff, denied schema
visibility, and budget exhaustion.

## Live Pressure Run

External provider use is explicit and still operates only on the temporary
fixtures:

    PYTHONPATH=src python3 scripts/run_benchmarks.py --live --provider bailian --env-file .env

The live report is useful for tracking provider-specific tool selection and
repair behavior. It is not part of unit tests and may vary across models.

## Interpreting Reports

Every result includes success or failure, termination reason, elapsed time, LLM
requests, tool calls, tool errors, useless and repeated-call guards, compaction,
acceptance checks, changed files, test evidence, and residual risk. A benchmark
must not be made to pass by adding task-specific Runtime keyword branches.

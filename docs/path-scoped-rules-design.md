# Path-Scoped Rules MVP

## Goal

Give each authorized workspace root optional advisory rules without letting one
project's conventions leak into another project's prompt or permission boundary.

## Format

Rules live under a project root at .local-agent/rules/*.md. Each file uses a
small YAML-like header:

    ---
    paths:
      - src/**/*.java
      - web/**/*.vue
    priority: 10
    description: Java and Vue conventions.
    ---
    Rule body goes here.

Patterns are root-relative. Priority ranges from -100 to 100; higher priority
rules are rendered later when several rules match.

## Runtime Behavior

- At startup, and after workspace add, remove, reset, or move, Runtime indexes
  .local-agent/rules independently for every authorized root.
- Every provider request gets lightweight metadata: root, rule source, patterns,
  priority, and description. Rule bodies are omitted at this stage.
- A rule body is added only if the user request names a matching path or a
  successful tool result references a matching path. Relative request paths are
  interpreted in the primary workspace; absolute tool paths retain their root.
- The index is rebuilt outside compaction, so metadata and a matching body are
  restored on the next request even when older conversation history was folded.
- Invalid or unreadable rule files produce diagnostics and are skipped. Valid
  rules continue to work.

## Precedence and Safety

Rules are advisory guidance only. Current user instructions and freshly read
repository evidence override them. They do not grant tools, modify approval
policy, or widen workspace authorization. Shell and Git remain primary-root
operations even if an additional root contains matching rules.

## OMP Alignment

This follows OMP's project-context principle while remaining intentionally
smaller: discover scoped local guidance, attach only relevant context, and keep
the active tool policy as the real security boundary. It does not attempt to
copy OMP's full extension or TUI rule system.

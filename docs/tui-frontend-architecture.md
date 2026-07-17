# Full-screen TUI frontend architecture

Status: implementation contract for T-234 through T-239.

## Scope

The TUI is an explicit, replaceable frontend under `local_agent.frontends.tui`.
It consumes the existing `AgentCommand`, `AgentEvent`, and
`InteractionRequest`/`InteractionResult` boundaries. It does not own Runtime,
tool, approval-policy, evidence, workflow, session, or finalization semantics.

The existing terminal chat remains the default interactive frontend. `--tui`
selects the full-screen frontend. If stdin/stdout are not TTYs or curses is not
available, startup falls back to terminal chat before constructing a TUI event
sink.

## Reference facts

Codex keeps UI state in its app owner and routes input, protocol events, and
worker completions through one event loop. Its protocol projection and Runtime
remain outside widgets. Interrupt is an intent; the authoritative turn-completed
notification clears running state. Terminal restoration is guarded across normal
and exceptional exits. Relevant owners are:

- `codex-rs/tui/src/app.rs` and `app/event_dispatch.rs`
- `codex-rs/tui/src/chatwidget/protocol.rs`
- `codex-rs/tui/src/chatwidget/interaction.rs`
- `codex-rs/tui/src/tui.rs`

OMP separates terminal rendering/input in `packages/tui` from coding-agent
message, tool, interaction, and session semantics. Its event controller mutates
view models and invalidates visible state instead of rendering on every event.
Resize reflows from transcript state. Dynamic text is sanitized before styling.
Relevant owners are:

- `packages/tui/src/tui.ts`, `terminal.ts`, and `stdin-buffer.ts`
- `packages/coding-agent/src/modes/controllers/event-controller.ts`
- `packages/coding-agent/src/modes/components/transcript-container.ts`
- `packages/coding-agent/src/tools/approval.ts`

LCA intentionally does not copy Codex multi-agent routing, app-server RPC,
inline native scrollback replay, OMP extension widgets, smooth reveal, or
JavaScript reference-equality invalidation.

## Owners

- `messages.py`: immutable cross-thread message types.
- `mailbox.py`: bounded queue, delta coalescing, and explicit loss accounting.
- `model.py`: safe `AgentEvent` projection and immutable display state reducer.
- `worker.py`: one synchronous Runtime command worker and focused interaction bridge.
- `controller.py`: UI-thread input focus, command routing, and reducer dispatch.
- `text.py`: terminal text sanitization, cell width, wrapping, and clipping.
- `view.py`: pure responsive frame composition.
- `screen.py`: curses terminal session, key decoding, drawing, and restoration.
- `app.py`: startup/fallback/lifecycle composition.

No TUI module imports Runtime phase owners, ToolRegistry, ExecutionPolicy,
EvidenceLedger, Finalization, provider protocol, or session storage.

## Lifecycle

1. CLI selects a terminal sink or TUI mailbox before constructing Runtime.
2. TUI builds its reducer/controller before starting the producer worker.
3. Runtime worker serially dispatches typed commands.
4. Runtime-thread event callbacks project to bounded, display-safe messages and
   never mutate widgets.
5. The UI loop drains messages and is the only TUI-state writer.
6. A focused interaction is correlated by request ID and settles exactly once.
7. Ctrl-C first cancels the focused interaction, then requests cooperative turn
   cancellation. Running clears only after the terminal Runtime lifecycle event.
8. Shutdown rejects new input, resolves pending interaction, stops the worker,
   and restores curses state in `finally`/wrapper cleanup.

## Rendering and trust

The transcript model is the display fact source; wrapped rows are derived cache.
Resize and scroll never mutate transcript entries. Manual scroll disables
follow-tail until the user returns to the bottom.

User, model, tool, provider, and interaction text is sanitized before curses
styling. Raw tool arguments, provider markup, environment values, and secrets are
not projected into TUI messages. Tool timeline entries carry only bounded name,
status, error preview, and output length. Provisional assistant text is marked as
such and an identical authoritative final is not duplicated.

## Test matrix

- reducer traces: duplicate/out-of-order delta, exact/different final, terminal event closure;
- queue: contiguous coalescing, saturation, critical-event preservation, close;
- interaction: answer/cancel/timeout/late resolution and focus ownership;
- snapshots: 40/80/120 columns, Unicode, controls, long text, tools/todos, modal;
- worker: UI-thread isolation, command serialization, crash closure, cancellation;
- CLI: explicit selection and non-TTY fallback with legacy chat behavior unchanged;
- PTY: normal exit, Ctrl-C, resize, crash restoration, and no leaked raw mode;
- immutable/live: packaged gate plus one fresh real provider coding flow.

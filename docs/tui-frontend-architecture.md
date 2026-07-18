# Normal-screen TUI frontend architecture

Status: implemented through T-243.

## Scope

The TUI is a replaceable frontend under `local_agent.frontends.tui`. It
consumes the existing `AgentCommand`, `AgentEvent`, and
`InteractionRequest`/`InteractionResult` boundaries. It does not own Runtime,
tool, approval-policy, evidence, workflow, session, or finalization semantics.

`lca` starts this frontend on a POSIX TTY. `--chat` keeps the lighter terminal
chat frontend, and non-TTY or non-termios environments fall back before a TUI
event sink is constructed.

## Reference facts

Codex keeps its main transcript in the normal terminal buffer and inserts
settled rows into native scrollback. Mutable input and in-flight content remain
in an inline viewport; alternate screen is reserved for explicit overlays.
Relevant owners are `codex-rs/tui/src/tui.rs` and `insert_history.rs`.

OMP separates committed transcript rows from the mutable live tail. Its TUI
controller commits immutable rows once, re-renders only mutable content, and
borrows alternate screen only for configured full-screen overlays. Relevant
owners are `packages/tui/src/tui.ts` and `terminal.ts`.

LCA ports these lifecycle invariants into synchronous Python. It does not copy
the Rust/ratatui or TypeScript/Bun implementation, and it intentionally leaves
source-backed resize replay, extension widgets, smooth reveal, remote UI, and
concurrent turns out of this phase.

## Owners

- `messages.py`: immutable cross-thread message types.
- `mailbox.py`: bounded queue, delta coalescing, and loss accounting.
- `model.py`: safe `AgentEvent` projection and immutable display reducer.
- `worker.py`: one synchronous Runtime command worker and interaction bridge.
- `controller.py`: UI-thread focus, command routing, and input state.
- `text.py`: sanitization, cell width, wrapping, and clipping.
- `view.py`: pure normal-tail and search-overlay frame composition.
- `native_renderer.py`: stable-row commit, mutable-tail repaint, and overlay ownership.
- `screen.py`: POSIX input mode, byte decoding, signal handling, and restoration.
- `app.py`: startup, fallback, and lifecycle composition.

No TUI module imports Runtime phase owners, ToolRegistry, ExecutionPolicy,
EvidenceLedger, Finalization, provider protocol, or session storage.

## Lifecycle

1. CLI selects a TUI mailbox before constructing Runtime.
2. The controller/reducer starts before the single Runtime worker.
3. Runtime events cross the mailbox as bounded, display-safe typed messages.
4. The UI thread is the only reducer and terminal writer.
5. Settled transcript entries commit exactly once to the normal terminal buffer.
6. Provisional assistant text, activity, palette, interaction, and composer stay
   in one mutable live tail below committed history.
7. `Ctrl-F` search temporarily enters alternate screen; leaving search restores
   the normal buffer and repaints the live tail.
8. Ctrl-C is a cooperative intent. Turn closure still comes from the typed
   Runtime lifecycle.
9. Normal exit, exception, SIGTERM/SIGHUP, and suspend/resume restore termios,
   bracketed paste, cursor visibility, and any borrowed overlay.

## Terminal invariants

- Main rendering never emits alternate-screen enter, mouse-capture enable, or
  scrollback-clear (`ED3`). Wheel and trackpad input therefore belong to the
  terminal's native scrollback.
- Search is the only alternate-screen owner and pairs `1049` and `1007` modes.
- Live-tail cleanup uses cursor-relative movement plus `ED0`; committed rows are
  never repainted during composer, tool, or resize updates.
- Resize relies on terminal reflow for committed history and recomputes physical
  rows before clearing the mutable tail.
- Exact streaming final content is committed once. A different authoritative
  final remains a separate, explicitly labelled entry.

## Trust boundary

User, model, tool, provider, and interaction text is sanitized before rendering.
Raw tool arguments, provider markup, environment values, and secrets never
cross the TUI projection. Tool rows contain only bounded name, status, safe
error preview, and output length.

## Test matrix

- reducer: ordered/late delta, exact/different final, unique local IDs;
- renderer: commit-once, provisional tail, resize cleanup, no main-screen
  `1049h`/`1007h`/`ED3`, paired search overlay;
- input: fragmented CSI, UTF-8, bracketed paste, and bounded failure;
- worker/interaction: typed isolation, cancellation, timeout, and late reject;
- PTY: logo, `/workspace list`, native history, search overlay, resize, normal
  exit, Ctrl-C, SIGTERM, and termios restoration;
- immutable release: clean detached source identity plus installed `lca` smoke.

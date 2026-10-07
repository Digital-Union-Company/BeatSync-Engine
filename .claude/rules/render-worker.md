---
paths:
  - "src/beatsync_fork/render_worker.py"
  - "tests/test_render_worker.py"
  - "tests/test_render_cancellation.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

# The render lifecycle and cancellation contract (C3-R1A)

`src/beatsync_fork/render_worker.py` is the whole contract. It is **stdlib-only** (CLAUDE.md's hard
rule) and it decides *that* cancellation was requested — never *when* it is observed. Every safe
boundary is the caller's choice, which is why this module is testable on a bare interpreter while the
thing it coordinates is FFmpeg.

For *where* the boundaries sit and how the GUI wires Cancel, read
`.claude/rules/variant-lab.md` (C3-R1A section) and `.claude/rules/pipeline-core.md`. This file is
the module's own contract, not a second copy of the implementation.

## What the module owns

```
RenderCancelled        an ordinary Exception, raised at a safe boundary
RenderOutcomeKind      the five typed terminal causes
RenderLifecycleState   seven states + a monotonic transition table
RenderOutcome          one frozen typed truth (kind + display message)
RenderLifecycle        one cancellation Event, one state machine, one invocation id
```

**Owns nothing that runs.** No Gradio object, no `Popen`, no thread handle, no filesystem path, no
FFmpeg command, no Qwen model or process, no stage data, and **no media or cache identity**. It also
carries **no module-level registry** of lifecycles — a dict of them would be a second render
authority and would leak one object per render for the life of the process. A live instance holds
exactly `_invocation_id`, `_cancel_event`, `_state_lock`, `_state`, and a test measures that on a
real object rather than reading it off the source.

## `RenderCancelled` is an ordinary `Exception`, deliberately

Not `BaseException`. That choice has one large consequence and it is the point of it: a broad
`except Exception` **does** catch it, so **every** catch site on the render path must name
`except RenderCancelled` *before* its generic handler. A `BaseException` would have skipped those
handlers for free — and would also have skipped cleanup that legitimately has to run.

Grep `except RenderCancelled` to enumerate the covered sites;
`tests/test_render_cancellation.py` pins their **count per module** and their **handler order**, so a
new broad catch cannot arrive without someone revisiting that number. Handlers may re-raise bare,
store the exception to re-raise once quiescent, or translate it into a truthful typed status — and
exactly one may **swallow** it, the post-commit ProRes preview (see below).

## BOUNDARY_ONLY_CANCEL

```
FFmpeg-class subprocess   terminate -> bounded grace -> kill if needed -> REAP -> then raise
in-flight Stage 5 / Qwen  never hard-killed; effective at the next boundary after it returns
```

An FFmpeg-class child may be stopped mid-call, and `RenderCancelled` is raised **only once the reap
has returned** — never while a child might still be alive, because an orphan would keep writing into
the process-global processing directory the next render is about to clear.

A Stage-5/Qwen call is different and the asymmetry is deliberate: one call can hold GPU minutes, and
killing it mid-inference abandons a model process and discards work already paid for. A cancellable
Stage 5 would also need a cancellation token inside the persisted-semantics layer, where nothing
session-scoped or creative is allowed to exist at all. So the token is simply **not passed into**
`video_analysis.py` — a contract, not an omission, and `tests/test_render_cancellation.py` asserts
that `video_analysis.py`, `stage5_qwen_scene_worker.py` and `qwen_progress.py` never mention
`lifecycle`, `RenderCancelled` or `render_worker`.

## One lifecycle per top-level render event

```
one Create Music Video click        -> one RenderLifecycle
the WHOLE two-candidate C3-R0 batch -> one RenderLifecycle, spanning BOTH candidates
```

Never one per candidate, never one per internal `process_video()` call. A per-candidate lifecycle
would make Cancel a per-candidate control the UI never offered: stopping candidate 1 would leave
candidate 2 to start under a fresh, uncancelled token.

Only the two mutex-owning wrappers construct one. `tests/test_render_cancellation.py` pins that
`gui.py` contains exactly two constructions and that no module below them contains any.

## Terminal-marking belongs to the wrappers, exactly once

Marking happens **only** in `process_video_guarded` / `render_selected_variants_guarded`, after
everything they ran. **Never inside `process_video`'s `worker()`** — the batch calls that function
once per candidate against one shared lifecycle, and `_transition` silently no-ops once terminal, so
marking from there would freeze the batch's reported state on candidate 1's outcome and candidate 2's
real result would never reach the lifecycle.

## Monotonic state, and why terminal is a silent no-op

```
STARTING -> RUNNING -> [CANCEL_REQUESTED -> CANCELLING] -> FINISHED | FAILED | CANCELLED
```

`_ALLOWED_TRANSITIONS` is the whole table and an unlisted transition **raises** `ValueError`. A
transition requested *after* a terminal state **silently does nothing**. Those two look inconsistent
and are not:

- an illegal transition is a programming error, so it must be loud;
- a late transition is an unavoidable race — an abandoned stream's finalizer can run after the normal
  completion path already finished the lifecycle — so it must find nothing to do rather than explode
  inside a `finally`, where it would mask whatever was actually propagating.

What it may never do is **resurrect or rewrite** a finished lifecycle. There is no reset, restart or
reuse entry point, and a test asserts none exists.

`request_cancel()` is **total**: it comes from a different Gradio event than the render, so it is
idempotent, never blocks, never raises, and sets the flag from *any* state including terminal. Its
only state effect is one advisory `RUNNING -> CANCEL_REQUESTED` nudge; the render thread owns
`CANCELLING` and the terminal states. Reaching a terminal state **does not clear** the request — the
batch reads `cancel_requested()` *after* its candidates ran to derive its own terminal cause.

## `lifecycle=None` is today's behaviour, byte-for-byte

Every seam takes `lifecycle` **appended last with a default of `None`** — fourteen of them, pinned by
name and position. `None` means no boundary check anywhere fires and both media runners fall straight
through to the original blocking `subprocess.run(..., timeout=timeout)` as their *first* statement, so
nothing — no `Popen`, no poll loop, no clock — runs ahead of it. The headless CLI,
`video_processor.main` and every pre-R1A caller are unaffected, and no positional argument moved.

Measured rather than asserted: with no lifecycle, **no `Popen` is created at all**.

## The GUI's active-render slot is NOT owned by this module

This module deliberately provides no way to *find* a lifecycle. Reachability from a separate Cancel
click is `gui.py`'s capacity-one active-render slot, holding `(invocation_id, lifecycle)` or `None` —
never a `Popen`, a thread handle or a history of past invocations. Only a plain string `invocation_id`
ever enters `gr.State`, which deep-copies and may serialize what it holds.

Keeping the slot out of here is what stops this module from becoming a process-global render registry.

## Durable commit semantics

The durable `os.rename` promotion into `output/` is the **one** success commit point.
`session_state[RENDER_OUTCOME_KEY] = SUCCESS` is written exactly once, immediately after the durable
path is recorded, and **nothing afterwards may downgrade it**.

The ProRes preview is post-commit convenience work. A cancellation arriving while the preview child is
already running terminates and reaps that child, discards the partial preview (it is simply never
selected — `preview_path` falls back to the durable output), and leaves the outcome `SUCCESS`. This is
the **one** place a `RenderCancelled` may be swallowed, and swallowing is correct there: the render is
finished and the user owns the `.mov`, so propagating would turn a completed render into a cancelled
one. The exemption is located structurally (the `is_prores` block after the commit point), never by a
token a future author could sprinkle elsewhere to opt out.

## Abandonment is not cancellation

A dropped Gradio event, a `close()`, or an exception while draining must keep waiting for the render in
flight exactly as before R1A. `process_video`'s finalizer still joins its worker with **no timeout and
no kill**, and **no `finally` anywhere may call `request_cancel()`** — that would convert every
abandoned stream into a Stop the user never pressed. `request_cancel` has exactly one call site in
`gui.py`: the Cancel handler's slot lookup. Tests pin both.

Cancel only makes the worker *reach* a terminal state sooner. It never changes *whether* the finalizer
waits for it.

## What is explicitly deferred to C3-R1B

```
RENDER_SELECTION_SIZE = 2        unchanged
3+ candidates                    NOT IMPLEMENTED
continue-after-failure           NOT IMPLEMENTED
continue-after-cancellation      NOT IMPLEMENTED
```

`RenderOutcomeKind.SHARED_FATAL` is in the vocabulary with **no producer**, and that is deliberate:
telling a shared-input failure from a candidate-local one is exactly the evidence continuing would
need, and it does not exist yet. Do not add a producer for it to unlock R1B by the back door.

R1A produces three causes — `SUCCESS` after the durable promotion, `CANCELLED` from a caught
`RenderCancelled`, `CANDIDATE_LOCAL` for an Audio Layers failure — and falls back to `UNKNOWN_FATAL`
for anything it has not proven. A plain early return leaves the key `None`, which every consumer reads
conservatively.

No cache, schema or version constant participates in any of this: `CACHE_CONTRACT_VERSION`,
`ANALYSIS_VERSION` and `L2_CACHE_VERSION` are untouched, and a test asserts the fork's creative, cache
and media-identity modules never mention a lifecycle.

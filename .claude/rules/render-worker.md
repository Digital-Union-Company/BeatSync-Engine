---
paths:
  - "src/beatsync_fork/render_worker.py"
  - "tests/test_render_worker.py"
  - "tests/test_render_cancellation.py"
  - "tests/test_stage5_cancellation.py"
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
RenderCancelled        an ordinary Exception, raised at a safe boundary; carries an optional phase
PHASE_VIDEO_ANALYSIS   the ONE named cancellation phase (FUN Stage-5 Cancel)
RenderOutcomeKind      the five typed terminal causes
RenderLifecycleState   seven states + a monotonic transition table
RenderOutcome          one frozen typed truth (kind + display message)
RenderLifecycle        one cancellation Event, one state machine, one invocation id
```

### `RenderCancelled.phase` is a structured field, and there is exactly one value

`PHASE_VIDEO_ANALYSIS = "video_analysis"`, defaulting to `""`, set by the raiser and read **by
attribute** — never recovered by parsing `str(exc)`, which is display text. Every pre-existing raise
site is unchanged, and a reader that ignores the field behaves exactly as before.

**It names a phase, not an outcome.** `RenderOutcomeKind` is still the only typed terminal cause and
is still assigned explicitly; the phase decides *wording* only, and `gui._process_video_impl` writes
`RenderOutcomeKind.CANCELLED` regardless of which status string it picks. That separation is what
keeps `tests/test_render_failure_classification.py`'s "no status-text inspection" rule intact.

**One value, and it stays one.** A phase exists only where the operator-visible wording genuinely
differs. Stage 5 is the one place that qualifies — it is the only part of a render that can run for
an hour with nothing to show, so "cancelled" and "cancelled while analysing your videos" are
different facts to the person reading them. Do not grow this into a per-boundary vocabulary; that
would make the status panel a second description of the pipeline.

The FFmpeg scene pass raises from `ffmpeg_processing._run_media_command`, which is shared with clip
extraction and must not learn which stage called it. Its phase is therefore stamped by **one
backstop** in `auto_mode.analyze_beats_auto`, whose Stage-5 `try` contains exactly one call — so
anything cancelled inside it was cancelled during video analysis *by construction*. It uses
`cancel.phase = getattr(cancel, "phase", "") or PHASE_VIDEO_ANALYSIS`, so an already-stamped phase
is never overwritten.

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
exactly one may **swallow** it, the post-commit delivery preview (see below) — which covers all four
processing modes, not only ProRes.

## BOUNDARY_ONLY_CANCEL — superseded for Stage 5 (FUN Stage-5 Cancel)

```
FFmpeg-class subprocess   terminate -> bounded grace -> kill if needed -> REAP -> then raise
Stage 5 deterministic     cancellable DURING the pass, at frame/window/source boundaries
Stage 5 Qwen worker       cancellable DURING the wait: tree-terminate -> grace -> kill -> REAP
the Qwen worker PROCESS   still holds no token and knows nothing about cancellation
Stages 1-3                still uncancellable mid-stage; their boundaries already bracket them
```

An FFmpeg-class child may be stopped mid-call, and `RenderCancelled` is raised **only once the reap
has returned** — never while a child might still be alive, because an orphan would keep writing into
the process-global processing directory the next render is about to clear.

**C3-R1A kept the token out of Stage 5 entirely, and that is retired.** The original argument — one
call can hold GPU minutes, so killing it mid-inference abandons a model process and discards work
already paid for — turned out to argue for *stopping it properly*, not for not stopping it. A real
owner incident settled it: an entire video library was selected by accident against a cold cache,
Cancel was pressed, and Stage 5 kept working for about **an hour**, because the only boundaries were
before and after the whole `analyze_video_sources` call.

What the token can now reach, and what it still cannot:

| | before | now |
|---|---|---|
| `video_analysis.py` | no token | takes `lifecycle=None`, appended last |
| `beatsync_fork/qwen_progress.py` | no token | takes a zero-argument **predicate**, never a token |
| `auto_mode/stage5_qwen_scene_worker.py` | no token | **unchanged — still no token** |

**The worker process staying cancellation-free is the surviving half, and it is the half that
mattered.** It writes persisted semantic records, so nothing session-scoped or creative may enter it
— the media-neutral boundary in `.claude/rules/stage5-worker.md`. It also needs no token: the
**parent** owns the child, so stopping it is the parent's job. A cooperative worker would be a
second, redundant cancellation path inside the one process this repository least wants extra state
in. `tests/test_render_cancellation.py` still asserts the worker mentions none of
`lifecycle`/`RenderCancelled`/`render_worker`/`cancel_requested`.

**`qwen_progress.py` observes cancellation without knowing a lifecycle exists.** It is a fork module
and stdlib-only by CLAUDE.md's hard rule, so `stream_worker_process` takes
`cancel_requested: Callable[[], bool] | None` and answers in booleans. That ban is now checked over
the module's **identifiers**, not its characters: its docstring explains the boundary, and a
substring scan cannot tell an explanation from a violation.

**The token itself stays opaque in Stage 5.** `video_analysis.py` imports `RenderCancelled` and
`PHASE_VIDEO_ANALYSIS` and calls exactly `cancel_requested()`; it constructs no `RenderLifecycle`,
keeps no registry, and no creative, session or render state enters Stage-5 identity, a Qwen request,
the prompt or a record. The invariant the old omission protected is now enforced by what the token
*can do* rather than by its absence.

### The five classes of Stage-5 boundary

```
before the cache scan     an already-landed Stop costs nothing at all
the identity phase        per source, inside _compute_cache_paths_parallel
the record-lookup loop    per source
between analysis jobs     per source, serial and parallel alike
inside one source's work  scene detection (FFmpeg child), frame decode every 32 frames,
                          metric math every 16 windows, and the Qwen worker wait
```

Three properties hold on all of them, and they are what make the feature safe rather than merely
responsive:

- **Nothing already durable is lost.** Records checkpointed before the Stop stay on disk;
  cancellation deletes nothing, and the next run finds them as ordinary cache hits.
- **Nothing incomplete becomes durable.** `_cache_entry_is_complete` is still the one completion
  rule and is **unchanged** — a test asserts it, `_qwen_job_completed`, `_checkpoint_cache` and
  `_save_cache` never mention cancellation at all. Cancellation adds **no write path**: a test walks
  every `except RenderCancelled` in `video_analysis.py` and forbids `_checkpoint_cache`,
  `_save_cache`, `os.remove`, `os.unlink`, `shutil` and `open(` inside one.
- **A cancellation is never reported as an analysis failure.** Every broad handler on the path names
  `RenderCancelled` first. Two mattered most: the parallel block's serial retry, which would have
  *re-run* the cancelled source once per remaining source; and `analyze_beats_auto`'s Stage-5
  `except Exception`, which would have printed "video analysis failed" and continued into Stage 6 on
  **fallback sampling** — producing a video from random clips for a user who had just pressed Cancel.

Two shapes inside Stage 5 are worth naming because they look like over-engineering and are not:

- **A cancelled identity task returns `None`, and the phase then raises.** `None` is cheap for a
  queued task, but per source it is a *verdict* ("identity unprovable, no cache for this source").
  The `raise` after the pool closes the only path by which a cancellation could be read as one.
- **A cancelled window pass raises rather than returning empty metrics.** An empty metric means
  "this window yielded nothing usable", and a source whose windows all came back empty is a
  *legitimately* candidate-less source, which the completion rule accepts as reusable. Returning
  empties would persist "this video has no good moments" for a video nobody finished looking at.

**Stages 1-3 stay uncancellable mid-stage, deliberately.** ~15.7 s measured end to end,
process-locally cached, already bracketed by boundaries; making librosa cooperative would buy nothing
a user could perceive. A test pins that `stage1_audio.py`, `stage2_features.py` and
`stage3_sections.py` mention no cancellation at all.

### The worker-owned `llama-server` goes with the worker

The Qwen worker starts its own `llama-server`, and killing only the direct Python child **orphans
it** — the pre-existing timeout behaviour recorded in `.claude/rules/stage5-worker.md`. A cancelled
analysis that leaves a GPU server resident is not cancelled in any sense the operator cares about, so
the cancellation path terminates the **process tree**:

```
tree_terminator(child.pid)   FIRST, while the direct child is still its server's live ancestor
terminate -> grace -> kill -> REAP   the same escalation the timeout path uses
```

`qwen_progress.terminate_process_tree(pid, run=...)` runs `taskkill /F /T /PID <pid>` and nothing
else. **Containment is the pid argument, not a filter**: there is no image-name sweep and no
enumeration, so an unrelated `python.exe`, `llama-server.exe` or BeatSync process is unreachable from
here. `taskkill`'s exit 128 ("not found") is treated as success — after a terminate/kill race that is
the desired outcome. The function never raises; `run` is injectable so both branches are testable on
a bare interpreter.

**Timeout behaviour is untouched.** The terminator is wired only to the cancellation path, `timeout`
is still measured against the real clock and still reported as `timed_out`, and `outcome.cancelled`
is checked **before** the timeout and return-code branches so the two are never reported as each
other. A timeout still orphans exactly what it always did; that gap is pre-existing and this feature
does not widen it.

**A cancelled worker's response file is never opened.** A worker stopped mid-loop has written no
response, and reading whatever is at that path is how a partial result would become semantic
authority. The A1 per-job partials are *not deleted* either — they are simply not consumed, because
recovery is reachable only from a usable-but-empty response.

### `cancel_requested=None` is today's behaviour, byte-for-byte

A test extracts the `cancel_requested is None` branch and asserts its body is exactly
`returncode = process.wait(timeout=timeout)` — one blocking call, no loop, no clock, nothing ahead of
it. Likewise `_cancelled(None)` is `False` and `_check_cancelled(None, …)` does nothing, so the
headless CLI and every pre-FUN caller are unaffected. A predicate that *raises* is treated as "not
cancelled": a broken status check must not be able to kill a run that is making progress.

## One lifecycle per top-level render event

```
one Create Music Video click         -> one RenderLifecycle
the WHOLE 2-4-candidate C3 batch     -> one RenderLifecycle, spanning ALL selected candidates
```

Never one per candidate, never one per internal `process_video()` call. A per-candidate lifecycle
would make Cancel a per-candidate control the UI never offered: stopping candidate 1 would leave the
remaining candidates to start under a fresh, uncancelled token. It is also what closes the
continue-after-local-failure race — a `continue` cannot outrun a Stop when one shared token spans
the whole selection.

Only the two mutex-owning wrappers construct one. `tests/test_render_cancellation.py` pins that
`gui.py` contains exactly two constructions and that no module below them contains any.

## Terminal-marking belongs to the wrappers, exactly once

Marking happens **only** in `process_video_guarded` / `render_selected_variants_guarded`, after
everything they ran. **Never inside `process_video`'s `worker()`** — the batch calls that function
once per candidate against one shared lifecycle, and `_transition` silently no-ops once terminal, so
marking from there would freeze the batch's reported state on candidate 1's outcome and candidate 2's
real result would never reach the lifecycle.

### What the three terminal states mean for a batch (C3-R1B-b / R2)

A `RenderLifecycle` belongs to the **top-level render event**, so its terminal state answers *what
happened to the event* — not *did every candidate succeed*:

```
CANCELLED   an explicit cancellation request won
FINISHED    the batch exhausted its FULL selected candidate list under the authorized policy,
            however many attempted candidates failed locally along the way
FAILED      the event ended BEFORE exhausting its selection -- an early fatal stop, an unexpected
            exception, or an invariant ValueError
```

```python
if lifecycle.cancel_requested():        CANCELLED     # always wins
elif len(outcomes) == request.count:    FINISHED      # selection exhausted
else:                                   FAILED        # ended early
```

**The completion test is selection exhaustion, never the last candidate's outcome.** R1 derived it
from `session_state[RENDER_OUTCOME_KEY]`, which holds whatever the final candidate happened to
write. That was sufficient while every candidate failure stopped the batch, and became
order-dependent the moment `CANDIDATE_LOCAL` started continuing:

```
CANDIDATE_LOCAL, SUCCESS  ->  FINISHED
SUCCESS, CANDIDATE_LOCAL  ->  FAILED      <- same batch result, different state
```

Both attempted their whole selection, both report `1 / 2 succeeded; 1 failed`, and both carry batch
`outcome_kind is None`. Only the order differed. `tests/test_render_failure_classification.py`
pins that mirrored pair as a regression, asserting on the **actual** shared lifecycle the wrapper
installed rather than a reconstruction.

Two consequences worth stating, because they look surprising and are correct:

- **A `SHARED_FATAL` or `UNKNOWN_FATAL` on the FINAL selected candidate leaves the lifecycle
  FINISHED.** Nothing was left unattempted, so the event completed. The candidate still truthfully
  carries its own fatal cause and the summary still reports its failure — lifecycle state is not an
  aggregate candidate success counter, and nothing is erased or reclassified.
- **Candidate failures are represented by their candidate outcomes and the summary, not by the
  lifecycle.** Do not reach for lifecycle state to answer "did anything fail"; that is
  `RenderBatchOutcome.failed`.

The defensive `if not lifecycle.is_terminal(): mark_terminal(FAILED)` backstop is unchanged, for a
path that never reached the derivation at all (an abandoned generator, an exception before the
loop). **Abandonment is still not an explicit Cancel** — no finalizer calls `request_cancel()`.

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

The **delivery preview** is post-commit convenience work. A cancellation arriving while the preview
child is already running terminates and reaps that child, discards the partial preview (it is simply
never selected), and leaves the outcome `SUCCESS`. This is the **one** place a `RenderCancelled` may
be swallowed, and swallowing is correct there: the render is finished and the user owns the master,
so propagating would turn a completed render into a cancelled one. The exemption is located
structurally — the `try` that calls `build_delivery_preview_command`, after the commit point — never
by a token a future author could sprinkle elsewhere to opt out.

**Deliverable Export V1-A changed three things about that block and none of this contract.** The
preview is no longer ProRes-only (every mode's master carries `pcm_s24le`, muxed as `ipcm`, which an
ordinary browser cannot decode), so the structural anchor moved off `if is_prores:`; the handler now
**records** a contained failure instead of restoring `preview_path`, because there is nothing to
restore — the display path starts at the durable output and moves onto the preview only after FFmpeg
exited 0 and the file was validated as present and non-empty; and a `TimeoutExpired` **is** caught
now. That last one reverses an earlier deliberate choice, and the reason it reverses is this very
contract: after the commit point an uncaught timeout escapes into the generic handler and reports a
finished, promoted render as an error, which is exactly the downgrade forbidden above. The exempt
handler may touch neither `preview_path`, `RENDER_OUTCOME_KEY`, `LAST_OUTPUT_PATH_KEY` nor control
flow, and a test pins all four.

**V1-B-S1 added a second post-commit step and deliberately no second exemption.** The subordinate
share publication (`_publish_share_no_replace`, contract in `.claude/rules/gui-integration.md`)
runs after the preview and reports failure **by return value**, never by raising — so it needs no
handler of its own and **must not grow one**. Exactly one `except RenderCancelled` may live after
the commit point and it is still the preview's; a test asserts the share region contains no
`except` at all and that the function's `RenderCancelled` handler count is unchanged. The publisher
is wrapped in its own broad guard so that promise holds structurally rather than by inspection: an
earlier draft constructed its digest one line above its `try`, and a test that broke the digest
backend escaped the helper entirely — which, after the commit point, would have reported a
finished, promoted render as `❌ Error`.

## Abandonment is not cancellation

A dropped Gradio event, a `close()`, or an exception while draining must keep waiting for the render in
flight exactly as before R1A. `process_video`'s finalizer still joins its worker with **no timeout and
no kill**, and **no `finally` anywhere may call `request_cancel()`** — that would convert every
abandoned stream into a Stop the user never pressed. `request_cancel` has exactly one call site in
`gui.py`: the Cancel handler's slot lookup. Tests pin both.

Cancel only makes the worker *reach* a terminal state sooner. It never changes *whether* the finalizer
waits for it.

## Truthful producers (C3-R1B-a) and the one continuation they bought (C3-R1B-b)

```
RENDER_SELECTION_MIN = 2         C3-R1B-b
RENDER_SELECTION_MAX = 4         C3-R1B-b -- frozen product contract, not a tunable
continue-after-CANDIDATE_LOCAL   IMPLEMENTED (C3-R1B-b); it is the ONLY class that continues
continue-after-cancellation      NOT IMPLEMENTED, and not planned
```

**C3-R1B is complete as of C3-R1B-b.** One lifecycle still spans the whole batch, however many
candidates it holds -- that property needed no change, which is the clearest evidence the R1A
design generalised. `render_worker.py` itself was **not modified** by either R1B milestone.

**R1A left `SHARED_FATAL` with no producer. C3-R1B-a gave it real ones**, and that was the whole of
that milestone — it changed **no** continuation policy. **C3-R1B-b then spent the classification on
exactly one continuation**: `CANDIDATE_LOCAL` is recorded and the batch carries on; `SHARED_FATAL`,
`UNKNOWN_FATAL` and `CANCELLED` stop it.

```
SHARED_FATAL      the live source-gate refusal; the six primary audio/video selection failures;
                  the voice preflight; AudioMixPlanError; an errno.EXDEV durable promotion
CANDIDATE_LOCAL   the early output collision; a FileExistsError promotion; the SFX preflight;
                  SmartMixStructureError
UNKNOWN_FATAL     AudioMixExecutionError; the defensive music-duration fallback probe; any other
                  promotion OSError; Stage 1-3/4 failures; extraction and encode failures;
                  MemoryError; the generic except Exception
SUCCESS           exactly once, immediately after the durable promotion
CANCELLED         only from a caught RenderCancelled
```

Two rules decide every row, and both are mechanical rather than editorial:

- **`SHARED_FATAL` requires proof that nothing the candidate resolves is an input to the outcome.**
  Not "it would probably fail again" — the batch-frozen values have to be the only inputs.
- **`CANDIDATE_LOCAL` requires only that the failure does *not* prove every remaining candidate must
  fail.** Reachability counts: the SFX preflight's root and roles are frozen, but whether it runs at
  all depends on the candidate's own `sfx_amount`, so a broken library does not condemn the batch.

Everything else fails closed as `UNKNOWN_FATAL`. **Classification is by exception type and by
batch-frozen values, never by parsing a message** — `tests/test_render_failure_classification.py`
asserts that every assignment to `RENDER_OUTCOME_KEY` names an enum member explicitly, and that
`gui.py` contains no status-text inspection.

The vocabulary itself is unchanged: `render_worker.py` was **not modified** by R1B-a. Five members
were always enough; what was missing were producers.

No cache, schema or version constant participates in any of this: `CACHE_CONTRACT_VERSION`,
`ANALYSIS_VERSION` and `L2_CACHE_VERSION` are untouched, and a test asserts the fork's creative, cache
and media-identity modules never mention a lifecycle.

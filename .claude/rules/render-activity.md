---
paths:
  - "src/beatsync_fork/render_activity.py"
  - "tests/test_render_activity.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`. The progress contract this must not duplicate is
> `.claude/rules/progress-events.md`; the lifecycle it reads is `.claude/rules/render-worker.md`;
> the two sibling activity surfaces it reuses are `.claude/rules/prep-activity.md` and the Raw
> Cutter R12 bullet in `.claude/rules/gui-integration.md`; the GUI seam is
> `.claude/rules/gui-integration.md`.

# UX-RENDER-ACTIVITY V1 — a long render looks alive, and the panel never lies about progress

The owner's complaint, 2026-11-28: during a long render — above all Stage 5 against thousands of
sources — the Render tab's only sign of life is a four-line `Status` textbox, and **a textbox that
has not changed for twenty minutes is indistinguishable from a frozen application.**

This is the third time this repository has answered that observation, and deliberately the third
time it has answered it the *same* way. Scan and Analyze got it in UX-PREP-VISUAL-PROGRESS; the Raw
Cutter got it in R12. So the two questions are separated here exactly as they were there, because
conflating them is how a panel ends up lying:

```
"is this running?"     -> a state chip, a CSS keyframe, and a BROWSER-anchored clock.
                          All three are true for as long as a worker is alive and cost NO
                          measurement at all.
"how far has it got?"  -> ONLY ProgressView.active_measurement(). Absent otherwise.
```

## What changed, and nothing else did

```
beatsync_fork/render_activity.py   NEW   pure policy: the status vocabulary, the stage pills, the
                                         determinate/indeterminate track, and every honesty rule
progress_view.completed_stages()   NEW   a second read-only projection, beside active_measurement()
gui.py  _RenderActivityReporter    NEW   the two clocks, the batch position, the measurement age
gui.py  _render_interlock_lock     NEW   the DISABLE half of the control interlock
gui.py  _render_interlock_restore  NEW   the RESTORE half, as a chained .then()
gui.py  _render_is_active          NEW   ONE diagnostic read of the existing active-render slot
gui.py  render_activity            NEW   one gr.HTML, directly above the Status textbox
gui.py  RENDER_ACTIVITY_CSS        NEW   where the MOTION lives, as one @keyframes
gui.py  CUT_ACTIVITY_JS            ->    one selector token, and a third uniform field
```

`video_analysis.py`, `video_processor.py`, `auto_mode/*`, `ffmpeg_processing.py`, every creative
module and every cache module are **untouched**. No stage, phase, event, cache key, identity,
creative value or frame-lock behaviour changed.

## The browser does the ticking, and that is why no poll was added

**`process_video`'s `status_queue.get()` is still an unbounded wait.** That looks like the obvious
thing to fix — it is exactly the defect UX-PREP-ACTIVITY fixed in `_run_prep_in_worker` — and it is
deliberately *not* fixed here, because it does not need to be:

* the elapsed clocks are advanced **by the browser**. The panel carries `data-job-ms` plus a
  `data-job-token`, and `CUT_ACTIVITY_JS` anchors its own one-second interval on that token, so the
  figures keep moving with **zero** server frames;
* the animation is a CSS `@keyframes`, so it needs no frames either.

So a forty-minute silent Stage 5 stretch stays visibly alive while this generator yields nothing,
and the pinned cadence of the one stream the progress-seam suite freezes did not have to move.
**Do not add a poll, a timer, a coordinator, a background worker or an endpoint here.** A test pins
that `status_queue.get(timeout` does not appear.

### The timer is anchored, not incremented, and the contract is the Raw Cutter's

`CUT_ACTIVITY_JS` gained **one selector token** and **one field triple**, and nothing else:

```
.querySelectorAll(".bs-cut-activity, .bs-render-activity")
["data-update-token", "data-update-ms", ".bs-cut-update-elapsed"]
```

Three consequences, each of which was a decision:

* **The panel does NOT carry `bs-cut-activity`.** Putting both classes on it would have made it tick
  with no change to the installer at all — tempting, and wrong: a shared timer that works because
  one surface pretends to be another is a thing nobody can safely rename later. The sharing is
  visible at the one place that does the sharing.
* **The `data-*` field NAMES are the cutter's, verbatim.** Those are the shared timer's parameter
  names rather than any one surface's property, and a second set of them would be a second timer
  contract to keep in step. Every child also carries a render-named class for styling, so no
  stylesheet depends on the `bs-cut-` spelling.
* **The third field needed no new logic.** Its token changes when a new measurement arrives, so the
  existing anchor map re-creates the anchor and the age restarts from the server's reading instead
  of advancing a stale one. Panels without that child (the cutter's) are skipped by the installer's
  existing `!out || !token` guard, so **cutter behaviour is byte-for-byte unchanged**.

`app.load(fn=None, …)` is still **four** registrations, and `tests/test_open_output_folder.py` holds
that identity allowlist.

### `gr.HTML`, and that is load-bearing

Measured against the served `gradio==6.19.0`, a `gr.Markdown` strips every `data-*`, `role` and
`aria-*` attribute and keeps only `class` and `style` — which is why `prep_status`' ARIA semantics
never reach the DOM. Through a Markdown component this panel would have **no tokens in the DOM and
therefore no clock at all between server frames**, which is the whole complaint. So it is a
`gr.HTML`, like the Raw Cutter's progress panel and for the same reason.

## The honesty rules

Every one of these is inherited from a sibling surface where it was a real defect first.

* **A determinate bar requires two measured integers.** `bool` is rejected first because it
  subclasses `int`; `total <= 0`, `done < 0` and `done > total` all fail closed.
* **An indeterminate track carries no fill element at all** (UX-PREP-VISUAL-PROGRESS F2). A
  `width:100%` fill paints exactly what a complete `N / N` paints, so an uncounted phase looked
  *finished*; a `width:0` fill is a quantity nobody measured. Emitting none is strictly better than
  dimming one, because then no element's width can be read as a figure. `aria-valuenow` is omitted
  too, which is ARIA's own rule — a screen reader announces a zero as a measurement.
* **The sweep is a SIBLING of the fill, spanning the whole track.** On the fill, a determinate
  `0 / 3620` renders a zero-width element and the animation meant to say "alive" has nowhere to
  travel — dead at exactly the count being complained about.
* **A measured zero is a true count and is never suppressed, smoothed or started elsewhere.**
* **A terminal panel carries no track at all** and `data-running="0"`, which is what freezes the
  browser clock. A full bar on completion would be a measurement nobody took; a bar on a failure
  would claim the work got somewhere.
* **Nothing is inherited across a phase transition** — count, total, unit, percentage or clock. The
  module holds no state between calls, so there is no mechanism by which it could be.
* **`measured_percent` is the only route to any percentage**, and a test pins that `* 100.0` appears
  exactly once in the module. There is deliberately **no** overall-render percentage and **no**
  weighted stage completion: the stages have wildly different, data-dependent costs, so any single
  number spanning them would be a prediction dressed as a measurement.
* **The completed-stage figure is a labelled COUNT, never a bar** — the licence requirement 2 grants
  explicitly. Its denominator is `len(PIPELINE_STAGES)`, **derived from `progress.STAGE_TITLES`**
  rather than typed, and `completed` means the stage reported END. It is never inferred from the
  active stage number: "stage 5 is active, therefore 1–4 are done" is an assumption, and it is how a
  skipped stage would be reported as completed.
* **No ETA, no rate, no throughput, ever**, restated here because this is the surface where an
  operator would most want one and where the data to compute one honestly does not exist.
* **Elapsed time is not evidence of progress**, so the age of the last measurement is reported
  **separately** — and with no verdict attached. A long gap during Stage 5 is normal; a single
  source can take minutes. The panel says how long it has been and deliberately does not say that
  anything is wrong.
* **An unknown status fails closed to idle** — never to running (an animation over a dead worker)
  and never to completed, which is the one answer a caller must not receive by accident.
* **`running` is DERIVED from the status**, so a caller that forgets a flag cannot emit a frozen
  panel with a live clock.
* **A single render has no candidate context, and `None` is not `1 / 1`.** A batch position is an
  ordinal, so it uses `_measured_position` rather than `_measured_counts`: `Candidate 0 / 4` is not
  a modest reading of a real quantity, it is a position that does not exist. (Found by this
  module's own totality test before it could reach a panel.)
* **A broken panel can never break a render.** Every entry point is guarded, the same rule
  `progress.emit()` rests on: a broken status widget must not be able to lose hours of analysis.

## Where the panel comes from, and why it goes through `session_state`

`ProgressView` lives inside `process_video`, so that generator is the one place holding both the
folded measurement and the view it came from. But its 3-value yield contract (`video, status,
state`) is pinned and must not widen — the worker thread still carries nothing but `session_state`.
So:

```
process_video            renders the panel, parks it on session_state[RENDER_ACTIVITY_KEY]
the gate core            projects it onto the widget, as element 7 of its existing projection
process_video_guarded    carries it out through `yield from` -- no loop of its own needed
the C3 batch             unpacks it and passes it through untouched
```

That is **exactly** the seam the Audio Layers and Smart Mix read-outs already use, and it is why the
single-render wrapper needed no reshaping at all: the 3→6 projection was already in the core, so it
became a 3→7 projection there and nowhere else.

`gr.skip()` — never `''` — is the answer when no panel has been published for a frame. An empty
string would **blank** the `gr.HTML`, and "nothing rendered one this frame" is not a reason to erase
what is legitimately on screen.

**A refusal carries no activity frame.** Five refusal yields (the core's source-gate and playlist
refusals, the batch's build-request refusal, and both wrappers' mutex-busy refusals) emit
`gr.skip()` for the panel, because a refused render performed no work and a timer there would be
counting a job that never began. That is the rule the Raw Cutter's `refuse(...)` and Prepare &
Verify's declined click already follow. The wrapper's **terminal** yield then freezes the panel, and
it runs on the refusal paths too, so the pre-gate `Starting` frame is never left standing.

## One reporter per top-level render event

```
one Create Music Video click      -> one _RenderActivityReporter
the WHOLE 2-4-candidate batch     -> one _RenderActivityReporter
```

Never one per `process_video()` call. This mirrors the `RenderLifecycle`, and that is the
requirement rather than a symmetry: *"Total elapsed time spans the complete batch; current phase
timing belongs to the active candidate."* A reporter constructed inside `process_video` would
restart the total clock on candidate 2.

The timers are **`raw_cutter_activity.ActivityClock`, reused rather than reimplemented** — it
already owns a job clock from a caller-supplied start, a phase clock that resets only on a *real*
phase change, and tokens derived from the start reading so the browser can anchor on them. Only its
four measured numbers are read off the snapshot; its cutter-specific fields are ignored. A third
copy of a two-timer clock would be a third place for the freeze and reset rules to drift.

Two rules about the phase clock, both tested on the real reporter with an injected clock:

* **the phase signature is `(candidate, stage, phase)`** — so candidate 2's Stage 5 is a different
  stretch of work from candidate 1's, and the phase clock answers "how long has THIS candidate been
  in this stage";
* **it excludes the counts** — an advancing Stage 5 counter is progress *within* one phase, not a
  new phase, and resetting on it would make the phase clock useless.

## `Cancelling`, and the one latency it costs

`Cancelling` reaches the panel from `lifecycle.cancel_requested()`, read **by attribute**, on the
render stream's next frame. Cancel's own `outputs` therefore stay exactly `[status_output]` — it
gains no second widget and no new writer, and C3-R1A's isolation is untouched.

**The consequence is recorded rather than hidden:** between a Stop landing and the next progress
frame the chip still reads `Running`, while the status line already says the cancellation was
requested. Stage 5 and extraction both emit a frame per item, so the gap is short; a genuinely
silent stretch (the Qwen model load, ~8 s) can hold it for a few seconds. Widening Cancel's
`outputs` to fix that was considered and rejected: the Cancel handler knows only an invocation id,
so the panel it could build would carry no start time and would **reset the elapsed clock** — which
is the one thing requirement 3 forbids.

## The control interlocks

**Which controls these are was decided by measurement, not by sweeping the UI.** The question "does
this genuinely conflict with an active render" already has an authoritative answer in `gui.py`: the
set of non-blocking `_RENDER_LOCK.acquire()` callers. There are exactly six.

```
DISABLED      process_btn, render_selected_variants_btn, prep_verify_btn, sample_btn
NOT disabled  cut_run_btn, cut_batch_start_btn
NOT a conflict  the Audio Cutter, source confirmation, Scan, Analyze, Cancel,
                Open Output Folder, tab navigation, every read-only panel
```

* **The two Raw Cutter buttons are deliberately left alone.** Both already have their own
  server-managed interactive state — `cut_run_btn` is enabled by a successful Analyze and appears in
  three separate `outputs` lists — so a render event writing them would be a **second authority over
  one widget**. Restoring them would mean either guessing `interactive=True`, which offers Run with
  no plan and makes the UI lie, or re-deriving the cutter's plan state inside a render handler,
  which couples two subsystems that currently share nothing but a mutex. What they already have is
  the right answer: an instant, accurate refusal naming the holder, costing nothing and losing no
  work. The brief's own instruction is to restore from real readiness *"rather than blindly enabling
  every button"*, and for these two there is no readiness a render handler can honestly compute.
* **The Audio Cutter holds no `_RENDER_LOCK` at all** — measured, not assumed. Disabling it would
  invent a conflict the admission architecture does not recognise.
* **Source confirmation is not a conflict.** The gate re-verifies the live controls once and renders
  from the paths it froze there, so a confirmation changed mid-render cannot reach the render in
  flight.

### The shape, and why the restore is a `.then()`

```
btn.click(fn=_render_interlock_lock, …)        declared FIRST -- the DISABLE half
btn.click(fn=<the render>, …)                  UNCHANGED: same fn, inputs, outputs, arity
   .then(fn=_notify_drain, …)                  UNCHANGED
   .then(fn=_render_interlock_restore, …)      the RESTORE half
```

The lock half is a **separate registration** precisely so the render event's own `fn`, `inputs`,
`outputs` and emitted arity stay byte-identical and every positional and arity contract guarding
them is untouched. Measured on the pinned version: it runs ~60 ms before the render handler is
entered. That ordering is measured rather than contractual and **nothing rests on it** — a late
landing would briefly show `Starting` again and self-correct on the next frame.

**The restore is chained, and that is the whole reason it is not a terminal yield.** Measured on
`gradio==6.19.0`: a `.then()` link runs *after* the generator's `finally` — 30 ms after the mutex
release in the probe. So `process_video`'s finalizer has joined the render worker and the wrapper
has released `_RENDER_LOCK` before anything comes back, which makes *"keep conflicting operations
disabled until the worker and owned subprocesses have actually stopped"* **structural**. A terminal
yield could not promise it: it runs inside the `try`, before the join.

Two further rules:

* **Create Music Video is restored from the SOURCE GATE, never to `True`** — its enabled state
  belongs to the confirmation state machine, and blindly enabling it would offer a render over an
  unconfirmed or newly invalidated source set. An unusable `source_state` fails **closed** on that
  one control and leaves the three diagnostics alone.
* **A newer render keeps everything locked.** If the slot still holds a render when the restore runs
  — an older invocation's delayed `.then()` arriving while a newer job is live — every widget gets
  `gr.update()`, i.e. no change at all.

### `_source_ui_updates` had to learn about renders, and that was a real hole

Scan and Confirm take **no** mutex, so they run happily while a render does — and
`_source_ui_updates` is the *other* authority over Create Music Video's enabled state. An operator
who re-scanned and re-confirmed mid-render therefore had `is_confirmed() == True` projected straight
onto Create, **re-enabling it underneath a running job from a completely unrelated handler**. Its
one expression is now `state.is_confirmed() and not _render_is_active()`.

The authorization boundary has not moved and is not this. What was wrong was the *claim*: a lit
Create button during a render says "you may start one".

### Abandonment, and why it needs nothing

A `.then()` link does **not** run for an abandoned event (measured: the generator's `finally` ran,
the `.then()` did not). That costs nothing, because the only way to abandon a render from the UI is
to leave or reload the page, and a `gr.update(interactive=False)` is a transient update rather than a
config change — the replacing page rebuilds the controls from the Blocks config, enabled. Measured
immediately after a mid-stream reload: every disabled control read back as enabled. **Nothing is
yielded from a `finally`**; yielding during a close raises `RuntimeError` and would replace a clean
join with a second failure.

## Nothing here is an authorization boundary

`_RENDER_LOCK.acquire()` remains the sole admission authority, `RenderLifecycle` the sole
cancellation authority, and `_RENDER_LOCK.locked()` is still called nowhere. `_render_is_active()` is
**diagnostic**: it reads the existing capacity-one slot, is total, answers `False` when it cannot
read it, and can word a panel or grey a button and nothing else. All six `_RENDER_LOCK` holders
refuse correctly whether or not their button was ever greyed out — and a test pins the holder count
at six, so a seventh acquirer forces someone to decide whether its control belongs in the interlock.

## Measured, and recorded rather than assumed (gradio 6.19.0, port 7899)

Requirement 5 says to measure event-queue behaviour rather than assume concurrency settings reject
queued requests, and `CLAUDE.md` records that a plausible reading of a `concurrency_*` parameter has
already been wrong once here (S2-A R3). Full records in the task evidence; the four that shaped the
design:

| | |
|---|---|
| **M1** | A second **same-session** click on an event already running in a `concurrency_limit=1` lane is **DISCARDED** — one `render_entered`, zero second job, stable 4 s after completion. Measured with the client-side disable deliberately bypassed, so the answer does not depend on the UI guard. **Consequence: no server-side duplicate-render gate was added** — it would have been machinery for a defect that does not exist on this version. |
| **M2** | A separate instant `.click` registration on the same button runs **60 ms before** the slow handler is entered. |
| **M3** | A `.then()` link runs **after** the generator's `finally` — 30 ms after the mutex release. |
| **M4** | A render click from a **different browser session**, made while another owns the mutex, is **queued, not refused**, and admitted **33 ms after** the first releases. `_RENDER_LOCK` held throughout and the two renders never overlapped, so this is a **visibility** gap and not a safety one; what never happened is the *refusal*, because by delivery time the lock is free. Recorded, not closed with new machinery — requirement 5 permits a cross-session subsystem only for a demonstrated safety gap, and serialization held. The waiting session is instead told the truth: `STATUS_QUEUED`. |

`STATUS_QUEUED` is the eighth status, beyond the seven the brief lists, and M4 is why: showing that
session `Running` would be false and showing it nothing is the dead-button complaint inverted.

## One deliberate deviation from the brief, and why

The brief asks for **total elapsed time in `HH:MM:SS`**. What is rendered is
`raw_cutter_activity.format_elapsed`: `MM:SS`, widening to `H:MM:SS` past an hour.

That is a conscious choice, not an oversight. There is **one** elapsed formatter in this codebase
and three surfaces share it — the Raw Cutter panel, Prepare & Verify, and now this one. Writing a
second formatter for one surface is the duplication `prep_activity` deliberately avoided when it
imported this one instead; changing the shared one would change the Raw Cutter's display as a side
effect of a Render-tab item. The format it produces is also the more readable of the two for the
common case: most renders are minutes, and `03:27` reads better than `00:03:27`.

Changing it is a one-line change in one place if the owner prefers the strict form — and it would
then apply to all three surfaces, which is the decision to make rather than a detail to slip in.

## Open, and recorded rather than claimed closed

* **The `Cancelling` chip lags the status line by one frame** — see above. Bounded by the pipeline's
  own event cadence, not by a timer.
* **An abandoned render leaves its panel reading `Running` in that browser page.** Nothing is
  yielded from a `finally`, so no terminal frame is emitted; the page that replaces it renders the
  idle panel from the Blocks config. Same disposition as the Raw Cutter's panel.
* **A panel is stale by up to one browser tick between frames**, and by up to one *server* frame
  for anything the browser does not advance (the counts, the stage, the pills). It is never *wrong*
  — it is a reading that was true when it was emitted.
* **The cross-session queued render is not refused** (M4). It is reported honestly and left to the
  mutex, which held.
* **Reduced motion was verified by CSSOM rule inspection, not by emulation** — the extension cannot
  set the preference, so the reduced-motion *appearance* has not been seen. The rule block is
  present in the served stylesheet and keeps a static diagonal stripe, so an active panel still
  differs from a terminal one, which has no track at all.

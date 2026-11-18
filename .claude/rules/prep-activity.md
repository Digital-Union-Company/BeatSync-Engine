---
paths:
  - "src/beatsync_fork/prep_activity.py"
  - "tests/test_prep_activity.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`. The two operations this serves are
> `.claude/rules/source-verification.md`; the preparation pair is
> `.claude/rules/library-preparation.md`; the GUI seam is `.claude/rules/gui-integration.md`; the
> progress contract it must not duplicate is `.claude/rules/progress-events.md`.

# UX-PREP-ACTIVITY — Prepare & Verify shows trustworthy in-flight state

**Scan Library and Analyze already streamed structured progress. Verify Sources and Sample Decode
did not, and that was the whole complaint.** Each was a plain function returning one final string,
wired with `show_progress='hidden'`, so from the operator's side an accepted click and a dead
button were indistinguishable for as long as the work took — and the work is a bounded sweep over a
library that can hold thousands of files, or a probe plus a 30-second decode.

A function cannot say "working" and then work. That is the same observation R13 made about Video
Review's Confirm, and it is reached here by the same route and answered the same way.

## What changed, in five places and no more

```
beatsync_fork/prep_activity.py   NEW   pure policy: phase labels, the pulse, the rendered block,
                                       and every honesty rule about what a frame may say
gui.py  _prep_phase              NEW   the phase slot + _mark_prep_phase / _prep_phase_now
gui.py  _activity_frames         NEW   the ONE shared stream: worker thread + bounded poll
gui.py  _verify_sources_frames   NEW   the generator the Verify button is wired to
gui.py  _sample_decode_frames    NEW   the generator the Sample button is wired to
gui.py  _run_prep_in_worker      ->    the event wait is now a BOUNDED poll
```

`_verify_sources_impl` and `_sample_decode_impl` gained **only** phase-publishing calls.
`media_verify.py`, `media_sample.py`, `library_prep.py` and `video_analysis.py` are **untouched** —
no new cache write, no new probe, no new classification, no change to any verdict.

## The two collected forms are kept, and that is not optional

`_on_verify_sources_click` and `_on_sample_decode_click` keep their **exact signatures and their
string returns**, implemented as the collected form that drains its generator to the last frame.
The *buttons* are wired to the generators.

This is the split R13 made between `_on_clips_confirm` and `_clips_confirm_frames`, and here it is
forced by what already exists: `test_media_verify.py` calls `_on_verify_sources_click(...)` and
compares the result to a string by equality, and an AST test pins its parameter list to
`["folder_path", "recursive", "request"]`. A caller that does not need in-flight frames must not
have to care that they exist. **Do not collapse the pair**, and do not change either signature.

## The activity lives inside the ONE existing report textbox

`outputs=[prep_verify_report]` is pinned five ways across two accepted suites, and
`prep_verify_report` / `sample_report` are `gr.Textbox` — no markup, no browser tick. So:

* **No second component joins either `outputs` list.** The pinned isolation contract survives
  untouched: verification still writes its own report and nothing else, so it still cannot enable a
  render, disable one, move the preparation state machine or overwrite a render's status line.
* **The indicator is therefore plain text, and the liveness is SERVER-side.** There is no
  `setInterval` here and no shared class with the Raw Cutter's HTML panel — the frames are emitted
  by the generator, one a second.
* Consequence, recorded rather than hidden: between two emitted frames the figure on screen is
  stale by up to one tick. It is never *wrong* — it is a reading that was true when it was emitted.
  Same disposition as R13's progress figure.

## The honesty rules, which are the point of the item

* **Elapsed is MEASURED or absent.** Every reading comes from the caller's `time.perf_counter()`;
  the pure module reads no clock at all and a test walks its AST for one. A formatter with its own
  clock could not be tested without sleeping and would be measuring the wrong interval anyway.
* **Counts appear only when the backend measured them.** `done`/`total` render only as two real
  non-negative integers with `total > 0` and `done <= total`. `bool` is rejected first because it
  subclasses `int`. Anything else falls back to the indeterminate form — a count this module
  accepts becomes a determinate figure on screen, so "nearly a number" must fail closed.
* **No ETA, no rate, no throughput, ever** — not even when counts are known. The ban
  `progress-events.md` places on invented ETAs, restated here because this is the surface where an
  operator would most want one and where the data to compute one honestly does not exist.
* **Verify and Sample publish phases, not counters**, so no frame of theirs may show a percentage
  or a `done / total`. A test asserts exactly that against the shipped handlers. Instrumenting
  `verify_media_health` with a per-source callback would be a real improvement and is **not** done
  here — it is an interface change to an accepted, bounded-work module, and the roadmap explicitly
  permits "an indeterminate active state and elapsed time if measured" instead.
* **The pulse is mechanical, not decorative.** A Gradio update whose value is byte-identical to the
  previous one is **not delivered** — the hazard the R5 grid nonce exists for — so two frames that
  rendered the same would not arrive and the panel would freeze while work continued. `pulse`
  guarantees consecutive frames differ even within one whole second.

## Four terminal states, distinct and verbatim

```
completed   the report
failed      SOURCE VERIFICATION FAILED / SAMPLE DECODE FAILED  (+ the "nothing was changed" line)
refused     _verify_busy_message() / _sample_busy_message()    -- names the real holder
superseded  PREP_VERIFY_STALE_TEXT / PREP_SAMPLE_STALE_TEXT
```

**The activity stream decorates none of them.** They are returned exactly as before, because tests
compare them by equality and because "declined, nothing ran" and "ran and broke" lead an operator
somewhere different. `prep_activity.OUTCOMES` is a four-label vocabulary for tests and callers to
agree on, **not** rendered text. A test drives all four through the shipped handler and asserts the
four strings are different.

**A refusal emits exactly ONE frame and no activity frame.** An indicator for work that was
declined is the dead-button complaint inverted — R13's rule for the sweep gate, and
`ScanCoordinator`'s for a scan refused for a bad folder. A refused click still captures no
revision, pins nothing and performs no work, and a test asserts it does not run later either when
the mutex frees (the S2-A R3 defect, restated for this stream).

## The join is before the release, and that ordering is load-bearing

`progress-events.md` states the rule for `process_video`: *a generator's lifetime is its worker's
lifetime, because the caller's mutex is released the moment that generator returns.*

Verify and Sample hold `_RENDER_LOCK` across the whole stream. So if an abandoned Gradio event
released that mutex while the worker was still issuing FFprobe reads, a render could start straight
into the disk contention the mutex exists to prevent — the D0 condition.

`_activity_frames` therefore joins its thread in its own `finally`, and because the callers use
`yield from`, closing the outer generator closes the inner one **first**: join, then
`_end_verify_session`, then clear the phase, then clear the holder token, then release. A test pins
both the `finally` join and the source ordering.

**Abandoning the event therefore waits for the work.** That is not a cancellation and must not be
described as one, and no Cancel is built on it — exactly the disposition R13 records for its own
abandoned generator.

Nothing is yielded from any `finally`: yielding during a close raises `RuntimeError`, replacing a
clean join with a second failure.

## The phase slot is one global, and the invariant that makes it sound

`_prep_phase` is a single module-level string under `_VERIFY_FLAG_LOCK`, deliberately the same
shape as `_lock_holder`. A single global is sound **for exactly the reason the R5 revision pin is
sound**: `_RENDER_LOCK` admits at most one of render / verify / sample / cutter at a time, so at
most one phase is ever load-bearing. Two concurrent operations cannot both be reporting, because
two concurrent operations cannot exist.

* **It is diagnostic only and nothing branches on it.** A stale, missing or late phase can word a
  line of text and nothing else: it can never admit a worker, release a mutex, publish a report,
  change a verdict or move a revision.
* **`''` means "no phase published yet"** and renders as `Starting`, never as a guess.
* **It is cleared in the `finally`,** so a later operation can never display an earlier one's
  phase — the straggler rule applied to this slot, with its own test.
* The worker publishing into it is **not** "touching a Gradio component from a worker thread": the
  generator still does every widget update, exactly as `_run_prep_in_worker`'s `event_callback`
  only ever calls `queue.put`.

**Sample's phases come from the SEAM, not from a copy of the orchestrator's sequence.**
`_sample_decode_impl` wraps the two injected callables, because `run_sample_check` owns the order of
its own work and a restated sequence would drift the moment that core changed. The keyword set
reaching `run_sample_check` is still exactly `{probe, decode}` and the positional arguments still
exactly two, so the resource policy is **still enforced by omission** — there is no new parameter
and no widget value can reach one.

## Scan and Analyze: the event wait is now bounded

`_run_prep_in_worker` used `status_queue.get()` with **no timeout**, so between two
`ProgressEvent`s it yielded nothing at all — and a phase that emits no event (a cold library's
identity pass, a Qwen worker loading its model) left the panel frozen on its last line with nothing
to distinguish working from wedged.

It is now a one-second poll, and a tick with no event emits an activity notice carrying the pulse
and a measured elapsed reading. Three things it deliberately does not do: it invents no count (the
report box shows `ProgressView`'s real ones when they exist), it fabricates no percentage or
estimate, and it does not restate the phase `ProgressView` is already rendering — the notice says
`Working` and the box says which stage. **A timeout is not a sleep**: an event arriving
mid-interval is applied immediately, so the cadence never delays anything.

The notice goes in `prep_status`, which is already one of the four `prep_outputs`, so **the
preparation event wiring is byte-identical** — same inputs, same outputs, same four-tuple arity.

## What this must not acquire

* **No parallel coordinator, job manager, scheduler, pool or background service.** One thread per
  call, owned by the generator that started it, joined by that generator's `finally`. Nothing
  outlives the request, nothing is registered anywhere, and a test bans the names.
* **No second progress protocol.** No `ProgressEvent` is emitted, no stage or `ProgressView` phase
  key is defined, and nothing is parsed out of rendered text. Re-adding prose parsing at this seam
  would repeat the mistake Phase 2A deleted.
* **No new admission authority.** `_RENDER_LOCK.acquire(blocking=False)` is still the only thing
  that decides whether Verify or Sample may run; the acquire/release site counts are unchanged, and
  `_RENDER_LOCK.locked()` still appears nowhere.
* **No change to the lanes.** Both events keep no `concurrency_id` and `concurrency_limit=None`,
  for the measured S2-A R3 reason: a shared lane leaves the event queued and then runs it against
  click-time values, and a lane of 1 re-creates that one step in.
* **Nothing enters `gr.State`** — no lock, thread, handle, lifecycle or phase.
* **No cache write, no media change, no analysis.** `WORKING_FOOTER` says so to the operator, and
  it is true of all four operations: Verify and Sample write nothing at all, Scan creates the cache
  directory and no record, and Analyze's writes stay on the existing checkpoint path.

## Open and recorded rather than claimed closed

* **Determinate per-source progress for Verify is NOT implemented.** `verify_media_health` measures
  its own sliding window internally but exposes no progress callback, and adding one is an
  interface change to an accepted bounded-work module. Until then Verify is honestly indeterminate.
  Do not fake a count from the enumerated source list — the sweep's own budget means the number it
  would reach is not the number it enumerated.
* **No browser-side tick.** The figure is server-emitted text in a `gr.Textbox`, so it is stale by
  up to one tick between frames. The Raw Cutter's anchored `CUT_ACTIVITY_JS` is the mechanism for a
  smooth clock and it needs an HTML panel; adopting it here would mean a new component in a pinned
  `outputs` list and is a separate decision.
* **Sample Decode still has no Cancel**, and this adds none. The bound is the approved 30 s decode
  timeout inside the accepted seam. Abandoning the event waits for the child, as above.
* **GUI runtime acceptance is not claimed.** No Gradio was started and no FFmpeg executed by this
  work. Actual click behaviour, the frame cadence as rendered by `gradio==6.19.0`, and how a
  `gr.Textbox` repaints a streamed value remain a separately authorized milestone.

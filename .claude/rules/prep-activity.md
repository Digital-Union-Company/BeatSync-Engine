---
paths:
  - "src/beatsync_fork/prep_activity.py"
  - "tests/test_prep_activity.py"
  - "tests/test_prep_visual_progress.py"
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

**That argument covers the MUTEX HOLDERS only, and the slot is scoped to them (R2).** Scan and
Analyze take no lock — they run on their own unlimited lanes through `prep_scan_btn.click` /
`prep_analyze_btn.click` — so "two concurrent operations cannot exist" is simply false for them,
and a reader outside the lock inherits none of the soundness. The first implementation had
`_run_prep_in_worker`'s notice read the slot, and the accepted source review of PR #109 found the
consequence: with a Verify in `PHASE_MEDIA_HEADERS`, a silent Scan tick rendered

```
Scan Library — Reading media headers..   (00:01 elapsed)
```

— one operation's phase under another operation's name, which is exactly the truthful-phase
requirement this item exists to satisfy. Reproduced deterministically on the shipped seam before
the fix, and the correction is **scoping, not machinery**:

```
WRITERS  _verify_sources_impl, _sample_decode_impl,
         _verify_sources_frames, _sample_decode_frames     -- all hold _RENDER_LOCK
READERS  _activity_frames                                  -- runs only inside those two generators
         _run_prep_in_worker  -> names fork_prep_activity.PHASE_WORKING unconditionally
```

`test_every_writer_and_reader_of_the_phase_slot_holds_the_mutex` pins those as **exact sets**
rather than banning a name, so a future read from an unlocked path fails in the suite instead of
in front of an operator. Scan and Analyze lose nothing: their real, detailed phases are already in
the report box through `ProgressView`, and the notice's job was never to restate them. **No second
slot, no coordinator, no new mutex and no new GUI output was added** — the two families simply
never have to describe each other.

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

**`Working` here is a literal, not a fallback (R2).** The notice is built with
`phase=fork_prep_activity.PHASE_WORKING` and never consults `_prep_phase` — see the phase-slot
section above for the cross-operation attribution defect that `_prep_phase_now() or PHASE_WORKING`
caused. Do not reintroduce the `or`: it reads as a harmless default and is not one, because the
slot belongs to operations this one does not exclude.

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

  (**Superseded for Scan and Analyze only, by UX-PREP-VISUAL-PROGRESS R1 below**, which ran a
  served smoke against the real `gradio==6.19.0`. Verify and Sample still carry this disposition:
  their report boxes were not exercised in a browser by either item.)

---

# UX-PREP-VISUAL-PROGRESS R1 — the same observations, as a visible panel

UX-PREP-ACTIVITY made Scan and Analyze **speak** through a silent phase. They still were not
**visible**, and the owner reported the consequence from the installed GUI on 2026-11-22:

> a Scan of 6,363 sources shows `0 / 6363 (0.0%)` for several minutes, and the only sign of life is
> a small `Working… (03:27 elapsed)` line below the report.

## Two halves, two causes — and only one of them is a defect

* **The count is TRUE, and this is the part that is easy to get wrong.**
  `classify_library_sources` emits `start(current=0, total=len(sources))` and *then* runs
  `_compute_cache_paths_parallel` over the whole library, which emits **nothing** until the
  per-source verdict loop begins. So `0 / 6363` is a real measurement of a real counter that has
  genuinely not advanced. **Do not "fix" it by suppressing the count, smoothing it, or starting it
  somewhere other than zero.**
* **The silence is the defect.** A determinate bar parked at 0.0% is indistinguishable from a dead
  button, and the placement made it worse: `prep_status` sat *below* a ten-line report box and the
  Analyze button, so nothing within sight of the pressed control changed.

The answer separates two questions the old line conflated:

```
"is this running?"     -> a state chip + a CSS animation. True for as long as the worker is alive,
                          and they cost no measurement at all.
"how far has it got?"  -> ONLY measured counts. Absent when nothing measured one.
```

**They are not alternatives.** A 0%-filled bar still carries the sweep, because the count being
zero is not evidence that nothing is happening.

## Where the motion lives, and why it is not a timer

In `PREP_ACTIVITY_CSS`, as one `@keyframes` declared once on the Blocks. The server emits one frame
a second, so an animation driven by the frames would step once a second and read as a stutter; a
browser-side timer would mean a fourth authorised JS installer plus per-element state a Gradio
re-render destroys. **Measured in the served app:** while a single server frame — the `0 / 8000`
one — was on screen for 680 ms, the sweep's computed `background-position-x` took **17 distinct
values** from `-40%` to `42.3%`, with no network traffic and no script.

**The sweep is a SIBLING of the fill, spanning the whole track.** First attempt put it *on* the
fill; a determinate `0 / N` then renders a zero-width element and the animation had nowhere to
travel — dead at exactly the count the owner reported. The element carrying the motion must never
be the one whose width is a measurement.

## What the panel may say

* **`activity_panel_html`** renders the RUNNING panel from the same `ActivityState` the one-line
  form renders, so it can say nothing `activity_line` could not. The one-line form is passed **in**
  as `summary`, which makes the call site the proof that there is one renderer rather than two.
* **`status_panel_html`** renders terminal and idle. **No track at all** — a full bar on completion
  would be a measurement nobody took, and a bar on a failure would claim the work got somewhere.
* **`measured_percent` is the only route to a percentage**, used by both forms. A test pins that
  `* 100.0` appears exactly once in the module.
* `STATUS_IDLE` + an empty notice renders **`""`**, not an empty card. That is the blank-space half
  of the complaint, fixed at the source.
* **Unknown status fails closed to idle**, never to running (an animation over a dead worker) and
  never to completed (the one answer a caller must not receive by accident).
* **Whitespace in every interpolated string is collapsed to one space.** Escaping alone is not
  enough: `html.escape` leaves a newline alone, this lands in a **Markdown** component, and a blank
  line inside a raw HTML block is how a Markdown renderer decides the block ended. That is layout,
  not rewording — the notice slot is a one-line status by its own contract.

## The terminal state is classified from STRUCTURE, never from prose

```
failed   library_prep.is_failure_report(report_text)   -- tests only FAILURE_PREFIX, one definition
refused  notice == library_prep.REFUSAL_NOTICE         -- one constant, two gui.py call sites
         refusal WINS: a declined run never started, so it cannot have failed
```

`FAILURE_PREFIX` and `REFUSAL_NOTICE` exist so the reader and the writer cannot drift apart; a test
asserts the literal appears once in `library_prep.py` and nowhere in `gui.py`. Nothing branches on
either — they can word a panel and nothing else.

**`_prep_ui_updates` gained three keyword-only arguments, and exactly one caller passes them**: the
terminal frame, which is the only code that observed an operation finish. Every widget `change`
handler gets the neutral idle panel, so an invalidation can never leave a green `Completed` chip
standing over the scan it just invalidated. They default to plain `''`/`None` because this function
is AST-executed against synthesised namespaces by two suites — a default that *evaluated* a module
attribute would fail at definition time in a namespace that does not carry it.

**An abandoned stream claims nothing.** Nothing is yielded from a `finally`, so a closed generator
emits no terminal frame and the last thing on screen stays the honest RUNNING panel.

## The phase and the counts come from THIS operation's own events

`ProgressView.active_measurement()` is a new read-only projection of state the view has already
folded — added so the counts can be had without either wrong alternative: reaching into `_stages`,
or parsing them back out of `render()`, which is the prose parsing Phase 2A deleted. It reports one
`_PhaseView`, so the per-`(stage, phase)` monotonicity rule applies unchanged and an uncounted
phase reports `None`, never zero.

**This does NOT reintroduce the R2 cross-operation defect.** `_prep_phase` is still never read by
`_run_prep_in_worker`: that slot belongs to the mutex-holding Verify and Sample. `view` holds only
the events this operation emitted, so it can describe no other. `PHASE_WORKING` remains the label
before the first event arrives, and a test drives a real Verify holding `PHASE_MEDIA_HEADERS` while
a Scan ticks, asserting the Scan panel never shows it.

## The layout move, and the one window it is safe in

`prep_status` moved from below the Analyze button to directly beneath `prep_scan_btn`, above the
report box. It is a **layout move only** — same widget, same name, same type, same `elem_id`, same
position in `prep_outputs`, same four-tuple arity; Gradio matches `outputs` by position in the
list, not on screen.

**It may not move further.** Four other suites slice `gui.py` raw with
`source.index("LABEL_PREP_SECTION") : source.index("prep_status = gr.Markdown")`, and one of them
requires `prep_batch_size = gr.Number(` to be *inside* that span. Above the scan control the span
loses it. A test in `tests/test_prep_visual_progress.py` pins the window.

**And `gr.HTML` was the obvious change and the wrong one**, for the same reason: those four slices
key on the literal `prep_status = gr.Markdown`.

## Measured in the served app (gradio 6.19.0, port 7881, synthetic library, Qwen disabled)

```
t=   91 ms  running    Running  00:00  "Working"                                 fill 100%  sweep+pulse
t=  521 ms  running    Running  00:00  "Classifying 8000 ... · 0 / 8000 (0.0%)"  fill   0%  sweep+pulse
t= 1448 ms  running    Running  00:01  "classified · 1 / 8000 (0.0%)"            fill   0%  sweep+pulse
t= 1496 ms  running    Running  00:01  "classified · 8000 / 8000 (100.0%)"       fill 100%  sweep+pulse
t= 1509 ms  completed  Completed 00:01 "8000 video(s) need analysis."            no track, no animation
```

Component count **516 = 516** against a base worktree built by the identical script; no `elem_id`
added or removed; CSS +2,772 bytes. A deliberately absent folder produced the **Failed** panel with
Analyze disabled and the full error in the report box.

## Open, and recorded rather than claimed closed

* **`gr.Markdown`'s sanitizer strips every `data-*`, `role` and `aria-*` attribute — MEASURED.**
  `class` and `style` survive, so the animation, the tones, the determinate fill width and the
  state class all work, and the state and the measurement are readable as **text** (the chip and
  the detail line). But the `role="progressbar"` / `aria-valuenow` semantics and the `aria-label`
  **do not reach the browser**. They are still emitted — they are correct, cost nothing, and would
  become live if this ever moved to `gr.HTML` — and `tests/test_prep_visual_progress.py` asserts
  them on the **server-side markup only**. Do not cite those tests as evidence of browser
  behaviour. `sanitize_html=False` would make them live and is deliberately **not** taken: it
  removes a defence-in-depth layer from a component fed operator-controlled strings (folder paths,
  FFmpeg errors), for an a11y gain the text already mostly provides.
* **A sustained minutes-long silent phase was not reproduced in the browser.** The synthetic
  library's 1 KB files make the identity pass instant, and the HOT reserve was reached, so it was
  not grown. The sustained case is asserted server-side instead (several poll ticks, monotonic
  measured clock, every frame distinct); the browser evidence covers the determinate-zero frame,
  which is the one the owner reported.
* **Reduced motion was verified by CSSOM rule inspection, not by emulation.** The
  `prefers-reduced-motion` block is present in the served stylesheet; the extension cannot set the
  preference, so the reduced-motion *appearance* has not been seen.
* **Analyze's panel was verified server-side only** (both operations are parametrised in the
  suite). Running a real Analyze needs the Qwen backend, which the sandbox deliberately disables.
* **Responsiveness was verified by narrowing the panel's CONTAINER**, down to 240 px with no
  horizontal overflow at any width. `resize_window` cannot move this extension's rendered viewport,
  so a `max-width` media query was not exercised — and the panel has none: it reflows by
  `flex-wrap`.

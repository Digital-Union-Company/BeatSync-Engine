---
paths:
  - "src/beatsync_fork/media_verify.py"
  - "src/beatsync_fork/media_sample.py"
  - "src/ffmpeg_processing.py"
  - "tests/test_media_verify.py"
  - "tests/test_media_sample.py"
---
<!-- `src/gui.py` is deliberately ABSENT from this list. `docs/claude/README.md` records the
     architecture: gui.py loads only `gui-integration.md`, and each subsystem rule is scoped to its
     own implementation plus its GUI seam test -- which `tests/test_media_verify.py` is, since it
     carries the concurrency, live-input and output-isolation assertions. `gui-integration.md`'s
     routing table points here. -->

> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

## Read-only source verification (MEDIA-VERIFY-S2-A)

**Verify Sources** answers two questions about the Media Library Preparation folder, quickly and
without touching anything: do these files still have reusable Stage-5 cache, and do they have basic
media problems. It exists because the owner sometimes overwrites previously analysed videos, and
finding that out by rebuilding Qwen cache costs GPU hours.

`beatsync_fork/media_verify.py` owns the statuses, the bounded work policy, aggregation and the
report. `ffmpeg_processing.probe_media_metadata` owns the one child process.
`gui.py::_verify_sources_impl` wires them to the live widgets. Scope boundary: S2-A does **not**
decode files, repair or invalidate cache, retry a render, or change Stage 6.

### The honesty boundary — the whole point of the milestone

- **`MEDIA_METADATA_OK` means the container header parsed and a video stream was described. It is
  not proof that every frame decodes.** FFprobe reads metadata; `-show_format -show_streams` never
  decodes a picture. The D0 failure that motivated this (a truncated MP4 with no `moov` atom) *is*
  caught, but a file with an intact header and a shredded middle is not.
  `media_verify.METADATA_NOT_DECODE_TEXT` — "Metadata readable — full video decoding not verified."
  — is carried verbatim in **every** report, and a test asserts no report ever says "intact", "no
  corruption" or "fully verified". Do not reword any of this into a stronger claim.
- **A probe timeout is not a corruption verdict.** A 5400-RPM drive under concurrent load times out
  for reasons unrelated to the file's bytes, so `MEDIA_PROBE_TIMEOUT` reports *unverified* and says
  so in the detail. `classify_probe` checks `timed_out` **first**, before any exit code, so a stale
  return code on a timed-out probe cannot be reinterpreted.
- **A skipped source is never a successful one.** The work budget stops *scheduling*; everything it
  did not reach becomes `MEDIA_NOT_CHECKED` with its own reason, the run reports
  `PARTIAL / INCOMPLETE`, and `VerificationRun.complete` is false. There is no code path that
  upgrades a non-verdict into a verdict.
- **A worker exception yields no opinion.** Our tooling failing is not evidence about the file, so
  it is `MEDIA_NOT_CHECKED` with the exception named — never a clean read and never corruption.
- **The two questions never merge into one score.** Cache reuse and media health are independent
  properties (an overwritten file is perfectly readable *and* has no reusable cache), so the report
  keeps `CACHE VERIFICATION` and `MEDIA HEALTH` as separate sections with separate counts.
- **An unprovable Qwen backend identity is `UNVERIFIABLE`, not a damaged cache.** When
  `classify_library_sources` returns `ai_cache_disabled`, no per-source cache verdict can exist, so
  none is invented. Printing "0 reusable" there would be a fabricated verdict; a test forbids it.

### Bounds — all of them load-bearing

- `MAX_PROBE_WORKERS = 4`, a hard cap, with `DEFAULT_PROBE_WORKERS = 4`. D0 measured a 41-file
  sample on the owner's mechanical source drive: 1.50 s sequential, 0.59 s at four workers. Beyond
  four, a 5400-RPM spindle seeks rather than reads. `resolve_worker_count` clamps into `1..4` and
  falls back to the default on junk rather than raising.
- Sources are submitted through a **sliding window** of `workers * 2`, not all at once, so a
  2,000-source library never queues 2,000 work items and never has more than four live children. A
  test drives 1,200 sources and asserts peak concurrency. R3 added the assertion that was missing:
  counting probes that have *entered* cannot see the window at all, because the thread pool caps
  that number whether a window exists or not — so a second test records `submit` calls on the real
  executor while every worker is blocked. Deleting the window used to leave the suite green.
- The cache phase of the same click is **not** bounded by `MAX_PROBE_WORKERS`: it inherits the
  classifier's existing `_CACHE_IDENTITY_WORKER_CAP = 16` identity pool and submits one item per
  source. The four-worker cap is about the FFprobe children only; do not restate it as a
  click-level bound.
- `DEFAULT_PROBE_TIMEOUT_SECONDS = 20` per file, `DEFAULT_BUDGET_SECONDS = 600` overall. Every
  probe is bounded; a test asserts the timeout reaches every call.
- **Everything started is reaped.** The budget never abandons a running child — a test records
  start/finish per probe and asserts the sets match on a budget-exhausted run.
- `WorkBudget` and `verify_media_health` take their clock as an argument, so partial-run behaviour
  is tested without sleeping.
- **D0's ~30 s extrapolation for ~2,000 sources was never measured on the whole library and is not
  a performance guarantee.** Do not restate it as one. The committed benchmark measures harness
  overhead (~0.05 ms/source) on synthetic fixtures with a stub probe, deliberately not the owner's
  library.

### Render exclusion — reuse the mutex, and keep the Verify event OUT of the render lane

Verification issues up to four concurrent reads against the same drive a render extracts clips
from, and D0 established that exactly this contention is what pushed clip extraction past its 120 s
bound. So the two are mutually exclusive, via **`_RENDER_LOCK` itself**:

- Verify while a render runs → its `acquire(blocking=False)` fails → it refuses immediately.
- Render while Verify runs → the **existing** `acquire(blocking=False)` fails → the render refuses.

The second direction needed no change to the render path; it already refuses when the lock is held
and does not care who holds it. That is the whole reason this reuses the mutex:
`if not lock.locked(): start()` is a check-then-act race, and a second lock would have to be
ordered against the first. One non-blocking `acquire` is the test and the acquisition in a single
indivisible step. A test asserts there are exactly three `_RENDER_LOCK.acquire(blocking=False)`
sites and three matching releases, and that `_RENDER_LOCK.locked()` appears nowhere.

**The mutex is necessary but was not sufficient, and R3 is why.** S2-A also put the Verify event in
`concurrency_id=RENDER_CONCURRENCY_ID` with `concurrency_limit=1` as "cooperative UX". Measured on
the pinned `gradio==6.19.0`, that is what defeated the contract above: `Queue.get_events()` skips a
concurrency group already at its limit and leaves the event **in the queue**, so a Verify clicked
during a render never reached the `acquire` at all. It was entered only once the render finished,
ran automatically against the folder captured at *click* time, and overwrote the newer
"folder changed" notice. Both refusal messages were unreachable, and this file claimed an immediate
refusal the operator could never observe.

So:

- `prep_verify_btn.click` carries **no `concurrency_id`** and **`concurrency_limit=None`**. Do not
  put it back in the render lane, and do not give it a lane of 1 — a limit of 1 re-creates the
  same defect one step in, queuing a *second* Verify click instead of letting the mutex refuse it.
  A refusal is microseconds of work, so an unlimited lane costs nothing; the mutex, not the lane,
  is what bounds the actual disk reads to one at a time. Tests pin the absent `concurrency_id` and
  the `None` limit, and the measured behaviour is reproduced in an isolated fixture outside the
  suite (Verify refused in 0.19 s against a 5 s render; never auto-ran afterwards).
- **`CANCEL_CONCURRENCY_ID` is untouched.** Both render events keep
  `concurrency_id=RENDER_CONCURRENCY_ID` with `concurrency_limit=1`.
- The release is in a `finally`, so an exception or an abandoned request cannot leave the app
  permanently refusing renders. Tests cover success, `Exception` and `BaseException`.
- A running render is **never** signalled, throttled or terminated to make room for verification,
  and verification is never terminated to make room for a render.
- `_verification_running` (guarded by the small `_VERIFY_FLAG_LOCK`) exists **only** so a refusal
  can name the real holder. It is not an exclusion mechanism; the mutex is. It is set immediately
  after acquiring and cleared immediately before releasing, so a caller that has just failed to
  acquire attributes the lock correctly. The one-statement window between the two acquisitions can
  mislabel a refusal message and can never admit a second worker.
- **Two refusals, two messages.** `_busy_message()` answers the RENDER side
  (`VERIFY_BUSY_MESSAGE` / `RENDER_BUSY_MESSAGE`); `_verify_busy_message()` answers the VERIFY side
  (`PREP_VERIFY_ALREADY_RUNNING_TEXT` / `PREP_VERIFY_RENDER_BUSY_TEXT`). Telling the operator a
  render is running when another verification owns the mutex is a wrong answer. Neither message may
  promise that the request will wait its turn — it was *declined* — and a test bans "wait for it",
  "queued", "will start automatically" and friends from all three strings.
- Nothing is stored in `gr.State`: no lock, no thread, no process handle, no cancellation object.
  Verification owns no `RenderLifecycle` and never installs one.

### A report is never published for a library the operator has moved on from

The folder/recursive `change` listeners run in their own Gradio lane and can therefore complete
**while** a verification is still working. Clearing the textbox is not enough on its own: the older
answer would simply overwrite the newer notice.

- `_on_verify_invalidate` bumps a **per-session** revision; `_on_verify_sources_click` captures that
  revision once the mutex is held and, if it has moved by the time it finishes, returns
  `PREP_VERIFY_STALE_TEXT` instead of its report. The failure text is withheld on the same
  condition — it names a folder too.
- **The baseline is captured AFTER the mutex is taken, never before.** A refused click must leave
  nothing behind: no captured revision, no map entry and no pin. `_begin_verify_session` therefore
  runs on the accepted path only, and it both pins and captures in **one** locked step —
  pin-then-read as two steps would let an invalidation land in between and be folded into the
  baseline, so the terminal comparison would see no change and publish the stale report.
- **Session-scoped, never one process-global counter.** A global counter would let a folder edit in
  one browser session invalidate a legitimate, still-running verification in another. The key is
  `gr.Request.session_hash`, injected by Gradio into both the `.click` and the `inputs=None`
  `.change` handler (measured on 6.19.0: present, stable within a session, distinct across two
  clients). A request with no session hash falls into one shared named bucket — the fallback must
  not be unique per request, or an invalidation could never match its own verification.
- **Bounded, and the bound is the cleanup.** `demo.unload` was measured *not* to fire for a closed
  client on this version, so no end-of-session hook is relied on. `_verify_revisions` holds at most
  `_VERIFY_REVISION_CAP = 512` integers with least-recently-touched eviction, and an entry is only
  created by an actual invalidation — a session that never edits its folder never appears. Reading
  a revision must stay a read; a lookup that inserts would let an idle session occupy a slot. The
  map holds `str -> int` and nothing else: no lock, thread, handle or lifecycle may enter it.
- **The ACTIVE verification's revision is exempt from eviction, and that exemption is the
  correctness guarantee.** This rule previously claimed eviction "is safe in one direction only"
  and "can never let a stale report be published as current". That was **false**, and an
  independent review reproduced it: `_verify_revision` reports `0` for an **absent** key, so
  dropping a running verification's entry made "I was invalidated" and "nothing changed"
  indistinguishable. Session A captures `0`, A's folder changes storing `A = 1`, 512 other
  sessions invalidate and evict A, the terminal read finds A absent and gets `0`, `0 == 0`, and
  the report for the folder the operator already left is published as current. Measured: 511
  other sessions were safe, 512 published it. The same hole had an ABA form — evicted at
  revision N, recreated at N by a later invalidation.
- The fix uses an invariant that already exists rather than adding a mechanism: `_RENDER_LOCK`
  permits at most **one** verification, so at most one revision is load-bearing at any instant.
  `_verify_active_session` names it for exactly the span of its own run and the eviction loop in
  `_bump_verify_revision` skips it. The cap still bounds the map **including** the pinned entry —
  when the cap is exceeded there are always at least `_VERIFY_REVISION_CAP` droppable keys,
  because only one key can ever be pinned. Other sessions evict normally, reads still never
  insert, and once the run ends the entry is ordinary again.
- **The staleness decision is made while the entry is still pinned.** `_end_verify_session`
  compares and then unpins inside one lock block, and it is called from the `finally` that also
  releases the mutex, so every terminal path — success, a reported exception, a `BaseException`
  propagating out, a withheld stale answer — drops the pin exactly once. Unpinning before the
  comparison would restore the original defect the moment that lock block is ever split, so the
  source order is fixed by a test as well as by the single critical section.
- The pin is a plain string. No lock, thread, event, handle or lifecycle object enters
  `_verify_revisions`, the pin or `gr.State`, and nothing is persisted.
- This is not a persistence layer and it is not `gr.State`. Do not promote it to one, and do not
  route it through `prep_state` or `source_state`.
- **Deliberately NOT extended to preparation Scan/Analyze.** Those are the operator's own
  long-running maintenance clicks in the same accordion; serializing them would be new behaviour
  for existing controls. Verification is bounded and read-only, so overlapping a Scan costs disk
  throughput only — unlike a render, which has a hard per-clip timeout that contention can break.

### Source stability — THREE observations, two windows

A click answers the cache question and the media question in sequence, so its two answers are taken
at different moments. S2-A bracketed only the probe, which left the gap between them invisible: a
file overwritten after being classified and before being probed reported `PREPARED` (old bytes)
beside `METADATA_OK` (new bytes), under one "Verified at" header and a `COMPLETE` banner. Two
confident verdicts about two different versions of one file. That was R3's MAJOR-1.

`_verify_one` therefore compares **three** `(size, mtime_ns)` observations:

1. the **baseline**, taken by `fingerprint_sources()` once per source in `_verify_sources_impl`
   **before** `classify_library_sources` is called — ordering is the fix, and a test asserts the
   call order `baseline → classify → media`;
2. immediately before FFprobe;
3. immediately after FFprobe.

Any disagreement is `MEDIA_CHANGED_DURING_CHECK` **whatever the probe said, including a clean
read**, and `MediaVerdict.change_window` records *which* window
(`ChangeWindow.CROSS_PHASE` / `DURING_PROBE` / `BOTH`, never collapsed to the narrower one). There
is no code path that pairs a change window with `METADATA_OK`. Nothing is deleted, rebuilt or
re-analysed in response.

- **Baselines are positional, never path-keyed.** A library may contain the same path twice; a dict
  would merge the two entries and hand the merged observation to both. `verify_media_health` pads
  or truncates a wrong-length sequence to `NO_BASELINE` rather than raising — a bad caller must not
  be able to fail a read-only click.
- `NO_BASELINE` is a distinct sentinel, **not** `None`. `None` already means "a baseline was taken
  and the file could not be stat-ed", and that becomes a cross-phase change the moment the file
  reappears. Omitting `baselines` entirely reproduces S2-A's probe bracket exactly.
- A source that was readable when classified and is gone now stays `MEDIA_UNREADABLE` — the
  accurate media verdict — but carries `CROSS_PHASE`, because its cache verdict is no more
  conclusive than its media verdict.
- The whole mechanism is `os.stat` and comparison. One extra stat per source, no file is opened, no
  hash is computed, and **no second identity formula exists** — Stage-5's `_video_signature` is
  untouched and is not consulted here.
- Like Stage-5's bounded fingerprint, this detects an overwrite and is **not** proof of whole-file
  byte identity; `CHANGE_DETECTION_LIMIT_TEXT` says so in every report. It is an accidental-change
  detector, not an integrity verifier, and it cannot see a replacement that preserves both size and
  timestamp. Do not reword that into a stronger claim.

### Reporting a change without inventing a new classification

- The classifier's counts are its **real output** and are printed verbatim. Changed sources are
  **never silently subtracted** from them — that would publish a classification nobody computed.
  What is added is the boundary: the counts describe each source *as it was when classified*, and a
  dedicated `Changed after classification - cache verdict INCONCLUSIVE` block names each affected
  path together with the verdict it was given.
- With a cross-phase change present, `Every source has a reusable Stage-5 cache record.` is
  withdrawn in favour of the "…when it was classified, but the inconclusive source(s) above changed
  since" wording. `No metadata problems found in any source.` cannot appear either, because
  `CHANGED_DURING_CHECK` is a problem status.
- `STATUS: COMPLETE` is about probe **coverage** and stays accurate, so `VerificationRun.complete`
  is deliberately unchanged. An `INCONCLUSIVE:` line sits next to the banner with both window
  counts, because a complete sweep is not the claim "everything is fine as it stands".
- When the backend identity is `UNVERIFIABLE` no per-source cache verdict exists to qualify, so
  none is printed — the media-side `INCONCLUSIVE:` line still is.
- The cache list and the media verdicts align **by position**, and the recorded path is compared as
  a guard. If they ever stop aligning, the row says the classification could not be matched rather
  than attributing one source's verdict to another.

### What verification must never touch

- **No new cache key and no second completion rule.** The cache question goes through
  `video_analysis.classify_library_sources()`, which already owns `_video_signature`/`_cache_path`/
  `_load_cache`/`_cache_entry_is_complete`. `_verify_sources_impl` reaches none of them directly,
  and the classifier's single `_load_cache` stays the one authorized record-read point.
- **No cache write of any kind** — no record created, checkpointed, rewritten or deleted. No Qwen
  worker is launched to answer the cache question. No media is modified; the only OS call
  `media_verify` makes is the `os.stat` behind the fingerprint.
- **The live widgets are the input, never `prep_state`.** `_on_verify_sources_click` takes
  `(folder_path, recursive)` from the two live widgets plus Gradio's injected `gr.Request`, and
  re-enumerates with `scan_library_folder`. The request is **not** a widget: it never appears in
  `inputs`, it defaults to `None` so the handler stays callable without Gradio, and exactly one
  attribute (`session_hash`) is read from it. Gradio delivers widget changes as separate queued
  events, so trusting a recorded scan would verify the *previous* library while the screen declared
  a different one — the same hazard the render gate and `_prep_analyze_impl` already take live
  values to avoid.
- **One output, its own.** `prep_verify_btn.click` writes `[prep_verify_report]` and nothing else —
  not `status_output`, `video_output`, `process_btn`, `source_state`, `render_invocation_state`,
  `director_proposal_state`, `variant_batch_state`, the render-outcome state, `prep_outputs` or any
  element of `source_outputs`. `prep_verify_report` is absent from `prep_outputs` and
  `source_outputs`, so verification can neither enable a render nor move the preparation state
  machine. `process_btn` stays controlled solely by the existing source-confirmation workflow.
- **Folder/recursive changes clear the report through their own `change` listeners**, not through
  `prep_outputs` — the preparation state machine's handling of those widgets is untouched. A stale
  report that looks current is a wrong answer, so the header always names the folder, the recursive
  setting and the verification time, *and* the per-session revision above stops an in-flight
  verification from publishing over the newer notice.
- **`_eligible_media_summary` remains the AI Director's only media authority.** Verification never
  reaches it, and it never consults verification.
- **No persistent report artifact.** The report lives in one textbox. A detailed export is a later,
  separately reviewed milestone; do not add a second persistence layer for media-health results.

### Report shape

Problem lines are bounded at `MAX_REPORT_DETAIL_LINES = 25` per status block, with the omitted
count stated explicitly and the true total in the block header. The bound is on the **number of
rows** — a displayed failure's own path is never shortened, because the path is the identification
the operator needs in order to act. That is S1's MAJOR-1 restated for this surface: a diagnostic
must not truncate away its own subject. A test drives a 900-character path through the report.

## Sample decode (MEDIA-VERIFY-S2-B, P1 — pure core + seam, NOT wired to the UI)

S2-A answers "did the header parse?". S2-B asks the next bounded question: **take one source, one
seek position, and make the decoder actually produce frames there.** `beatsync_fork/media_sample.py`
owns the fifteen statuses, renderer-equivalent stream selection, the progress parser, the classifier
and the report; `ffmpeg_processing.build_sample_decode_command` / `sample_decode_video` own the one
child process. P1 ships **no Gradio control and no event handler** — GUI and per-session
stale-result suppression are P2.

`probe` and `decode` arrive as **injected callables**, like `verify_media_health`'s `probe`. That is
what makes all fifteen statuses reachable with no FFmpeg, no FFprobe and no media, and it is why
`media_sample` never imports `ffmpeg_processing` (the hard rule runs one way only).

### `classify_probe` is NOT a prerequisite for a sample decode

The trap this contract exists to prevent. `classify_probe` selects the **first**
`codec_type == "video"` stream and validates *that* stream's geometry and duration. FFmpeg's
documented automatic selection — which is what `extract_clip_segment_ffmpeg_detailed` gets, because
it passes no `-map` — takes the **highest-resolution** video stream, ties broken by lowest index. On
a multi-video container those are different streams, so gating the decode on `classify_probe` would
both refuse files whose *unselected* first stream has a bad duration and bless a selected stream
nobody validated.

So: `probe_media_metadata` is reused for FFprobe **execution, timeout and launch-error semantics
only**. Its classifier is not consulted. `select_renderer_equivalent_stream` re-reads the raw
`-show_streams` payload and validates the stream that will actually be decoded. Every verdict
records the absolute stream index, codec, geometry, duration, **duration provenance**
(`stream`/`container`, because those are not interchangeable) and a renderer-equivalence label.
No report may imply equivalence that was not established. Do not change `classify_probe` to fix
this; the two subjects are meant to differ.

Fail closed (`SAMPLE_STREAM_AMBIGUOUS`) only where the renderer's pick genuinely cannot be
determined: a video stream with no usable absolute index; two or more video streams where any is an
`attached_pic` (the docs give the resolution rule and the `V` specifier's exclusion, but not how
automatic selection ranks an attached picture); or two or more where any lacks usable geometry. **A
pixel-area tie is resolvable, not ambiguous** — lowest index wins, per the documented rule. A lone
stream that is undescribable is `SAMPLE_METADATA_UNUSABLE`, not ambiguity: "I cannot tell which
stream" and "the stream I must test is undescribable" are different operator problems.

### No frame-rate arithmetic. Ever.

An earlier design classified a short sample as end-of-file when `seek + frames/fps` exceeded the
reported duration. It is unsound three times over: `classify_probe` returns `METADATA_OK` **without
validating `fps`**, so the divisor can legitimately be `0`; `r_frame_rate` is a base/maximum rate
that for VFR sits far above the true average, biasing the comparison toward *claiming damage* on
healthy files; and for VFR `frames/fps` is not the time span of those frames at all. A test walks
the classifier AST and fails on any division whose right operand names a frame rate.

Underproduction is therefore **always** `SAMPLE_UNDERFILLED_UNVERIFIED` — inconclusive, never
"damaged", never "confirmed EOF" — and always reports observed *and* requested counts. `fps` is
report context only. The one surviving duration use is the division-free precondition
`seek >= duration → SAMPLE_SEEK_OUT_OF_RANGE`, a refusal rather than a verdict on the bytes.

### The argv, and why `frame=` is named "output frames"

`-frames:v` is documented as "stop writing to the stream after framecount frames" — a bound on
**output** frames. With `-ss` before `-i`, accurate-seek pre-roll frames are decoded but never
counted, so the counter *understates* decoder work. Hence `SAMPLE_OUTPUT_FRAMES_OK`: never describe
it as "frames decoded" or "frames verified".

`-fps_mode passthrough` is the one deliberate divergence from the renderer's `cfr` and is
load-bearing: under CFR, FFmpeg can reach the requested count by **duplicating a single good
frame**, which would let a damaged source pass. `-v error` means a clean run prints nothing, so a
non-empty stderr *is* the decoder-error signal — no pattern matching, and the bias is toward
withholding success. `-f null -` writes no file; no `-y` is needed. Forbidden: a filter chain, any
encoder argument, `-y`, `-xerror`, `-noaccurate_seek`, a second output-side seek, any retry. The
diagnostic is **not** the render: a clean sample says the decoder produced frames, never that the
production NVENC extraction would succeed.

### Status precedence

Timeout and non-zero exit are checked **before any frame count**, so partial progress that happens
to reach the requested total can never become a success. Then: a failed post-decode observation
(fails closed — claims no change, permits no success), a change observed during the decode, the
evidence tier (no terminal `progress=end`, missing/malformed count, a count *above* the request, or
an explicitly positive dup/drop), `frame=0`, underfill, stderr-at-exit-0, and finally the one
positive status. `change_window` and `stability` are **separate fields, never statuses**, so a
timeout that also saw the file move reports both facts instead of hiding one behind the other.

Missing `dup_frames`/`drop_frames` are **UNKNOWN**, never converted to zero and never read as proof
of zero; unknown does not block success (FFmpeg need not emit those keys, and requiring them would
make success unreachable rather than rigorous), while an explicitly positive one does.

### Bounds, and what is not claimed

30 output frames, FFprobe 20 s (S2-A's constant, reused not redefined), decode 30 s, 60 s
scheduling budget, one source, one worker. **Every one is a conservative policy choice, not a
measured optimum.** The budget gates *scheduling*: it is re-measured after the last pre-decode stat
and must leave room for the whole decode timeout, so no child starts that the budget could not let
finish. `os.stat`, Windows process creation, termination and post-timeout pipe draining all sit
outside it — **no hard end-to-end wall-clock bound is claimed anywhere.**

`lifecycle=None` is passed to `_run_media_command`, matching `probe_media_metadata`: a diagnostic is
not a render. That also preserves evidence — `subprocess.run` attaches the partial capture to
`TimeoutExpired`, whereas the lifecycle branch raises a bare `TimeoutExpired(cmd, timeout)` with no
output. Those attributes are documented as **always bytes when any output was captured, regardless
of `text=True`**, and may be `None`, so every parser entry point goes through
`coerce_stream_text`. No new runner, no new `RenderLifecycle`, no new cancellation authority; there
is no Cancel control in P1, and a browser disconnect does not necessarily interrupt the child.
`_run_media_command` captures stderr without a size limit — `-v error` makes large output unlikely
and the bounds cap how long a pathological file can write, but neither establishes a hard memory
bound. Do not claim otherwise.

**Portable-build acceptance of `-progress`, `-fps_mode passthrough` and `-f null -` is UNVERIFIED**
until a separately authorized real-FFmpeg integration test. P1 executes no FFmpeg at all.

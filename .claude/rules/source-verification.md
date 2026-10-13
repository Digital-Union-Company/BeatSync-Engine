---
paths:
  - "src/beatsync_fork/media_verify.py"
  - "src/ffmpeg_processing.py"
  - "tests/test_media_verify.py"
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
  test drives 1,200 sources and asserts peak concurrency.
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

### Render exclusion — reuse the mutex, do not add a second one

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

- The release is in a `finally`, so an exception or an abandoned request cannot leave the app
  permanently refusing renders. Tests cover success, `Exception` and `BaseException`.
- A running render is **never** signalled, throttled or terminated to make room for verification.
- `_verification_running` (guarded by its own small `_VERIFY_FLAG_LOCK`) exists **only** so
  `_busy_message()` can name the real holder. It is not an exclusion mechanism; the mutex is. It is
  set immediately after acquiring and cleared immediately before releasing, so a caller that has
  just failed to acquire attributes the lock correctly. The one-statement window between the two
  acquisitions can mislabel a refusal message and can never admit a second worker.
- Nothing is stored in `gr.State`: no lock, no thread, no process handle, no cancellation object.
  Verification owns no `RenderLifecycle` and never installs one.
- **Deliberately NOT extended to preparation Scan/Analyze.** Those are the operator's own
  long-running maintenance clicks in the same accordion; serializing them would be new behaviour
  for existing controls. Verification is bounded and read-only, so overlapping a Scan costs disk
  throughput only — unlike a render, which has a hard per-clip timeout that contention can break.

### Source stability

`fingerprint_source` brackets each probe with `(size, mtime_ns)`. A source that changed inside that
window is `MEDIA_CHANGED_DURING_CHECK` **whatever the probe said, including a clean read** — the
bytes FFprobe described are not the bytes now on disk, and the cache classification taken earlier
in the click describes neither with certainty. Nothing is deleted, rebuilt or re-analysed in
response. Like Stage-5's bounded fingerprint, this detects an overwrite and is **not** proof of
whole-file byte identity; neither is presented as one.

### What verification must never touch

- **No new cache key and no second completion rule.** The cache question goes through
  `video_analysis.classify_library_sources()`, which already owns `_video_signature`/`_cache_path`/
  `_load_cache`/`_cache_entry_is_complete`. `_verify_sources_impl` reaches none of them directly,
  and the classifier's single `_load_cache` stays the one authorized record-read point.
- **No cache write of any kind** — no record created, checkpointed, rewritten or deleted. No Qwen
  worker is launched to answer the cache question. No media is modified; the only OS call
  `media_verify` makes is the `os.stat` behind the fingerprint.
- **The live widgets are the input, never `prep_state`.** `_on_verify_sources_click` takes exactly
  `(folder_path, recursive)` and re-enumerates with `scan_library_folder`. Gradio delivers widget
  changes as separate queued events, so trusting a recorded scan would verify the *previous*
  library while the screen declared a different one — the same hazard the render gate and
  `_prep_analyze_impl` already take live values to avoid.
- **One output, its own.** `prep_verify_btn.click` writes `[prep_verify_report]` and nothing else —
  not `status_output`, `video_output`, `process_btn`, `source_state`, `render_invocation_state`,
  `director_proposal_state`, `variant_batch_state`, the render-outcome state, `prep_outputs` or any
  element of `source_outputs`. `prep_verify_report` is absent from `prep_outputs` and
  `source_outputs`, so verification can neither enable a render nor move the preparation state
  machine. `process_btn` stays controlled solely by the existing source-confirmation workflow.
- **Folder/recursive changes clear the report through their own `change` listeners**, not through
  `prep_outputs` — the preparation state machine's handling of those widgets is untouched. A stale
  report that looks current is a wrong answer, so the header always names the folder, the recursive
  setting and the verification time.
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

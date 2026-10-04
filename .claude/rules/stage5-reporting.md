---
paths:
  - "src/video_analysis.py"
  - "tests/test_stage5_reporting_truth.py"
  - "tests/test_qwen_scalar_boundary.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

## Stage 5 returns two different truths (R1)

`analyze_video_sources` returns **current-run execution facts** and **cached-library aggregates**, and
they must never be presented as the same thing. Production proved why: a fully warm run — 845/845
cache hits, **zero** sources re-analysed, **zero** Qwen workers launched, zero inference — printed
`visual workers: 1`, `Qwen performance: … 3.12 candidates/s` and `Qwen tags: 8704/8704 in 3031.9s`.
Every one of those numbers came from cached records. This is a reporting defect only; the cache and
the Qwen runtime behaved correctly throughout.

- **Library aggregates** (`qwen_tag_count`, `qwen_frame_count`, `qwen_seconds`,
  `qwen_inference_seconds`, `qwen_model_id`, `qwen_concurrency`, `qwen_peak_vram_gb`) sum or select
  over `videos`, which contains every cache hit. They are **historical by construction** and are
  preserved unchanged for compatibility. A warm library legitimately carries large Qwen counts and
  timings with no inference having happened this run.
- **Current-run facts** carry the `_this_run` suffix (`sources_analyzed_this_run`,
  `analysis_workers_used`, `qwen_jobs_this_run`, `qwen_completed_jobs_this_run`,
  `qwen_incomplete_jobs_this_run`, `qwen_requested_count_this_run`, `qwen_frame_count_this_run`,
  `qwen_tag_count_this_run`, `qwen_seconds_this_run`, `qwen_inference_seconds_this_run`). **Any
  claim about work performed or performance achieved must use these.**

**Requested, decoded and tagged are three different facts** and must not be conflated:
`qwen_requested_count_this_run` is what was *submitted*; `qwen_frame_count_this_run` is what the
worker *proved* it decoded; `qwen_tag_count_this_run` is what was *actually merged*. Only the first
is knowable without a usable worker response, which is what makes a failed attempt reportable at
all. **The UI's `N/M tags` denominator is the requested count**, never the decoded count — a job
that requested 10 and decoded 8 must not render as a flawless `8/8`. Current-run decoded frames are
counted only when the worker reported a real integer: the persisted `timings["qwen_frame_count"]`
falls back to the requested count for source-record compatibility, and **that fallback must never
leak into current-run truth** (the legacy field keeps it unchanged).

Load-bearing details:

- **Current-run counts come from `_new_run_stats()`**, an invocation-scoped dict threaded only into
  the paths that analyse an *uncached* source. It is never populated from a cached record, so a cache
  hit cannot inflate it, and a second call in the same process starts from zero. It is ephemeral
  top-level metadata: a separate object from `video_data`, so there is no path by which it reaches
  `_checkpoint_cache`. **No cache field, no cache identity, no contract bump.**
- **Batch accounting is two-phase, and submission truth comes first.** `_complete_deferred_qwen_batch`
  records jobs, requested candidates and the shared-worker wall time **immediately after
  `_run_qwen_worker_batch` returns — before the empty-response branch**. A worker that timed out,
  exited non-zero or produced an unreadable response still consumed a real attempt on real sources;
  recording only in the per-job merge loop reported `0 jobs`, which the UI rendered as "no inference
  this run". Every submitted job starts *incomplete* and is promoted only by its own returned
  evidence, so an empty response leaves them all incomplete with no extra bookkeeping. The per-job
  loop therefore must **not** increment `qwen_jobs` — that would double-count every success.
- **Current-run wall time counts each worker invocation once.** `qwen_seconds_this_run` adds the one
  measured `batch_seconds` for a shared batch, and the one measured call duration for a
  single/inline invocation. Never sum the amortized per-source figures — they scale with source
  count and would inflate the total. Worker-reported inference time is added only where it is
  present and numeric; missing timing is absence of evidence, so it contributes zero and is never
  invented. Accounting is observability and must stay non-fatal: it may not introduce an exception
  on malformed timing data that the runtime would otherwise survive.
- **A Qwen job is counted only where a request is genuinely issued**, which is why
  `qwen_jobs_this_run` is *not* `len(deferred_jobs)`. That would be wrong in both directions: the
  serial path (`_analyze_single_video` with `defer_ai=False`) runs Qwen **inline** and never appears
  in `deferred_jobs`, while a deferred source whose candidate list is empty passes through the batch
  orchestration and never reaches the worker. The counters therefore live at the two places that
  actually submit work — after `_run_qwen_worker` in the facade, and in the batch's per-job merge
  loop, which only runs for sources that made it into `request_jobs`.
- **Both recording sites are written inline rather than through a shared helper.**
  `_annotate_candidates_with_qwen` and `_complete_deferred_qwen_batch` are AST-extracted and executed
  by `tests/test_stage5_cache_completion.py`, so they must stay self-contained with respect to
  helpers outside that suite's extraction list. Do not "tidy" this into a module-level function
  without also updating that suite.
- **A job that ran but did not complete is still a job.** It is counted and separately tallied as
  incomplete, so a failed pass can neither be reported as success nor silently vanish. On a failure
  path where elapsed time is not provable, the UI **omits** the timing rather than understating it.
- **Zero uncached jobs means zero analysis workers used.** `_video_analysis_workers` keeps its
  `video_count <= 1 → 1` contract untouched — the genuine single-source case needs it — and the call
  site supplies the truth (`… if jobs else 0`). The analysis block is guarded by `if jobs:`, so
  nothing is ever submitted when the library is fully warm.
- **The Stage-5 START event is pre-cache-classification.** It fires before the cache scan, so it
  cannot know how many sources need analysing; it says `Checking N source video(s)`. The post-scan
  metric (`H cached, J to analyze, W worker(s)`) remains the authority on real work.
- **The five-line CMD budget is not the bug and must not be raised.** `_stage5_summary` is ordered so
  current-run truth wins: sources/cache/analysed → current-run Qwen status → `Analysis time …,
  cache H/N` → library summary → optional *explicitly labelled* cached metadata. The authoritative
  `Analysis time` line used to be the one dropped; a test now pins that it survives.

Not fixed here, and deliberately out of scope: production measured **~69 s** inside Stage 5 on that
warm run (`total_elapsed` brackets the whole `analyze_video_sources` body and flows to the END
event's `elapsed_seconds`), while later profiling — run after the same ~2.52 GB of bounded
fingerprint windows were already in the OS cache — measured **~3.2–3.4 s**. That discrepancy is
**unresolved**. This work fixes reporting truth, not Stage-5 performance.

### What L1B measured about that discrepancy — and what it did not

L1B (bounded-parallel source identity) produced much stronger evidence on a larger real library, and
it is recorded here exactly as measured. **The historical ~69 s run was not reproduced**, so the
discrepancy above is **not** declared resolved.

Measured on the real Windows library `J:\New folder\Cuts`, **1672 sources**, ~**4.75 GiB** of bounded
fingerprint bytes:

| measurement | figure |
|---|---|
| first-touch / current-state **serial** identity | **~103.8–110.5 s** |
| immediate **warm serial** identity | ~6.5–10.5 s |
| warm **16-thread** identity benchmark | **~0.746 s** median (8.77x vs the warm serial benchmark baseline of 6.545 s) |
| controlled **cold parallel** speed-up | **NOT MEASURED** |

Three things follow, and the boundaries between them matter:

- **Identity I/O now strongly explains the *shape* of the historical discrepancy.** A cold, serial
  identity pass over a real library costs ~100 s at 1672 sources; a warm one costs single-digit
  seconds. That is the same order of magnitude, and the same direction, as ~69 s versus ~3.2–3.4 s.
- **It is not the same run, and it is not a reproduction.** The ~69 s figure came from an 845-source
  library through a full `analyze_video_sources()` call. The current real library had **417 cache
  misses**, so a full warm `analyze_video_sources()` was correctly **not** run — doing so would have
  launched real Qwen work and changed the library under measurement. The exact historical full-call
  number therefore remains unreproduced, and nothing here retcons it.
- **The 8.77x is a WARM benchmark speed-up of the identity phase only.** It is not a cold-start
  speed-up, not a Stage-5 speed-up, and not a measurement of the ~103.8–110.5 s first-touch case
  under parallelism. The first-touch serial figure is evidence of **potential** user value, nothing
  more. **Do not claim a cold parallel improvement**, a production Stage-5 improvement, or a
  first-touch improvement, until one is measured on Windows against the exact candidate; the
  pre-implementation benchmark is design authority, not post-implementation acceptance.

Identity parity under parallelism *is* fully measured: **20,064 comparisons, 0 mismatches, 0 `None`
results, 0 missing results, 0 duplicate results.** The contract is
`.claude/rules/stage5-cache-identity.md`; the telemetry definition change
(`cache_identity_seconds` is now wall-clock phase latency, not a serial sum) is
`.claude/rules/scale-diagnostics.md`.

## Counts and optional telemetry have different trust contracts (T1)

Everything Stage 5 reads out of a Qwen response arrived as parsed JSON from a subprocess, and
everything it reads out of a cache record arrived off disk. Those values split into two kinds, and
conflating them is what produced a class of crashes:

- **Counts are load-bearing.** `frame_count`/`tag_count` decide semantic completion, so `_is_count`,
  `_coerce_count` and `_reported_count` keep their D1 semantics **frozen**. Do not redefine them.
- **Durations, VRAM, batch size and the model id are optional telemetry** — pure observability.
  `_qwen_job_completed`, `_stored_ai_cache_is_consistent` and `_cache_entry_is_complete` never read
  them, and a test asserts those bodies reference neither the fields nor the telemetry helpers.

**Semantic completion must never depend on optional telemetry.** 10 requested / 10 decoded / 10
returned for exactly the requested ids is a *complete* job even if `inference_seconds` is `"bad"`. The
old code got this wrong in two different ways: the shared-batch call site is unguarded, so a malformed
duration aborted Stage 5 outright; the inline facade *is* wrapped in `except Exception`, so a malformed
`batch_size` or `peak_vram_gb` silently produced `ai_enabled=False` — permanent re-analysis of a fully
tagged source, caused by a field no completion rule reads.

**`NaN`/`Infinity` are a real hazard here, not a hypothetical.** `json.loads`/`json.load` accept the
bare `NaN`, `Infinity`, `-Infinity` tokens (Python's default non-standard `parse_constant`) and
`json.dump` emits them (`allow_nan=True` by default). So a non-finite value survives the worker
response file *and* a full cache round trip, `float()` will not reject it, and one of them makes every
sum it enters non-finite silently and permanently. Two tests demonstrate this rather than assert it.
Do not "simplify" that away, and do not try to fix this by turning `allow_nan` off in `_save_cache` —
the writer is a transport primitive, not the semantic-policy layer.

The telemetry seam (`_is_real_number`, `_optional_telemetry_number`, `_telemetry_seconds`,
`_telemetry_total`, `_is_nonnegative_count`, `_bounded_count`, `_telemetry_text`, `_as_mapping`,
`_record_telemetry`, `_record_candidate_count`) is stdlib-only and deliberately field-specific rather
than one blind coercer. Load-bearing details:

- **An accepted telemetry number is a real `int`/`float`, not a `bool`, finite and `>= 0`.** A numeric
  string is **not** accepted — `float("2.5")` succeeding is no reason to believe a string in a numeric
  field, and laundering it hides a broken producer. `int(2.7)` → 2 and `int(True)` → 1 were exactly
  that kind of fabrication for `batch_size`. Negatives are rejected because every field here is
  physically non-negative *and* because `_fmt_seconds` already clamps display with `max(0.0, …)`, so a
  negative stayed invisible on screen while corrupting the total.
- **`None` means unknown and is not the same as `0.0`.** An unprovable `peak_vram_gb` persists as
  `None`, because the worker reports a literal `0.0` — coercing malformed to `0.0` would make a corrupt
  record indistinguishable from every healthy one in the library. A genuine `0` stays `0` everywhere;
  never write `value or default` on a field where zero is a measurement.
- **No aggregate may be non-finite, including from individually finite parts.** Enough finite values
  overflow a running total, so `_telemetry_total` checks the accumulator and degrades to a neutral
  `0.0` rather than reporting `inf`. Never fabricate a plausible measurement to keep a number finite.
- **Validating the parts is necessary but not sufficient: *every* telemetry sum goes through
  `_telemetry_total`.** R1 validated each scalar and made only the final library aggregate
  overflow-safe, which left two earlier sums on raw floating-point addition — and `1e308 + 1e308` is
  `inf` from two values that each passed finite-and-non-negative. Measured on the R1 head: the
  per-job `prefetch + inference + amortized_model` wrote **`"qwen_seconds": Infinity` into a
  checkpointed record whose semantics were complete**, so the source stayed reusable while carrying
  exactly the value the contract excludes; and `qwen_inference_seconds_this_run` reached `inf` from
  two jobs in the batch path and from two successive calls in the inline path. So the rule is about
  the *operation*, not just its inputs: any place telemetry quantities are combined — a per-job sum
  or a running `run_stats` total — uses the shared aggregator, never `+` or `+=`. The parent-measured
  `run_stats["qwen_seconds"]` accumulations were routed through it too; they are `perf_counter`
  deltas and were never at risk, but the invariant is then structural rather than resting on an
  argument about how large a monotonic-clock delta can get. A test walks both orchestration bodies
  and fails on any augmented assignment to a float telemetry key, because a *future* site is the
  failure mode that got through R1's own review. **Do not add a second summation helper** — reuse
  `_telemetry_total`; a test asserts it is the only one.
- **Counts respect their natural bound.** `_is_count` admits negatives (`_is_count(-5)` is `True`),
  which is how a worker-reported `frame_count: -5` reached `qwen_frame_count_this_run`; the sign and
  bound checks therefore live in `_bounded_count`, not in `_is_count`. Current-run decoded frames must
  be `<= ` the candidates that job submitted, and library totals are bounded by the record's own
  candidate count, so a candidate-less record with hand-edited huge counts cannot inflate them.
- **There is deliberately no wall-time ceiling on durations.** Bounding a worker-reported duration by
  the parent's measurement of the call makes the value depend on how long the surrounding call happened
  to take, so a stubbed or replayed worker — the only way this code is testable without a GPU — has
  every legitimate duration rejected. Finiteness and sign are what make the aggregate safe; the ceiling
  added nothing there and cost testability. Counts keep a bound because they have a real one in scope.
- **Nested containers are untrusted; the top-level response is not re-checked.**
  `beatsync_fork.qwen_progress` already proves the loaded payload is an object — do not duplicate that.
  A truthy non-dict `semantics_by_job` used to pass the empty-response branch and then raise
  `AttributeError` on `.get`; it now degrades to the same outcome as no usable semantics (job attempted,
  job incomplete, nothing invented). `_as_mapping` is safe at load-bearing containers too, and that is a
  property rather than luck: collapsing a non-dict to `{}` can only make a completion check *fail*.
- **The library aggregation reads cache hits, so it must be tolerant rather than strict.** On a warm run
  every value in that block comes off disk. A record can pass `_cache_entry_is_complete` *and*
  `_stored_ai_cache_is_consistent` while carrying malformed optional telemetry, stay reusable — so
  nothing recomputes it — and crash or poison the aggregate on every later warm run. The fix is tolerant
  reading, **not** a stricter loader: old cache files are never rewritten and never migrated, and a
  valid entry round-trips byte-identically. Malformed-record *order* must not change the outcome either;
  the old `next(...)` converted only up to the first truthy record, so whether Stage 5 crashed depended
  on where a malformed source sorted. (Which *valid* `model_id` wins is still first-come and legitimately
  order-sensitive.)
- **Sanitise at ingestion, before a record is written.** Never by rejecting the whole record in
  `_save_cache`, and never by changing `json.dump(allow_nan=…)`.
- **This is robustness at a trust boundary, not a claim about the shipping worker.** It emits
  `perf_counter` deltas, `max(1, min(32, int(slots)))` and a literal `0.0`, so it cannot produce these
  shapes; `stage5_qwen_scene_worker.py` was **not** modified and producer-side validation was shown not
  to be required. The exposure is the retained request/response and cache JSON under
  `input/video_analysis_cache/`, a future worker change, or a regression. Concurrency gets
  non-negative-integer robustness only, because its real ceiling (`min(32, …)`) lives in the worker and
  restating it here would be a drifting magic number.
- **No cache-contract or analysis-version bump *for T1*.** T1 changed neither semantic meaning,
  candidate scoring, the candidate schema, cache identity nor the completion contract, and tolerant
  reading never contradicts a stored value. (The contract constant later moved to `stage5_cache_v3`
  for an unrelated reason — P2's media-neutral prompt; `auto_av_analysis_v8_llama_vulkan_batched` is
  still unchanged.)
- **The extraction lists are part of this contract.** `_complete_deferred_qwen_batch` and
  `_annotate_candidates_with_qwen` are AST-extracted and *executed* by
  `tests/test_stage5_reporting_truth.py` and `tests/test_stage5_cache_completion.py`. Any module-level
  helper those bodies call must be added to both suites' extraction tuples, or they fail with
  `NameError` — that coupling is the price of testing the real bodies on a bare interpreter, and it is
  the reason those two suites list helper names explicitly.

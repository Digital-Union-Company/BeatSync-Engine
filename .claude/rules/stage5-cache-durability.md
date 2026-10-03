---
paths:
  - "src/video_analysis.py"
  - "tests/test_stage5_cache_durability.py"
  - "tests/test_stage5_cache_completion.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

## Durability invariants (D1)

Before D1 the only save site was a terminal loop at the end of `analyze_video_sources`, so an
interruption *anywhere* earlier discarded every newly analysed source — measured: 3 sources and all
their Qwen tags completed, **0** durable cache entries. The rules that replaced it:

- **Every completion point checks checkpoint eligibility**, rather than Stage 5 saving once at the
  end. `_checkpoint_cache` is called after each serial `_analyze_single_video` (inline AI), after
  each parallel deterministic result, after `_complete_deferred_qwen`, and after **each per-job
  merge** inside `_complete_deferred_qwen_batch`; the terminal loop survives only as a backstop. A
  *call* is not a write — the completion rule decides, so what each shape actually persists is:

  | shape | at that point | durable? |
  |---|---|---|
  | non-AI parallel or serial | complete on arrival | **yes, immediately** |
  | AI-deferred parallel result | `ai_deferred=True` | **no** — only after its Qwen result completes |
  | candidate-less with scoring evidence | no Qwen work exists | **yes** (the explicit exception) |
  | shared Qwen batch, per job | after the worker's final response returns | **yes, per job** |

  So a failing or missing sibling job, and a parent interruption during the post-response merge loop,
  cannot discard jobs already written. **While the shared worker is still in flight its per-job
  results are not durable at all** — they exist only inside that process until its final response is
  written, and the streamed progress channel carries no semantic result authority. Closing that gap
  would need a two-phase deterministic-only record, which D1 deliberately does not introduce.
- **`_cache_entry_is_complete()` is the single completion rule.** Never re-answer "is this reusable?"
  anywhere else — the scattered version is exactly how a failed Qwen run became a permanent
  AI-complete hit. It rejects a non-dict payload, a wrong `analysis_version`, a missing/non-string
  `video_file`, non-list `candidates`, and **anything with `ai_deferred` truthy**; under
  `require_ai` it additionally demands `ai_enabled` unless there are no candidates *and* the
  deterministic scoring pass is shown to have run (see the candidate-less rule below).
- **`_checkpoint_cache()` is the only thing that may start a write.** It consults the rule first, so
  checkpointing early can never publish a deferred or failed-AI record. `analyze_video_sources`
  must not call `_save_cache` directly; a test asserts that.
- **`ai_enabled` is a fact, not a convenience.** Set it `True` only when Qwen genuinely completed —
  and that applies to **every** path: the deferred single, the deferred batch, *and* the serial
  inline one. The facade reports completion through the private `_QWEN_COMPLETED_KEY`, which each
  caller **pops before `timings.update(...)`** so it never reaches a cached payload. Never restate
  the request as the result: `ai_enabled = enable_ai and not defer_ai` was exactly the R2 defect —
  the inline path reported AI-complete for a Qwen run that had failed, timed out or been skipped.
- **A submitted Qwen job completed only if every *requested* candidate came back tagged.**
  `_qwen_job_completed(timing, envelope_present, requested_ids, returned_ids)` is the one rule, shared
  by the single and batch paths. It requires **all** of: the expected per-job envelope exists;
  `timing` is a dict; `frame_count` and `tag_count` are real integer counts; the requested set is
  non-empty; `frame_count == len(requested_ids)`; `tag_count == frame_count`; and
  `returned_ids == requested_ids`. `requested_ids` is what `_select_ai_candidates` actually submitted,
  not every deterministic candidate.

  Three weaker definitions were tried here first, and each is worth remembering:

  1. **emptiness of `semantics`** — conflated a finished worker with a dead one.
  2. **envelope membership alone** — `_run_semantics_for_video` always returns a timings dict and
     `main` always records it under the job id, so the envelope only proves *the job loop returned*.
     Inside it, `_normalize_semantic` returns `{}` for any candidate whose semantic content is invalid
     (missing numeric key, disallowed emotion/use, empty description), `_run_inference_wave`
     classifies `{}` as **failed** and retries it (server retry → reduced-slot restart → serial
     fallback), and a candidate still failing is simply **absent** from the returned semantics.
  3. **`tag_count == frame_count`** — proves every *decoded* frame was tagged, but
     `_prefetch_candidate_frames` returns only frames it could read
     (`ready = [p for p in plans if p["image"] is not None]`), so `frame_count` can be **smaller than
     the requested set**. requested 10 / decoded 8 / tagged 8 looked complete while two candidates had
     no semantics at all.

  So **no single signal is sufficient**: not the envelope, not a count, not tag presence. The ids
  matter too — a foreign semantic id is not evidence that one of *our* candidates completed, and an
  extra id means the response does not match the request, so the match is exact. Tags that *did*
  arrive are still merged; only the verdict changes, so the next run retries instead of inheriting a
  silent gap. A globally non-empty batch response is still no proof that *this* job ran, and one
  failing job never fails its siblings.

  Worker counts are read defensively because the response is parsed JSON from a subprocess:
  `_coerce_count` stops a non-numeric count raising `ValueError` out of the whole analysis, `_is_count`
  rejects `bool` (which subclasses `int`, so `frame_count: true` would otherwise have compared equal
  to 1), and `_reported_count` uses **presence** rather than truthiness — `timing.get(k) or default`
  silently rewrote a genuine `frame_count = 0` into the requested candidate count in stored timings.
- **A *stored* AI record must also be self-consistent, not just flagged.**
  `_stored_ai_cache_is_consistent()` runs whenever `require_ai` reuse depends on `ai_enabled`, and it
  is deliberately **not** the live rule: `_qwen_job_completed` needs `requested_ids`, which legacy
  payloads never stored (nor the `BEATSYNC_QWEN_MAX_WINDOWS` value in force), so replaying it against
  an old record would mean inventing evidence. **Never call `_qwen_job_completed` from the loader.**

  It asks only whether the persisted fields contradict each other: `timings` is a dict with real
  integer `qwen_frame_count`/`qwen_tag_count` (not bools), `frame_count > 0`,
  `tag_count == frame_count`, `frame_count <= len(candidates)`, candidate ids are usable strings, and
  the `ai_analyzed` ids are unique, a subset of the candidate ids, and number exactly `tag_count`.

  It deliberately does **not** require `frame_count == len(candidates)` — a smaller value is the
  normal result of `_select_ai_candidates` limiting the submitted set.

  This exists because a read-only audit of the real 2196-entry cache found **4** records claiming
  `ai_enabled=True` with `qwen_frame_count=10`, `qwen_tag_count=9` and 9 `ai_analyzed` candidates —
  exactly the false-complete shape D1 exists to prevent — which the pre-R6 loader accepted because it
  only checked `bool(data["ai_enabled"])`. Measured effect *in the D1-era cache*: 2192 of 2196 accepted,
  those 4 rejected and naturally recomputed on next encounter. Two further records (398 candidates/114
  tagged, 525/119) were internally coherent but historically unverifiable, and remained reusable under
  the D1 loader pending the D2 decision — which the D2 generation transition has since settled by
  orphaning them. **No runtime cache file was ever edited** — the audit and the verification are
  read-only, and rejection simply becomes an ordinary cache miss.
- **A candidate-less source is complete only if the deterministic pass actually ran.** Two very
  different outcomes both end with `candidates == []`: a source whose windows yielded no usable
  moments, and a source OpenCV could not open (`"Warning: OpenCV could not open …; candidate
  analysis skipped."`). `_deterministic_analysis_completed()` tells them apart using existing
  durable evidence — `timings["candidate_scoring_seconds"]`, written once immediately after
  `_measure_windows` inside the `cap.isOpened()` branch and nowhere else. The genuine case is
  reusable (with `ai_enabled` left honestly `False`, not falsified); the open failure is not cached
  at all, so the source is retried. An empty candidate list **alone** is not evidence of success —
  D1 accepted it and would have retired a readable source permanently on one transient decode
  failure. This check applies under both `require_ai` modes.
- **`BEATSYNC_QWEN_MAX_WINDOWS=0` means no Qwen work was completed**, so `ai_enabled` is `False` and
  no AI-keyed checkpoint is written. That is deliberate: `QWEN_MAX_WINDOWS` is *not* part of cache
  identity, so caching a knowingly Qwen-less record under the AI model key would poison it for a
  later run that does want tags. The supported way to cache deterministic-only results is
  `BEATSYNC_DISABLE_QWEN=1`, which `auto_mode/__init__.py` turns into `enable_ai=False` and which
  therefore produces the separate `no_ai` cache identity.
- **The writer publishes through a unique same-directory temp**
  (`tempfile.mkstemp(prefix=<name>., suffix=.tmp, dir=<cache dir>)`), `flush` + `os.fsync`, then
  `os.replace`, with best-effort temp cleanup in `finally`. The old shared `path + ".tmp"` let two
  processes collide: measured, one writer's `os.replace` published the *other* writer's payload
  while reporting success, and the loser failed with `FileNotFoundError` into a warning
  `QuietConsole` discards. Never reintroduce a temp name derived from the final path.
- **`PROCESS_CRASH_ATOMICITY` is provided** (a reader sees the old entry or the new one, never a
  partial final file — verified at all four boundaries). **`POWER_LOSS_DURABILITY` is not claimed**:
  the temp is fsynced, the containing directory is not. Do not upgrade that claim without testing it.
- **Historical D1-era state is no longer authoritative.** The D2 identity contract supersedes it;
  the D1-era figures and the pre-D2 `int(st_mtime)` identity are recorded in
  `docs/claude/stage5-cache-history.md`. Current identity behaviour is
  `.claude/rules/stage5-cache-identity.md`.
- **Never run a destructive cache test against the real runtime cache.** Mutation tests belong in
  `C:\tmp\BeatSync-Engine-DigitalUnion\tasks\...`; the runtime cache is read-only for study work.

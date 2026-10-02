---
paths:
  - "src/beatsync_fork/library_prep.py"
  - "src/video_analysis.py"
  - "tests/test_library_preparation.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

## Media Library Preparation (P V1 + P2)

Preparing a library means running the Stage-5 work for new/changed sources **before** a render, so
the render finds a warm cache. `video_analysis.classify_library_sources()` answers "which sources
already have reusable cache?" and `beatsync_fork/library_prep.py` holds the state and the report.

**It prepares the library, full stop — not a track and not an edit style.** P V1 required an audio
file and derived `smart_preset` from it, because that preset was the one `audio_profile` field
reaching the cache key. P2 made persisted semantics media-neutral, so preparation is now **trackless**:
no audio control, no Stage 1–4 pass during a scan, no `TrackIdentity`, no stored profile. One
preparation serves every track, every preset and every creative seed. The report says
`Semantic mode: media-neutral`; do not reword anything here to imply track or style dependence.

That removal is also where the measured **~15–20 s** per-scan track-profile component went. A scan
now costs folder enumeration + source identity (the D2 bounded fingerprints) + cache-record lookups.

- **`classify_library_sources` reuses the production primitives and adds no key formula.** Identity
  is `_cache_path`/`_video_signature`; reuse is `_load_cache`, i.e. the one completion rule. It
  splits a miss three ways purely for wording — key file absent → `new_or_changed`, key file present
  but rejected → `incomplete_or_invalid`, `_cache_path is None` → `source_identity_unavailable`. A
  brand-new file and a changed same-path file are deliberately **one** label: path+content identity
  cannot tell them apart, and inventing the distinction would need a content-addressed index.
- **It mirrors the orchestrator's once-per-invocation identity block rather than extracting it.**
  `tests/test_stage5_cache_identity.py` asserts that structure *inside* `analyze_video_sources`, and
  the expensive part (fingerprints, key formula) is shared through the helpers regardless. Do not
  "DRY" the six-line pattern without rewriting those assertions.
- **Unprovable backend identity blocks preparation entirely.** When AI is available but
  `_qwen_backend_signature_token` returns `None`, Stage 5 runs with caching off — a preparation run
  would then spend GPU hours and persist nothing, and the next scan would show the same counts. The
  classifier skips per-source work (no verdict could exist) and Analyze stays disabled. This is
  *narrower* than "no AI": a legitimately AI-disabled run uses the existing `no_ai` identity and
  classifies normally.
- **Scan is read-only with respect to cache *records*.** It creates the cache directory, because
  `_cache_path` always has; it never writes, updates or checkpoints a record. Do not "fix"
  `_cache_path`'s `os.makedirs` to make the claim tidier.
- **Scan classifies the whole library once; Analyze sends only `subset_for_analysis()`.** On the
  measured 902-source library that is 4 paths, not 902. Analyze then calls the *existing*
  `analyze_video_sources`, so all persistence stays with `_checkpoint_cache`; preparation never
  calls `_analyze_single_video`, `_checkpoint_cache` or `_save_cache` itself. Per-source staleness
  between the two clicks is deliberately unchecked — the analyzer re-derives each selected source's
  own identity anyway.
- **What invalidates a scan:** the folder, the recursive flag, and — rechecked at Analyze time — the
  backend token, the config token and the effective Qwen mode. That is the whole list: `config_token`
  covers the three Qwen env knobs, and after P2 there is nothing else in identity for a preparation
  to bind. The P V1 track check (path/size/`mtime_ns` plus the derived preset) is **retired**, and a
  test asserts `analyze_refusal` acquired no substitute for it. **Analyze batch size is deliberately
  not on that list** — see the bounded-batch section below.
- **Analyze takes the live preparation controls, not just `gr.State`.** Gradio delivers widget
  changes as separate queued events, so a user can retarget the folder or the recursive flag and click
  Analyze before the `change` handler has run — which would analyse the *previous* library's
  classification while the screen declared something else. `declaration_refusal` compares the live
  declaration against the recorded scan **first**, before the runtime identity is recomputed and
  before anything is analysed; a mismatch drops the scan and asks for a rescan rather than silently
  re-targeting it. It is practical equality only (normalised folder, exact `recursive`) and stats
  nothing. Never reduce `prep_analyze_btn`'s inputs back to `prep_state` alone; a test pins the click
  inputs against the handler's parameter order.
- **Strictly separate from the Create Video gate.** Its own `gr.State`, its own `prep_outputs`, and
  no overlap with `source_state` / `source_outputs` / `confirm_action` / `process_btn`. Local folder
  only: browser uploads live under `input/gradio_uploads/`, which `cleanup_on_startup` clears, so a
  path-keyed preparation of them would be worthless.
- **Nothing new in identity.** Preparation adds no field to any cache payload and no input to any
  key; a test asserts the identity and completion functions never mention it. (P2's `v2 → v3`
  contract bump is about the Qwen prompt, not about preparation.)
- **Progress:** a trackless scan has no Stages 1–4 to report, so it shows only Stage 0 under the
  `library_classify` phase; Analyze uses the existing Stage 5 events. Do not fake the removed stages.

### Analyze submits one bounded batch

Scan still classifies the **whole** library — that is the P.1 rule and it is unchanged. What one
Analyze click submits is bounded by `library_prep.DEFAULT_ANALYZE_BATCH_SIZE` (**100**), exposed as an
`Analyze batch size` number box and taken as a prefix of the already-frozen
`subset_for_analysis()` ordering via `subset_for_analysis_batch(limit)`.

**The reason is a property of Stage 5's shared worker, not a deficiency of the cache.** Stage 5
batches multiple videos into one Qwen worker process, and that worker writes its response JSON only
after its **entire** job loop finishes — so the parent can checkpoint individual completed records
only once the whole batch returns (this is the same D1 boundary already documented above: "while the
shared worker is still in flight its per-job results are not durable at all"). Submitting a cold
1107-source library therefore exposed all of it to a single all-or-nothing worker invocation.
Bounding the submission bounds that exposure.

Two claims this explicitly does **not** make:

- **It does not make an in-flight worker resumable.** If a batch's worker dies before producing its
  response, that batch may still need repeating; the bound only limits what that costs. The worker
  was **not** modified — incremental worker responses remain possible future work, and a test asserts
  no such machinery was invented here.
- **It is not a library cap.** A 5000-source scan still reports 5000 needing analysis; the batch size
  only decides how many of them one click submits. `normalize_batch_size` therefore has **no upper
  bound** and the `gr.Number` carries no `maximum`. Tests pin both.

Load-bearing details:

- **Batch size is execution policy, never classification identity.** It is absent from
  `LivePrepDeclaration`, from `PrepScanResult` and from `RuntimeIdentity`, so changing it never
  invalidates a scan: `set_batch_size` is the one preparation transition that does **not** route
  through `_invalidated`, and the retuned state keeps the *same* scan object. Folder and recursive
  still invalidate. That asymmetry is the whole design — a scan valid at 100 is exactly as valid at
  50, and re-fingerprinting 1107 sources to act on a different bound would be pure waste.
- **Keeping the scan is not the same as keeping its rendered text.** The report quotes the batch size
  (`Analyze batch: 100 per run`, `This run submits the next 100`), so `set_batch_size` re-renders it
  from the scan already in hand; otherwise the screen contradicted itself — widget 50, button
  "Analyze next 50", report still claiming 100. That was only ever a *reporting* bug, since the
  handler always used the live value, but a report disagreeing with the button is what a user
  believes. Re-rendering reads only counts already recorded in the scan: no filesystem access, no
  classification, no identity probe. With **no** scan recorded the existing text is preserved
  verbatim, because it is then the intro, a failure message or a finished batch's summary — none of
  which a batch-size change may overwrite.
- **Analyze reads the live widget, Scan does not receive it at all.** `prep_analyze_btn` inputs are
  `[prep_folder, prep_recursive, prep_batch_size, prep_state]` and a test pins that against
  `_on_prep_analyze_click`'s parameter order; `prep_scan_btn` keeps `[prep_folder, prep_recursive,
  prep_state]`, because how much of a classification one click later consumes cannot affect how any
  source was classified. A live batch size differing from the stored one is **not** a stale scan and
  must never be refused as one.
- **`normalize_batch_size` mirrors `variation.normalize_seed`.** `bool` rejected first (it subclasses
  `int`), whole positive ints and whole positive floats accepted, plain decimal strings accepted, and
  **fractional values fall back to the default rather than being floored** — `100.5` is not a request
  for 100, and truncating would submit a batch the user never chose. Nothing here may raise mid-run.
- **After a batch the scan is dropped, exactly as before.** Its counts describe the library as it was
  before the run. The summary reports `Submitted this batch` and `Sources analyzed this batch` and
  then asks for a re-scan; it **never** prints `old outstanding − N` as a remaining count, because the
  library can also change on disk between clicks. A test asserts `summarize_analysis_run` contains no
  subtraction at all. Preparation never silently re-scans 1107 sources on the user's behalf.
- **No cache change whatsoever.** `CACHE_CONTRACT_VERSION` and `ANALYSIS_VERSION` are untouched, no
  cache payload gained a field, and `video_analysis.py` was **not modified by this feature at all** —
  a test asserts the batch names appear nowhere in it. Persistence stays entirely with the existing
  `analyze_video_sources` → `_checkpoint_cache` path; bounding the input adds no write path.

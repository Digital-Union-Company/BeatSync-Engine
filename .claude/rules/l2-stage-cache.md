---
paths:
  - src/beatsync_fork/stage_cache.py
  - src/auto_mode/__init__.py
  - tests/test_stage_cache.py
  - tests/test_l2_stage3_cache_identity.py
---

> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

# L2 stage caching, V1: the process-local post-Stage-3 artifact

`beatsync_fork/stage_cache.py` holds **one** most-recent entry, in **this process only**, carrying
the five facts the pipeline still needs after Stage 3. `analyze_beats_auto` looks it up once, before
the expensive front end runs, and publishes once, after Stage 3 has succeeded.

```
HIT   ->  skip librosa.load, librosa.util.normalize, librosa.effects.hpss,
          detect_master_beat_grid, analyze_wave_features, analyze_sections
          then continue into the existing Stage-4 code

MISS  ->  run the pre-L2 body verbatim, then publish
```

## What is cached, and what is deliberately not

```
CACHED      audio_duration   beat_times   tempo   features   sections

NOT CACHED  y   y_harmonic   y_percussive   beat_frames   onset_env
            selected_beats   selection_info   audio_visual_profile
            video_analysis   the Stage-6 plan   the render output
```

The raw front-end arrays are **not needed after Stage 3**, so they must not be retained merely
because they were expensive to compute — that would hold megabytes of audio per entry for no reuse
at all. `STAGE3_BUNDLE_FIELDS` is the whole contract, and a bundle missing any one of the five
degrades to an ordinary miss rather than entering the pipeline as half a result.

## The measured boundary

Medians on the real Windows track (`Nero - Satisfy.mp3`):

| step | measured |
|---|---|
| audio load | 0.502 s |
| normalize | 0.001 s |
| HPSS | **12.453 s** |
| Stage 1 | 0.487 s |
| Stage 2 | 0.993 s |
| Stage 3 | 1.299 s |
| **reusable total (front end + Stages 1-3)** | **~15.735 s** |
| Stage 4 | **0.0076 s** |
| Stage 6 | 2.930 s |

The post-Stage-3 artifact serialises to ~**344 KB**.

**Stage 4 is uncached on measured grounds, not from caution.** ~7.6 ms does not pay for the extra
key, the extra correctness surface or the resolved-Freestyle key design it would need. **Stage 6 is
uncached because every target scenario changes something it reads** — 2.93 s is real, but a Stage-6
key would have to cover the seed and all six creative controls, which is the whole point of the
scenarios this cache exists for.

## One entry, process-local, no persistence

The measured user-value scenarios are **same-track repeated renders** and **candidate 2 of a
sequential C3 render batch**. One entry captures both without inventing a multi-track lifetime
policy. So there is deliberately:

- no LRU and no configurable cache size;
- no unbounded dictionary of historical keys (`Stage3ProcessCache.__slots__` is literally
  `_lock`, `_key`, `_value`);
- no cache directory, no cache JSON, no pickle artifact — `stage_cache.py` contains exactly **one**
  `open()`, and it is `"rb"`, for the fingerprint. `os.makedirs`, `json.dump`, `pickle`, `np.save`
  and `tempfile` are banned by test;
- **no persistence across process restart.** A restart starts cold, and that is the contract — do
  not describe a hit as a disk or persistent cache hit.

A new key replaces the previous pair **atomically**: the key and the value are read and written
together under one `threading.Lock`, so a lookup observes either the complete old entry or the
complete new entry, never a half-updated pair. The lock is **not** held across the deep copy and
never across audio analysis — a miss computes outside the lock and stores only afterwards.

## The key, and what owns it

```
Stage3CacheKey(
    version                   = L2_CACHE_VERSION
    track_identity            = abspath + st_size + st_mtime_ns + bounded content fingerprint
    audio_window              = (effective_start_time, effective_duration)
    analysis_config_identity  = the seven Stage 1-3 AutoWaveConfig fields, by name
    use_gpu_requested         = bool(use_gpu)
)
```

Frozen and hashable, structured rather than a concatenated string, so a malformed component cannot
silently collide with a well-formed one.

**`L2_CACHE_VERSION = "l2_stage3_v1"` owns the meaning and schema of this artifact.** Bump it when
Stage 1-3 algorithm/output semantics change, or when the stored bundle contract changes. **L2 and
Stage 5 have independent contracts**: `CACHE_CONTRACT_VERSION` (`stage5_cache_v3`) and
`ANALYSIS_VERSION` are *not* touched for an L2 change, and `stage_cache.py` may not mention either.
V1 caused no cache migration, no invalidation and no payload change anywhere.

### Track identity is accidental-staleness protection

Same geometry as the accepted D2 pattern, re-expressed rather than imported — `stage_cache.py` must
not import `video_analysis.py`, reuse `_video_signature` or reuse `_cache_path`:

```
BLAKE2b, 16-byte digest, file size hashed first, then content

size <= 3 MiB   the whole file
size >  3 MiB   exactly three non-overlapping 1 MiB windows
                [0, 1 MiB)
                [mid, mid + 1 MiB)   mid = max(1 MiB, min(size // 2 - 512 KiB, size - 2 MiB))
                [size - 1 MiB, size)
```

The clamp guarantees no byte is hashed twice, so bytes read are exactly `min(size, 3 MiB)`.

This is the **same class of accidental stale-result prevention as Stage 5** — not an adversarial or
cryptographic media identity, and no claim is made about bytes outside the sampled windows of a large
file (a test documents that two files differing only in an unsampled region share a fingerprint).
`st_mtime_ns` closes integer-second truncation; the fingerprint is what catches an exact-mtime
restore, which is why the timestamp alone is not enough.

**The absolute path stays in identity**, exactly as in Stage 5: moving the same audio file to a
different path re-keys. Content-only identity would deduplicate copies, which is a semantic change
V1 does not make.

**Unprovable identity means no cache, never a weak key.** An unstatable or unreadable source yields
`track_identity() -> None`, so `stage3_cache_key() -> None`, so the cache is unavailable for that
invocation and the render follows the existing uncached path. There is no fallback token.

### The effective audio window

The key uses the **effective load window**, not the raw UI syntax. The pipeline's own rule stays the
one authority and is not restated anywhere:

```
offset   = start_time
duration = end_time - start_time   only when end_time is truthy and end_time > start_time
           None                    otherwise
```

`_stage3_cache_key(audio_file, start_time, duration, cfg, use_gpu)` is handed **`duration` as the
pipeline just resolved it** — the same value passed to `librosa.load` — so there is no second trim
contract to drift. Two inputs producing identical current `librosa.load()` semantics therefore
reuse; different effective trims do not. `None` stays distinguishable from `0.0`.

### Stage 1-3 analysis-config identity

Exactly the `AutoWaveConfig` fields the front end or Stages 1-3 actually read:

```
sr                   the librosa.load call
hop_length           Stages 1, 2 and 3
n_fft                Stages 2 and 3
wave_smooth_beats    Stage 2
phrase_beats         Stage 2
bar_beats            Stage 2
section_min_seconds  Stage 3
```

Read by name off `STAGE3_ANALYSIS_CONFIG_FIELDS` through `getattr`, so there is one list and the
drift guard can compare it against the real source. **`tests/test_l2_stage3_cache_identity.py`
re-derives that set from the live Stage 1-3 source on every run** and fails if a field is read that
the identity does not cover — so a future Stage 1-3 edit cannot silently widen what affects the
artifact while leaving the key behind. It also asserts the reverse: no Stage-4-only field
participates, because an over-broad identity would needlessly invalidate reusable work.

### `use_gpu` separation is conservative, not a parity claim

Stage 2 has a CPU/CuPy execution branch and this repository has **no byte-exact CPU/GPU parity
contract**, so V1 keeps the two *requested* modes in separate cache identities. **No claim is made
that their outputs differ.** Sharing one artifact between them would be asserting a parity nobody has
measured; proving that parity is what would justify dropping this component later.

### What may never enter the Stage-3 key

Not the Variation Seed, Cut Density, Micro Cuts, Semantic Emphasis, Energy Response, Motion Bias or
Source Diversity. Not a creative preset name, Variant Lab state, a Director prompt or proposal, a
Freestyle checkbox, declaration or resolved section rule. Not the video source list, Stage-5 cache
state, the Qwen model or config. Not the encoder, FPS, voice/SFX settings or output path.

None of those exist or matter before Stage 4, so including one would **over-invalidate** the artifact
with pure UI producer state. The whole point is that *every* creative-control change reuses the
~15.7 s of Stage 1-3 work. Tests prove all seven controls, all four presets and six Freestyle shapes
(off, on with no rule, all-Base, a Stage-6-only rule, a uniform non-base density and a heterogeneous
one) reach one identical key and produce one hit each.

## Defensive deep copying is load-bearing

The bundle holds mutable values — NumPy arrays, dicts, a list of section dicts — and downstream code
legitimately mutates `beat_info`. So:

```
PUT   store copy.deepcopy(value)        the producer keeps no live handle into the cache
GET   return copy.deepcopy(stored)      a consumer's mutation cannot reach a later hit
```

The stored graph is **never** reachable from any caller, which is also why the copy can safely happen
outside the lock. This does not rely on downstream code being careful, and it must not be "optimised"
into a shallow copy or a direct return. `stage_cache.py` therefore imports `copy`, which is why
`tests/test_no_runtime_dependency.py`'s stdlib allow-list includes it.

## Fail open: this is an optimization, never execution authority

If key creation, lookup, copying or the store fails unexpectedly, the render takes the existing
uncached path. It must never fabricate a hit, never return partial data, and never fail an otherwise
valid render. The three seams (`_stage3_cache_key`, `_stage3_cache_get`, `_stage3_cache_put`) catch
**`Exception` only** — a `KeyboardInterrupt` or a `MemoryError` still reaches the caller, and a test
asserts no bare `except` and no `BaseException`.

Existing failures keep their current behaviour: a `librosa.load` error, a Stage 1/2/3 exception and
the empty-audio `ValueError` are untouched. Once the uncached path starts, its exceptions are exactly
what they were.

**Nothing is published before Stage 3 succeeds.** The `_stage3_cache_put` call is the last statement
of the miss branch, so anything raising above it leaves the cache untouched and a later call
recomputes. A store that itself fails costs the **next** call its reuse — never the current render.

## What still runs on every render

- **Stage 4 always executes**, including on a seed-only or Stage-6-control-only re-render. There is
  no Stage-4 key, no Stage-4 artifact and no resolved-Freestyle Stage-4 key in production code; that
  design remains documentation-only future work.
- **Stage 5 remains independently authoritative.** `video_analysis.py` was not modified and its cache
  scan behaves exactly as before. The L2 key knows nothing about video files, the candidate library,
  Qwen or the Stage-5 contract — so a **source-library change cannot invalidate cached audio
  analysis**, and an **audio change cannot invalidate Stage-5 media records**.
- **Stage 6 always reruns.** No Stage-6 key, no plan persistence.

## Truthful progress on a hit

`progress.py`, `progress_view.py` and `gui.py` were **not** modified — the existing event model
already supports this. On a hit, each skipped stage emits a START/END pair:

```
Stage 1   "Reusing cached beat grid"                      beats, tempo
Stage 2   "Reusing cached energy and rhythm features"     average_wave
Stage 3   "Reusing cached musical sections"               sections, section_types
```

Three rules make it honest: every one of those events carries `cached=True`; **none carries
`elapsed_seconds`**, because there is no fresh Stage 1-3 timing and inventing one would be a
fabricated measurement; and the metadata is the *cached facts themselves*, so the panel shows the
real analysis about to be used. The legacy `progress_callback` is still advanced through 1, 2 and 3,
so an old consumer does not jump straight to Stage 4. On a miss the original events are unchanged —
real timings, no `cached` marker.

The console prints exactly one line, and it claims only what is true:

```
   ♻️  Reusing process-local audio analysis cache (Stages 1-3)
```

Not a disk cache, not persistent, and not reuse of Stage 4, 5 or 6.

## C3 gains reuse with no C3 code change

`variant_lab.py`, `variant_batch.py`, `render_batch.py` and the GUI's C3 orchestration were **not**
modified. C3 runs two sequential single-render pipelines: candidate 1 populates the one entry, and
candidate 2 hits it whenever the audio identity, the audio window, the analysis config and the
`use_gpu` request match — which they do, because C3 varies creative controls only. There is
deliberately **no** explicit "share with candidate 2" mechanism; the benefit is a property of the
cache being process-local rather than call-local, which a test demonstrates across two independently
built pipeline namespaces.

## The pipeline-core note this supersedes

`.claude/rules/pipeline-core.md` records that C3-R0 "invented no stage cache" and that Stages 1-3 are
"legitimately repeated per candidate". The first half is still true *of C3-R0*. The second is what
this milestone changed — and it changed it without splitting `analyze_beats_auto`, which was the
objection recorded there: the cache wraps the existing computation in place rather than hoisting it.

# Fork modification log

This file records the modifications this fork makes to the upstream project, as required by
**AGPL-3.0 §5(a)** ("cause the modified work to carry prominent notices stating that you modified it,
and giving a relevant date").

| | |
|---|---|
| **Fork** | [Digital-Union-Company/BeatSync-Engine](https://github.com/Digital-Union-Company/BeatSync-Engine) |
| **Upstream** | [Merserk/BeatSync-Engine](https://github.com/Merserk/BeatSync-Engine) |
| **Upstream baseline** | `06679c1f28c6d0b56c901495e52497cccebc744a` |
| **Fork modifications began** | 2026-09-26 |
| **Licence** | GNU Affero General Public License v3.0 (unchanged from upstream) |

Fork-specific code lives in `src/beatsync_fork/`. Upstream modules are touched only at small call sites
marked `# [FORK]`, so upstream synchronisation stays straightforward. Upstream Auto Mode creative
behaviour is preserved as the default.

---

## Unreleased

### Added — 2026-09-30 (Creative Controls Extra PR1: Source Diversity, Micro Cuts)

Two more controls on the Creative Controls Core seam. `CreativeProfile` grows to six fields; both
new controls are 0–100 with **50 = current behaviour**.

| Control | Range | Neutral | Owned by | What it changes |
|---|---|---|---|---|
| Source Diversity | 0–100 | 50 | Stage 6 (**dynamic** half) | how hard the plan spreads across source videos |
| Micro Cuts | 0–100 | 50 | Stage 4 | the rare half-beat accent layer, and only that |

- **Source Diversity scales exactly two penalties.** `factor = 3 ** ((v - 50) / 50)` multiplies the
  planner's `-0.10` recent-source penalty and its capped `usage[video_file] * 0.012` source-usage
  penalty. The two **candidate-level** protections (`-0.28` for a recently used candidate id, and the
  capped `usage[id] * 0.10`) are byte-identical at every setting — diversity decides whether the edit
  returns to the same *source*, never whether it may repeat a *moment*. Deque lengths are unchanged.
  Base 3 is measured: on the real 509-candidate / 41-source TEST1 pool over 148 real segments it moved
  unique sources 31 → 39 and halved top-source usage 20 → 10 for a ~4 % mean legacy-score cost, with
  zero adjacent source repeats even at the reuse end; base 2 was visibly weaker and base 4 started
  producing adjacent source repeats at 0.
- **Source Diversity is dynamic and stays out of L1A.** It reads `usage`/`recent_videos`, so it is
  deliberately *not* in `ScoringControls` and not a key of the static table. A test asserts the
  `_static_base_score` evaluation count is identical at diversity 0 / 50 / 100, and that moving it
  does not trigger Energy Response's flow column.
- **Micro Cuts owns one layer.** It derives only `max_micro_cut_ratio`
  (`0.025 * 3^d`, hard-capped at 0.08) and `micro_percentile` (`96.5 − 6·d`, clamped to 90 … 99.9);
  `micro_min_gap` is untouched because it is the anti-flicker floor, and the `wave >= 0.88` gate
  inside `add_rare_micro_cuts` is untouched because measurement showed it is not the binding
  constraint. At **0** the layer is switched off via `enable_rare_micro_cuts=False` rather than scaled
  down — scaling alone cannot reach zero. Measured on the real Nero track: 0 → 0 extras (143 cuts),
  50 → 4 (147, the exact production baseline), 75 → 6 (149), 100 → 11 (154), minimum gap constant at
  0.464 s throughout.
- **Cut Density and Micro Cuts compose, in that order, and neither rewrites the other's fields.**
  Density shapes the main grid; Micro Cuts rewrites only the accent policy. `micro_cuts=50`
  short-circuits before the micro config is derived, so Cut Density's Core behaviour is bit-identical
  — a test recomputes it without any Micro Cuts involvement at densities 0/50/100 and requires exact
  arrays. The absolute accent count still scales with grid size (`max_extra` is a ratio of the
  selected grid); that pre-existing proportionality is intended and deliberately not "corrected".
- **50 is still today, exactly.** Every neutral control takes an explicit branch: `micro_cuts=50`
  passes the `CONFIG` singleton itself through (asserted by identity, not equality), and
  `source_diversity=50` runs the untouched penalty expressions (asserted at the source, so equality of
  results cannot hide a `× 1.0`).
- **Stage 5 untouched.** `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3`, `ANALYSIS_VERSION` stays
  `auto_av_analysis_v8_llama_vulkan_batched`, and `src/video_analysis.py`,
  `src/auto_mode/stage5_qwen_scene_worker.py` and `src/beatsync_fork/library_prep.py` are byte-identical
  to main.
- **UI / CLI.** Two more sliders in Creative Direction (0–100, step 1, default 50) and
  `--source-diversity` / `--micro-cuts`. Both are render-request creative state: no handler, absent
  from `source_outputs`/`prep_outputs`, live `process_btn` inputs with pinned positional alignment.
  Randomize stays seed-only. No filename suffix — seed remains the only one.
- **Deferred deliberately:** Semantic Emphasis is *not* here, not even as a dormant field. It needs a
  deterministic candidate view reconstructed from Stage-5 primitives and carries a different
  drift-risk class, so it is its own PR. Presets follow after both have runtime acceptance.
- Files: `src/beatsync_fork/creative.py`, `src/auto_mode/__init__.py`,
  `src/auto_mode/stage6_av_planner.py`, `src/gui.py`, `src/ui_content.py`, `src/video_processor.py`,
  `tests/test_source_diversity.py` (new), `tests/test_micro_cuts.py` (new),
  `tests/test_creative_profile.py`, `tests/test_creative_controls_interactions.py`,
  `tests/test_creative_controls_seam.py`, `tests/test_cut_density.py`,
  `tests/test_library_preparation.py`.

### Added — 2026-09-30 (B0 + Creative Controls Core: Cut Density, Energy Response, Motion Bias)

Phase A's single creative control — the variation seed — is generalised into one resolved **Creative
Profile** carrying four controls, and three new ones are implemented on top of the boundary P2
established.

| Control | Range | Neutral | Owned by |
|---|---|---|---|
| Variation Seed | 0 / positive | 0 | Stage 6 — which of the good candidates wins |
| Cut Density | 0–100 | 50 | Stage 4 — how many beats become cuts |
| Energy Response | 0–100 | 50 | Stage 6 — how hard scoring follows the music's target |
| Motion Bias | 0–100 | 50 | Stage 6 — calm vs. dynamic source material |

- **50 is today, exactly.** `seed=0, cut_density=50, energy_response=50, motion_bias=50` reproduces
  current main's selected cut times, segment targets, candidate choices, legacy seed-0 RNG stream,
  plan length and output filenames. Every neutral control takes an explicit branch that calls the
  pre-existing legacy path rather than running the new arithmetic with a neutral coefficient, because
  even an operation-order change would make "upgrade and change nothing" untrue. Stage 4's neutral
  result is pinned against an independent recomputation of the pre-Core algorithm.
- **Mappings.** Cut Density: `f = 2 ** ((d - 50) / 50)`, so 0.5 / 1.0 / 2.0 at 0 / 50 / 100; minimum
  intervals and maximum holds divide by `f`, the global cut-ratio band multiplies by it (capped at
  0.95 / 0.98), beat steps re-quantise half-up into `[1, 8]`, and the weak-score breathing threshold
  scales by `1/f`. Energy Response: `factor = 1 + ((r - 50) / 50) * 0.6` (0.40 … 1.60), blending each
  target score against the same candidate's generic `flow` score. Motion Bias:
  `centered * 0.15 * (2 * motion - 1)`. Ordering is fixed and documented: legacy score → energy blend
  → motion shift → one clamp.
- **Stage 5 is untouched, and that is the point.** No control reaches `_qwen_config_token`,
  `_video_signature`, `_cache_path`, a Qwen request, the Qwen prompt or a persisted semantic payload.
  `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3` and `ANALYSIS_VERSION` stays
  `auto_av_analysis_v8_llama_vulkan_batched`; `video_analysis.py`, `stage5_qwen_scene_worker.py` and
  the preparation workflow were not modified. Cut Density *does* legitimately change
  `audio_visual_profile` (it changes `selected_beats`, hence `average_cut_interval`, `cut_count` and
  possibly `smart_preset`) — allowed, because since P2 that profile has zero executable effect inside
  Stage 5. A regression test proves two densities derive identical cache paths for the same sources.
- **L1A survives.** Energy Response and Motion Bias are render-scoped constants, so the static score
  table stays `candidates × distinct targets`; Energy Response adds one `candidates`-wide `flow`
  column and nothing else. Never `candidates × segments`.
- **UI / CLI.** Three sliders in Creative Direction (0–100, step 1, default 50) and three optional CLI
  flags (`--cut-density`, `--energy-response`, `--motion-bias`). All are render-request creative
  state: none registers a source-invalidating handler, so none can clear a source confirmation,
  disable Create Video, trigger a scan or touch Media Library Preparation. Randomize still writes the
  seed only. Filenames are unchanged — the existing `_seedNNN` suffix rule is the whole rule; the
  resolved profile is reported in the console, the success panel, `render_info["creative"]` and the
  plan summary instead.
- **Not implemented, deliberately:** Freestyle, an AI Director, Variant Lab, Creative Recipe
  persistence, per-section profiles, Micro Cuts, Source Diversity, Semantic Emphasis, L2 stage caching
  and a second Qwen pass. The future L2 invalidation boundaries are documented in `CLAUDE.md`, not
  built.
- Files: `src/beatsync_fork/creative.py` (new), `src/auto_mode/__init__.py`,
  `src/auto_mode/stage4_select.py`, `src/auto_mode/stage6_av_planner.py`, `src/gui.py`,
  `src/ui_content.py`, `src/video_processor.py`, `tests/test_creative_profile.py` (new),
  `tests/test_cut_density.py` (new), `tests/test_creative_scoring.py` (new),
  `tests/test_creative_controls_interactions.py` (new), `tests/test_creative_controls_seam.py` (new),
  `tests/test_creative_seed.py`, `tests/test_library_preparation.py`, `tests/test_scale_diagnostics.py`.

### Added — 2026-09-30 (Media Library Preparation: bounded analysis batches)

Preparation's Analyze click now submits one bounded batch of the outstanding sources instead of all
of them. Default **100**, exposed as an `Analyze batch size` control.

- **Why.** Stage 5 batches multiple videos into one shared Qwen worker, and that worker writes its
  response JSON only after its entire job loop finishes — so the parent can checkpoint completed
  semantic records only once the whole batch returns. A cold 1107-source library therefore exposed
  every source to a single all-or-nothing worker invocation. Bounding the submission bounds that
  interruption/retry exposure while still amortising one Qwen model load across each batch.
- **Not incremental worker checkpointing.** The Qwen worker was **not** modified and an interrupted
  in-flight batch does **not** resume internally — if its worker dies before producing a response,
  that batch may need repeating. The change limits how much work one failure costs, nothing more.
- **Not a library cap.** A 5000-source scan still reports 5000 needing analysis; `normalize_batch_size`
  has no upper bound and the number box carries no `maximum`. Scan still classifies the whole library
  in one pass (the P.1 rule, unchanged).
- **Batch size is execution policy, not classification identity.** It is absent from
  `LivePrepDeclaration`, `PrepScanResult` and `RuntimeIdentity`; `set_batch_size` keeps the recorded
  scan, so retuning the bound between Scan and Analyze needs no re-scan. Folder and recursive still
  invalidate. Analyze reads the **live** widget value; Scan does not receive it at all.
- **No cache or contract change.** `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3` and
  `ANALYSIS_VERSION` stays `auto_av_analysis_v8_llama_vulkan_batched`. `src/video_analysis.py` was not
  modified by this change at all, no cache payload gained a field, and persistence remains entirely
  with the existing `analyze_video_sources` → `_checkpoint_cache` path. P2 media-neutral semantics are
  unchanged.
- **Reporting.** The post-run summary states what was submitted and what was analysed for that batch
  and then asks for a re-scan; it never presents `old outstanding − N` as an authoritative remaining
  count, because sources can change on disk between clicks. No UI text claims a batch has a known
  duration.
- Files: `src/beatsync_fork/library_prep.py`, `src/gui.py`, `src/ui_content.py`,
  `tests/test_library_preparation.py`.

### Changed — 2026-09-30 (P2: persisted Stage-5 semantics are media-neutral)

Stage 5's Qwen semantics now describe the **video itself** rather than the video as seen through one
edit style. This establishes a deliberate architectural boundary: Stage 5 records intrinsic media
truth (persistent), while Stage 6 and any future creative/director layer interpret it for the current
project (ephemeral, per render).

- **The Qwen prompt no longer carries music or edit context.**
  `stage5_qwen_scene_worker._build_prompt()` takes no arguments and instructs the model to assess the
  moment only from what is visually present, explicitly not adapting its tags to music, song energy,
  edit style or pacing. The previous `"The music edit style is {smart_preset}."` conditioning is gone.
  The semantic schema is **unchanged** — same eight numeric keys, same `emotion` and
  `recommended_use` enums, same description contract, same greedy sampling — because the validation
  experiment tested the existing schema under a media-neutral prompt.
- **No audio profile crosses the worker boundary.** Neither the single nor the batch request JSON
  carries `audio_profile`; `_build_prompt` was its only consumer. Every private seam that existed
  solely to forward it lost the parameter. `analyze_video_sources(..., audio_profile=None, ...)` is
  retained as an integration signature with **zero** effect — it reaches no cache key, no request, no
  prompt and no persisted record.
- **`smart_preset` is out of cache identity.** `_qwen_config_token()` now takes no arguments and keys
  only `BEATSYNC_QWEN_MAX_WINDOWS`, `_FRAME_WIDTH` and `_MAX_NEW_TOKENS` — all three still re-key.
  `_qwen_prompt_style_hint` is removed. A source therefore needs one semantic analysis per compatible
  source identity + Qwen backend identity + media-semantic Qwen configuration, not one per preset.
- **`CACHE_CONTRACT_VERSION` moves `stage5_cache_v2` → `stage5_cache_v3`**, because a v3 record means
  something different from a v2 one. `ANALYSIS_VERSION` stays `auto_av_analysis_v8_llama_vulkan_batched`
  (deterministic candidate scoring, window building and the candidate schema are untouched). There is
  **no migration**: v2 keys are never produced or looked up again, no v2 semantics are reused, and the
  old records are left on disk untouched — no compatibility loader, no rewriter, no automatic deletion.
  A one-off cold v3 rebuild is the intended cost.
- **Media Library Preparation is now trackless.** The audio-file control, the Stage 1–4 profile pass
  during a scan, `TrackIdentity`, the stored profile snapshot and the track staleness guard are all
  removed; preparation inputs are the folder and the recursive flag. One preparation serves every
  track, preset and creative seed, and the report states `Semantic mode: media-neutral`. The live
  stale-widget guard, the P.1 subset-only Analyze rule and the strict separation from the Create Video
  confirmation gate are unchanged. This removes the measured ~15–20 s track-profile component from
  every preparation scan.
- **Normal Create Video is unchanged.** Stages 1–4 still run and Stage 6 still receives `beat_info`,
  sections, energy, targets, the creative seed and the candidate tags/scores. Stage 6 scoring and
  planning logic were not modified.
- **Validated on real material before adoption** (23 analysed sources, 17 source groups, 10,913
  deterministic candidates, 115 identical A/B semantic moments): 115/115 decoded and tagged with zero
  failures on both sides; post-merge mean score differences ≤ ~0.023; planner seeds 0/101/202 with zero
  fallbacks and near-identical editorial scores; blind human review of 40 frames preferring the neutral
  side 10/16 decisive, with fewer editorial-leak and false-action flags. The conclusion is that
  media-neutral semantics are not materially worse on real content, remain equally usable by Stage 6,
  and stop music/edit intent leaking into persisted media semantics.
- No Freestyle, Director, Hybrid or Neutral/Music-aware mode is implemented here, and no speculative
  abstraction was added for them. `tests/test_media_neutral_semantics.py` covers the prompt, the
  request payloads, the identity invariant, the v2→v3 boundary and the creative-state boundary.

### Added — 2026-09-29 (Creative Phase A: user-controlled variation seed)

A **Variation Seed** in the UI (plus `--seed` on the CLI) that produces a different but reproducible
clip plan from the same analysed source library.

- **Seed 0 is legacy and is the default.** Stage 6 keeps its existing argmax, its existing penalties
  and its existing `_stable_rng(index, target, start)` jitter stream, unchanged. Upgrading and
  leaving the box alone cannot change anyone's edit merely because this feature now exists, so seed 0
  never reaches the new selection rule. Output filenames for seed 0 are also unchanged.
- **A positive seed changes the winner rule only.** Scoring and penalties are untouched; the planner
  takes the best 6 candidates, drops anything more than 0.12 below the best, and makes a seeded
  weighted draw favouring the near-best. The previous `rng.random() * 0.015` jitter was far too small
  to make a user-facing seed useful — that is what this replaces, for positive seeds only.
- **The seed is creative state, never analysis identity.** It rides on `beat_info["creative"]`, which
  Stage 6 already receives. `video_analysis.py` is unmodified; `CACHE_CONTRACT_VERSION` and
  `ANALYSIS_VERSION` are unchanged; the seed is absent from every cache-identity function and from
  `audio_visual_profile` (whose `smart_preset` *is* keyed into the Qwen config token). Changing the
  seed re-plans; it never re-analyses. It is likewise not wired into any source-confirmation handler,
  so it cannot clear a confirmed source set.
- New stdlib-only fork module `src/beatsync_fork/variation.py` owns seed normalisation and the
  selection rule; `tests/test_creative_seed.py` covers it, the planner wiring and both boundaries.

### Fixed — 2026-09-28 (R2: individually valid telemetry could still sum to Infinity)

Follow-up to the telemetry-boundary work below, from review of that change. R1 validated every
telemetry scalar individually — finite, non-negative, real `int`/`float` — and made the **final library
aggregate** overflow-safe via `_telemetry_total`, then claimed that newly written records carry no
non-finite optional telemetry. Two earlier sums were still using raw floating-point addition, so that
claim did not hold: `1e308 + 1e308` is `inf` from two values that each passed the R1 contract.

Reproduced against the R1 head with the real extracted production bodies:

1. **Per-job batch telemetry.** `qwen_seconds = prefetch_seconds + inference_seconds +
   amortized_model` produced `inf` from `prefetch_seconds = inference_seconds = 1e308`, and — because
   completion is correctly independent of telemetry — the job was still complete, so the record was
   checkpointed with **`"qwen_seconds": Infinity` on disk** and stayed reusable. That is precisely the
   value R1 set out to keep out of a newly written record.
2. **Current-run inference total.** `run_stats["qwen_inference_seconds"] += inference_seconds` reached
   `inf` from two jobs each reporting an individually valid `1e308`, in the shared-batch path and,
   across two successive calls, in the single/inline path. `qwen_inference_seconds_this_run` could
   therefore still be `Infinity`, contradicting the published "current-run floats stay finite" rule.

**Fixed** by routing those combinations through the **existing** `_telemetry_total` — no new helper,
and a test asserts it remains the only telemetry summer. The per-job sum becomes
`_telemetry_total((prefetch_seconds, inference_seconds, amortized_model))`; both current-run inference
accumulations become `_telemetry_total((running_total, contribution))`. The two parent-measured
`run_stats["qwen_seconds"]` accumulations were routed through it as well: they are `perf_counter`
deltas and were never at risk, so this is behaviour-preserving for every reachable value, but the
invariant is then structural instead of resting on an argument about how large a monotonic-clock delta
can be. Current-run wall time is still parent-measured and still counted once per worker invocation.

Healthy telemetry is untouched — `1.0 + 4.0 + 2.0` is still exactly `7.0`, multi-job totals still add
up (`4.0 + 6.0 = 10.0`), and a legitimate zero sum is still zero. Nothing about the R1 number contract
changed: no wall-time ceiling was reintroduced, numeric strings and bools are still rejected, and
`_is_real_number` / `_optional_telemetry_number` / `_telemetry_seconds` / `_bounded_count` /
`_is_nonnegative_count` / `_telemetry_text` / `_as_mapping` / `_record_telemetry` /
`_record_candidate_count` are unchanged. Completion, cache identity, checkpointing and semantic merge
are untouched; `CACHE_CONTRACT_VERSION` stays `stage5_cache_v2` and `ANALYSIS_VERSION` stays
`auto_av_analysis_v8_llama_vulkan_batched`; no re-key, no migration, no cache rewrite.

`tests/test_qwen_scalar_boundary.py` gains 14 tests. Six of them are red against the R1 head and green
after the fix (per-job stored sum, the on-disk `Infinity`, the amortized-model share, and the batch and
inline current-run totals); the rest are regression guards for healthy values, legitimate zero,
wall-time provenance, and record reusability. One is structural rather than behavioural: it walks both
orchestration bodies and fails on any augmented assignment to a float telemetry key, because a *new*
unsafe sum being added later is exactly the failure mode that survived R1's own review.
`tests/test_stage5_cache_completion.py` adds `_telemetry_total` to its AST extraction tuple — the
orchestration bodies now call it, so its previous "aggregation-only" note is corrected there.

### Fixed — 2026-09-28 (Qwen telemetry trust boundary: optional metadata can no longer crash or poison Stage 5)

**Scope note first, because it matters for how this reads.** This is boundary robustness, not a report
that the shipping worker misbehaves. `stage5_qwen_scene_worker.py` cannot emit any of the malformed
shapes below: it reports `time.perf_counter()` deltas, `max(1, min(32, int(slots)))` and a literal
`0.0` for VRAM. The exposure is the request/response and cache JSON that is **deliberately retained**
under `input/video_analysis_cache/` (and therefore visible and editable), plus any future worker change
or regression. The worker was not modified.

**The defect.** Optional Qwen telemetry — durations, VRAM, batch size, model id — was converted with
bare `int()`/`float()` at two independent boundaries, while the semantic-completion contract never
reads any of it.

1. **Worker-response ingestion.** `float(timing.get("inference_seconds") or 0.0)` in
   `_complete_deferred_qwen_batch` raised `ValueError` on a non-numeric string — the case originally
   reproduced while testing the Stage-5 reporting work, and deliberately left unfixed there. The same
   shape reached `model_load_seconds`, `prefetch_seconds`, `batch_size` and `peak_vram_gb`, in both the
   shared-batch and the single/inline path. The batch call site is unguarded, so a telemetry field
   could abort Stage 5 after real GPU minutes; the inline path *is* wrapped in `except Exception`,
   which was worse — a malformed `batch_size` turned a fully tagged source into `ai_enabled=False`,
   i.e. permanent re-analysis caused by a field no completion rule reads.

2. **Library aggregation.** The tail of `analyze_video_sources` reads the same kinds of field back out
   of every returned record, and on a warm run **every one of those records is a cache hit**. A record
   can satisfy `_cache_entry_is_complete` *and* `_stored_ai_cache_is_consistent` while carrying
   malformed optional telemetry, because neither rule looks at durations, VRAM, concurrency or the
   model id. Such a record stayed reusable — so nothing ever recomputed it — and crashed or poisoned
   the aggregation on every subsequent warm run. A poison pill.

**The NaN/Infinity half is the dangerous half, and it is real rather than theoretical.**
`json.loads`/`json.load` accept the bare `NaN`, `Infinity` and `-Infinity` tokens through Python's
default non-standard `parse_constant`, and `json.dump` emits them because `allow_nan` defaults to
`True`. A non-finite value therefore survives the worker response file *and* a full cache round trip,
`float()` will not reject it, and one of them makes every sum it enters non-finite **silently and
permanently**. Two tests demonstrate this end to end rather than asserting it.

Secondary findings, all reproduced: `_is_count` admits negatives (`_is_count(-5)` is `True`), so a
worker-reported `frame_count: -5` reached `qwen_frame_count_this_run`; `int(2.7)`/`int(True)` laundered
a float and a bool into a fabricated concurrency; `str(...)` turned `123` and `{"a": 1}` into invented
model ids `"123"` and `"{'a': 1}"`; `qwen_concurrency`'s `next(...)` converted only up to the first
truthy record, so whether Stage 5 crashed depended on where a malformed source happened to sort; a
truthy non-dict `timings` raised `AttributeError` before any scalar was read; and a truthy non-dict
`semantics_by_job` slipped past the empty-response branch and then raised on `.get`.

**Fixed.**

- **Counts and optional telemetry now have explicitly different trust contracts.** `_is_count`,
  `_coerce_count` and `_reported_count` are load-bearing for completion and are **unchanged** — a
  frozen D1 contract. A separate seam handles telemetry: `_is_real_number`,
  `_optional_telemetry_number`, `_telemetry_seconds`, `_telemetry_total`, `_is_nonnegative_count`,
  `_bounded_count`, `_telemetry_text`, `_as_mapping`, `_record_telemetry`, `_record_candidate_count`.
- **An accepted telemetry number must be a real `int`/`float`, not a `bool`, finite and `>= 0`.** A
  numeric string is *not* accepted: `float("2.5")` succeeding is not a reason to believe a string in a
  numeric field, and laundering it would hide a broken producer. Negatives are rejected because every
  field here is physically non-negative — and because `_fmt_seconds` already clamps display with
  `max(0.0, ...)`, so a negative stayed invisible on screen while still corrupting the total.
- **Semantic completion never depends on optional telemetry**, which is now pinned by test rather than
  by convention. 3 requested / 3 decoded / 3 returned for exactly the requested ids stays complete,
  checkpointable and cached even when `inference_seconds` is `"not-a-number"`, `NaN` or negative, and
  when `batch_size`, `peak_vram_gb`, `model_load_seconds` or `model_id` are malformed.
  `_qwen_job_completed`, `_stored_ai_cache_is_consistent`, `_cache_entry_is_complete` and
  `_checkpoint_cache` are untouched; a test asserts none of them reads a telemetry field or helper.
- **Unknown is kept distinct from a legitimate zero.** An unprovable `peak_vram_gb` is stored as
  `None`, not `0.0`, precisely because the worker reports a real `0.0` — coercing would make a corrupt
  record indistinguishable from every healthy one. A genuine `0` still reads as `0` everywhere.
- **No aggregate can be non-finite, including from individually finite parts.** Filtering NaN/Inf per
  value is not sufficient: enough finite values overflow a running total. `_telemetry_total` checks the
  accumulator and degrades to the neutral `0.0` rather than reporting `inf`. No plausible figure is
  ever fabricated to keep a number finite — unknown is preferable to false precision.
- **Every telemetry sum goes through that aggregator, not only the final library one.** Validating the
  individual scalars is necessary but *not* sufficient, and the first cut of this work got that wrong —
  see the R2 correction below.
- **Counts respect their natural bound.** Current-run decoded frames count only when the worker
  reported a real non-negative integer `<= ` the candidates that job actually submitted, so a claim
  larger than the request is not evidence. Library totals are bounded by the record's own candidate
  count, so a candidate-less record with hand-edited huge counts cannot inflate them.
- **Nested containers are normalised, the top-level one is not re-checked.**
  `beatsync_fork.qwen_progress` already proves the loaded response is an object and was not modified.
  A malformed `semantics_by_job` now degrades to the same outcome as no usable semantics: the job stays
  attempted, stays incomplete, nothing is invented, Stage 5 survives. Collapsing a non-dict to `{}` can
  only make a completion check *fail*, never pass.
- **Old cache files are tolerated on read, never rewritten.** There is no migration and no cache
  rewrite; a valid entry round-trips byte-identically. The fix for the poison pill is that the
  aggregation reads tolerantly, not that the loader rejects more.
- **New records are sanitised before they are written**, so nothing malformed reaches a cache payload —
  done at ingestion, not by making `_save_cache` reject a record and not by changing
  `json.dump(allow_nan=...)`. The writer is not the semantic-policy layer.

**Current-run reporting invariants are preserved.** `qwen_seconds_this_run` stays the parent's own
`perf_counter` measurement, counted once per worker invocation, and is untouched.
`qwen_jobs_this_run`, `qwen_requested_count_this_run`, `qwen_tag_count_this_run` and the
completed/incomplete tallies stay parent-derived. `qwen_frame_count_this_run` and
`qwen_inference_seconds_this_run` are hardened as above. The requested/decoded/tagged split and the
`tagged / requested` UI denominator are unchanged; `src/gui.py` needed no change.

**No wall-time ceiling on durations, and that is deliberate.** Bounding a worker-reported duration by
the parent's measurement of the call looks attractive, but it makes the value depend on how long the
surrounding call happened to take, so a stubbed or replayed worker — the only way this code is testable
without a GPU — has every legitimate duration silently rejected. Finiteness and sign are what make the
aggregate safe. Counts keep a bound because they have a real one in the same scope.

**No cache-contract bump and no analysis-version bump.** `CACHE_CONTRACT_VERSION` stays
`stage5_cache_v2` and `ANALYSIS_VERSION` stays `auto_av_analysis_v8_llama_vulkan_batched`. Semantic
output meaning, candidate scoring, the candidate schema, cache identity and the completion contract are
all unchanged; this is optional-telemetry sanitisation plus tolerant reading. Reading is strictly *more*
tolerant, so no record that current main accepts becomes unreadable, and no stored value is
contradicted.

`tests/test_qwen_scalar_boundary.py` is new and covers the malformed-shape matrix at both boundaries,
completion independence, the nested-container cases, the NaN/Infinity poison pill against a genuinely
reusable synthetic record, aggregate finiteness and order-independence, legitimate zero, the frozen D1
count semantics, and cache isolation. No model inference, no production run and no runtime-cache write
was involved in authoring or validating it.

### Fixed — 2026-09-28 (Stage 5 reports current-run work, not cached-library history)

**The defect, observed in production.** A fully warm run — 845/845 cache hits, **zero** sources
re-analysed, **zero** Qwen workers launched, zero model inference, zero Stage-5 cache writes, all of
it independently verified — printed a Stage-5 summary reading `Source videos: 845, visual workers: 1`,
`Qwen: enabled, model Qwen3VL-2B-Instruct-Q8_0 (llama.cpp Vulkan), batch 4`,
`Qwen performance: batch 4, 3.12 candidates/s` and `Qwen tags: 8704/8704 in 3031.9s`. Every one of
those figures was loaded from cached records and described historical work. The five-line CMD budget
was then exhausted, dropping the one line that actually described the run:
`Analysis time: …, cache 845/845`.

This was a reporting and observability defect only. The cache, the completion contract and the Qwen
runtime all behaved correctly; nothing about stored results was wrong.

**Root cause.** Three independent issues:

1. the top-level `qwen_*` aggregates sum or select over `videos`, which includes every cache hit, so
   on a warm run they are pure history — yet the UI rendered them as current performance;
2. `_video_analysis_workers(0)` returns `1` (its `video_count <= 1 → 1` contract), and `workers` was
   computed unconditionally even though the analysis block is guarded by `if jobs:`, so the summary
   claimed one analysis worker where none was used;
3. the five-line budget dropped `Analysis time …` because the historical Qwen lines came first.

A fourth, smaller issue found in the same audit: the Stage-5 START event fires *before* the cache
scan yet announced `Analyzing N source video(s)` — stronger than the system can know at that point.

**The change — additive, ephemeral, cache-neutral.** Stage 5 now returns explicit current-run
execution facts alongside the untouched library aggregates: `sources_analyzed_this_run`,
`analysis_workers_used`, `qwen_jobs_this_run`, `qwen_completed_jobs_this_run`,
`qwen_incomplete_jobs_this_run`, `qwen_frame_count_this_run`, `qwen_tag_count_this_run`,
`qwen_seconds_this_run`, `qwen_inference_seconds_this_run`. They come from an invocation-scoped
`_new_run_stats()` dict threaded only into the paths that analyse an uncached source, so a cache hit
cannot inflate them and a second call in the same process starts from zero. The existing aggregate
fields keep their values and meaning for compatibility; the UI simply stops presenting them as
current work.

**The accounting invariant, which is subtler than it looks.** `qwen_jobs_this_run` is deliberately
**not** `len(deferred_jobs)`. That would be wrong in both directions: the serial path
(`_analyze_single_video` with `defer_ai=False`) runs Qwen **inline** and never appears in
`deferred_jobs`, while a deferred source whose candidate list is empty passes through batch
orchestration without ever reaching the worker. Counting therefore happens at the two places that
genuinely submit work — immediately after `_run_qwen_worker` in the facade, and in the batch's
per-job merge loop, which only runs for sources that made it into `request_jobs`. A configured skip
(`BEATSYNC_QWEN_MAX_WINDOWS=0`) and a candidate-less source both correctly count as zero jobs. A job
that ran but did not complete is still counted, and separately tallied as incomplete, so a failure
can neither masquerade as success nor disappear.

Both recording sites are written inline rather than via a shared helper, because
`_annotate_candidates_with_qwen` and `_complete_deferred_qwen_batch` are AST-extracted and executed
by `tests/test_stage5_cache_completion.py` and must stay self-contained.

**Worker truth.** `_video_analysis_workers` is unchanged — its one-job contract is relied upon — and
the call site now reports the workers actually used (`… if jobs else 0`).

**START wording.** `Analyzing N source video(s)` → `Checking N source video(s)`. The post-scan metric
(`H cached, J to analyze, W worker(s)`) remains the authority on real work.

**Console summary.** `_stage5_summary` is reordered so current-run truth wins the five-line budget:
sources/cache/analysed → current-run Qwen status → `Analysis time …, cache H/N` → library summary →
optional cached metadata, explicitly labelled as cached. The budget itself is **not** raised; it was
never the bug. Warm runs now read
`Sources: 845, cache 845/845, analyzed this run 0` / `Qwen: enabled, no inference this run`;
mixed runs read `Qwen this run: 2 job(s), 20/20 tags, in 14.0s` rather than the library's 8704/8704.

**No cache impact.** `CACHE_CONTRACT_VERSION` stays `stage5_cache_v2` and `ANALYSIS_VERSION` stays
`auto_av_analysis_v8_llama_vulkan_batched`. The new fields are top-level return metadata on a dict
that is separate from `video_data`, so they cannot reach `_checkpoint_cache`; a test pins that. No
cache re-key, no source-cache payload change, no Qwen request-schema change, no semantic-output
change, no candidate-scoring change, and no completion/checkpoint change. The 845 existing records
remain reusable.

**Not fixed, and explicitly out of scope.** Production measured **~69 s** inside Stage 5 on that warm
run — `total_elapsed` brackets the whole `analyze_video_sources` body and flows to the END event's
`elapsed_seconds`, so it was a genuine Stage-5 measurement. Later profiling, run once the same
~2.52 GB of bounded fingerprint windows were already in the OS cache, measured **~3.2–3.4 s**. The
discrepancy is **unresolved**; this work changes reporting truth, not Stage-5 performance.

**R2 — failed Qwen attempts are still current-run work.** Review of the first cut found a real
defect in the new accounting: `_complete_deferred_qwen_batch` recorded only inside its per-job merge
loop, which sits *after* the early return taken when the shared worker produces no usable response.
A worker that timed out, exited non-zero or returned unreadable output therefore reported
`qwen_jobs_this_run = 0`, and the UI rendered `Qwen: enabled, no inference this run` — false, since a
real attempt had been made on real sources. Batch accounting is now two-phase: **submission truth**
(jobs, requested candidates, the one measured shared-worker wall time) is recorded the moment
`_run_qwen_worker_batch` returns, before the empty-response branch, with every submitted job starting
incomplete; **response truth** (completion, decoded frames, merged tags, inference seconds) is
applied per job afterwards and deliberately does not re-count the job, which would double-count every
success.

R2 also separates three facts that the first cut conflated. `qwen_requested_count_this_run` (new) is
what was submitted, `qwen_frame_count_this_run` is what the worker proved it decoded, and
`qwen_tag_count_this_run` is what was actually merged. The UI's `N/M tags` denominator is now the
**requested** count: previously a job that requested 10 candidates and decoded only 8 rendered as a
flawless `8/8`, hiding the two that never arrived, and on a worker-level failure there was no decoded
count at all. Current-run decoded frames are counted only when the worker reports a real integer —
the persisted `timings["qwen_frame_count"]` keeps its legacy fallback to the requested count for
source-record compatibility, and that fallback is now prevented from leaking into current-run truth.
Current-run wall time counts each worker invocation once (one `batch_seconds`, or one single-call
duration), never the amortized per-source figures, which scale with source count.

Changed: `src/video_analysis.py`, `src/gui.py`, `tests/test_stage5_reporting_truth.py` (34 tests),
`CLAUDE.md`, `CHANGELOG-FORK.md`. Suite 651 passed / 2 skipped (617 + 34 new; same two pre-existing
`WinError 1314` symlink skips). The R2 tests execute the real `_annotate_candidates_with_qwen` and
`_complete_deferred_qwen_batch` bodies with only the worker subprocess stubbed — the first cut
asserted on shape and so never exercised the failure path it got wrong.

### Fixed — 2026-09-27 (Qwen targeted semantic recovery: one persistent rejection no longer retires a source)

**The defect.** A candidate whose semantics `_normalize_semantic` rejected made its *entire source*
permanently uncacheable. `_qwen_job_completed` requires `returned_ids == requested_ids`, so one missing
tag means the job is incomplete, nothing checkpoints, and the source is re-analysed on every run
forever. Two sources in the real 845-file production library were in exactly that state — measured 10
requested, 10 decoded, **9** tagged — costing a ~51 s Stage 5 tax on every warm run.

**Measured root cause: truncation, not a field-level rejection.** The primary request budgets
`_max_new_tokens()` (default 128) and leaves `description` an unbounded schema string. llama-server
returns `finish_reason="length"` with `completion_tokens` exactly at the budget and non-empty but
truncated text; `_parse_json_object`'s `\{.*\}` finds no closing brace, `json.loads` fails, and
`_normalize_semantic` rejects at its not-a-dict guard. The raw text already contained all 8 numeric
keys, a valid `emotion` and `recommended_use`, and substantial description content; what was missing
was **JSON termination** — generation stopped mid-description, leaving both the description string's
closing quote and the object's closing brace unemitted (the captured output has an odd quote count).
The description value is therefore not syntactically complete, so this is not a case lenient
brace-matching could have rescued. Greedy decoding (`temperature 0`, `top_k 1`) makes it byte-for-byte
reproducible, so each retry re-issues the identical request: the candidate can never resolve. Verified
by reproducing the request out of process and capturing the raw output that production discards.

**A bigger token budget alone is not the fix — measured, not assumed.** One of the two cases is a
degenerate repetition loop (`lips moving, lips open, lips closed, …`) that consumes whatever budget it
is given: still truncated at 160, 192 **and 256** tokens, the description growing 350 → 470 → 614 → 880
characters and never closing. The grammar bound is the half that terminates the loop; the extra budget
is only needed so the bounded JSON can close (131 and 129 tokens observed). The bound alone also fails,
leaving the other case one token short of closing. Full matrix, 3/3 repetitions each:

| variant | case A (long description) | case B (repetition loop) |
|---|---|---|
| 128, current schema | invalid | invalid |
| 160 / 192 / 256, current schema | valid | **invalid at every budget** |
| 128 + `maxLength 96` | invalid | valid |
| **160 + `maxLength 96`** | **valid** | **valid** |
| 192 + `maxLength 96` | valid | valid |

Smallest variant recovering both: **160 tokens + `description.maxLength = 96`**. Bounds of 112 and 128
also pass but retain more of the repetition, which is why 96 was chosen.

**The change.** Recovery is last in the control flow, with every *applicable* pre-existing primary
retry/fallback path ahead of it. Those tiers are conditional rather than a fixed sequence every
candidate walks: the initial attempt always runs, the server retry applies while a server is still
available, the reduced-slot restart fires only on its existing `valid_ratio < 0.70` condition (and
returns recursively, so only the innermost wave reaches recovery), and the serial/CLI fallback applies
per the existing backend state. Recovery forces none of them to run. A candidate still unresolved after
whichever tiers applied gets **exactly one** targeted recovery generation with those two measured
parameters. Same image, same prompt, same model, same greedy sampling. If it succeeds the semantic is
added normally; if it fails, behaviour is unchanged: candidate absent, job incomplete, no checkpoint.

**Deliberately narrow:**

- **The primary path is unchanged.** `_max_new_tokens()` still governs the ordinary request and remains
  D2-keyed via `BEATSYNC_QWEN_MAX_NEW_TOKENS`; `SEMANTIC_SCHEMA` keeps `description = {"type":
  "string"}` with no bound; prompt and sampling untouched; `_recovery_semantic_schema()` deep-copies
  instead of mutating the global. Proven cross-branch against one shared llama-server instance: primary
  output is **byte-identical** to merged main on all four measured candidates (342 / 347 / 350 / 306
  chars), including both previously-successful controls.
- **Semantic rejection only.** `_is_semantic_rejection()` requires a falsy semantic *and* non-empty
  text. HTTP errors, a dead server, CLI timeouts, non-zero exits and empty generations are excluded —
  there is no truncated output to rescue, and re-asking would paper over a broken backend. Existing
  transport retry/fallback behaviour is preserved. The classification stays internal to the inference
  wave and never reaches a cached payload.
- **Recovery eligibility is recomputed from the collected semantics**, not from the stale `failed` list,
  because the serial fallback tier resolves candidates without rewriting it.
- **Hard-coded constants, not environment variables.** A tunable recovery knob would be result-affecting
  Qwen configuration absent from `_qwen_config_token()` — the exact defect D2 fixed for `MAX_WINDOWS`.
- **The override arguments default to `None`** on all three `generate()` primitives, so every existing
  caller behaves identically. The CLI client's ctx-fallback self-retry forwards them as well; without
  that, a recovery hitting a context error would silently retry as an ordinary 128-token unbounded
  request and truncate again while appearing to have run.
- **The completion contract is frozen.** `_qwen_job_completed`, `_stored_ai_cache_is_consistent`,
  `ai_enabled`/`ai_deferred` semantics and checkpoint eligibility are untouched. `src/video_analysis.py`
  is not modified at all.

**No cache re-key, no contract bump** for this first introduction, and the argument is structural rather
than empirical: a source current main caches had every requested candidate tagged on the primary path,
so recovery never runs and the persisted semantics are identical; a source that missed a candidate
failed `_qwen_job_completed` and therefore has **no complete record on disk at all** — recovery can only
turn an absence into a record, never contradict a stored one. `CACHE_CONTRACT_VERSION` stays
`stage5_cache_v2`, `ANALYSIS_VERSION` stays `auto_av_analysis_v8_llama_vulkan_batched`, and the 843
existing D2 production records remain reusable. A *future* change to the recovery constants does **not**
inherit this argument, because fallback-generated records will exist by then; default policy for such a
change is to bump the cache contract unless persisted-output compatibility is explicitly proven.

**Honest limit.** Recovery does not cure repetition. The recovered degenerate description is still
partially repetitive — it is merely valid JSON, schema-valid, normalization-valid and bounded to ≤ 96
characters. The point is that one runaway description no longer makes an entire source permanently
uncacheable; model prose quality is out of scope.

Measured recovery cost ~0.61–0.72 s per call (mean ~0.65 s), so ~1.3 s for the two known sources.
Prediction only, pending a production run: once both recover and checkpoint, a subsequent identical warm
run should show 845/845 cache hits and launch no Qwen worker.

Changed: `src/auto_mode/stage5_qwen_scene_worker.py`, `tests/test_qwen_semantic_recovery.py` (24 tests),
`CLAUDE.md`, `CHANGELOG-FORK.md`. Suite 617 passed / 2 skipped (593 + 24 new; same two pre-existing
`WinError 1314` symlink skips).

### Changed — 2026-09-27 (Stage 5 cache identity D2: one deliberate generation transition)

D1 made the cache *durable* and its completion state *honest*. What it deliberately left alone was
whether a cache key identifies the right inputs at all. Measured on current main before changing
anything:

- **Source identity collided.** `abspath + size + int(st_mtime)` gave the *same* signature to a file
  whose content was replaced inside the same second, and to one whose exact original mtime was
  restored — so a stale analysis was reused for different media, the worst error class in this cache.
- **Backend identity collided** for all four components. The token used the file *basename* plus size
  plus `int(mtime)`, so a same-name/same-size/same-second swap was invisible and an override pointing
  at another copy of a model did not re-key.
- **`BEATSYNC_QWEN_MAX_WINDOWS` was absent from identity.** Runs with 120, 60, 0 and unset all shared
  one cache file, so a 60-window result was silently reused by a run asking for 120. The same applied
  to `BEATSYNC_QWEN_FRAME_WIDTH` and `BEATSYNC_QWEN_MAX_NEW_TOKENS`, both of which change the persisted
  semantics.
- **There was no persisted completion contract**, so nothing distinguished a record written under one
  set of rules from another.

D2 fixes all of it in **one** generation transition rather than several.

`CACHE_CONTRACT_VERSION = "stage5_cache_v2"` is a single constant used both as the first signature
component and as the stored `cache_contract` field, so a key and its payload cannot disagree about
their generation. `ANALYSIS_VERSION` is untouched — its job is candidate scoring, window building and
the candidate schema, none of which changed.

Source identity is now `CACHE_CONTRACT_VERSION | ANALYSIS_VERSION | abspath | st_size | st_mtime_ns |
bounded content fingerprint | backend token | Qwen config token`. `st_mtime_ns` alone would not have
been enough: it closes the integer-second truncation but an exact-mtime restore still collides, so the
fingerprint is the part that actually catches it. The fingerprint is BLAKE2b/16 over the size plus
either the whole file (≤ 3 MiB) or three non-overlapping 1 MiB windows (head, a clamped middle, tail),
measured at **5.1 ms/source → ~3.6 s and ~2.1 GB for 700 sources**. No cryptographic claim is made —
bounded fingerprints are for practical accidental stale-cache prevention.

Backend identity now covers each component by absolute path, size, `st_mtime_ns` and content: a full
hash for the two tiny executables (9 KB, 83 KB) and the bounded fingerprint for the two GGUFs
(1.83 GB, 0.82 GB). The `llama --version` string remains as extra evidence rather than the only strong
signal, so a failed probe stays non-fatal.

**One measurement shaped the whole design.** `_qwen_backend_signature_token` was reached from
`_video_signature`, i.e. **once per source — 702 times**, and was not memoised. Adding content
fingerprints there without changing that would have cost **61.7 minutes** per run. The orchestrator now
computes the backend and config tokens once per `analyze_video_sources` invocation and threads them into
every signature: ~9–20 ms total. It is invocation-scoped rather than module-cached, so a second call in
the same process still sees a swapped model, and a test asserts exactly four component fingerprints for
N sources.

Qwen identity uses *effective* values mirroring the runtime's own clamps, so an unset variable and its
explicit default agree, and a malformed value agrees with the default the worker really falls back to.
It keys `MAX_WINDOWS`, `FRAME_WIDTH`, `MAX_NEW_TOKENS` and — added in R2 below —
`audio_profile["smart_preset"]`. Runtime-only knobs (slots, device, timeouts, batching) are deliberately
excluded, and the whole `audio_profile` is deliberately not hashed.

**R2 — two gaps the first pass left.** Both were reproduced through the real `analyze_video_sources`
before being fixed:

- **Prompt context was missing from identity.** The worker's `_build_prompt` reads
  `audio_profile.get("smart_preset", "rhythmic_gmv_amv")` and interpolates it straight into the Qwen
  prompt, and `analyze_video_sources` forwards the audio profile into the worker request — so two runs
  differing only in preset produce different semantics. Measured on the first D2 commit: presets
  `rhythmic_hype_gmv_amv` and `cinematic_soft_amv` produced the *same* cache file. The effective style
  hint is now part of the Qwen identity token (missing key still equals the explicit default, and a
  `no_ai` key is unaffected), with a seam test that reads the worker's own call so a worker-side change
  to the key or default fails the suite. The worker is untouched.
- **A failed backend identity recomputed per source, and could recover mid-run.** The orchestrator
  passed the failed `None` token onward, but `_video_signature` reads `None` as "not supplied, compute
  it now" — so the same value meant two different things at that boundary. Measured: 6 sources produced
  **7** backend-token calls (1 + N), and a token that failed once then succeeded re-enabled caching
  inside that run, writing 6 records contrary to the documented run-level fail-closed contract. An
  explicit `ai_cache_disabled` state now short-circuits before `_cache_path` is called at all: 1 call,
  0 writes, no mid-run recovery, and analysis still returns all 6 sources. The success path already
  behaved correctly and still does — 1 call per invocation, 2 across two invocations.

`CACHE_CONTRACT_VERSION` is deliberately **not** bumped for R2: the first D2 commit is unshipped and no
D2 cache generation exists yet, so this is remediation inside the same unshipped generation rather than
a new one.

**Unprovable identity now means no cache rather than a weak key.** A stat or fingerprint failure, source
or backend, makes the signature and cache path `None`: no lookup, no write, and the render continues.
The old `ai_missing` placeholder is gone, because a *stable* token for an unprovable input is precisely
what allows a stale entry to be reused.

The contract marker is stamped in `_analyze_single_video`, where records are built, so AI,
deterministic/`no_ai` and candidate-less records all carry it; `_save_cache` remains a pure transport
primitive.

**Cost and legacy.** Exactly one cold rebuild is accepted by design — ~0.86–1.27 h for the current
702-source library — after which warm runs pay only the ~3.6 s identity scan plus ~20 ms of backend
work. There is **no migration, no cleanup and no D1→D2 compatibility loader**: new inputs mean new
filenames, so the 2196 pre-D2 records are simply never looked up. They remain on disk as harmless
historical artifacts (~55 MB, alongside a similar new generation), and `input/video_analysis_cache/` is
preserved by policy. The two D1 records whose completeness could never be proven become unreachable and
rebuild naturally, so that open question closes without a judgement call. A cold D2 run was verified to
return a payload and stored records identical to base main, cache-only metadata aside.

### Fixed — 2026-09-27 (Stage 5 cache durability D1: checkpointing + AI completion truth)

**The measured reason.** Until now `analyze_video_sources` wrote the cache in a single terminal loop
at the very end of Stage 5, so an interruption before that loop discarded *everything* new. Measured
against the pre-D1 code with three uncached sources: interrupting during the deterministic pass left
**2 completed analyses and 0 durable cache entries**; interrupting after every deterministic analysis
*and* every Qwen tag had finished still left **3 completed sources and 0 durable entries**. A hard
`os._exit` mid-run left 0. On the real 702-source library (median 4.4 s, mean 6.5 s per source) a full
cold rebuild is 0.85–1.26 hours, all of which a single Ctrl-C could previously throw away.

- **Per-source checkpointing.** Every point at which a source could have become complete now *checks
  checkpoint eligibility* through the one completion rule, and writes only if the rule accepts:
  after each serial `_analyze_single_video`, after each parallel deterministic result, after
  `_complete_deferred_qwen`, and after **each per-job merge** in `_complete_deferred_qwen_batch`. The
  terminal loop remains only as a backstop and goes through the same guard, so it can no longer
  promote an incomplete record into an accepted cache entry.

  What that actually persists, per shape:

  - a **non-AI** parallel or serial result is complete on arrival and is written immediately;
  - an **AI-deferred** parallel result carries `ai_deferred=True`, so the rule *refuses* it at that
    point — it becomes durable only once its Qwen result genuinely completes;
  - a **candidate-less** result that finished its deterministic scoring pass is the explicit
    no-Qwen-work exception and is written (see the R2 note below for why the scoring evidence is
    required);
  - in the **shared Qwen batch**, each per-job merge is checkpointed independently *once the worker's
    final response has returned*, so a failing or missing sibling — and a parent interruption during
    the post-response merge loop — no longer discards jobs already written.

  The boundary this does **not** cross: while the shared worker is still in flight, its per-job
  results exist only inside that process and nothing about them is durable yet. Streamed worker
  progress is presentation only and carries no semantic result authority. See *Honest limits* below.
- **One completion rule.** New `_cache_entry_is_complete()` answers "is this payload reusable for this
  request?" in one place. It rejects non-dict payloads, wrong `analysis_version`, missing/non-string
  `video_file`, non-list `candidates`, and anything with `ai_deferred` truthy; under `require_ai` it
  also demands genuine AI completion. `_checkpoint_cache()` consults it before any write, which is
  what makes early saving safe.
- **`ai_enabled` stopped lying.** Previously `_complete_deferred_qwen` swallowed a Qwen exception and
  then set `ai_enabled=True` unconditionally, so a failed run was cached as AI-complete and Qwen
  never retried for that source. Completion now comes from an explicit signal the Qwen facade
  reports on every return path — *not* from "no exception was raised". (What that signal is allowed to
  mean was itself tightened twice afterwards; see R4 and R5 below.) Deterministic candidates and
  visual tags are untouched on failure.
- **Batch failure is judged per job.** A globally non-empty `semantics_by_job` was being treated as
  proof that every requested job completed; a job simply absent from the response became
  `ai_enabled=True` with 0 tags and was reused as AI-complete forever. Completion is now a per-job
  membership test, one missing job no longer fails its siblings, and the existing total-batch-failure
  behaviour is preserved.
- **Candidate-less sources are no longer re-analysed forever** — but only when the deterministic
  scoring pass genuinely completed, proven by the existing scoring evidence in `timings`. There is
  then no Qwen work to do, so the source is reusable, expressed through the completion rule with
  `ai_enabled` left honestly `False` rather than faked to satisfy the loader. An empty candidate list
  on its own is **not** proof of success: an OpenCV-open failure produces the same shape, and R2
  below records that the first pass wrongly accepted it.
- **Hardened writer.** `_save_cache` now publishes through a unique same-directory temp file
  (`tempfile.mkstemp`), `flush` + `os.fsync`, then `os.replace`, with best-effort cleanup in
  `finally`. The old shared `path + ".tmp"` was demonstrably unsafe across processes: under
  deterministic barriers one writer's `os.replace` published the *other* writer's payload while
  reporting success, and the loser failed with `FileNotFoundError` into a warning the GUI discards.
  After the fix, 160 concurrent writes from two processes produced 0 exceptions, no shared temp
  names, no mixed payloads and no orphan temps. `PROCESS_CRASH_ATOMICITY` is preserved (verified at
  all four boundaries); **power-loss durability is still not guaranteed** — the temp is fsynced, the
  containing directory is not.
- **Minimal load validation.** The loader now refuses version-correct but malformed or *foreign*
  payloads, and can be told which source it expected. Unexpected extra fields remain allowed for
  forward compatibility.

**R2 — three completion gaps the first pass left open.** Each was reproduced against the first D1
commit before being fixed:

- **The serial inline path ignored the completion signal entirely.** `_analyze_single_video` still
  returned `ai_enabled = enable_ai and not defer_ai`, so a `workers == 1` run reported AI-complete
  whatever Qwen did. Measured on the pre-R2 code: worker returning `{}` → `ai_enabled=True`; worker
  raising → `ai_enabled=True`; `BEATSYNC_QWEN_MAX_WINDOWS=0` → `ai_enabled=True`. It also let the
  private `_qwen_completed` flag reach `timings` — and therefore the cached payload — through
  `timings.update(qwen_info)`. The inline path now pops the flag before the update and derives
  `ai_enabled` from it, exactly like the deferred paths.
- **Zero semantic tags were conflated with a worker-process failure.** The first pass keyed on
  emptiness of `semantics`, so a worker that finished and a worker that never produced a response
  both reported not-completed. R2 replaced that with membership of the job's entry in the response
  envelope — which R4 below shows was still wrong, in the opposite direction.
- **An OpenCV-open failure was accepted as a candidate-less success.** `"Warning: OpenCV could not
  open …; candidate analysis skipped."` returns `candidates == []`, which the D1 completion rule
  treated as "nothing for Qwen to do, therefore complete" — so one transient decode failure would
  have cached an empty result and retired a readable source permanently. Measured on the pre-R2
  code: the open-failure record was accepted under both `require_ai` modes **and** checkpointed. The
  rule now requires `timings["candidate_scoring_seconds"]`, which is written only inside the
  `cap.isOpened()` branch; a genuine no-usable-moments result keeps it and stays reusable, the
  failure does not and is retried.

**R4 — per-job Qwen completion required actual per-frame results.** R2/R3 defined completion as
membership of the job's entry in the worker response (`timings_by_job["single"]`, or `job_id` in
`semantics_by_job`) and documented "valid completed response + zero tags = SUCCESS". Read against the
worker, that is too weak: `_run_semantics_for_video` always returns a timings dict and `main` always
records it under the job id, so the envelope only proves **the job loop returned**. Inside the loop,
`_normalize_semantic` yields `{}` for any candidate whose semantic content is invalid,
`_run_inference_wave` classifies `{}` as failed and retries it (server retry → reduced-slot restart →
serial fallback), and a candidate still failing is simply **absent** from the returned semantics.

Measured against the R3 commit, all of these were wrongly reported as complete and cached as
AI-complete: `frame_count=3 tag_count=0 semantics={}`; `frame_count=0 tag_count=0`;
`frame_count=4 tag_count=3` (partial); and the batch equivalents, which were also checkpointed.

New shared rule `_qwen_job_completed()` — used by **both** the single and batch paths so the
arithmetic exists once — requires the expected per-job envelope, a dict `timing`, `frame_count > 0`,
and `tag_count == frame_count`, plus basic count coherence (`tag_count` may not exceed the semantic
records actually returned). Tags that did arrive are still merged; only the verdict changes, so an
incomplete job is retried next run instead of inheriting a silent gap. A successful sibling in a
partially-failed batch remains independently complete and checkpointed, and total-batch-failure
behaviour is unchanged.

Two accompanying corrections: **tag count is not forbidden from completion truth** — it is meaningful
only together with the envelope and the real `frame_count`, and the earlier blanket claim to the
contrary is removed. And the claim that *every* `_run_qwen_worker` failure returns `{}` is narrowed:
that holds for process-level failures (launch error, non-zero exit, timeout, unreadable response),
but candidate-level inference failures are swallowed and retried **inside** the worker, so a
successful worker process can still return an incomplete per-job semantic result. Worker counts are
now read through `_coerce_count`, closing a latent `ValueError` crash on a malformed payload (present
in the batch path before R4). The candidate-less no-Qwen-work case is untouched and remains separate:
no job is submitted, so the per-frame rule does not apply to it.

**R5 — completion must cover the *requested* candidate set, not just the decoded one.** R4 required
`frame_count > 0` and `tag_count == frame_count`, which proves every *decoded* frame was tagged. But
`_prefetch_candidate_frames` returns only the frames it could actually read
(`ready = [p for p in plans if p["image"] is not None]`), so `frame_count` can be smaller than the
candidate set the parent submitted — the worker even logs `{len(ready)}/{len(candidates)}`. Measured
against the R4 commit: a job requesting 3 candidates that decoded and tagged only 2 was reported
complete and cached as AI-complete, leaving a requested candidate with no semantics at all.

Completion now requires the requested set to be covered end to end — `frame_count` must equal the
number of candidates `_select_ai_candidates` submitted, `tag_count` must equal `frame_count`, and the
**returned semantic ids must equal the requested ids exactly**. R4 passed only
`len(semantic_by_id)`, so a response whose counts looked perfect but whose ids were foreign counted as
completion; measured on R4, three foreign ids satisfied it. An extra id now also fails, because the
worker keys semantics by our own candidate ids and anything else means the response does not match the
request.

Two robustness corrections alongside it. A worker-reported `frame_count = 0` was being rewritten into
the requested candidate count in the stored timings by `timing.get(key) or default`; `_reported_count`
now decides on **presence**, so a genuine zero survives and the fallback applies only when the field is
absent. And `bool` subclasses `int`, so R4's `isinstance(..., int)` check accepted
`frame_count: true` and then compared it equal to 1; `_is_count` rejects bools.

The candidate-less no-Qwen-work case is untouched and deliberately does **not** go through these
requested-count rules: no job is submitted, so there is nothing to cover.

**R6 — legacy AI records must be self-consistent too.** R5 hardened how *new* records are created, but
`require_ai` reuse of an *existing* candidateful record still ultimately trusted
`bool(data["ai_enabled"])` — a flag written by the very code this branch has repeatedly proven could
set it wrongly. A read-only audit of the real 2196-entry runtime cache measured the consequence:

| | |
|---|---|
| source-cache entries | 2196 (plus 28 qwen debug/repro artifacts, excluded) |
| provably R5-complete | 2190 (`candidate_count == frame_count == tag_count == ai_analyzed`) |
| **definitely inconsistent** | **4** — `ai_enabled=True` with `qwen_frame_count=10`, `qwen_tag_count=9`, 9 `ai_analyzed` |
| unverifiable but internally coherent | 2 (398 candidates/114 tagged, 525/119) |

The 4 are exactly the false-complete shape D1 exists to prevent, and the pre-R6 loader accepted all
2196. New `_stored_ai_cache_is_consistent()` now runs whenever `require_ai` reuse depends on
`ai_enabled`, rejecting only contradictions provable from **already-persisted** fields: real integer
counts (not bools), `frame_count > 0`, `tag_count == frame_count`,
`frame_count <= len(candidates)`, usable candidate ids, and `ai_analyzed` ids that are unique, a subset
of the candidate ids, and number exactly `tag_count`.

It deliberately does **not** call `_qwen_job_completed`: that needs `requested_ids`, which legacy
payloads never stored (nor the `BEATSYNC_QWEN_MAX_WINDOWS` value in force), so replaying the live rule
against an old record would mean inventing evidence. For the same reason it does not require
`frame_count == len(candidates)` — a smaller value is the normal result of `_select_ai_candidates`
limiting the submitted set, so the two unverifiable records remained reusable under the D1 loader and
were left to the D2 completion-contract decision. *The D2 transition above has since settled that by
orphaning them.*

Measured against the real cache at the D1 merge boundary, read-only: **2192 accepted, 4 rejected**, the
rejected set exactly the four audited files. **No runtime cache file was created, edited, renamed or deleted** — a rejection is
an ordinary cache miss, and the source is recomputed and republished through the already-hardened
writer. No migration command is provided and none is needed. `require_ai=False` deterministic reuse is
unaffected (those candidates are real work), and the candidate-less path still answers to
`_deterministic_analysis_completed`.

`BEATSYNC_QWEN_MAX_WINDOWS=0` now reports not-completed rather than AI-complete, deliberately:
`QWEN_MAX_WINDOWS` is not part of cache identity, so a knowingly Qwen-less record must not be stored
under the AI model key. `BEATSYNC_DISABLE_QWEN=1` remains the supported deterministic-only path — it
is turned into `enable_ai=False` in `auto_mode/__init__.py` (verified), producing the separate
`no_ai` cache identity.

R2 changed no cache key, signature or `ANALYSIS_VERSION`, and preserved every first-pass fix
(per-source checkpointing, unique fsynced temps, atomic replace, batch per-job completion, loader
validation, terminal backstop). A successful uninterrupted run is payload-equivalent in **both**
execution shapes — parallel/deferred and serial/inline — with the only difference being the removal
of the leaked `_qwen_completed` key from the serial shape's stored timings.

**Cache compatibility — at the D1 merge boundary.** There was no cache-key, signature or
`ANALYSIS_VERSION` change: `_video_signature` still used `int(stat.st_mtime)`, so existing entries
remained addressable. Verified read-only against the real cache at that point: all entries addressable,
and 2192 of 2196 reusable (see R6 — 4 intentionally rejected as self-contradictory). A successful
uninterrupted run returned a payload identical to the pre-D1 result (candidates, per-video records and
summary compared field by field). *The later D2 generation transition documented above supersedes this
contract and naturally orphans every pre-D2 entry, so these figures describe the D1-era cache, not
current lookup behaviour.*

**Honest limits — as of D1.** D1 introduced no two-phase deterministic-partial cache contract, so if
the process died while a shared Qwen worker was still running *before its response returned*, that
batch's deterministic work had to be recomputed — still true today, since D2 changed identity, not that
contract. **D1 did not fix source-identity collisions**: `int(st_mtime)` gave the same signature to a
file rewritten in place with the same size inside the same second, and an exact-mtime restore collided
regardless of precision. Source and backend identity hardening was deferred to D2, because moving to
`st_mtime_ns` re-keys the entire cache and forces a cold rebuild; **the D2 section above now closes that
work** and accepts exactly one such rebuild.

### Changed — 2026-09-27 (Phase 3C: NVENC clips decode in software)

The NVENC extraction path no longer asks FFmpeg for CUDA input decoding. **`h264_nvenc` still does the
encoding** — only the input-side `-hwaccel cuda` request is gone, so the architecture is now software
decode → the existing CPU filter chain → NVENC encode. The CPU-encode branch keeps its `-hwaccel auto`
and is untouched.

A read-only decode-path study measured three real paths on RTX 3080 / driver 617.14 over 5 real sources
× 4 deterministic windows (2.0 s, 1280×720, 30 fps, 60 frames per clip), in balanced interleaved order:

- **`-hwaccel cuda` was not actually decoding on hardware for most sources.** It failed to initialise on
  48 of 60 clips (33 requested nvdec decode surfaces against a limit of 32) and FFmpeg fell back to
  software decode anyway — the warning was the only thing it reliably produced.
- **Where real NVDEC *did* engage, it was slower.** On the one sampled source whose surface count fits,
  hardware decode was positively confirmed from FFmpeg's own verbose decoder log and ran ~20 % slower
  than software decode (0.530 s vs 0.436 s per clip), because this CPU filter chain
  (`trim,setpts,scale,fps`) has to pull the frames back to system memory.
- **Genuine CUDA decode is achievable but still not worth it.** `-hwaccel cuda -threads 8` produced
  confirmed hardware decode on all 5 sources with zero failures, exact frame counts and byte-identical
  output — and was slower than plain software decode on 20 of 20 clips. (`-threads 16` and
  `-extra_hw_frames 0` did not resolve the surface count at all.) It is not adopted.
- **Output is unchanged, byte for byte.** All 20 A/B pairs produced identical output *files* — not
  merely equal frame counts or equivalent content — and each variant was deterministic across
  repetitions. The post-change smoke reproduces the study's Variant-B files exactly: 5/5 SHA-256 match.
- **Modest, consistent speedup.** Sequential median 0.434 s vs 0.449 s (faster on 18/20 clips, and in
  every balanced repetition); at the real 4-worker NVENC cap, 12.17 s vs 13.46 s wall for 40 clips
  (3.286 vs 2.972 clips/s), with 0 decode-init failures instead of 32 and ~500 MiB lower peak VRAM.

The FFmpeg argv delta is exactly the removal of the two tokens `-hwaccel cuda`, proven by diffing the
argv the production function really constructs, before and after, with every other token in the same
order. No filter graph, encoder, quality-argument, frame-lock, worker-policy, resolution or FPS change;
`get_nvenc_quality_args`, `get_cpu_h264_quality_args`, `seconds_to_frame_count`,
`frame_count_to_seconds`, `build_frame_aligned_cut_timeline` and `_effective_clip_workers` are all
AST-identical to the base commit, and `video_processor.py`, the Phase 3B diagnostic module,
`ANALYSIS_VERSION` and the analysis-cache identity are unmodified.

The Phase 3B regression fixture that carries the old `cuvidCreateDecoder` / decode-surface warning text
is **deliberately retained**. Phase 3C removes the production trigger, not the requirement that the
diagnostic selector keep telling a recovered warning apart from the fatal encoder cause.

### Fixed — 2026-09-27 (Phase 3B: Stage 6 FFmpeg failure diagnostics)

A Stage 6 render of 701 sources lost every clip and reported `283 clip(s) failed; refusing to
concatenate an incomplete timeline`. FFmpeg had already said exactly why —
`Driver does not support the required nvenc API version. Required: 13.1 Found: 13.0` — but
`extract_clip_segment_ffmpeg()` printed that to stdout and returned a bare `False`, `create_clip_parallel`
turned the `False` into the fixed string `"FFmpeg extraction failed"`, and the GUI redirects stdout into
`QuietConsole`. Identifying the cause took a dedicated forensic phase. This change makes the reason
travel with the failure. Diagnostics only: no encoder, command, timing or render behaviour changed.

- **New fork module `src/beatsync_fork/ffmpeg_diagnostics.py`** (stdlib-only). `summarize_ffmpeg_failure`
  ranks stderr lines and returns one bounded line; `describe_output_problem` and `describe_exception`
  cover the cases where there is no stderr worth quoting. Vendor-neutral by construction: the marker
  tuples contain no NVIDIA/NVENC/CUDA-specific tokens and no incident-specific values such as `13.1` or
  `610.00`, and a test asserts exactly that token set. Generic diagnostic terms stay intentionally
  allowed — `_SPECIFIC_MARKERS` does include `"driver"`, which matches any vendor — so the next failure
  family benefits too.
- **`extract_clip_segment_ffmpeg()` keeps its `-> bool` signature** and becomes a thin delegate to the
  new `extract_clip_segment_ffmpeg_detailed() -> Tuple[bool, str]`. The call graph shows exactly one
  in-repo caller, but the boolean function is a module-level API in an upstream file, so it was left
  compatible rather than converted. The FFmpeg command construction is **byte-identical** to the base
  commit (all 17 `cmd`/`filters`/frame-arithmetic statements compare equal by AST).
- **`create_clip_parallel` reports `f"FFmpeg extraction failed: {reason}"`**, keeping the historical
  wording as a prefix so existing expectations still match. Everything downstream already worked:
  `clip_failures` → Stage 6 warning → `first_failures[:3]` on the refusal → `ProgressView`. No GUI, no
  `progress.py` and no `ProgressView` change was required; `gui.py` is untouched.
- **A successful clip stays successful.** The summariser is only reached inside the
  `returncode != 0` branch, asserted by a seam test. This mattered concretely *at the time of Phase 3B*,
  when the NVENC path still requested `-hwaccel cuda`: successful NVENC clips on driver 617.14 were
  emitting `cuvidCreateDecoder … CUDA_ERROR_INVALID_VALUE` / `more than 32 (33) decode surfaces` while
  FFmpeg fell back to software decode, so the validated 150-clip render would otherwise have acquired a
  large crop of spurious "reasons". Phase 3B observed the behaviour without quantifying it; Phase 3C
  later measured it at **48 of 60** old-path clips failing CUDA decode initialisation, with one sampled
  source family engaging real NVDEC and emitting no such warning — so it was most successful clips, not
  all of them. (Phase 3C also removed the request, so current renders no longer emit it — the rule
  itself is unchanged and still load-bearing.)
- **Bounded**: 240 characters, one line, control characters stripped, heap addresses collapsed so the
  same failure yields a reproducible string. A 1 MB stderr produced a 61-character reason in test. The
  full text still reaches the console through the pre-existing print.
- **The complete-timeline refusal is untouched.** Measured against real `create_music_video` with two
  clips forced to fail out of nine: `RuntimeError: 2 clip(s) failed; refusing to concatenate an
  incomplete timeline.`, no output file written, `failed_clips=2 total_clips=9`, two warning events and
  two `first_failures` entries each carrying the decisive driver/API text within 215 characters.

One defect the fixture caught before commit: the first selector took the *earliest* diagnostic line,
which in the real capture is the recovered nvdec warning four lines above the fatal encoder error. The
selector now anchors on FFmpeg's consequence lines and picks the specific lines nearest that boundary.

Verified on the current environment (RTX 3080, driver 617.14): real NVENC extraction returns `True` with
2,964,751 bytes and exactly 60 frames, its detailed reason empty; real CPU H.264 extraction returns
`True` with 11,944,372 bytes and exactly 60 frames. `ANALYSIS_VERSION`, cache identity, the frame-lock
timeline builder, NVENC/CPU quality arguments, `_effective_clip_workers`, `-hwaccel cuda` and
`logger.check_nvenc()` are all unchanged; 21 protected files are blob-identical to the base commit.

### Added — 2026-09-26 (Phase 2B: live Qwen worker progress)

Closes the last observability hole in the pipeline: the parent → Python-worker subprocess boundary.
Observability only — no creative, inference, scoring, cache-identity or render behaviour changed.

- **New fork module `src/beatsync_fork/qwen_progress.py`** (stdlib-only, like the rest of the package).
  Three pieces: a namespaced one-line JSON wire protocol (`encode`/`decode`/`is_protocol_line`), a
  `QwenProgressTranslator` that folds payloads into Stage 5 `phase="qwen"` events, and
  `stream_worker_process()` / `run_qwen_worker()`, the `Popen`-based runner.
- **The worker emits machine-readable progress** (`src/auto_mode/stage5_qwen_scene_worker.py`).
  `BEATSYNC_QWEN_PROGRESS\t{…}` lines for `worker_state` (`loading_model`, `backend_ready`,
  `prefetch`), `job_start`, `job_progress` and `job_end`, **additive** to the existing human-readable
  lines, which are byte-for-byte unchanged. Emission goes through a guarded `_emit_progress()` that
  swallows every failure — including a missing fork package — because a status line must never cost a
  run that has already spent GPU minutes. The only other change to this file is an optional
  `job_context` parameter on `_run_semantics_for_video()` carrying
  `job_index`/`job_total`/`job_id`/`source_name` so emitted lines can be attributed. Prompt, schema,
  vocabularies, device/slot/context selection, frame prefetch, inference waves and the
  `LlamaServerClient`/`LlamaMtmdClient`/`QwenLlamaClient` classes are all AST-identical to the base
  commit.
- **The parent streams instead of capturing** (`src/video_analysis.py`).
  `_run_qwen_worker_batch()` and `_run_qwen_worker()` now call `fork_qwen.run_qwen_worker()` with the
  same argv, environment, UTF-8-with-replacement decoding, timeout and `{}`-on-failure contract they
  had with `subprocess.run(capture_output=True)`. `event_callback` is threaded through
  `analyze_video_sources` → `_analyze_single_video` / `_complete_deferred_qwen` /
  `_complete_deferred_qwen_batch` → `_annotate_candidates_with_qwen` → both worker launches, so live
  progress works in **both** execution modes rather than silently only in batched runs.
- **stdout and stderr are drained concurrently** on two threads while the main thread owns
  `wait(timeout=…)`. Required, not decorative: a failing llama.cpp run emits megabytes of Vulkan
  diagnostics, and draining stderr only after `wait()` deadlocks once the pipe buffer fills. stderr is
  retained as a bounded tail (2400 chars batch / 1800 single — the limits the old code already
  printed), so RAM stays flat however loudly the worker fails.
- **Reported truthfully.** Per-job progress is an *uncounted* `STATE` event: the only provable
  denominator is the current job's `len(frame_items)`, so the panel shows
  `Qwen job 17 / 300 · 64 / 120 candidates (53.3%) · 2.1 candidates/s · batch 8` and never invents a
  cross-job total. The rate is the worker's own measured `idx / elapsed`, labelled **`candidates/s`**.
  Staying uncounted also keeps `· 758 sources completed` visible and avoids fighting `ProgressView`'s
  per-`(stage, phase)` monotonicity, which would otherwise reject job 18's restart at `1 / 130` as a
  stale straggler.
- **The response JSON is still the only semantic authority.** Nothing is reconstructed from stdout; a
  test asserts the streaming path returns exactly what the old capture path returned for identical
  response JSON, and that results are identical with and without a progress callback. Request/response
  files remain retained per the project's PCBUS-HK-v1 override (the `finally:` blocks are still bare
  `pass`).
- **Scope of `Popen`.** `stage5_qwen_scene_worker.py` already used `subprocess.Popen` for its internal
  `llama-server` lifecycle long before Phase 2B, handing it dedicated log-file handles rather than the
  worker's pipes; that code is untouched and a test pins it to exactly one `Popen` inside
  `LlamaServerClient._start`. The short `llama-mtmd-cli --version` probe remains a plain
  `subprocess.run`. A repo-wide "no `Popen`" invariant would be both false and about the wrong
  boundary.
- **Measured, not assumed.** Red evidence first: under `capture_output=True` a worker emitting its
  first progress line at ~0s delivered **0 callbacks before exit**, first observed at 0.857s — the exit
  timestamp. After the change, the real worker with the real Qwen3VL-2B GGUF on Vulkan reported
  `loading model` at +0.17s and `backend ready` at +8.98s of a 10.38s run. Timeout process-tree
  boundary was measured both ways: the direct Python worker is killed and an already-started
  `llama-server` is **orphaned**, identically to the pre-Phase-2B path, because both kill only the
  direct child. That gap is pre-existing and was deliberately not "fixed" with `taskkill /T`.

Two defects found by the new tests and fixed before commit: a job's first `job_progress` was being
swallowed by the throttle window (the update that replaces "starting" with a real count), and closing a
pipe from the waiting thread blocked on the reader's buffer lock, turning a 2s timeout into a 120s
return whenever a grandchild held the write end — reader threads now own their own close and are joined
against one shared deadline.

`ANALYSIS_VERSION` is unchanged (`auto_av_analysis_v8_llama_vulkan_batched`): a progress-only change must
not invalidate a 758-video analysis cache. `tests/test_no_runtime_dependency.py` now discovers fork
modules from disk instead of a hardcoded list, so a future module cannot escape the stdlib-only guard.

### Fixed — 2026-09-26 (Phase 2A review remediation, round 3)

- **`ProgressEvent.data` is now immutable *recursively*** (`src/beatsync_fork/progress.py`). R2's
  `MappingProxyType` was shallow, and the pipeline genuinely emits nested mutable values —
  `section_types=[...]` from Stage 3, `first_failures=[...]` from the Stage 6 refusal — so
  `event.data["section_types"].append("evil")` still succeeded, and `as_dict()`'s shallow copy handed
  the *same* nested objects to every consumer. Small stdlib-only `_freeze`/`_thaw` pair: mappings
  become `MappingProxyType` over recursively frozen copies, lists and tuples become tuples, scalars
  (including `str`/`bytes`) are untouched. `as_dict()` recursively thaws back to plain
  `dict`/`list`, so the payload shares no mutable container with the event and stays JSON
  serialisable. Only the container types the pipeline actually emits are handled; no serialization
  framework was added.
- **Stage 5 no longer bills cache hits as analysis throughput** (`progress.py`,
  `src/video_analysis.py`). `StageCounter.rate` was `current / elapsed`, and the source counter
  advances for cache hits too, so 420 instant cache hits plus one slow real analysis reported
  **839.9 sources/s** in the reproduction. The rate basis is now tracked separately from the
  completion count: `advance(..., counts_toward_rate=False)` records a cache hit without billing it,
  and `begin_rate_window()` re-bases the clock once the cache scan finishes. The count still reads
  `421 / 758` with cache hits visible; the rate now reads `0.1 analyzed sources/s`, labelled via a
  `rate_unit` so a bare `sources/s` cannot be misread next to a count that includes cache hits.
  **Stage 6 clip throughput is unchanged** — it opens no rate window, so its basis is the whole
  counter exactly as before. No ETA was added.
- **A stale straggling progress event no longer wipes a good measurement** (`progress_view.py`).
  Monotonicity protected `current` but `rate`/`elapsed`/`message` were overwritten unconditionally, so
  an out-of-order Stage 6 event carrying no rate silently degraded
  `612 / 1216 · 4.8 clips/s · elapsed 2m 07s` to `612 / 1216`. An event ignored for the count is now
  ignored for those fields too, and a known measurement is never replaced by `None`. Found by the
  portable UI smoke, not by the unit tests.
- **28 new tests** (`tests/test_progress_truth.py`), 16 of which were confirmed failing against
  `95150de` first, including AST/source assertions that `video_analysis.py` really opts cache hits out
  of the rate, really re-bases the window, and really labels the rate unit.

### Fixed — 2026-09-26 (Phase 2A review remediation)

- **Progress counters are now subphase-aware** (`src/beatsync_fork/progress_view.py`,
  `src/video_analysis.py`). Monotonicity was enforced per *stage*, but Stage 6's ProRes path contains
  two counted subphases with different units and denominators — conversion counts sources, extraction
  counts clips — so the conversion count leaked into extraction. Reproduced before the fix:
  758 sources followed by 100 segments rendered **`758 / 100 (758.0%)`**; with 1216 segments,
  extraction appeared to begin **62.3% complete**. The monotonicity key is now `(stage, phase)`, taken
  from the `data["phase"]` marker the pipeline already emitted. Nothing carries across a phase
  boundary — not the count, total, unit, rate or elapsed time — while monotonicity *within* each phase
  is unchanged, so a straggling `4 / 10` after `7 / 10` still shows `7 / 10`.
- **No fake percentage for uncounted phases.** `Final assembly started` previously still displayed
  `1216 / 1216 (100.0%)` as though the assembly itself were complete; the Qwen phase likewise wore the
  deterministic pass's `758 / 758 (100.0%)`. An uncounted active phase now shows its own state plus a
  history line (`· 1216 clips completed`) instead of inheriting a counter. The Qwen state events carry
  `phase="qwen"` so the view can tell the two apart. **Qwen live N/T progress remains NOT implemented
  (Phase 2B); no `Popen` was introduced.**
- **`ProgressEvent.data` is genuinely read-only.** `frozen=True` only prevented field rebinding, so
  `event.data["x"] = ...` succeeded and an event handed to several consumers could be edited under the
  others. `data` is now a `MappingProxyType` over a private copy; `as_dict()` is built field by field
  because `dataclasses.asdict` deep-copies and cannot handle a mappingproxy. Stdlib only, no
  serialization framework.
- **23 new tests** (`tests/test_progress_phases.py` plus immutability regressions), including AST
  assertions that `video_processor.py` really emits `prores_convert` / `prores_extract` / `assembly`
  and `video_analysis.py` really emits `qwen`, so the suite cannot drift into testing an invented
  event shape.

### Added — 2026-09-26 (Phase 2A — structured progress / observability)

- **Structured progress core** (`src/beatsync_fork/progress.py`, `progress_view.py`): an immutable
  `ProgressEvent(stage, kind, message, current, total, elapsed_seconds, rate, data)` with a `Stage`
  enum, `EventKind` (start/progress/metric/state/warning/error/end), JSON-friendly `as_dict()`, and a
  `StageCounter` that is monotonic, bounded and throttled (~2 updates/sec, always emitting the final
  one). `emit()` swallows any callback exception — and a `None` event — so observability can never
  fail a render; `KeyboardInterrupt` still propagates. Stdlib-only, so the whole core is testable on a
  bare interpreter. `ProgressView` accumulates events into the status panel.
- **The GUI no longer parses prose for stage identity.** `gui.py` previously recovered the current
  stage with `re.search(r"Stage (\d+) is processing", message)`; that regex is gone. Stage identity is
  now the integer `event.stage`. The existing architecture is preserved — worker thread → `queue.Queue`
  → generator → widgets — and the `event_callback` only ever enqueues, so no Gradio component is
  touched from a worker thread (asserted by a test).
- **Stages 1-4** emit start/end boundaries with useful metrics (beats + tempo, features profiled,
  section count, cuts/beats/ratio/interval/preset). No ETAs: these stages take seconds, so a projection
  would be noise.
- **Stage 5 deterministic analysis** reports real source progress — `total` is the actual source count,
  **cache hits count as completed work** (a fully cached run shows completion rather than sitting at
  zero), and a video that fails in parallel and is retried serially advances the counter exactly once.
  Worker count and cache-hit count are reported as metrics.
- **Stage 6 rendering** reports `current / total` clips against the frame-locked segment count, with
  measured rate and elapsed. `current` advances only for clips actually created, individual failures
  surface as concise warnings (first three in detail, then a running count), and the
  incomplete-timeline refusal — unchanged in behaviour — is now visible in the UI instead of only on a
  discarded stdout.
- **ProRes** reports both of its serial loops: sources converted and segments extracted.
- **Final assembly** emits start/finish states and a concise error on failure. No fake percentage for a
  single FFmpeg call.
- **Backward compatible.** `progress_callback` and `console_callback` are untouched and
  `event_callback` is optional, so CLI/headless callers keep working. Legacy string statuses are still
  accepted on the same queue, but only shown before any structured event arrives, so they cannot
  overwrite richer output.
- **Qwen live progress is NOT implemented.** `video_analysis.py` still launches the worker with
  `subprocess.run(capture_output=True)`, so the worker's own `Qwen llama.cpp tagged N/T` lines remain
  invisible to the parent until it exits. Only honest high-level states are emitted
  (started / tags N/T / finished / failed). Live streaming is **Phase 2B**; no `Popen` was introduced.
- **100 new tests** (`test_progress_core.py`, `test_progress_sequence.py`, `test_gui_progress_seam.py`)
  plus the fork no-runtime-dependency guard extended to both new modules.

### Fixed — 2026-09-26 (Phase 1B review remediation)

- **The render gate now validates the LIVE source controls, not only the stored session state**
  (`src/beatsync_fork/input_confirmation.py`, `src/beatsync_fork/input_session.py`, `src/gui.py`).
  Gradio delivers widget changes as separate queued events, so at the moment Create is clicked the
  `gr.State` can lag behind the widgets. Reproduced before fixing: a browser confirmation of 2 files
  was **allowed** while the live `gr.File` value already held 3, because `current_snapshot_for_render`
  re-stat'ed `confirmed.paths` instead of the live list; likewise a confirmed folder was allowed while
  the folder textbox already pointed elsewhere. `process_video_guarded` now receives `source_mode`,
  `source_folder`, `source_recursive` and `video_input` as render-request inputs and builds a
  `LiveSourceDeclaration`; `check_declaration()` compares declared intent (mode / folder / recursive)
  before any filesystem work, so a folder the user has navigated away from is never even scanned. The
  event-driven invalidation is unchanged and still provides immediate UX feedback. Handler parameter
  names mirror the widget names because Gradio supplies them positionally, and a test asserts the two
  lists line up name-for-name.
- **Folder identity now covers the whole supported scope, not just the ready subset** (same modules).
  Reproduced before fixing: after confirming 1 ready `.mp4`, an external writer created a 0-byte
  `new.mp4`; the scanner rejected it as `empty_file`, the ready list was unchanged, and the gate
  **allowed** the render even though a new supported source entry had appeared. This is not
  hypothetical — the real `Cuts` library was observed gaining about one MP4 per minute. `SourceSnapshot`
  gained `excluded`, an ordered tuple of `ExcludedEntry(path, reason)` for supported-extension files the
  scan could not use, and it participates in the digest (`SNAPSHOT_VERSION` → v2). Only the stable
  reason code is recorded, never OS error text, which would make the digest unstable. Unsupported files
  (`.mp3`, `.txt`, `.jpg`) are deliberately excluded from identity, so adding one does not invalidate a
  confirmation. The render list remains the **ready** files only; no media is hashed.
- **35 new tests** (`tests/test_input_gate_live.py`, `tests/test_gui_guard_seam.py`): live browser list
  +1 / −1 / same-count replacement / reorder / empty / missing file, live folder / recursive / mode
  changes, "a folder outside the declaration is never scanned", new empty and new unreadable supported
  files, rejected↔ready transitions, unsupported additions ignored, and a dependency-injected seam test
  proving `process_video` is never reached after a denial.

### Added — 2026-09-26 (Phase 1B — GUI source modes + confirmation gate)

- **Local folder video-source mode** (`src/gui.py`, `src/ui_content.py`), the new default and the
  recommended mode for large libraries. It calls the existing `beatsync_fork.input_manager` — no
  duplicate enumeration logic in the UI — and shows exact
  `Discovered / Supported / Rejected / Ready / Duplicates / Total size`. Source files are referenced
  **in place**: no Gradio upload, no copies. Browser-files mode remains available and unchanged.
- **Authoritative confirmation gate.** `Create Music Video` now starts disabled and is enabled only by
  an explicit `Confirm N files` action.
- **`src/beatsync_fork/input_confirmation.py`**: `SourceSnapshot` — an ordered identity over
  (normalised path, size, mtime_ns) per file plus mode, scan root and recursive flag, digested with
  SHA-256. Confirmation is over the **set**, not the count, so two different lists of equal length are
  never interchangeable. File contents are not hashed: the head+tail fingerprint in `input_manager` is
  for duplicate candidacy and must not become a per-render cost. Also provides `describe_change()` for
  actionable abort messages and `evaluate_gate()`.
- **`src/beatsync_fork/input_session.py`**: the source state machine (`set_mode`, `set_folder_path`,
  `set_recursive`, `scan_folder_action`, `set_browser_files`, `confirm_action`) and
  `resolve_for_render()`. Pure, Gradio-free and stdlib-only, so the gate is fully testable without a
  web server.
- **Render-time re-verification.** `process_video_guarded()` in `gui.py` re-derives source identity
  from the filesystem on every click and refuses before Stage 1 if it no longer matches the
  confirmation. UI disablement alone is treated as a courtesy, not a guarantee.
- **Invalidation rules**: mode switch, folder path change, recursive toggle, re-scan, and any browser
  file-list change all clear the confirmation. FPS, encoder, output filename and audio deliberately do
  not — they are not source identity, and a test asserts the confirmation survives them.
- **Honest browser semantics.** Browser mode reports only `Backend ready: N`. The browser-side
  selected/pending count is never transmitted to Python, so it is never displayed; a test asserts no
  such number is printed.
- **64 new tests** (`tests/test_input_confirmation.py`, `tests/test_input_gate.py`) covering snapshot
  identity (added / removed / replaced-same-count / renamed / size / mtime / order / root / recursive /
  mode), empty-set refusal, and the full gate matrix. The fork no-runtime-dependency guard now covers
  the two new modules as well.

### Added — 2026-09-26

- **Repository workflow policy** (`CLAUDE.md`): `DU-REPO-WORKFLOW-v1` task-closeout rules.
- **Fork identity surface** (`src/beatsync_fork/__init__.py`): `FORK_NAME`, `FORK_VERSION`,
  `UPSTREAM_BASELINE_COMMIT`, `fork_identity()`.
- **Local input manager core** (`src/beatsync_fork/input_manager.py`,
  `src/beatsync_fork/input_report.py`): deterministic local-folder scanning with explicit
  discovered / supported / rejected / duplicate / ready accounting and cheap duplicate-candidate
  fingerprinting. **Not yet wired into the GUI** — see `CLAUDE.md`.
- **Test harness** (`pytest.ini`, `requirements-dev.txt`, `tests/`): first automated tests in the
  project, including a 1000-file regression test proving the input manager does not truncate large
  source lists.
- **`.gitignore`**: runtime/build artefacts (`bin/`, `input/`, `output/`, `__pycache__/`).

### Changed — 2026-09-26

- **`README.md`**: added a fork notice and a licence section. Upstream content is otherwise unmodified.

### Fixed — 2026-09-26

- **Input scan no longer silently skips an unscannable directory** (`src/beatsync_fork/input_manager.py`).
  `os.walk` ignores `scandir` failures unless an `onerror` callback is supplied, so an unreadable,
  vanished or disconnected nested directory was skipped in silence while `scan_folder()` still
  returned an `InputSet` that could report INPUT READY — the same silent-truncation failure the module
  exists to prevent, sourced from the filesystem instead of the browser. Traversal is now
  all-or-nothing: a complete walk returns an `InputSet`, and any directory that cannot be listed
  raises `InputScanError` naming the failing path and preserving the original `OSError` as `__cause__`.
  `followlinks=False` and all ordering, classification and accounting semantics are unchanged.
  Individual files that cannot be stat'ed remain `unreadable` rejections rather than scan failures,
  because a named rejection is not a silent loss.
- **Non-recursive scans no longer drop an entry whose metadata probe fails**
  (`src/beatsync_fork/input_manager.py`). The non-recursive iterator selected entries with
  `os.path.isfile()`, which *suppresses* stat/access errors and returns `False`. A locked, offline or
  vanished top-level source file therefore disappeared before the classifier saw it — absent from
  `ready`, absent from `rejected`, and missing from `discovered_count`, so even the
  `discovered == ready + rejected + path_collisions` invariant could not detect the loss. Reproduced:
  a three-file folder with one unreadable `.mp4` reported `discovered=2, ready=2, rejected=0` and
  INPUT READY. The iterator now decides only "directory or not" from the `scandir` entry and passes
  everything else to the classifier, which accounts for it as `unreadable`. This also removes a
  redundant `stat` per entry. Ordinary directories are still excluded; recursive behaviour,
  ordering, duplicate and collision semantics are unchanged. Side effect: a broken symlink at the top
  level is now reported as an `unreadable` rejection instead of vanishing.

### Removed — 2026-09-26

- Untracked the stale compiled artefact `src/auto_mode/__pycache__/stage5_qwen_scene_worker.cpython-313.pyc`
  from Git. The file was left on disk (it is regenerable, and removing files on disk is not this
  change's business).

### Unchanged

No change to generated video output. Auto Mode stages 1-6, `AutoWaveConfig` defaults, the analysis
cache format, seed/randomness behaviour, `gui.py` and the render path are untouched.

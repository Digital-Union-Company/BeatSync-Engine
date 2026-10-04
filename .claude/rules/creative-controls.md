---
paths:
  - "src/beatsync_fork/creative.py"
  - "src/beatsync_fork/variation.py"
  - "src/beatsync_fork/deterministic_view.py"
  - "src/auto_mode/stage4_select.py"
  - "src/auto_mode/stage6_av_planner.py"
  - "tests/test_creative_profile.py"
  - "tests/test_creative_scoring.py"
  - "tests/test_creative_seed.py"
  - "tests/test_creative_controls_interactions.py"
  - "tests/test_creative_controls_seam.py"
  - "tests/test_cut_density.py"
  - "tests/test_micro_cuts.py"
  - "tests/test_semantic_emphasis.py"
  - "tests/test_source_diversity.py"
  - "tests/test_deterministic_view.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

## The creative variation seed (Phase A)

The user picks a **Variation Seed**; it changes which clips the planner chooses, and nothing else.

- **Seed 0 is legacy, and that is a product contract, not an implementation detail.** `_choose_candidate`
  keeps its original argmax branch for seed 0 — same penalties, same `rng.random() * 0.015`, and
  critically the same RNG stream: `_stable_rng(index, target, start)` with **no** seed component.
  Prepending a `0` would change the hash input, change the jitter and silently change every default
  render. A positive seed uses `_stable_rng(seed, index, target, start)` instead. A test recomputes
  the pre-seed algorithm independently and pins the legacy plan against it.
- **A positive seed changes the winner rule, not the scoring.** `_adjusted_score` is the old inline
  arithmetic lifted out verbatim so both branches score identically; `variation.select_index` then
  takes the best `TOP_K = 6`, drops anything more than `SCORE_WINDOW = 0.12` below the best, and makes
  a weighted draw. The window is deliberately smaller than the planner's own 0.28 "seen recently"
  penalty, so variation can never undo a repeat penalty the planner applied on purpose.
- **The seed rides on `beat_info["creative"]`.** `analyze_beats_auto(creative=…)` normalises it once
  and stores it; Stage 6 is the only reader. No analysis signature changed, and `create_music_video`
  did not change at all. **Creative Controls Core generalised that dict** from `{"seed": n}` to the
  whole resolved profile — see the section below. `variation.py` remains the sole authority on what a
  seed means, and `CreativeProfile` delegates every seed decision to it rather than reimplementing
  any of this.
- **It must never touch cache identity.** `video_analysis.py` was unmodified by Phase A, both version
  constants were unchanged, and the seed is absent from `_video_signature` / `_cache_path` /
  `_qwen_config_token` and from `audio_visual_profile`. At the time, that last clause mattered because
  `audio_visual_profile["smart_preset"]` *was* keyed into the Qwen config token, so the profile was the
  one dict a future creative control could accidentally re-key 845 Qwen records through. **P2 closed
  that route entirely**: no `audio_profile` field reaches Stage-5 identity any more. Keeping creative
  state off the profile is still right — it describes the track — but it is no longer the last line of
  defence. Tests assert all of it by AST. Changing the seed re-plans; it never re-analyses.
- **It is not source identity either.** The widget is outside the Video Source group and is wired into
  no source handler and no `source_outputs`, so changing it cannot clear a confirmed source set. It is
  a render-request input alongside FPS and the encoder, and `test_gui_guard_seam.py` still pins the
  positional alignment between the click `inputs` list and the handler's parameters.
- **Anything not a positive whole number normalises to legacy**, and the boundary is an explicit type
  check rather than `int(value)`: `None`, `""`, a negative, `NaN`, `inf`, **`7.9` and `True`** all
  become 0. Truncating `7.9` to 7 would render a seed the user never chose and would make two
  different inputs reproduce as the same "reproducible" variation; `bool` has to be rejected first
  because it subclasses `int`. A number box can produce all of these and none may raise mid-render.

## The Creative Profile — three more controls (B0 + Creative Controls Core)

`beatsync_fork/creative.py` holds one immutable, normalised **`CreativeProfile`** carrying four
controls. It is stdlib-only, so the whole mapping is testable on a bare interpreter.

```
CreativeProfile(seed=0, cut_density=50, energy_response=50, motion_bias=50)
```

| Control | Range | Neutral | Owner | What it changes |
|---|---|---|---|---|
| Variation Seed | 0 / positive | 0 | Stage 6 | which of the good candidates wins |
| Cut Density | 0–100 | 50 | **Stage 4** | how many beats become cuts |
| Micro Cuts | 0–100 | 50 | **Stage 4** | the rare half-beat accent layer, and only that |
| Semantic Emphasis | 0–100 | 50 | Stage 6 (static) | deterministic visual evidence vs. semantic reading |
| Energy Response | 0–100 | 50 | Stage 6 (static) | how hard scoring follows the segment's target |
| Motion Bias | 0–100 | 50 | Stage 6 (static) | calm vs. dynamic source material |
| Source Diversity | 0–100 | 50 | Stage 6 (**dynamic**) | how hard cuts spread across source videos |

Stages 1–3 read **none** of it; Stage 5 reads **none** of it. Stage 4 reads Cut Density and Micro
Cuts; Stage 6 reads the seed, Semantic Emphasis, Energy Response, Motion Bias and Source Diversity.

**Creative Presets (PR3) are not an eighth control.** They are named sets of values for the six
0–100 sliders, resolved entirely in the UI — see `.claude/rules/creative-presets.md`. Nothing in this table
changes because a preset was selected; only the slider values do.

**AI Director V1 is not an eighth control either, and not a new Stage-4/6 mechanism.** It is a
**second reviewed producer of these same seven values**, alongside Variant Lab:

```
PRESET SELECTOR  ->  writes six slider values            (PR3)
VARIANT LAB      ->  resolves a CreativeRecipe from a master seed, ranges and a spread   (C2/C3)
AI DIRECTOR      ->  proposes a CreativeRecipe from one sentence, applied on an explicit press
THE SEED + SIX SLIDERS  =  still the sole execution truth
```

Director Apply writes `variation_seed`, the six sliders and the preset label — and nothing else. It
produces a `CreativeRecipe`; this table, `CreativeProfile`, Stage 4 and Stage 6 keep ownership of
what those numbers *mean*, and no stage learns that a Director exists. Generate Proposal writes no
execution widget at all. The full contract, including the measured one-shot invocation and why
`CreativeRecipe.from_mapping` is reused unmodified, is `.claude/rules/director.md`.

**Freestyle V1 is not an eighth control either — it is the first thing that makes five of these
seven values stop being global.** Presets, Variant Lab and the Director are all *producers* of one
global seven-value recipe. Freestyle is orthogonal to all three: it declares a sparse, per-**section**
delta applied on top of whatever the sliders currently hold.

```
THE SEED + SIX SLIDERS   = the global base, still the sole execution truth
A FREESTYLE RULE         = a per-SECTION override of five of them, resolved at render time
    cut_density · semantic_emphasis · energy_response · motion_bias · source_diversity
STILL EXACTLY ONE EACH   = the Variation Seed, and Micro Cuts
```

Two consequences for this table. **Cut Density's owner becomes "Stage 4, per section"**: configs are
derived per distinct effective density and the global `final_wave_cleanup` is replaced by a
section-scoped cleanup plus a boundary pass, because its density band is computed from the global
beat count. And **Stage 6's static/dynamic split is now load-bearing twice over**: `ScoringControls`
becomes a per-*segment* value and the L1A table grows a key dimension
(`(ScoringControls, target)`, built lazily), while Source Diversity stays threaded separately exactly
because it reads the running `usage` counter. A control moved into the wrong half would now become a
table key per section rather than once per render.

`beat_info["freestyle"]` is the bus key, with one reader per stage. Changing a rule **re-plans and
never re-analyses**, and nothing about a rule reaches Stage-5 identity. Full contract:
`.claude/rules/freestyle.md`.

**Stage 6 has two halves and the split is load-bearing.** Semantic Emphasis, Energy Response and
Motion Bias are *static* — they depend on (candidate, target) only, so they live in the L1A
precompute table.
Source Diversity is *dynamic*: it reads the running `usage` counter and the `recent_videos` window,
so it must never enter that table. `ScoringControls` therefore carries the static half only, and the
diversity factor is threaded separately as an explicit parameter. A control in the wrong half is one
refactor away from becoming a table key.

**50 is today, exactly, and that is a product contract.** An all-neutral profile reproduces current
main's selected cut times, segment targets, candidate choices, legacy seed-0 RNG stream, plan length
and filenames. The mechanism is not "the arithmetic happens to be neutral" — every neutral control
takes an **explicit branch that calls the pre-existing legacy path**, because even a harmless
operation-order change (`flow + 1.0 * (target - flow)`, `score + 0.0`, a config rebuilt with a factor
of 1.0) would make "upgrade and change nothing" untrue. `ScoringControls` therefore uses `None`, not a
neutral number, for a neutral control, and Stage 4's `density_factor` is `None` rather than `1.0`.

`normalize_control` mirrors `variation.normalize_seed`'s explicit type boundary, with one deliberate
difference: a control has a real range with meaningful ends, so `120 → 100` and `-10 → 0` are
**clamped** rather than refused. A *fractional* value is different in kind — `50.5` is not a request
for 50, and flooring it would render a setting the user never chose — so it falls back to 50, as does
`bool` (rejected first, since `True` would otherwise read as "nearly maximally sparse") and anything
malformed. Nothing here may raise mid-render.

### Cut Density is a Stage 4 control

`f = 2 ** ((cut_density - 50) / 50)` → 0.5 / 1.0 / 2.0 at 0 / 50 / 100. Exponential so the control is
symmetric in *ratio*, which is how cut spacing is perceived. Applied in two halves, because density
lives partly in the config's safety floors and partly in the selector's stepping:

- `auto_mode.density_scaled_config` derives a **new** `AutoWaveConfig` with `dataclasses.replace`:
  the four `*_min_interval` floors and four `*_max_hold` ceilings divided by `f`, and
  `target_cut_ratio_min`/`_max` multiplied by it and capped at `0.95` / `0.98`. `AutoWaveConfig` is
  frozen and `CONFIG` is a process-wide singleton — **never mutate it**; the caps never actually bind
  at the reachable factor range (`0.46 × 2 = 0.92`) and exist only as a guard.
- `select_wave_cuts` / `select_section_wave_cuts` take `density_factor`. `adaptive_beat_step` stays
  the pure musical mapping it has always been — density **re-quantises its answer** afterwards via
  `creative.scale_beat_step`, `max(1, min(8, floor(step / f + 0.5)))`. Explicit half-up, not
  `round()`: banker's rounding sends both 1.5 and 2.5 to 2, flattening two different musical
  situations onto one spacing. The weak-score breathing threshold `0.42` scales by `1/f`.

**Rare micro-cut policy is not this control's business.** `enable_rare_micro_cuts`,
`max_micro_cut_ratio`, `micro_min_gap` and `micro_percentile` are identical at every density. The
absolute micro-cut *count* still moves, because `max_extra` is a ratio of a selected grid density does
change — a proportional consequence, not a policy change. **This is not the future Micro Cuts
control.**

Measured on the accepted design probe's real track (566 beats, 123 BPM, 278 s, 13 sections): neutral
147 cuts / 25.97 % / 1.876 s average interval; density 100 → 195 cuts / 34.45 % / 1.412 s (+33 %);
density 0 → roughly −52 %. The mapping is monotonic and retains safe rhythmic spacing. The range is
**asymmetric on purpose** — the selector is discrete and beat-anchored — so do not retune it to look
symmetric. The synthetic fixture in `tests/test_cut_density.py` is shaped like that track and
reproduces the same shape (139 → 204 at density 100, 139 → 75 at density 0).

Cut Density does **not** add cuts after Stage 4, and it preserves beat/bar/phrase alignment: it steps
through the existing grid differently. A cut at exactly `0.0` is reachable through
`final_wave_cleanup`'s pre-existing "too sparse, add clean anchors" branch, which a high density
reaches more often; `build_frame_aligned_cut_timeline` drops it as it always has.

### Micro Cuts is the other Stage 4 control, and owns one layer

`auto_mode.micro_cut_scaled_config` derives a config for the **rare half-beat accent layer** only —
the extras `add_rare_micro_cuts` may add on top of the main grid. It rewrites exactly two fields:
`max_micro_cut_ratio` (`0.025 * 3^d`, hard-capped at `MICRO_CUT_RATIO_CAP = 0.08`) and
`micro_percentile` (`96.5 − 6·d`, clamped to 90 … 99.9).

Three things it deliberately does **not** touch:

- **`micro_min_gap`** — the anti-flicker floor, not a creative dial. The point of a bounded accent
  layer is that it cannot become flicker.
- **The `wave >= 0.88` gate inside `add_rare_micro_cuts`** — measured on real material, that gate is
  *not* the binding constraint (the ratio budget is: at every percentile from 99 → 92 the candidate
  count is identical for wave gates 0.88 … 0.70). Exposing it would add a configurable literal for no
  behavioural gain.
- **`add_rare_micro_cuts` itself** — it still only reads `cfg`. The control is a derived config, not
  a new mechanism inside the algorithm.

**At 0 the layer is switched off** (`enable_rare_micro_cuts=False`) rather than scaled down: `0.025/3`
still rounds to one extra on a typical grid, and "None" on the slider has to mean none.

Measured on the real Nero track (Stages 1–4 only, no Stage 5): 0 → 0 extras / 143 cuts; 25 → 2 / 145;
**50 → 4 / 147, the exact production baseline**; 75 → 6 / 149; 100 → 11 / 154. Minimum gap constant at
0.464 s throughout.

**Composition order is density → micro, and the two are independent in policy.** Cut Density writes
none of the four micro fields and Micro Cuts writes none of the grid fields, so neither rewrites the
other. That is *not* a claim that the final counts are numerically independent: `max_extra` is a ratio
of the selected grid, so a denser grid still permits proportionally more accents. That pre-existing
proportionality is intended — do not "correct" it.

### Energy Response and Motion Bias are Stage 6 controls

Neither touches the cut timeline, the audio features, the sections, the per-segment target
distribution or anything Stage 5 persisted. They change **only** the static candidate score, at one
seam: `_effective_base_score(candidate, target, controls, flow_score=…)`.

One explicit, documented ordering:

1. the legacy `_static_base_score(candidate, target)` — **unchanged, and still taking only
   `(candidate, target)`**, so a scoring term depending on anything else cannot be added there by
   accident;
2. **Energy Response** — `flow + factor * (target - flow)` where `factor = 1 + ((r - 50) / 50) * 0.6`
   (0.40 … 1.60) and `flow` is the *same candidate's* score for the generic `flow` target. Low
   settings pull every segment towards generic visual suitability; high settings exaggerate what the
   music asked for. `flow` is its own fixed point, so a flow segment cannot move;
3. **Motion Bias** — `+ centered * 0.15 * (2 * motion - 1)`, symmetric about `motion = 0.5`, reading
   motion through the planner's existing `candidate["motion"] → semantic["camera_motion"] → 0.0`
   fallback (`_candidate_motion`). A second definition of "dynamic" would silently diverge;
4. one clamp back into the existing `[-1.0, 2.0]` range.

**0.15 is sized, not arbitrary:** large enough to be visible against the seeded selector's
`SCORE_WINDOW = 0.12`, and materially smaller than the planner's own `0.28` "seen recently" penalty —
so a candidate's own bias gain can never offset its own repeat penalty. Do not retune it without a
discovered defect. Neither control depends on the seed: they modulate scoring, the seed still picks
the winner afterwards.

**L1A survives, and that is load-bearing.** Both controls are render-scoped constants, so they add no
dimension to the static table — it stays `candidates × distinct targets`. Energy Response needs one
extra `candidates`-wide **`flow` column** (built only when the control is non-neutral, and only once),
because the blend needs a flow reference for every target even when no segment targets flow. Never
`candidates × segments`. The controls are **bound explicitly and passed down**, never read from a
module global; `controls` rides alongside `base_scores` through `_choose_candidate` → `_adjusted_score`
so the *fallback* path (a missing or misaligned table) recomputes the same number the table would have
held instead of silently dropping back to legacy scoring.

`_materialize_clip` records `plan["score"]` through the same effective scoring. It is diagnostic, but a
plan reporting the legacy score for a render that chose on a modified one is a quietly misleading
record. Clip timing and anchoring are untouched.

### Source Diversity is the dynamic Stage 6 control

`factor = 3 ** ((source_diversity − 50) / 50)` — 1/3 at 0, 1.0 at 50, 3.0 at 100 — and it multiplies
**exactly two** of `_adjusted_score`'s four repeat penalties:

| Penalty | Level | Scaled by Source Diversity? |
|---|---|---|
| `cid in recent_ids` → −0.28 | candidate | **no** |
| `min(0.28, usage[cid] * 0.10)` | candidate | **no** |
| `video_file in recent_videos` → −0.10 | source | **yes** |
| `min(0.18, usage[video_file] * 0.012)` | source | **yes** |

The scaling is `factor * legacy_penalty`, i.e. applied to the **already-capped** amount, so the cap
scales with the control rather than re-clipping it. Deque lengths (`recent_ids` 10, `recent_videos` 5)
are unchanged.

**The candidate-level protections are never scaled**, and that is the whole design: Source Diversity
decides how willing the edit is to return to the same *source video*, and must never be able to buy a
repeated *moment*. Tests assert both candidate penalties are exactly 0.28 / capped-0.28 at every
setting.

**It is dynamic, so it stays out of L1A.** It is resolved once per plan but read per segment against
`usage`/`recent_videos`, so it is threaded as an explicit `source_diversity_factor` parameter through
`build_planned_clip_sequence → _choose_candidate → _adjusted_score`, never through `ScoringControls`.
A test asserts the `_static_base_score` evaluation count is identical at diversity 0 / 50 / 100, and
that moving diversity alone does not trigger Energy Response's flow column.

Base 3 is measured, not assumed. On the real 509-candidate / 41-source TEST1 pool over 148 real
segments: unique sources 31 → 39, top-source usage 20 → 10, HHI 0.054 → 0.031, for a **~4 %** mean
legacy-score cost, with zero adjacent source repeats even at the reuse end. Base 2 was visibly weaker
(31 → 37); base 4 reached all 41 sources but started producing adjacent source repeats at 0.

### Semantic Emphasis reinterprets persisted media truth (PR2)

`factor = 1 + ((semantic_emphasis - 50) / 50)` -> 0.0 / 1.0 / 2.0. The effective static score is
`det + factor * (full - det)`, where `full` is `_static_base_score(candidate, target)` and `det` is
the **same scorer** applied to the candidate's reconstructed deterministic view. There is
deliberately no second scorer, so the two readings can differ only in the candidate handed to them.

**0 does not disable Qwen.** Stage 5 is untouched and still runs and persists exactly as before; this
changes only how an already-analysed library is interpreted at plan time. Never describe it as
"AI off" in UI text or docs.

**The deterministic view has to be reconstructed, because Stage 5 does not keep it.**
`_merge_semantic` fuses Qwen's reading into `quality_score`, `action_score`, `beauty_score`,
`tension_score`, `soft_score` and `tags` **in place**, discarding the pre-fusion values. What
survives is the raw CV layer underneath - `motion`, `brightness`, `contrast`, `saturation`,
`sharpness`, `colorfulness` are never written by the fusion - and Stage 5's deterministic scores are
a pure function of exactly those six. `beatsync_fork/deterministic_view.py` therefore recomputes the
pre-Qwen candidate *forward* from them. It is not an inversion: nothing is divided back out, no clamp
is undone, and nothing is inferred from the semantic side. The input candidate is never mutated.

**Stage 5 was deliberately not refactored to share those formulas**, so `deterministic_view.py` is a
second copy - and that is this feature's one real hazard. Editing production Stage-5 code to suit a
Stage-6 creative feature would risk changing persisted floating-point values and pull the cache
contract into it. The copy is the lesser risk **only because drift is made loud**:
`tests/test_deterministic_view.py` runs the *real* `_build_candidate` over a threshold-crossing
matrix and requires the view to be the **identity** on its output; separately extracts the quality
arithmetic out of the window-measuring code and evaluates it (quality never reaches
`_build_candidate` - it arrives in `metrics`, so the fixed-point test cannot see it); and fails if
`_merge_semantic` ever starts overwriting one of the six primitives. If Stage 5's deterministic
formulas change, those tests fail and reconciliation becomes a conscious decision.

**Energy Response blends against the semantic-adjusted flow score**, not the legacy one - otherwise
the two sides of that blend would describe the same candidate under two different interpretations.

**The effect is target-dependent, by construction.** Stage 5 motion-gates semantic action
(`0.28 * semantic * motion_gate`), so on high-motion material the persisted reading is already close
to the deterministic one. Measured on the real 509-candidate TEST1 pool, mean |semantic -
deterministic| static-score divergence is ~**0.143** on `soft` and **0.142** on `build`, but only
~**0.023** on `drop` and **0.029** on `rhythm`. That is correct behaviour - do not retune the factor
to manufacture drama on action material.

**On a candidate Qwen never touched the control is exactly inert**: the deterministic view *is* the
candidate, so `det + factor * (full - det)` is `det` for every factor. It cannot invent an "AI
effect" where no AI reading exists.

Real-material evidence (read-only): reconstructing deterministic quality and re-applying Stage 5's
documented fusion reproduces the persisted `quality_score` with max error **exactly 0.0** on 509/509
prepared TEST1 candidates.

### Stage 5 isolation is the real B0 invariant

No control reaches `_qwen_config_token`, `_video_signature`, `_cache_path`, a Qwen request, the Qwen
prompt or a persisted semantic payload. `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3`,
`ANALYSIS_VERSION` stays `auto_av_analysis_v8_llama_vulkan_batched`, and `video_analysis.py`,
`stage5_qwen_scene_worker.py` and the preparation workflow were **not modified**.

**Cut Density legitimately changes `audio_visual_profile`.** It changes `selected_beats`, so
`_build_audio_visual_profile` derives a different `average_cut_interval`, `cut_count` and possibly
`smart_preset`. That is allowed, and a test asserting otherwise would be false. The invariant is:
*whatever Cut Density changes upstream must never change Stage-5 cache identity or persisted media
semantics.* Since P2 the audio profile has zero executable effect inside Stage 5, so nothing derived
from it can reach a key — `tests/test_creative_controls_seam.py` proves it both structurally (no
identity function mentions a control) and executably (two densities' profiles, one cache path per
source, over the real extracted `_cache_path`).

The separation cuts both ways: `BEATSYNC_QWEN_MAX_WINDOWS`, `_FRAME_WIDTH` and `_MAX_NEW_TOKENS` must
still re-key, or the isolation would be achieved by keying on nothing.

### Wiring and reporting

```
creative_preset  ──.input()──▶  the six sliders      # PR3, UI only; goes no further than here
                 ◀──.input()──

process_video_guarded(…, variation_seed, cut_density, energy_response, motion_bias,
                      source_diversity, micro_cuts, semantic_emphasis, …)
  → CreativeProfile.from_widgets(…)      # the one normalisation seam
  → process_video(…, creative=profile)
  → _process_video_impl(…, creative=profile)
  → analyze_beats_auto(…, creative=profile.as_dict())
  → beat_info["creative"] = profile.as_dict()
  → stage6_av_planner.creative_profile(beat_info)     # the one reader
```

The preset selector sits entirely above that chain: it writes slider *values* and is itself absent
from `process_video_guarded`'s inputs, so everything from `from_widgets` down is unchanged by PR3.

- **The Variation Seed has exactly five writers, and the list is extended by review.** Randomize,
  the three Variant Lab registrations that write it through `variant_lab_outputs`, and — since AI
  Director V1 — `apply_director_btn.click`, because a proposal *is* a `CreativeRecipe` and a recipe
  carries the seed. Pinned as an exact sorted list in `tests/test_creative_controls_seam.py` with
  the list indirection resolved; `generate_director_btn.click` is deliberately absent.
- **Four sliders' worth of render-request creative state, not source identity.** All three new
  controls sit in the Creative Direction group, register **no** handler of their own, appear in no
  source or preparation handler's `inputs`/`outputs`, and are absent from `source_outputs` and
  `prep_outputs` — so changing one cannot clear a confirmation, disable Create Video, trigger a scan
  or touch Media Library Preparation. They *are* live inputs to `process_btn.click`, and
  `tests/test_creative_controls_seam.py` pins the positional alignment against
  `process_video_guarded`'s parameters (Gradio passes them positionally). The profile is built only
  **after** the gate has allowed the render, and none of the four reaches `live_declaration`.
  **Randomize still writes the seed only**, and there is deliberately no reset or randomizer for the
  three controls.
- **`creative_seed` is now `creative_profile(beat_info).seed`** — one reader for one dict, so the
  creative state cannot fork into two independently-parsed views. `creative_seed` stays as the named
  compatibility surface `video_processor` already uses.
- **Filenames are unchanged.** `CreativeProfile.filename_suffix()` delegates to
  `variation.filename_suffix(seed)`, so the existing `_seedNNN` rule is the whole rule and the three
  new controls contribute nothing. The resolved profile is reported instead: `render_info["creative"]`,
  `summarize_clip_plan`'s `creative` / `creative_text`, the success panel, and the console's existing
  `Planner:` line — which prints exactly `legacy` on a neutral render, so the **five-line CMD budget is
  not raised**.
- **CLI parity:** `--cut-density`, `--energy-response`, `--motion-bias`, all optional with
  `default=None` and **no argparse `type=`** — the values go through `normalize_control`, so a
  malformed or out-of-range value clamps or falls back exactly as in the UI instead of aborting the run
  inside argparse. Omitting all three is today's behaviour. No new CLI mode.

### L2 invalidation — V1 implemented, the rest still documented

**What is implemented (L2 V1).** A **process-local, one-entry, post-Stage-3** cache: changing *any*
creative control reuses the audio front end and Stages 1–3 — measured at **~15.735 s** on the real
Nero track — and recomputes from Stage 4 onward. All seven controls, every preset and every Freestyle
declaration reach one identical Stage-3 key, because none of them exists before Stage 4.

**What is NOT implemented**, and must not be inferred from the table below:

```
no Stage-4 cache        Stage 4 measured ~7.6 ms and ALWAYS reruns
no Stage-6 cache        Stage 6 measured ~2.93 s and ALWAYS reruns
no persistent cache     process-local only; a restart starts cold
```

Full contract: `.claude/rules/l2-stage-cache.md`.

**The table below is the semantic future boundary, and it is deliberately broader than V1.** It
describes the ownership a *complete* L2 would have; V1 implements only its "Stages 1–3" column, and
reuse of the Stage-5 media library is the pre-existing Stage-5 cache rather than anything L2 added:

| Control | Reuse | Rerun |
|---|---|---|
| Variation Seed | Stages 1–5 | Stage 6 + render |
| Energy Response | Stages 1–5 | Stage 6 + render |
| Motion Bias | Stages 1–5 | Stage 6 + render |
| Source Diversity | Stages 1–5 | Stage 6 + render |
| Semantic Emphasis | Stages 1–5 | Stage 6 + render |
| Cut Density | Stages 1–3 **and the Stage-5 media library** | Stage 4, Stage 6 + render |
| Micro Cuts | Stages 1–3 **and the Stage-5 media library** | Stage 4, Stage 6 + render |

Only the two Stage-4 controls invalidate Stage 4 — and both still reuse the Stage-5 library
completely, which is the whole reason this boundary is worth having. In V1 that distinction is
unobservable in the cache, because Stage 4 reruns for every row anyway; it becomes load-bearing only
if a Stage-4 artifact is ever added.

**A Freestyle rule inherits the row of whichever control it overrides**, and that is the point of
having written this table before the feature existed: a rule that sets only `motion_bias` would reuse
Stages 1–5 and re-run Stage 6; a rule that sets `cut_density` would also re-run Stage 4. A
declaration whose rules all land on the base density is *already* treated as a global render by Stage
4's uniform short-circuit, so an L2 key derived from the **resolved** per-section values — not from
whether the checkbox is ticked — would reuse correctly with no extra machinery.

In V1 that resolved-value key is **not built**: every declaration reuses the Stage-3 artifact and
Stage 4 reruns regardless, so there is nothing for it to key yet. It remains the design for a future
Stage-4 artifact, and `stage_cache.py` and `auto_mode/__init__.py` deliberately contain no
`Stage4CacheKey`, no Stage-4 artifact and no resolved-Freestyle key — a test asserts that.

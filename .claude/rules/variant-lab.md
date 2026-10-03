---
paths:
  - "src/beatsync_fork/variant_lab.py"
  - "src/beatsync_fork/creative_recipe.py"
  - "tests/test_variant_lab.py"
  - "tests/test_creative_recipe.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

## Variant Lab V1 generates one reproducible recipe (C2)

`beatsync_fork/variant_lab.py` + `beatsync_fork/creative_recipe.py`. The user declares **what may
vary**; the lab resolves **exactly one** thing that will be rendered:

```
VARIANT LAB     = what MAY vary          (configuration, ephemeral)
CREATIVE RECIPE = what WILL be rendered  (seven integers, the execution artifact)
THE SIX SLIDERS = where the recipe lands (unchanged execution truth)
```

Generating writes the **existing** Variation Seed and the six sliders and nothing else, so the
pipeline keeps receiving the same seven values it always has. No Variant Lab state reaches
`CreativeProfile`, `beat_info["creative"]`, `process_video_guarded`, `render_info`, the filename or
any stage — the planner has never heard of recipes. **It renders nothing**; the user still presses
Create Music Video. Multi-variant generation, batch rendering and variant comparison are **C3** and
a test asserts none of that machinery exists.

- **The base is always the live sliders.** No base control inside the lab and no cached snapshot:
  the six current values are read at click time, so Balanced explores around Balanced and a
  hand-tuned Custom explores around that. The preset *name* is never read. Ranges are **explicit
  constraints** and deliberately do **not** follow the base around; a base outside its range clamps
  to the nearest edge (`anchor = clamp(base, lo, hi)`) rather than raising.
- **Named RNG sub-streams, and this is the load-bearing part.** Key format, pinned by golden-vector
  tests: `"variant_lab|1|<master>|<domain>|<name>"` → SHA-1 → first 12 hex digits → `random.Random`.
  Domains are `clips`, `controls` and `audio` — all three are **used** since E2 V1. A single sequential
  `random.Random(master)` would be simpler and is exactly what this must not be: adding one control
  later would shift every subsequent draw and silently invalidate every master seed a user wrote
  down. Five properties each have a test — enabling/disabling another control, re-ranging another
  control, reordering declarations, appending a future control, and adding a future *domain* — all
  leave a control's value and the clip seed untouched. `hashlib`, never `hash()` (process-randomised).
- **Golden vectors are literals**, recomputed independently rather than by calling the helper twice,
  so a change to the namespace, separator, digest, slice width or version position breaks a test
  instead of producing different-but-plausible recipes.
- **`VARIANT_LAB_ALGORITHM_VERSION = 1`** participates in every derivation and is recorded in
  provenance. There is deliberately **no dispatch table** — `resolve` delegates straight to
  `_resolve_v1`, because a dict with one entry is machinery for a migration that has not happened.
  If a V2 ever lands, retain `_resolve_v1` and add dispatch *then*.
- **Master Creative Seed ≠ Variation Seed.** The master is generator provenance; the clip Variation
  Seed is *resolved from it* via the `clips` stream into 1..999999 and is **never 0** — seed 0 is the
  planner's legacy branch. `🎲 New Variant` always mints a fresh master; `✨ Generate Variant` reuses
  the box or mints one when unset. Either way the master is **an output**, so it is visible before
  it is used: `variation.random_seed()` is the only non-deterministic call, it lives in the GUI, and
  the pure resolver refuses to resolve without a positive master.
- **A master seed alone is NOT a recipe identifier** (R1-A), and the help text must never say it is:

  ```
  MASTER SEED     = generator provenance   (reproduces a draw only with the same inputs)
  CREATIVE RECIPE = execution artifact     (the seven values; durable on its own)
  ```

  The resolver's real contract is *same master **and** same base, ranges, randomize selection,
  spread and algorithm version → same recipe*. Because the base is the live sliders and Generate
  **writes the recipe back into them**, pressing Generate twice with one master deliberately yields
  two different recipes — the second resolves from the first recipe. Pinned with exact values:
  Cinematic + master 582913 + spread 50 → `55/15/60/63/22/62`; immediately again → `71/9/55/77/16/71`;
  restore Cinematic → `55/15/60/63/22/62` again. The clip seed stays `822019` throughout, because it
  depends on the master alone. **Do not "fix" this with a cached base snapshot** — hidden state that
  disagrees with the visible sliders is worse than the honest explanation. A structural test keeps
  `INFO_MASTER_SEED` from reclaiming master-only reproducibility.
- **`🎲 New Variant` guarantees a different master, it does not merely hope for one** (R1-B).
  `random_seed()` draws from 1..999999 and *can* return the value already in the box, so
  `_fresh_variant_master_seed(previous)` draws once and, on a collision with a usable previous
  master, steps deterministically to an adjacent seed. One draw, never a retry loop waiting on
  `SystemRandom` to disagree. A monkeypatched forced-collision test replaces the old one that
  relied on the draw simply not colliding — no test here may carry a one-in-a-million failure.
- **Spread 0 is not a legacy render.** It freezes the six controls at their anchors *and still*
  resolves a positive clip seed, so clip selection varies. Help text and a test both say so.
- **Spread is distance, not height**, and the bias wording matters because the obvious phrasing is
  false. `anchor + spread * u * (headroom in that direction)`, `u ∈ [-1,1]` from the control's own
  stream, explicit half-up quantisation (never `round()` — banker's rounding collapses 2.5 and 3.5
  onto even values). The contract is **directional**: the anchor is the median and up/down is
  approximately a fair coin. Mean displacement is **not** zero when the anchor is off-centre — base
  30 in 0..100 has 70 points of headroom above and 30 below, so it drifts up, and a base on an
  endpoint can only move inward and sits still for roughly half of all masters. Do not "fix" either;
  a formula without them could not reach both endpoints at full spread.
- **Randomize OFF means leave it alone** — the exact live value, with its configured range ignored
  entirely rather than used to clamp. Clamping an excluded control would make the checkbox mean
  something weaker than off.
- **Range normalisation is total and never swaps.** Each endpoint takes a plain int or whole float
  clamped to 0..100; anything else (fractional, `NaN`, `inf`, string, `None`, `bool`) is that end's
  default. `lo > hi` collapses to `(lo, lo)` — `gr.Number` guarantees no ordering, so "min 80, max
  20" is a typo whose only unambiguous half is the floor, and swapping would resolve a range the
  user never asked for.
- **Defaults: Spread 50, all six randomized, every range 0..100 — including Source Diversity**,
  which is *not* quietly narrowed. Range-narrowing and spread are measurably redundant (`base ± 25
  at spread 50` is numerically identical to `full range at spread 25`), so Spread is the single
  wildness dial and ranges exist for genuine constraints.
- **Two trust contracts, deliberately opposite.** `CreativeRecipe.from_mapping` is
  **all-or-nothing** — wrong key set, missing or extra field, non-`int`, `bool`, fractional, out of
  range or seed 0 rejects the *whole* recipe — because it guards a contract boundary a future AI
  Director will cross, and a half-applied mixture of model output and silent 50s looks deliberate
  and is not. `CreativeProfile.from_mapping` stays lenient because it reads a possibly-stale
  internal bus. Constructing an invalid recipe **raises**; only the untrusted entry point degrades
  to `None`. `CreativeRecipe` carries no `master_seed`, `spread`, `ranges`, `algorithm_version` or
  `preset_label` — provenance lives on `VariantLabResolution`, so a non-random generator can emit a
  recipe without inventing a master seed it never had.
- **No new slider handlers.** The PR3 preset graph is untouched (one `.input()` per slider) and the
  lab's thirteen config widgets register nothing — they are read at click time. Because programmatic
  writes do not fire `.input()`, the generate handler computes `matching_preset(resolved six)`
  explicitly; at spread 0 that correctly reads the base preset's own name.
- **`gr.RangeSlider` does not exist in Gradio 6.19.0** (verified against the installed package), so
  each control gets an explicit min/max `gr.Number` pair. One `gr.CheckboxGroup` carries the
  selection using `(label, value)` choices, so the returned value is the exact field name — never
  derived from the display label by a lowercase/replace heuristic, which would be a silent
  correctness hazard since the resolver keys on exact names.
- **The report says "Last generated recipe", never "Current"**: the user may edit the seed or a
  slider afterwards, and the sliders remain execution truth.
- **Isolation.** Lab widgets are absent from `source_outputs`, `prep_outputs`, every source and
  preparation handler, `live_declaration` and `process_btn.click`. `CACHE_CONTRACT_VERSION` stays
  `stage5_cache_v3`, `ANALYSIS_VERSION` stays `auto_av_analysis_v8_llama_vulkan_batched`, and
  `src/auto_mode/*`, `video_analysis.py`, `video_processor.py`, `creative.py`, `presets.py` and
  `variation.py` are byte-identical. **No CLI flag** — the CLI already exposes all seven resolved
  values, which are the reproducible execution contract.

Measured on real material (Nero track + 41-source TEST1, real Stage 4 and Stage 6 per recipe against
a warm cache; no render, no Qwen, no cache write): 240 resolved recipes, **240 complete plans, zero
fallbacks, zero adjacent candidate repeats**, minimum cut gap unchanged. At spread 50 a variant
shifts the real cut count by a median 8%.

## Variant Lab audio variation (E2 V1)

E2 varies **exactly three** audio values and nothing else:

```
AUDIO_CONTROL_FIELDS = ("music_under_voice_percent", "sfx_amount", "sfx_level_percent")
```

- **A parallel path, not a wider recipe.** `resolve_audio()` is a sibling of `resolve()`;
  `_resolve_v1` and `resolve` were **not modified**, so every C2 golden vector is preserved
  structurally rather than by assertion. `_resolve_control` gained a `domain` parameter defaulting to
  `DOMAIN_CONTROLS`, which is why the C2 call site and its keys are byte-identical — and why E2 reuses
  the one spread formula instead of copying it.
- **One master seed, one Spread, shared by both halves.** There is deliberately no audio master seed
  and no audio Spread widget. `resolve_audio(master, config, spread, base)`.
- **The three field names are RNG stream names and are frozen forever.** Renaming one silently
  re-keys that control for every master seed a user has written down. They are the
  `AudioMixConfig`/`SmartMixConfig` field names, not the GUI's widget variable names.
- **The default ticked selection is EMPTY**, unlike the visual side's all-six. Opening an existing
  Variant Lab and pressing Generate must not move a mix level, so audio variation is opt-in. Two
  tests pin it (resolver default and widget default) and a mutation to all-three kills exactly those.
- **Spread 0 varies nothing here.** There is no audio analogue of the clip Variation Seed, so unlike
  C2 — where spread 0 still re-seeds clip choice — spread 0 holds all three at their base. The help
  text and `describe()` both say so.
- **Why only these three.** They are the audio settings that are already plain `0..100` integers, so
  `ControlRange`, `_resolve_control` and `_half_up` are reused verbatim: no second range
  implementation, no fractional-seconds model, no boolean randomization. Deliberately excluded, and
  these are product decisions rather than omissions: **voice clips** and the **SFX folder** are
  resource identity; **`avoid_drops`** is a *protective* rule with measured evidence behind it (a clip
  starting 0.44 s before a drop puts 96 % of its speech inside it), so a draw that flipped it off
  would produce a measurably bad variant; **enabled SFX roles** are structural intent whose toggling
  perturbs the frozen cross-role occupancy; and **`voice_start_delay` / `voice_min_gap`** are deferred
  fractional-seconds *placement* controls whose draws can legitimately make a render refuse. Adding
  any of them later is purely additive — a new stream name cannot perturb these three.
- **Each base uses the normaliser that OWNS its control**, and this is load-bearing, not tidiness:
  `music_under_voice_percent` falls back to **35** (Audio Layers) while both Smart Mix controls fall
  back to **50**. Routing all three through `creative.normalize_control` (fallback 50) would silently
  raise the music floor on a malformed widget value. `variant_lab` delegates to
  `audio_mix.normalize_music_under_voice` and `smart_mix.normalize_control` rather than restating
  them, exactly as `normalize_master_seed` delegates to `variation.normalize_seed`.
- **`AudioRecipe` is a trust boundary like `CreativeRecipe`**: frozen, validation **raises**, nothing
  clamps. It carries no path, role, voice clip, master seed, Spread or range — provenance lives on
  `AudioVariantResolution`. It is **not** a field of `CreativeRecipe` and must not become one.
- **The visible widgets stay execution truth.** `process_video_guarded` still builds
  `AudioMixConfig`/`SmartMixConfig` from the live widgets at render click time; no `AudioRecipe`,
  `AudioVariantConfig`, master seed or lab range reaches the planner, the renderer,
  `CreativeProfile`, `beat_info["creative"]`, `AudioMixPlan` or `SmartMixPlan`.
- **Isolation unchanged.** `audio_mix.py` and `smart_mix.py` were **not modified**;
  `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3` and `ANALYSIS_VERSION` stays
  `auto_av_analysis_v8_llama_vulkan_batched`; `video_analysis.py`, `video_processor.py`,
  `ffmpeg_processing.py`, `library_prep.py` and `src/auto_mode/*` are untouched. **No CLI flag.**
- **The GUI delegates and implements none of it.** `gui.py` contains no `rng_for` and no
  `DOMAIN_AUDIO`; it calls `fork_lab.resolve_audio(...)`, and the pure resolver is the only consumer
  of the `audio` domain. The writer matrix is pinned by **split** seam guards in
  `.claude/rules/audio-mixdown.md`'s suite — see that rule before touching an audio widget.

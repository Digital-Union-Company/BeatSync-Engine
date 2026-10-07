---
paths:
  - "src/beatsync_fork/variant_lab.py"
  - "src/beatsync_fork/variant_batch.py"
  - "src/beatsync_fork/creative_recipe.py"
  - "tests/test_variant_lab.py"
  - "tests/test_variant_batch.py"
  - "tests/test_creative_recipe.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

## Variant Lab V1 generates one reproducible recipe (C2)

`beatsync_fork/variant_lab.py` + `beatsync_fork/creative_recipe.py`. The user declares **what may
vary**; the lab resolves **exactly one** visual recipe:

```
VARIANT LAB     = what MAY vary          (configuration, ephemeral)
CREATIVE RECIPE = what WILL be rendered  (seven integers, the VISUAL execution artifact)
THE SIX SLIDERS = where the recipe lands (unchanged execution truth)
```

**This section is the visual half, and only the visual half.** E2 V1 added a *sibling* audio
resolution, so one press of Generate today does:

```
GENERATE =  one visual recipe  (CreativeRecipe: Variation Seed + the six creative sliders)
         +  one audio recipe   (AudioRecipe:    the three existing audio level widgets)
         resolved from ONE shared master seed and ONE shared Spread,
         written into the visible execution widgets — and it renders nothing.
```

**C3 V1 then added a second button beside Generate, not a second lab**: Generate Variants resolves
N candidates from one frozen reading of the screen, a table compares them, and Apply Selected
writes exactly one into those same execution widgets. It still renders nothing. The full contract
is the C3 section at the bottom of this file.

`AudioRecipe` is a **sibling** of `CreativeRecipe`, never a field of it, and no audio value enters
`CreativeProfile` or `beat_info["creative"]`. The visual contract below is therefore unchanged by
E2: the pipeline still receives exactly the same seven values it always has, because the three
audio levels reach the render the way they always have — as the live Audio Layers / Smart Mix
widgets that `process_video_guarded` normalises at Create Music Video click time. The audio section
at the bottom of this file is the full E2 contract.

Generating writes the **existing** Variation Seed, the six sliders and — only when the user ticks
something under Audio variation — the three audio level widgets. Nothing else. No Variant Lab state
reaches `CreativeProfile`, `beat_info["creative"]`, `process_video_guarded`, `render_info`, the
filename or any stage — the planner has never heard of recipes. **It renders nothing**; the user
still presses Create Music Video. **Multi-variant generation and comparison shipped as C3 V1** (bottom of this file);
**batch rendering is still deferred** and a split guard asserts none of that machinery exists.

- **The base is always the live sliders.** No base control inside the lab and no cached snapshot:
  the six current values are read at click time, so Balanced explores around Balanced and a
  hand-tuned Custom explores around that. The preset *name* is never read. Ranges are **explicit
  constraints** and deliberately do **not** follow the base around; a base outside its range clamps
  to the nearest edge (`anchor = clamp(base, lo, hi)`) rather than raising.
- **Named RNG sub-streams, and this is the load-bearing part.** Key format, pinned by golden-vector
  tests: `"variant_lab|1|<master>|<domain>|<name>"` → SHA-1 → first 12 hex digits → `random.Random`.
  Domains are `clips`, `controls`, `audio` and — since C3 V1 — `batch`; all four are **used**.
  The registry lives in `variant_lab.py` even though C3's orchestration lives in
  `variant_batch.py`, because a domain list split across two modules is how two domains
  eventually collide. A single sequential
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
  AUDIO RECIPE    = execution artifact     (the three levels; sibling, E2 V1)
  ```

  Both artifacts are *what the lab generated*, not a complete description of a render: the voice
  clips, voice timing, `avoid_drops`, the SFX folder, the enabled roles and the source media are
  pre-existing render/resource intent the lab never writes, and a replay needs those back too.

  The resolver's real contract is *same master **and** same base, ranges, randomize selection,
  spread and algorithm version → same recipe*. Because the base is the live sliders and Generate
  **writes the recipe back into them**, pressing Generate twice with one master deliberately yields
  two different recipes — the second resolves from the first recipe. Pinned with exact values:
  Cinematic + master 582913 + spread 50 → `55/15/60/63/22/62`; immediately again → `71/9/55/77/16/71`;
  restore Cinematic → `55/15/60/63/22/62` again. The clip seed stays `822019` throughout, because it
  depends on the master alone. **Do not "fix" this with a cached base snapshot** — hidden state that
  disagrees with the visible sliders is worse than the honest explanation. A structural test keeps
  `INFO_MASTER_SEED` from reclaiming master-only reproducibility — and, since E2 V1, from the
  opposite two failures as well: describing the generated values as only the Variation Seed and the
  six sliders, or implying that what the lab generates *is* the whole render.
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
  lab's config widgets register nothing — they are read at click time. Thirteen of them in C2;
  **twenty since E2 V1**, which added one `CheckboxGroup` and three min/max `gr.Number` pairs to the
  same accordion and registered nothing for any of them either. Because programmatic writes do not
  fire `.input()`, the generate handler computes `matching_preset(resolved six)` explicitly; at
  spread 0 that correctly reads the base preset's own name.
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
  *visual* values, which are the reproducible execution contract for the edit. That parity is a C2
  statement and does **not** extend to E2: no CLI flag was added for the three audio levels and
  there is no CLI equivalent of the audio generator, so the audio half of Variant Lab is GUI-only.

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

## Variant Lab multi-variant generation and comparison (C3 V1)

C2 and E2 resolve **one** recipe per click and write it straight back. That is a good way to wander
and a poor way to *choose*. C3 adds the choosing:

```
GENERATE N  ->  COMPARE N  ->  APPLY ONE  ->  (the user presses Create Music Video)
```

**C3 V1 renders nothing, and batch rendering is deliberately deferred.** A render is ~150 FFmpeg
clip extractions through the one hardware encoder a *single* render already saturates
(`_effective_clip_workers()` exists for exactly that contention); a candidate costs tens of
microseconds. Those two do not belong in one feature, and the deferred milestone is **C3-R**.

- **`beatsync_fork/variant_batch.py` orchestrates the frozen resolvers — it never re-implements
  them.** Every candidate is `variant_lab.resolve(...)` + `variant_lab.resolve_audio(...)`
  **unchanged**, with a derived candidate master. No second spread formula, no second range model,
  no second half-up rounding; a test forbids `uniform(`, `_half_up`, `anchor_for` and `hashlib` in
  the module. The dependency is one-way — `variant_batch → variant_lab`, never the reverse — and
  `variant_lab.py` gained exactly one constant (`DOMAIN_BATCH`) and nothing else, which is why
  every C2 and E2 golden vector is preserved *structurally* rather than by assertion.
- **One candidate master per index, keyed `variant_lab|1|<root>|batch|<i>`**, drawn into
  1..999999 — the same six-digit range every other seed the user sees lives in, so a candidate
  master can be read off the table and typed into the Master Seed box. Index-keyed rather than
  sequential, and that is the load-bearing choice: **asking for 8 candidates instead of 5 leaves
  the first 5 identical**, which one `Random(root)` stream could not promise.
- **Candidate masters are unique as a contract, not a probability.** Twelve six-digit draws collide
  about once in 14,000 batches, and two identical rows read as a bug. A collision steps
  deterministically forward (wrapping at the top) against the masters already fixed at **lower**
  indices only — so prefix stability survives, the scan is bounded by the count cap, and it is
  never a redraw (a test asserts no RNG call inside that loop). Same reasoning as C2 R1-B's
  `_fresh_variant_master_seed`, and the forced-collision test is monkeypatched rather than hoped for.
- **A candidate master alone is still not a recipe identifier.** R1-A's rule is unchanged and
  applies to both seeds: *candidate master + the same base, ranges, randomize selections, Spread
  and algorithm version → the same candidate*; the master alone is not enough. Two tests pin both
  halves, and no UI copy may claim master-only replay.
- **One Generate Variants click freezes the screen once.** All N candidates resolve from the *same*
  original visual and audio base — candidate 2 is never resolved from candidate 1. That is
  structural rather than careful: **generation writes no execution widget at all**, so there is no
  path by which a result could feed the next draw. Looping today's single Generate would chain,
  because *that* handler writes back; this one does not.
- **`VariantBatchDeclaration` is a declaration record, not the cached base snapshot C2 rejected.**
  It is never read as a substitute for the live widgets at resolve time — only compared against
  them. Same family as `SourceSnapshot` and `PrepScanResult`. Canonical by construction: explicit
  stable field order, plain int/str/tuple members, both ordered collections in their owning
  module's field order, and every base normalised by the normaliser that **owns** it (the 35/50/50
  audio split is not interchangeable), so `50` and `50.0` are one declaration rather than two.
- **Staleness is answered by equality, not a digest.** `live_declaration.matches(batch.declaration)`
  is plain structural equality between two small frozen records — no canonical-serialisation
  contract to keep in step, and **no digest, fingerprint or display tag exists beside it**. An R0
  draft carried a `short_digest()` that nothing consumed and that reached `rng_for` with a
  `"display"` domain string, inventing a **fifth** RNG domain to decorate a status line; R1 removed
  the method rather than registering the domain. A test now forbids `digest` / `hashlib` /
  `__hash__` in the module outright, so there is no second answer to "is this batch current", not
  even an unused one.
- **Apply validation never mints a master seed (R1), and that is a correctness rule rather than
  tidiness.** The three handlers share one normalisation helper, which takes a keyword-only
  `mint_unset_master`:

  ```
  Generate Variant / Generate Variants  ->  may mint an unset master, and SURFACE what they minted
  Apply Selected   (validation)         ->  NEVER mints; an unusable live master stays 0,
                                            so the declaration is deterministically stale
  ```

  R0 reused the Generate normalisation wholesale, so rebuilding the live declaration minted
  whenever the box was unusable. With the Master Seed blanked, a `random_seed()` that happened to
  return the batch's own root made the reconstructed declaration compare **equal**, and Apply wrote
  a candidate for a screen that no longer declared that root — an unsurfaced draw deciding a gate,
  the exact class C2 R1-B already rejected for `_fresh_variant_master_seed`. A forced-draw
  regression test pins it: `random_seed` is patched to return the batch root, and Apply must still
  refuse with **zero** invocations. Note the boundary is *usability*, not spelling —
  `normalize_seed` accepts `"582913"`, so the string form is the same declaration and Apply
  correctly succeeds.
- **The gate is live and fail-closed, and there are deliberately NO `.change()` invalidation
  handlers.** Apply re-reads the whole screen through the shared normalisation helper at click time
  and refuses unless it equals the stored declaration — the same reason `process_btn` validates the
  live source controls and `prep_analyze_btn` takes the live preparation controls: Gradio delivers
  widget changes as separate queued events, so state can lag the widgets. Adding a clearing handler
  to every slider would put a *second* binding on widgets whose single `.input()` is itself a
  load-bearing contract. The table is labelled **Last generated batch**, so its presence claims
  history, not currency.
- **A refusal changes nothing.** Every execution widget gets `gr.skip()`. A *stale* refusal also
  consumes the batch and clears the selector, so a stale list cannot be retried; a missing
  selection does **not** consume it, because the list is still perfectly valid and the user simply
  has not chosen one yet.
- **Apply is terminal for one batch.** A successful apply moves the live base, so every remaining
  candidate now describes a starting point that no longer exists. The batch is consumed and the
  selector cleared; generate again to explore from where you landed.
- **Apply writes the candidate's own master into the Master Seed box.** That field means
  *provenance for the recipe now on the sliders* — leaving the batch root there would make the
  visible seed disagree with `VariantLabResolution.describe()` in the report beside it. The batch
  root stays visible in the Last generated batch text. Two provenance levels, neither of them an
  identifier on its own.
- **One projection helper, one preset path.** Apply calls the existing `_variant_apply_outputs`
  with temporarily rehydrated resolution objects, so it writes the identical thirteen-widget tuple
  an ordinary Generate writes and recomputes `matching_preset` explicitly (programmatic writes do
  not fire `.input()`). A test asserts Apply's output equals a single Generate from that candidate
  master.
- **`gr.State` deep-copies its value, and that is a real constraint rather than a style note.**
  `VariantLabConfig` / `AudioVariantConfig` hold `MappingProxyType` and the resolutions hold those
  configs, so none of them may enter the batch. `VariantBatch` stores plain ints, strings, tuples
  and the two frozen recipe dataclasses; `rehydrate()` builds temporary resolutions inside one
  handler and never returns them into state. Tests assert `copy.deepcopy(batch) == batch` and walk
  every reachable object for a forbidden type.
- **Count: min 2, max 12, default 5.** The cap is **comparison legibility and a typo guard**, not a
  resource bound — twelve candidates cost about a millisecond. **Do not reuse this number as a
  future batch-render limit**; generating and rendering differ by roughly six orders of magnitude.
  (C3-R1B-b set the render cap independently at `RENDER_SELECTION_MAX = 4`, from measured render
  wall-clock. The two numbers still do not know about each other.)
  Normalisation mirrors `_normalize_endpoint`: `bool` rejected first, whole floats accepted,
  fractional / `NaN` / `inf` / string → default, out of range clamped.
- **GUI: six components** inside the *existing* Variant Lab accordion — a count `gr.Number`, a
  Generate Variants button, a read-only table `gr.Textbox`, a `gr.Radio` selector whose
  `(label, value)` value **is** the candidate index, an Apply button and a read-only status
  `gr.Textbox`. No `gr.Dataframe` dependency. The selector registers no handler; Apply reads it at
  click time. `VariantBatch.table_text()` is the one formatter — `gui.py` formats none of it.
- **Isolation.** `variant_batch_state` is comparison/selection state: `apply_variant_btn.click` is
  its only reader, and it is absent from `process_btn.click`, `source_outputs`, `prep_outputs`,
  `live_declaration`, `CreativeProfile`, `beat_info["creative"]` and both mix configs.
  `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3`, `ANALYSIS_VERSION` stays
  `auto_av_analysis_v8_llama_vulkan_batched`, and `video_analysis.py`, `video_processor.py`,
  `ffmpeg_processing.py`, `audio_mix.py`, `smart_mix.py`, `library_prep.py` and `src/auto_mode/*`
  are untouched. **No CLI flag** — C3 is a GUI comparison workflow, and the CLI already takes the
  resolved values directly.
- **The writer matrices were extended by exact list, never relaxed.** The six creative sliders now
  have exactly `creative_preset.input`, `generate_variant_btn.click`, `new_variant_btn.click` and
  `apply_variant_btn.click`; the three audio levels exactly the latter three. Note what is absent
  from both: `generate_variants_btn.click`, because generating candidates writes no execution
  widget. See `.claude/rules/audio-mixdown.md` and `.claude/rules/creative-presets.md`.
- **Two pre-C3 negative guards were split, not deleted** —
  `test_variant_lab.py::test_no_multi_variant_or_c3_machinery_was_added` and
  `test_creative_presets.py::test_the_gui_added_no_speculative_mode_machinery`. `variant_lab.py`,
  `creative_recipe.py` and `presets.py` still know nothing about C3; `gui.py` may carry only the
  accepted names. The load-bearing replacement is **structural**, not a token list: the call graph
  is walked from both C3 buttons and may not reach `process_video_guarded`, `process_video`,
  `analyze_beats_auto` or `create_music_video`. A rename cannot evade that the way a word list
  would.

## Rendering 2–4 compared candidates (C3-R0 → C3-R1B-b)

C3 V1 let the user generate N candidates, compare them and apply one. The payoff of a comparison
is watching the videos, and getting two meant two manual round-trips — with the batch consumed by
the first Apply and the base moved out from under the rest. C3-R0 closed that loop; **C3-R1B-b
completed it, and C3-R1B is finished with that milestone.**

```
generate N  ->  compare N  ->  tick 2 to 4  ->  Render Selected Variants
                                                candidate A, then B, then C, then D
```

- **A bounded range of 2 to 4, sequential, cancellable, and it continues past a candidate-local
  failure.** Each milestone bought the next, and the order matters:

  ```
  C3-R0      exactly 2, no stop channel at all -- a cancelled Gradio event could return its slot
             while the daemon worker was still alive and the next render would wipe the live one's
             process-global processing dir. Rather than ship a Stop that could not stop FFmpeg,
             C3-R0 shipped none and bounded the commitment to two renders.
  C3-R1A     the explicit shared lifecycle -- a batch becomes interruptible at safe boundaries.
  C3-R1B-a   every failure cause truthfully typed, and deliberately NO policy change.
  C3-R1B-b   the range, and the one continuation that classification earned.
  ```

- **MIN 2 / MAX 4 is a product contract, not a tunable.** Two is the floor because one candidate is
  not a comparison (ordinary Create Music Video already covers that). Four is the ceiling because
  past candidate 1 the cost is **linear with no economy of scale** — the only real shared saving,
  the ~15.7 s of Stage 1-3, is already fully banked at candidate 2 by the L2 process cache, so every
  later candidate costs the same again. C3-R1B/P0 measured ≈67 s for the first candidate and ≈51 s
  for each subsequent one on the NVENC path at ~150 clips (materially more on the serial ProRes
  path), which puts four in the same order as the single render a user already accepts. Do **not**
  raise it, make it configurable, add an "advanced" override, or derive it from machine speed.
  `RENDER_SELECTION_MIN` / `RENDER_SELECTION_MAX` are still deliberately unrelated to
  `CANDIDATE_COUNT_MAX = 12`: that bound is comparison legibility and costs a millisecond, this one
  is render minutes — now stoppable, but never instant. The exact-size `RENDER_SELECTION_SIZE`
  constant is **gone**, with no alias, and a test fails if it returns.
- **An over-long selection is refused, never truncated.** Five ticks answers `()` rather than
  rendering the first four — silently dropping a candidate the user explicitly chose would render
  something they did not ask for, after they committed to the wait.
- **A separate selector.** The Apply `gr.Radio` stays; rendering gets its own `gr.CheckboxGroup`,
  empty by default and never pre-filled. One control cannot honestly mean both "apply this one"
  and "render these two". Neither registers a handler; the selection is validated at click time.
- **Canonical ascending index order**, not tick order — the render sequence is a property of the
  batch, so re-ticking the same pair the other way round renders the same two videos in the same
  order.
- **The batch request is execution authority for its own invocation, and only that.** Two
  candidates are never simultaneously on screen, so live widgets cannot be the authority for a
  batch. Candidate values come from the **stored recipes** (`CreativeRecipe` / `AudioRecipe`,
  already-validated frozen artifacts — nothing is re-resolved); everything else (audio, voice,
  SFX, source, output, encoder, FPS) is frozen from the submitted event arguments, so edits made
  while the batch runs cannot reach it. This is not hidden mutable state: it is built explicitly
  from submitted values, lives for one invocation, is never cached, and reaches no Stage-5
  identity.
- **Rendering does NOT use Apply's stale-declaration gate.** Apply has one because it writes a
  historical candidate into the *current* screen; rendering reads resolved artifacts and writes no
  widget. A user who nudged a slider after generating may still render the pair. Do not conflate
  Apply staleness with recipe validity — and do not weaken Apply's gate.
- **Rendering does not consume the `VariantBatch`.** The comparison survives, so the pair can be
  rendered again or one of them applied. Apply keeps its existing terminal semantics; when Apply
  *does* consume the batch (success or stale refusal) it clears the render selector too, because
  render choices for a batch that no longer exists are an offer the app cannot honour.
- **`variant_batch_state` now has exactly TWO readers**: `apply_variant_btn.click` and
  `render_selected_variants_btn.click`. Pinned as an exact list, never a containment check.
- **Output identity cannot rest on the Variation Seed.** C3 deduplicates candidate *masters*
  deliberately; `CreativeRecipe.seed` is an independent draw and is not deduplicated anywhere —
  measured: root 5484 gives masters 945730 and 862920 that **both** resolve Variation Seed 536635.
  Since the render path names its file `_seed<VariationSeed>`, the batch derives a stem carrying
  the request tag, the candidate index and the candidate master
  (`music_video_batch<tag>_c01_m609591`) and lets the existing suffix follow unchanged. **Still
  required after H1**, for the other half of the reason: destructive replacement is now impossible,
  so two candidates on one name would make the second *refuse*, and a batch asked for two videos
  would deliver one. Identity is what lets both succeed; the promotion is what makes neither
  overwritable.
- **No special batch overwrite mode — C3-R0 inherits the universal GUI policy (H1).** C3-R0 owned
  a keyword-only `refuse_existing_output` that defaulted `False`, so the batch checked the
  destination before Stage 1 and again immediately before the move. That flag is **gone**, and the
  description it carried was too strong: two `os.path.exists` checks around a `shutil.move` are a
  TOCTOU pair — real protection against this batch's own second candidate, best-effort only
  against another process, and never "hard". H1 replaced the promotion itself with a single
  atomic no-replace `os.rename` shared by every GUI render, so the guarantee is now genuinely hard
  **and** no longer batch-only. Do not reintroduce a per-caller overwrite policy: a flag
  defaulting to the unsafe value is exactly how the ordinary single render stayed destructive
  while the batch was safe. See `.claude/rules/gui-integration.md` and
  `.claude/rules/pipeline-core.md`.
- **Durable output is the success authority**, not the preview and not the status prose. A ProRes
  render moves the real `.mov` into `output/` and then returns a session-temp `_preview.mp4`, so a
  preview step failing afterwards must not retroactively fail a finished render.
  `session_state[LAST_OUTPUT_PATH_KEY]` carries it: cleared before every attempt, set only after
  the promotion succeeds. Nothing parses `Output: …` out of a status string. Since H1 the clear
  happens in **both** places with the same lifecycle — the gate core clears it before the gate
  (a gate refusal never reaches the pipeline function) and `_process_video_impl` clears it for
  itself, so "empty unless a promotion succeeded" is a local property rather than one inherited
  from whichever caller happened to run.
- **Exactly one class continues; everything else stops. Prior success is never rolled back.**
  This is the whole of C3-R1B-b's behavioural change, and the matrix is the contract:

  ```
  SUCCESS           -> render the next selected candidate
  CANDIDATE_LOCAL   -> record the failure, then CONTINUE
  SHARED_FATAL      -> STOP; remaining candidates NOT ATTEMPTED
  UNKNOWN_FATAL     -> STOP; remaining candidates NOT ATTEMPTED
  CANCELLED         -> STOP; remaining candidates NOT ATTEMPTED
  ```

  Three properties make it safe rather than merely implemented:

  - **The decision is read off `candidate_outcome.outcome_kind`, never the raw `candidate_kind`
    local.** A producer that classified nothing leaves `candidate_kind` as `None`, and only the
    *model* decides what `None` means (`SUCCESS` with a durable path, `UNKNOWN_FATAL` without).
    Branching on the raw value would let an unclassified failure continue as
    "not `CANDIDATE_LOCAL`, therefore carry on".
  - **The loop-head cancellation check still comes first in every iteration**, so a `continue`
    cannot outrun a Stop. A Cancel arriving in the gap after a local failure leaves the remaining
    candidates NOT ATTEMPTED — there is no lifecycle gap, because one lifecycle spans all 2–4.
  - **Nothing is retried and nothing is reclassified.** A candidate is attempted at most once.

  Tests pin both halves: measured execution call order through the real handler body for every
  scenario, plus a structural guard that the loop contains **exactly one** `continue`, guarded by
  exactly `CANDIDATE_LOCAL`, and branches on no other class.

  The lineage of this bullet is worth keeping, because each step was deliberately *not* the next:

  ```
  R1A    every AudioMixError        -> CANDIDATE_LOCAL   (wrong for both reachable causes)
         eleven producers           -> key left None     -> derived UNKNOWN_FATAL
         the batch loop             -> CANCELLED or None -> every other class discarded
         SHARED_FATAL               -> no producer at all

  R1B-a  every reachable producer names its class; SHARED_FATAL has five;
         the batch threads the FULL class into RenderCandidateOutcome
         -- and still stopped after every non-success candidate.

  R1B-b  2..4 candidates, and CANDIDATE_LOCAL continues. C3-R1B is complete.
  ```

  The full producer matrix is `.claude/rules/render-worker.md`; the audio half is
  `.claude/rules/audio-mixdown.md`.
- **"A candidate failed" and "the batch stopped" are now independent facts**, which reshaped the
  outcome model. `RenderBatchOutcome.stopped_on_failure` is **gone** — a boolean meaning "something
  failed, therefore we stopped" cannot be true once a local failure is continued past — and
  `_failed_candidate()` became `_terminal_candidate()`, which names only the **last attempted**
  candidate, only if it genuinely failed, and only when work was actually left unrun. A failure on
  the *final* selected candidate stopped nothing, so it reads as a count. Early termination is read
  from the typed batch cause plus `not_attempted`, never from the existence of any failure.
- **A completed batch has `outcome_kind is None`, even carrying local failures.** It is deliberately
  not batch-`CANDIDATE_LOCAL`: that cause belongs to the candidate that suffered it, and the batch
  carried out its policy to the end. `failed` / `cancelled_count` are single-source counts that never
  come from status prose.

  **The batch-level cause domain is exactly three members wide**, enforced in
  `RenderBatchOutcome.__post_init__` since C3-R1B-b / R2 — `CANCELLED`, `SHARED_FATAL`,
  `UNKNOWN_FATAL`, or `None`. The two it rejects are not batch facts at all: `SUCCESS` is a
  *candidate* outcome (a finished batch reports counts and has no terminal cause, which is what
  `None` means), and `CANDIDATE_LOCAL` belongs to the candidate the batch **continued past** — so by
  definition it did not terminate the run. The field was documented this way while the type still
  accepted any `RenderOutcomeKind`; stating an invalid cause now raises `ValueError`. The model
  still derives only what a candidate *proves* — a cancelled candidate implies a cancelled event —
  and deliberately derives **no** fatal cause from candidate records, because whether a fatal
  candidate actually ended the run depends on whether work remained, which is the orchestrator's
  knowledge rather than the model's.
- **The top-level lifecycle state is not a candidate success counter.** `CANCELLED` when a
  cancellation won, `FINISHED` when the batch exhausted its full selected list under the authorized
  policy (local failures notwithstanding), `FAILED` when the event ended before exhausting it. It is
  read from **selection exhaustion**, never from the last candidate's outcome — which R1 did, making
  it order-dependent once `CANDIDATE_LOCAL` continued. Full contract and the mirrored regression
  pair: `.claude/rules/render-worker.md`.
- **Report ownership.** `audio_layers_report` and `smart_mix_report` keep `process_btn.click` as
  their **only** writer; the batch reads them from `session_state` after each candidate and the
  dedicated summary owns multi-render diagnostics. `variant_batch_table` and
  `variant_batch_status` keep describing generation and comparison only. The video preview shows
  the latest successful candidate and a later failure never blanks it.
- **One formatter.** `RenderBatchOutcome.summary_text()` renders the whole read-out; `gui.py`
  formats none of it, exactly as it formats none of the two mix reports.
- **Nothing in the pipeline changed.** `video_processor.py`, `ffmpeg_processing.py`,
  `video_analysis.py`, `audio_mix.py`, `smart_mix.py` and `src/auto_mode/*` are untouched; no
  stage cache, no shortlist, no rendered gallery, no Qwen or cache-contract change.
  `variant_batch.py` is untouched too and keeps its own render ban at full strength — that guard
  is what holds the generation/render module split honest.

  **C3-R1A did change pipeline files**, and that supersedes this bullet for R1A only: cancellation
  has to be *observed* where the time is spent, so `video_processor.py`, `ffmpeg_processing.py`,
  `audio_mixdown.py` and `auto_mode/__init__.py` each gained an optional `lifecycle=None` parameter
  and boundary checks. Everything else in this bullet still holds — no stage cache, no shortlist, no
  gallery, no Qwen change, and `CACHE_CONTRACT_VERSION` / `ANALYSIS_VERSION` / `L2_CACHE_VERSION` are
  untouched. `video_analysis.py` and `stage5_qwen_scene_worker.py` are untouched too, and that is a
  contract rather than an omission: see BOUNDARY_ONLY_CANCEL below.

## Cancelling a render (C3-R1A)

C3-R0 bounded the commitment because it had no safe stop channel. C3-R1A builds that channel. The
whole contract lives in `src/beatsync_fork/render_worker.py` (stdlib-only, Gradio-free), and
`tests/test_render_worker.py` plus `tests/test_render_cancellation.py` pin it.

```
Cancel click ──► _on_cancel_render_click(invocation_id: str)      own concurrency lane
                     └─► _request_cancel_if_matching(id)          the slot's own small lock
                           └─► lifecycle.request_cancel()         sets ONE threading.Event
                                 │
render thread ───────────────────┴──► raise_if_cancelled() at safe boundaries only
```

- **BOUNDARY_ONLY_CANCEL.** FFmpeg-class subprocesses may be terminated promptly, mid-call —
  `ffmpeg_processing._run_media_command` and `audio_mixdown._run` each poll their own child and
  terminate → grace → kill → **reap**, and raise `RenderCancelled` only once the reap has returned,
  never while a child may still be alive. An in-flight **Stage-5 / Qwen** call is *never* hard-killed:
  the token is not passed into `video_analysis.py` at all, and the cancellation becomes effective at
  the next boundary after that call returns naturally. The boundary that makes this work is in
  `auto_mode/__init__.py`, placed **immediately after** the Stage-5 `try/except` and before Stage 6,
  deliberately **outside** it — inside, a cancellation would be caught by that block's broad
  `except Exception` and reinterpreted as "video analysis failed", continuing into fallback sampling.
- **Exactly ONE `RenderLifecycle` per top-level render event.** One ordinary Create Music Video
  click, or the **whole** two-candidate batch — never one per candidate, never one per internal
  `process_video()` call. The two mutex-owning wrappers construct it; nothing below them constructs a
  second.
- **Terminal-marking belongs to the wrappers, exactly once, after everything they ran.** Never
  inside `process_video`'s `worker()`, which the batch calls **once per candidate** against one
  shared lifecycle: `RenderLifecycle._transition` silently no-ops once terminal (by design — no
  reset or reuse), so marking from there would freeze the batch's reported state after candidate 1.
- **Only a plain string crosses into Gradio.** `render_invocation_state` is a `gr.State('')` holding
  an opaque invocation id. A live `RenderLifecycle`, `threading.Event` or `Lock` must never enter
  `gr.State`, which deep-copies and may serialize its value. The server side keeps the live object
  in a **capacity-one active-render slot** holding `(invocation_id, lifecycle)` or `None` — never a
  `Popen`, a thread handle or a history of past invocations. `_clear_active_render` clears only if
  the slot still names that exact id, so a slow abandoned finalizer cannot unregister a newer render.
- **Cancel has its own concurrency lane** (`CANCEL_CONCURRENCY_ID`, never `RENDER_CONCURRENCY_ID`)
  and never Gradio's built-in `cancels=`. Queuing Cancel behind the render it must signal would make
  it useless, and `cancels=` would kill the event while leaving the daemon worker alive — the exact
  defect C3-R0 bounded itself to avoid. The handler **never acquires `_RENDER_LOCK`** and touches
  nothing but the slot.
- **Abandonment is not an explicit Cancel.** `process_video`'s finalizer still joins its worker with
  no timeout and no kill; a dropped stream keeps waiting for the render in flight exactly as before.
  Cancel only makes the worker *reach* a terminal state sooner.
- **The clip-extraction executor is proven quiescent before cancellation propagates.** In
  `create_music_video`, a `RenderCancelled` from a worker calls
  `executor.shutdown(wait=True, cancel_futures=True)` explicitly, stores the exception and `break`s —
  never `continue`s — and re-raises only **after** the `with` block has exited. `cancel_futures`
  stops not-yet-started futures from beginning expensive work; `wait=True` is what makes "no FFmpeg
  child outlives this function" true rather than hoped for.
- **`RenderCancelled` must survive every broad `except Exception` on the render path.** It is an
  ordinary `Exception` subclass on purpose — not `BaseException` — so each catch site names it
  explicitly *before* the generic handler. Grep `except RenderCancelled` to enumerate the covered
  sites. Two of them re-raise with no event at all: a cancellation must not be narrated as
  "Final assembly failed".
- **The durable promotion is the ONE success commit point.** `session_state[RENDER_OUTCOME_KEY]` is
  set to `SUCCESS` exactly once, immediately after `LAST_OUTPUT_PATH_KEY`, and nothing afterwards may
  downgrade it. A cancellation arriving during the ProRes **preview** step therefore stays a success:
  the preview is skipped (`preview_path` stays the durable output) and `RENDER_OUTCOME_KEY` is not
  touched. The user owns that `.mov`.
- **No cache, schema or version constant changed.** (The render count did move, in C3-R1B-b: the
  selection is now a 2..4 range. Raising the number of candidates reaches no cache identity --
  `CACHE_CONTRACT_VERSION`, `ANALYSIS_VERSION` and `L2_CACHE_VERSION` are untouched, and a test
  pins the cap alongside them for exactly that reason.)

## The boundary against Freestyle (Freestyle V1)

Freestyle and the Variant Lab both sit above the six sliders, so the thing worth stating is that
they do not know about each other:

```
VARIANT LAB  ->  resolves seven GLOBAL values from a master seed, ranges and a spread
FREESTYLE    ->  declares per-SECTION rules that modulate five of them at render time
```

A rule is not a recipe, a recipe is not a rule, and neither is an input to the other. The guard is
asserted in **both directions at once**, which is what makes it load-bearing rather than a word list:

- **`variant_lab.py` and `creative_recipe.py` know nothing about Freestyle**, and that ban keeps full
  strength — `test_the_frozen_resolvers_know_nothing_about_multi_variant_generation` already bans the
  word there. `CreativeRecipe` carries no section concept at all; a per-section field on it would
  make a recipe undeliverable to the sliders it exists to write.
- **`freestyle.py` knows nothing about the lab** — no `variant`, `master_seed`, `spread`,
  `ControlRange`, `rng_for`, `resolve_audio` or `hashlib`.
- **No Freestyle name is an RNG stream name.** The six creative fields and `AUDIO_CONTROL_FIELDS` are
  frozen stream names because renaming one re-keys that control for every master seed a user wrote
  down. Freestyle adds **no stream**: a rule is a declaration the user made, not something the lab
  resolves. `variant_lab.py` still declares exactly the four domains `clips`, `controls`, `audio`,
  `batch`, pinned as an exact set — the same discipline that made R1 remove C3's unused `"display"`
  domain rather than register it.
- **No lab or Director handler writes a Freestyle widget.** `generate_variants_btn.click`,
  `apply_variant_btn.click` and `apply_director_btn.click` all write none: a candidate or a proposal
  is seven *global* integers and has no opinion about section rules, so Apply must not silently reset
  them.
- **C3-R0 freezes one declaration per batch.** Freestyle is shared render intent, so the tuple is
  built **once, before** the candidate loop, exactly like every other shared render input — and a
  test pins that by line position, because the loop sits inside the lock-holding `try` and a nesting
  comparison would miss it. Both render wrappers take the identical eleven appended parameters.

Full contract: `.claude/rules/freestyle.md`.

---
paths:
  - "src/beatsync_fork/freestyle.py"
  - "tests/test_freestyle.py"
---

> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

# Freestyle V1

```
THE SIX SLIDERS   = the global base                       (unchanged execution truth)
A FREESTYLE RULE  = a per-SECTION delta on five of them   (a render request, never identity)
STILL EXACTLY ONE = the Variation Seed, and Micro Cuts
```

Every creative control this app has ever had is global for the whole track: one Cut Density, one
Motion Bias, one of everything, first beat to last. **Freestyle is the first feature that lets a
`drop` be cut differently from the `verse` before it.**

It is **not** an eighth control, and it is orthogonal to the three existing *producers* of a global
recipe. Presets, Variant Lab and the AI Director all answer "what should the seven global values
be?". Freestyle answers a different question, and never that one:

```
PRESET / VARIANT LAB / DIRECTOR  ->  one global seven-value recipe
FREESTYLE                        ->  a sparse per-SECTION delta on whatever the sliders now hold
```

A rule is not a recipe, a recipe is not a rule, and **neither is an input to the other**. The
companion contracts are `.claude/rules/creative-controls.md` (the seven controls and the Stage-6
static/dynamic split), `.claude/rules/creative-presets.md` (a preset's one new role as a *section
style*) and `.claude/rules/variant-lab.md` (the two-directional boundary against the lab).

## Five fields, derived — and two that are unreachable

`FREESTYLE_CONTROL_FIELDS` is **derived** from `presets.CREATIVE_CONTROL_FIELDS` by removing
`micro_cuts`, filtered by *name* rather than by index. It is never restated, so a seventh global
control cannot appear in one registry and not the other, and reordering the global tuple cannot
silently drop a different control.

| Control | Reached by a rule | Stage that reads it |
|---|---|---|
| Cut Density | **yes** | Stage 4, **per section** |
| Semantic Emphasis | **yes** | Stage 6 (static, L1A table) |
| Energy Response | **yes** | Stage 6 (static, L1A table) |
| Motion Bias | **yes** | Stage 6 (static, L1A table) |
| Source Diversity | **yes** | Stage 6 (**dynamic**, threaded per segment) |
| Micro Cuts | **no — global** | Stage 4, one global accent layer |
| Variation Seed | **no — global** | Stage 6, one global RNG stream |

**Why those two stay global.** Micro Cuts governs a rare half-beat accent layer whose entire
contract is that it cannot become flicker; a per-section ratio multiplied by a per-section density is
exactly how that contract gets lost to arithmetic nobody reviewed. The Variation Seed stays global so
the one number a user writes down keeps describing the whole render.

**The enforcement is structural, not careful.** `SectionOverride` has **no field** for either, so a
rule cannot touch them — there is nowhere to put the value. Do not add a field and a validation rule
instead; the absence *is* the guarantee, and `test_freestyle.py` asserts the field set equals
`FREESTYLE_CONTROL_FIELDS` exactly.

## Sparse, keyed by section TYPE, and retained while off

- **Unset inherits.** A rule sets *some* of the five fields; an unset field takes the live global
  value at render time. `effective_profile` returns the base profile **by identity** for an inactive
  declaration, an unruled section, an unknown section type, and a rule whose every value already
  equals the base's.
- **Keyed by type, so every instance of a repeated type shares one rule** —
  `REPEATED_SECTION_POLICY = ALL_INSTANCES_SHARE_RULE`. There is deliberately no per-*instance*
  editor and no timeline: a track's section boundaries are detected from the music *during* the
  render, so a position-keyed rule would describe a layout the user has never seen. Two `drop`s get
  the identical settings and then each selects from its own musical content — asserting that they
  produce equal cut *counts* would be asserting that two different drops are the same drop.
- **Rules are retained while the checkbox is off**, so unticking it to compare two renders loses
  nothing. Therefore **"has rules" is not permission to use them**: `is_active()` is, and it means
  *enabled **and** carrying at least one rule*. **Both Stage 4 and Stage 6 gate on that one method.**
  A real failure this closes: an `isinstance` check in one of them let Freestyle-off change the cut
  timeline while leaving scoring global, so the two stages disagreed about whether the feature was on.
- **A rule stores the numbers, never the preset name.** `override_from_style` resolves the recipe
  once and the label does not survive into the declaration, so a later retune of the preset table
  cannot change what a saved rule meant — the same reproducibility argument that keeps the preset
  name out of `CreativeProfile`.
- **Canonical ordering.** `overrides` is normalised to `SECTION_TYPES` order with unknown types and
  empty rules dropped, so two declarations built by different routes (the GUI tuple, a preset
  projection, a direct caller) compare equal when they say the same thing.

## `SECTION_TYPES` is the one copied upstream vocabulary

`freestyle.py` restates Stage 3's ten section labels — `classify_section`'s nine outcomes plus
`body`, which is what Stage 3 calls a track it could not divide at all. It is **copied rather than
imported** because importing `stage3_sections` would pull numpy and the whole upstream runtime into a
stdlib-only module, which is CLAUDE.md's hard rule for this package.

That copy is therefore only as trustworthy as its pin: `test_freestyle.py` AST-reads the **real**
`classify_section` and asserts every literal it can return has a rule slot, and that `body` is the
*only* addition. Never extend the tuple without that test agreeing, and never "fix" the drift by
importing Stage 3.

## Stage 4: two compositions, and the uniform one is byte-exact

Cut Density is the one Freestyle control that changes the cut timeline, so it is the one that needed
a new composition. `select_wave_cuts` dispatches on a **keyword-only** `section_settings`:

```
section_settings is None   ->  the EXACT legacy body
                               grid -> add_rare_micro_cuts -> final_wave_cleanup
section_settings is a dict ->  per section: select_section_wave_cuts + section_density_cleanup
                               then cross_section_safety -> add_rare_micro_cuts -> micro_extra_safety
```

**`final_wave_cleanup` is deliberately not called on the heterogeneous path.** This is a measured
correctness decision, not a shortcut: its density band comes from the *global* `len(beat_times)` and
its cap ranks cuts across the whole track, so running it after per-section selection lets one
section's rule delete cuts from unrelated sections. Measured on a binding-band fixture: changing only
the `drop` rule mutated five non-target sections, three of them not even adjacent.
`add_rare_micro_cuts` and `final_wave_cleanup` themselves are **unchanged**.

The three new Stage-4 helpers, and the one thing each owns:

- **`section_density_cleanup`** — `final_wave_cleanup`'s density policy with every global input
  replaced by the section's own: the ratio band from *this* section's beat count, the cap ranking
  *this* section's cuts, anchors drawn from *this* section's beats, nothing outside
  `[start, end)` read or written. The global version cannot be reused at any density.
- **`cross_section_safety`** — boundary-**straddling** pairs only. A single global `_unique_sorted`
  over the concatenation would re-apply one gap everywhere and thin a dense section with a sparse
  section's policy; that is the locality leak, not a fix for it. Threshold is
  `min(gap_earlier, gap_later)` — the denser side's own policy already permits that spacing.
  Resolution is **keep the earlier cut, drop the later one**, the rule `_unique_sorted` already
  documents, and the scan continues against the last *kept* cut so a run of rejections cannot chain
  off a cut that was itself dropped.
- **`micro_extra_safety`** — filters micro **extras** only and never touches the main grid.
  `occupied` starts as the grid alone and an accepted extra joins it **after** its own check, so an
  extra can never measure a zero distance to itself. The floor is the accent layer's own
  `max(micro_min_gap, median_beat * 0.45)`, which is density-independent because `micro_min_gap` is
  rewritten by neither derived config. Its `grid_values` filter is **redundant for the output** and
  kept for clarity — measured as an equivalent mutant; the seeded `occupied` set is the guard.

**The dispatch is decided on resolved values, not on the checkbox.**
`auto_mode._freestyle_section_settings` returns `None` whenever every section resolves to the same
effective Cut Density — Freestyle off, on with no rule, on with a rule that sets no density, or on
with every rule landing on the base. Those renders take the **exact legacy composition** and are
byte-identical to pre-Freestyle `main`, rather than a generalised equivalent of it. The two paths are
measurably **not** equivalent (a section can fall under its own local ratio floor and gain anchors
where the global band was satisfied); do not "simplify" them into one.

Configs are derived **per distinct effective density**, memoised, each composed exactly as the global
path composes: density first (`density_scaled_config`), then the **global** Micro Cuts policy on top
(`micro_cut_scaled_config`). Thirteen sections with two distinct densities derive two configs, shared
by identity, and a section at the base density gets the untouched base singleton with
`density_factor=None` — never `1.0`.

**Micro Cuts stays one global control on both paths.** Its policy comes from the globally derived
config, and no section rule may rewrite `enable_rare_micro_cuts`, `max_micro_cut_ratio`,
`micro_min_gap` or `micro_percentile`.

### A known legacy defect, recorded and deliberately NOT fixed

`add_rare_micro_cuts` computes `selected_sorted` before its loop and never adds an accepted extra
back, so two extras can land closer together than the accent layer's own floor; the post-micro
`final_wave_cleanup` enforces only `peak_energy_min_interval` and does not close it either. **Fixing
it would change the output of every existing uniform render, so it is out of scope.** Measured at 200
BPM on a funded 50-cut grid: 4 extras, closest pair **0.3000 s** against a 0.3400 s floor. The
Freestyle path does not inherit it — `micro_extra_safety` returns 2 extras, closest pair 2.4000 s —
and `test_micro_cuts.py` asserts the legacy defect **exists**, so the fix cannot be mistaken for a
no-op. If `add_rare_micro_cuts` is ever made self-safe on purpose, that test is the one that should
fail and be deleted with the decision recorded.

## Stage 6: L1A survived the refactor it was designed for

`ScoringControls` became a per-**segment** value, so the static table grew a key dimension:

```
before   candidates × distinct targets                   (one controls value, eager)
after    candidates × distinct (controls, target) pairs   LAZILY, on first use
never    candidates × segments                            (the pre-L1A shape)
```

- **Keyed by `(ScoringControls, target)`, built lazily**, so the column count is bounded by
  `min(distinct controls × distinct targets, segments)` and can never reach the pre-L1A shape even
  when the pair space is larger than the timeline. Measured on the real 9241-candidate /
  148-segment library: one candidates-wide column is 61 ms, today's global case is 5 columns
  (0.30 s), ten distinct profiles lazily is 10 columns (0.61 s), the pre-L1A shape was 148 columns
  (9.02 s).
- **No new identity type was invented.** `ScoringControls` is a frozen dataclass of three
  `float | None`, hence hashable and equality-deduping, so two sections resolving to the same values
  share one column.
- **Source Diversity stays out of the table** and is threaded per segment, because it reads the
  running `usage` counter. That static/dynamic split is now load-bearing twice over: a control in the
  wrong half would become a table key *per section* rather than once per render.
- **Deterministic views depend on the candidate only**, never on control values, so they are built
  once for the whole call if any active profile needs them, and reused by every table.
- **A flow column is cached per distinct controls tuple**, never shared across different ones —
  sharing would blend two different interpretations together.
- **The seed and the running state stay global.** `usage`, `recent_ids` and `recent_videos` are
  created once for the whole plan, and `_stable_rng(seed, index, target, start)` keeps the exact
  stream it has always had. A per-section `usage` counter would let one candidate be reused once per
  section, which is the opposite of what the repeat penalties exist for.
- **The eager global branch is still a dict comprehension.** An inactive declaration must take
  today's code, not a lazy equivalent of it — and that has to be asserted **structurally**, because
  with neutral controls the lazy table builds exactly the same columns, so a count-based test
  survived a mutation replacing `is_active()` with a bare `isinstance` check.

The recorded diagnostic score uses **that segment's** effective controls, so it describes what
actually selected the clip.

## The bus key, and what Freestyle never reaches

`beat_info["freestyle"]` carries a frozen `FreestyleDeclaration` of plain bools, strings, ints and
tuples: deepcopy-safe, with no mapping proxy, no path, no media and no cache handle. **One reader per
stage** — `stage6_av_planner.freestyle_declaration()`, exactly as `creative_profile()` is the one
reader of `"creative"` — and each is total: an absent, stale or foreign value degrades to an inactive
declaration and the stage takes its global path. A render can never fail because of Freestyle.

**Changing a rule re-plans and never re-analyses.** No Freestyle value reaches `_qwen_config_token`,
`_video_signature`, `_cache_path`, a Qwen request, the Stage-5 prompt or a persisted record;
`CACHE_CONTRACT_VERSION` stays `stage5_cache_v3` and `ANALYSIS_VERSION` stays
`auto_av_analysis_v8_llama_vulkan_batched`. Stages 1–3 never see the declaration at all — it is
resolved after Stage 3, because that is the first point at which real sections exist.

`freestyle.py` depends on `creative.py` and `presets.py` **only**, and the dependency runs one way:
`presets.py` was not modified and knows nothing about Freestyle. The module renders nothing, analyses
nothing, caches nothing, holds no mutable module state, and knows nothing about the Variant Lab, the
Director, `creative_recipe.py`, an RNG stream or a section *boundary*.

## GUI: eleven widgets, one read-out

A checkbox, ten section dropdowns (`Base` plus the four named presets — **`Custom` is excluded**,
since it is a state with no values to project) and a read-only summary textbox.

- **All eleven `.change()` registrations write only `freestyle_summary`.** Never a slider, the
  Variation Seed, `creative_preset`, the lab's master seed or an audio level. `.change()` is correct
  here (unlike the preset/slider graph, which needs `.input()` to stay acyclic) because nothing ever
  writes a Freestyle widget programmatically — and no lab or Director handler may start: a candidate
  or a proposal is seven *global* integers and has no opinion about section rules, so Apply must not
  silently reset them.
- **The summary never prints an inherited number.** It marks every inherited field `Base`, because
  the global controls have five legitimate writers (the preset selector, Variant Lab Generate / New /
  Apply, Director Apply) and a number there could not be kept honest. `summary_text` takes **no
  global control as an argument**, which makes that structural rather than a promise.
- **Execution authority is the live widgets, not a `gr.State`.** Gradio delivers widget changes as
  separate queued events, so a state can lag behind the widgets at click time — the same reasoning
  the live source gate rests on.
- **What crosses the boundary is a plain inline frozen tuple** `(enabled, style × 10)` in
  `SECTION_TYPES` order, **not** the fork record, because the frozen preservation suites AST-extract
  the render bodies and execute them against a synthesised namespace, so those bodies must not name a
  fork module. `auto_mode._resolve_freestyle` is the one conversion to a `FreestyleDeclaration`, and
  the success line reads the resolved declaration back off `beat_info` **duck-typed** for the same
  reason.
- **Appended explicitly and last** to both `process_btn.click` and
  `render_selected_variants_btn.click` — not behind a list concatenation, because the positional
  seam tests read those lists' own `.elts`. Every pre-existing parameter keeps its index.
- **A C3 batch builds its tuple once, before the candidate loop**, so both candidates provably get
  equal declarations and a mid-batch dropdown edit cannot reach candidate 2. Freestyle is shared
  render intent, so it gets exactly the treatment audio, voice, SFX, source, output, encoder and FPS
  already get; `render_batch.py` is untouched.
- **`describe()` is unlabelled.** `ui_content` adds the `Freestyle:` label for the success panel,
  exactly as it adds `Creative variation:` for `CreativeProfile.describe()`; Stage 4's console line
  prints it in parentheses after its own counts.

## Deliberately not built

No CLI flag, no new environment variable, no stage cache, **no per-section Micro Cuts**, **no
per-section Variation Seed**, no numeric per-section sliders, no section-*instance* editor, no visual
timeline and no pre-render "Analyze Music" workflow. Tests ban each by name (`freestyle_micro`,
`freestyle_seed`, `freestyle_slider`, `freestyle_numeric`, `freestyle_instance`,
`freestyle_timeline`, `analyze_music`, `freestyle_director`, `freestyle_variant`), and
`per_section_profile` stays banned everywhere: Freestyle is a sparse *override* composed into the
existing `CreativeProfile`, never a stored per-section profile object.

**L2 stage caching is still future work**, and `creative-controls.md`'s invalidation table already
says how it would land: a rule inherits the row of whichever control it overrides, so a
`motion_bias`-only rule reuses Stages 1–5, and a `cut_density` rule also re-runs Stage 4. An L2 key
derived from the **resolved** per-section values — not from whether the checkbox is ticked — would
reuse correctly with no extra machinery, because Stage 4's uniform short-circuit already treats an
all-on-base declaration as a global render.

## Runtime acceptance (Freestyle V1)

**Not re-verified for the current `freestyle.py`.** That module, this rule and
`tests/test_freestyle.py` were absent from the tree when the feature was otherwise finished, so the
module was **reconstructed** against its existing call surface and test assertions; the full suite
passes, but the run below is the **original** implementation's evidence and was not repeated. It
cannot be repeated off-Windows — no portable runtime, no FFmpeg, no models. Repeat it on the Windows
machine before citing the cut counts as current, and record the result here. See `CHANGELOG-FORK.md`
under *Added — 2026-10-04 (Freestyle V1)* for the full status.

Real Stages 1–6 through real FFmpeg on a 140 BPM / 72 s synthetic track with Stage-3 sections
`intro`/`drop`/`verse`/`finale` and three sources, in an isolated scratch build. Freestyle OFF gave
47 cuts; ON with every section `Base` gave a **byte-identical** 47; ON with `drop=High Energy` +
`intro=Cinematic` gave 43 and reproduced exactly on a repeat run. Both renders came out at 72.000 s
with video and audio streams. Holding a per-section baseline and moving only the `drop` rule, the
number of other sections that changed was **zero**.

One measured behaviour worth stating because it looks wrong and is not: ruling `drop` at density 65
or 100 *reduced* that section from 17 cuts to 14. The **global** Cut Density slider does exactly the
same on the same material (50 → 17; 65/80/100 → 14), so the per-section control faithfully reproduces
the global one, non-monotonicity included. That discreteness is pre-existing — `creative-controls.md`
records the same effect in the 72–82 region — and a per-section control inherits it rather than
curing it.

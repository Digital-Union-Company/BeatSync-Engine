---
paths:
  - "src/beatsync_fork/presets.py"
  - "tests/test_creative_presets.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

## Creative Presets are named slider recipes and nothing else (PR3)

`beatsync_fork/presets.py` holds four immutable recipes for the six 0–100 controls. **A preset is a
name for six numbers.** Selecting one writes those sliders and then the name stops existing:

```
PRESET          = a name for six numbers      (UI convenience, ephemeral)
THE SIX SLIDERS = the sole execution truth     (already the whole contract)
```

| Preset | Cut Density | Micro Cuts | Semantic Emphasis | Energy Response | Motion Bias | Source Diversity |
|---|---|---|---|---|---|---|
| Balanced | 50 | 50 | 50 | 50 | 50 | 50 |
| Cinematic | 30 | 25 | **65** | 40 | 30 | 50 |
| Dynamic | 65 | 60 | 50 | 70 | 70 | 75 |
| High Energy | 100 | 85 | 50 | 85 | 80 | 80 |

The preset **name** reaches nothing: not `CreativeProfile`, `beat_info["creative"]`,
`process_video_guarded`, `process_video`, `analyze_beats_auto`, any stage, `render_info` or the
filename. The selector is deliberately **not** a `process_btn` input — the six sliders already are,
and they stay the only creative values the render request carries, so the planner has no idea
presets exist. Nothing is persisted either, and that is a reproducibility decision rather than
minimalism: the six integers already describe a render completely, so if a recipe is ever retuned an
old render reproduces from *its recorded values* instead of from a historical label.

- **`Custom` is a state, not a recipe.** It means only "the six live values match no named recipe",
  so it has no entry in `PRESETS` and selecting it writes nothing (`gr.skip()` per slider). There is
  deliberately no "last selected preset" state anywhere.
- **The selector is an honest read-out of the sliders.** Every slider's handler recomputes the label
  from **all six** values via `matching_preset`, so a manual edit reads `Custom` *and* a manual edit
  back onto a recipe reads that recipe's name again. The label is a statement about the whole tuple,
  which is why each of the six handlers reads all six rather than being told which one moved.
- **`.input()` in both directions, and that is what makes the graph acyclic.** Gradio 6.19's
  `.input()` fires only for a *user* change; `.change()` also fires for programmatic updates. So the
  preset→slider write cannot re-trigger the slider handler and the slider→selector write cannot
  re-trigger the preset handler. No re-entrancy guard is needed, and a `.change()`/`.release()`
  binding on any of those seven widgets would reintroduce the cycle — a test rejects one. The six
  slider bindings are written out **explicitly per widget**, not in a loop and not through `gr.on`:
  either of those hides the binding from the per-widget seam assertions, which is evasion rather
  than compliance.
- **One user handler per slider; four permitted programmatic writers.** These are two different
  statements and conflating them is how this rule went stale. The older wording — "the only
  permitted writer of a slider is `creative_preset.input`" — stopped being true the moment C2
  shipped, and C3 added a fourth. Current truth:

  ```
  USER HANDLER (each slider registers exactly one, and it must be .input()):
      <slider>.input()  ->  creative_preset

  PERMITTED PROGRAMMATIC WRITERS of a slider, exactly these five:
      creative_preset.input
      generate_variant_btn.click
      new_variant_btn.click
      apply_variant_btn.click      (Variant Lab C3 V1 — Apply Selected Variant)
      apply_director_btn.click     (AI Director V1 — Apply Proposal)

  PERMITTED WRITERS of `creative_preset`, exactly these ten:
      the six <slider>.input handlers
      generate_variant_btn.click
      new_variant_btn.click
      apply_variant_btn.click
      apply_director_btn.click
  ```

  `generate_variants_btn.click` and `generate_director_btn.click` are deliberately **not** writers
  of either: generating candidates for comparison, and generating a proposal for review, both
  write no execution widget. `test_creative_controls_seam.py` and `test_creative_presets.py` pin
  both lists exactly and resolve list indirection, so neither an extra writer nor one hidden behind
  an intermediate list can arrive unreviewed. Preset *semantics* are unchanged by any of this: the
  label is still recomputed from all six values, and a programmatic write still does not fire
  `.input()`, which is why both projection helpers compute `matching_preset` themselves.

- **A Director never outputs a preset label (AI Director V1).** Which named recipe six numbers
  happen to match is a GUI read-out, not something a model may assert — so the Director's JSON
  schema has no preset property, `director.py` contains no label concept at all, and Apply
  recomputes `presets.matching_preset(...)` from the proposal's six numbers and returns it
  explicitly (`50×6 -> Balanced`, anything unmatched `-> Custom`). `director.py` does import
  `presets`, but only for `CREATIVE_CONTROL_FIELDS`, which is the one six-field registry its schema
  is derived from; a test pins that as the module's *only* use of it. The Director does not reuse
  `_variant_apply_outputs` — that helper also writes the lab's Master Seed, the three audio levels
  and the lab report — but it does reuse `matching_preset`, so there is still exactly one
  preset-label path. See `.claude/rules/director.md`.
- **`creative_control_sliders` order *is* a contract.** It is the preset handler's `outputs` and
  every slider handler's `inputs`, and Gradio matches those positionally, so it must stay in
  `CREATIVE_CONTROL_FIELDS` order (which is **not** the widget declaration order). A test pins it.
- **Balanced is exactly current behaviour** and is the reset. It resolves through the *existing*
  profile to `legacy` — no Balanced branch was added to the pipeline.
- **Seed independence.** The Variation Seed is in no recipe, is not an output of any preset handler,
  and `presets.py` has no seed concept at all (asserted against the module's executable source, so
  the docstring may still explain why). Randomize is unchanged.
- **Values are plain literal integers, not normalised at definition time.** The tests prove
  `creative.normalize_control` returns each one unchanged, so a typo fails a test instead of being
  silently clamped into a slider position nobody chose. `presets.py` therefore does not import
  `creative.py`; it is inert data that the existing boundary happens to accept.
- **`matching_preset` and `resolve_preset` are total.** They run on live widget values during a UI
  interaction, so `None`, a string, a mapping, a wrong-length sequence, a `bool` element, `NaN`/`inf`
  and an object whose `__iter__` raises all answer `Custom`/`None` rather than raising. The
  conversion catch is deliberately broad (`except Exception`, around that one step only) because a
  narrower `TypeError` catch let a non-`TypeError` `__iter__` escape — a test demonstrates it.
  Floats are accepted because a slider reports `50.0`; `bool` is rejected first since it subclasses
  `int`. Names match **exactly and case-sensitively** (`"cinematic"` → `None`), so the displayed
  value and the applied recipe can never be two separately-decided things.
- **The recipes are coherent recipes, not a diagonal.** Cinematic is the only preset that moves
  Semantic Emphasis (the only name that genuinely implies contextual reading) and the only one that
  leaves Source Diversity neutral — at ~20 % fewer cuts the source-reuse pressure is already lower,
  so the control is not spent where it was not needed. Dynamic and High Energy leave Semantic
  Emphasis neutral because Stage 5 motion-gates semantic action, so on the action material a dense
  edit selects the two readings nearly coincide. High Energy is **not** "all sliders at 100":
  exactly one control is at its limit. Dynamic needs Source Diversity 75 rather than 65 because
  Motion Bias 70 concentrates the edit on high-motion sources, and at 65 the measured source
  concentration was *worse* than neutral.
- **High Energy's Cut Density is 100 on measured grounds.** Cut Density has a **pre-existing
  non-monotonic region around 72–82** on real material (integer beat-step re-quantisation combined
  with the min-interval floor's reject-and-advance), so density 90 measured ~1.548 s average
  interval — only ~6 % shorter than Dynamic's ~1.650 s — while 100 gives ~1.377 s, ~17 % shorter.
  Consuming that control's headroom is the accepted cost of the strongest named pacing recipe.
  **Cut Density itself is unchanged**; the quirk is recorded here, not fixed, and the chosen recipes
  (65 and 100) sit clear of it. Do not retune these recipes without new measurement.
- **No CLI preset flag.** The CLI already exposes all six controls (`--cut-density`,
  `--micro-cuts`, `--semantic-emphasis`, `--energy-response`, `--motion-bias`,
  `--source-diversity`); six explicit numbers are the reproducible contract. A `--preset` alias
  would add precedence ambiguity (`--preset X --motion-bias 70`) and recipe-name versioning
  ambiguity for no gain.
- **Source gate and preparation isolation** is structural: `creative_preset` is absent from
  `source_outputs`, `prep_outputs`, every source and preparation handler, `live_declaration` and the
  render request, and a test walks all seven preset registrations asserting none of their outputs
  can reach a gate or preparation widget.
- **Two existing seam tests were amended, not weakened.** They asserted that a creative slider
  registers *no* handler and that *nothing* writes one. Each now registers exactly one, so the
  assertions moved to the stronger properties the originals protected: exactly one handler, it must
  be `.input()`, and its only output is `creative_preset`. Keep
  `test_creative_profile.py::test_no_preset_or_director_field_was_added` — it is now the
  load-bearing proof that presets stayed a UI layer.

Measured on the real Nero track + 41-source TEST1 pool (Stage 4 and Stage 6 run per recipe against a
fully warm cache; no render, no Qwen, no cache write): Balanced 147 cuts / 1.876 s / 33 sources;
Cinematic 117 / 2.345 s / 34; Dynamic 167 / 1.650 s / 36; High Energy 200 / 1.377 s / 40 of 41. All
four produced complete plans with zero fallbacks, zero adjacent candidate repeats and an unchanged
minimum cut gap across seeds 0, 7, 101, 4242, 381944 and 999999.

## What presets deliberately are *not*

A preset is not a planner mode, a Stage-5 input, a cache key or a second interpretation path. Named
Creative Profile *values* were the whole feature, and the recipe values were chosen from measured
behaviour on real material rather than guessed — which is why presets landed only after all seven
controls had runtime acceptance.

Everything adjacent stayed out of scope here and acquired no dormant abstraction: Freestyle, an AI
Director, Source Groups and L2 caching. Variant Lab and `CreativeRecipe` were on that list until C2
implemented them — the guard was then **split rather than weakened**: `presets.py` still knows
nothing about either (a preset remains a named set of slider values with no generator concept),
while `gui.py` may wire the lab up. The pre-existing prohibition on a preset *button*, randomiser or
reset for the six controls is unchanged.

## The Director boundary — implemented; so is Freestyle now

**AI Director V1 shipped, and it landed exactly where C2 said it would.** `CreativeRecipe` was
written as the contract a future AI Director should *produce*, which is why it carries no master
seed, ranges or spread — so the Director emits one without pretending to have had them, and
`creative_recipe.py` needed no change at all. The guard here was therefore **split rather than
weakened**: `presets.py` still knows nothing about a Director (a preset remains a named set of
slider values with no generator concept), while `gui.py` may wire one up, and the accepted GUI
surface is an explicit allow-list so a *second* director concept cannot drift in beside it.

The accepted direction is preserved and was not spent: **persistent media truth + ephemeral
Creative Profile.** Director V1 is media-blind, has no cache, requires no Stage-5 re-analysis,
leaves `CACHE_CONTRACT_VERSION` and `ANALYSIS_VERSION` untouched, and overwrites no persistent
semantics — it proposes seven integers for the controls that already existed. Full contract:
`.claude/rules/director.md`.

**Freestyle V1 shipped too, and a preset gained exactly one new role: it is now also a *section
style*.** Selecting `Cinematic` for the `drop` sections projects that recipe's **five** values —
Cut Density, Semantic Emphasis, Energy Response, Motion Bias, Source Diversity — into a
`SectionOverride`, and leaves Micro Cuts and the Variation Seed global. Three properties keep this a
UI convenience rather than a second recipe concept:

- **`presets.py` was not modified and knows nothing about Freestyle.** A preset is still a name for
  six numbers; `freestyle.py` imports `presets` and reads it, never the reverse.
- **A rule stores the numbers, never the name.** `override_from_style` resolves the recipe once and
  the label does not survive into the declaration, so a later retune of the table cannot silently
  change what a saved rule means. Same reproducibility argument that keeps the preset name out of
  `CreativeProfile`.
- **`Custom` is excluded from the section-style choices**, because it is a *state* meaning "the live
  values match no recipe" and therefore has no values to project. `Base` — meaning "inherit the live
  global value at render time" — takes its place as the default.

The pre-existing prohibition on a preset *button*, randomiser or reset for the six global controls is
unchanged, and so is the `Custom`-is-not-a-recipe rule. Full contract: `.claude/rules/freestyle.md`.

Two different things are easy to conflate here, and the tests encode both separately:

**Implemented elsewhere, but `presets.py` must still know nothing about it.** **L2 Stage-3 caching**
(`beatsync_fork/stage_cache.py`, `L2_CACHE_VERSION = "l2_stage3_v1"`) and **Freestyle V1**
(`beatsync_fork/freestyle.py`) both ship. A preset stays a *name for six slider values*: it acquires
no cache concept and no section mechanism. `stage_cache` therefore remains in the globally-speculative
ban list for the files this rule governs, and `freestyle` moved to `_PRESETS_ONLY_SPECULATIVE` —
accepted in `gui.py`, still forbidden inside `presets.py`. That split is the point; do not read either
ban as a claim that the feature does not exist.

**Genuinely not implemented anywhere, and no speculative abstraction was added for any of it:**
source groups, **per-section Micro Cuts**, **a per-section Variation Seed**, a per-section *instance*
editor (Freestyle keys on section *type*), a second style-aware Qwen pass over a shortlisted
candidate set, and a **content-aware Director** that reads the Stage-5 library. Tests assert the
absence of shortlist machinery, of `director_cache` / `director_history` / `director_media` /
`director_render` / `director_transform`, and of `freestyle_micro` / `freestyle_seed` /
`freestyle_timeline` / `analyze_music`. None of these is scheduled: they are recorded so the bans have
a stated reason, not as a roadmap. The permanent constraints are unchanged: creative state never
enters Stage-5 cache identity, no per-render interpretation overwrites persistent semantics, and a
future second pass would stay run-scoped rather than becoming the library's durable truth.

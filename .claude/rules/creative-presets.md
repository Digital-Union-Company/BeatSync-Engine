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
  be `.input()`, its only output is `creative_preset`, and the only permitted writer of a slider is
  `creative_preset.input`. Keep
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

## Future Freestyle / Director boundary

Not implemented, and no speculative abstraction was added for it (a test asserts no
director/freestyle/shortlist/stage-cache machinery exists). C2's `CreativeRecipe` is the contract a
future AI Director should *produce* — which is why it carries no master seed, ranges or spread, so a
non-random generator need not pretend to have had them. The accepted direction is
preserved: **persistent media truth + ephemeral Creative Profile.** A future Freestyle mode may vary
these controls per section; a future Director may interpret the same Stage-5 library differently.
Neither may require Stage-5 re-analysis, creative state may never enter Stage-5 cache identity, and no
per-render interpretation may overwrite persistent semantics.

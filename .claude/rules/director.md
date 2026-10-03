---
paths:
  - "src/beatsync_fork/director.py"
  - "tests/test_director.py"
---

> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

# AI Director V1

```
natural-language editing intent
    -> local text-only Qwen generation        (gui.py runs the subprocess)
    -> strictly validated visual proposal     (beatsync_fork/director.py)
    -> the user reviews it
    -> the user explicitly presses Apply Proposal
    -> the existing Variation Seed + six Creative Controls
    -> the user may edit, use Variant Lab, or render normally
```

**The Director proposes. It never renders, and it is not a second execution path.** It is a
*second producer* of the artifact Variant Lab already produces — which is exactly what
`creative_recipe.py` anticipated when it refused to carry a master seed, a spread or any other
generator provenance:

```
VARIANT LAB    = a recipe from a master seed, ranges and a spread
AI DIRECTOR    = a recipe from one sentence
CREATIVE RECIPE = what WILL be rendered   (seven integers, the VISUAL execution artifact)
THE SIX SLIDERS + THE SEED = where it lands (unchanged execution truth)
```

Existing visible execution controls stay authoritative. No stage, planner, profile or cache has
heard of a Director.

## Visual only

V1 produces one `CreativeRecipe` and nothing else:

```
DIRECTOR_V1_OUTPUT_SCOPE           = VISUAL_ONLY
DIRECTOR_WRITES_RESOURCE_IDENTITY  = NO
```

It does **not** generate or modify `music_under_voice`, `sfx_amount`, `sfx_level`, voice files,
voice timing, `avoid_drops`, the SFX folder or roles, the source folder or files, FPS, the encoder,
the output filename, the source confirmation or the Media Library Preparation state. Those are
resource identity and physical render intent; a text prompt is not an authority on them.

## The model emits six controls; the GUI mints the seed

```
DIRECTOR_VARIATION_SEED_POLICY = GUI_MINTS
MODEL_SCHEMA_INCLUDES_SEED     = NO
```

The schema's properties are **derived** from `presets.CREATIVE_CONTROL_FIELDS` — there is no second
six-field registry, and `director.py` carries an import-time assertion to that effect plus a second
one pairing every field with a semantic description. Each control is `integer`, `0..100`, required;
`explanation` is optional; `additionalProperties` is `false`. No schema-version machinery.

With `additionalProperties` false the grammar cannot emit a `seed`, and the strict parser rejects
one that arrives anyway — not ignores it, because a producer that thinks it owns the seed is a
producer worth failing. Order is the contract:

```
model output -> strict six-control validation -> mint the Variation Seed
             -> CreativeRecipe.from_mapping   -> DirectorProposal
```

The mint is the existing `variation.random_seed()`, called in `gui.py` because the pure module owns
no randomness. There is no second seed implementation. A test counts the draws: an invalid response
must consume **zero**, so a malformed answer cannot produce a plausible-looking half proposal.

## The CreativeRecipe trust boundary is reused, unmodified

`creative_recipe.py` was **not touched** and `from_mapping` was **not weakened**. The whole
all-or-nothing contract still holds: missing field, extra field, wrong type, `bool`, `float`, out of
range or seed 0 rejects the *whole* recipe. No coercion, no clamping, no partial application, no
silent defaults, no fallback to Balanced. `CREATIVE_RECIPE_STRICT_BOUNDARY_PRESERVED = YES`.

## Strict execution, tolerant explanation

Two trust contracts, deliberately opposite, and conflating them is the mistake worth naming:

```
valid six controls + missing explanation      -> valid proposal, explanation ""
valid six controls + non-string explanation   -> valid proposal, explanation ""
valid six controls + overlong explanation     -> valid proposal, explanation bounded
invalid execution control                     -> the WHOLE proposal is rejected
```

`DIRECTOR_EXPLANATION_POLICY = TOLERANT_NON_LOAD_BEARING_BOUNDED`. The explanation is normalised
for display only (whitespace collapse, `EXPLANATION_MAX_CHARS = 280`) and never enters
`CreativeRecipe`, `CreativeProfile`, `beat_info`, `render_info`, Stage 4/5/6, cache identity, a
Stage-5 Qwen request or the planner. Failing a good recipe because the prose was ugly would be
strictness pointed at the one field where it buys nothing.

**The schema's `maxLength` is advisory on the installed build, and that is measured.** llama.cpp
does not compile `maxLength` into its JSON-schema grammar there: declared limits of 60, 160 and 280
all produced identical ~460-character strings. So the schema asks and `normalize_explanation`
guarantees. The reason a bound is wanted at all is Stage 5's documented truncation defect — an
*unbounded* string ate the token budget so the JSON never closed
(`.claude/rules/stage5-worker.md`) — and the mitigation is the same shape: ask for one short
sentence, budget `MAX_NEW_TOKENS = 320` at roughly three times the measured ~115-token need, and
bound what reaches the screen.

## Parse the whole of stdout strictly

No regex fishes a `{...}` out of surrounding prose. A broad `\{.*\}` search is how a truncated
object, a code fence or a chatty preamble gets silently half-accepted, and Stage 5's own truncation
defect lived exactly there. The parse is: strip, remove the one fixed `[end of text]` marker,
`json.loads` the entire remainder, require a mapping, require the exact key set, require six plain
in-range `int`s. `--json-schema` is defence in depth; **the parser is the authority**.

`_END_OF_GENERATION_MARKER` is the one deliberate exception and it is a *constant, not a pattern*:
llama.cpp appends `" [end of text]"` to its own output when generation stops on end-of-sequence.
Stripping exactly that, exactly once, from exactly the end leaves every failure mode intact — a
marker in the middle, a different suffix and real trailing commentary all still fail. Do not
generalise it into a regex or a list of tolerated suffixes.

## Media-blind, slider-blind, cacheless

```
DIRECTOR_INSPECTS_MEDIA        = NO
DIRECTOR_READS_CURRENT_SLIDERS = NO
DIRECTOR_CACHE                 = NONE
```

The invocation receives the instruction, the system prompt and the schema. It receives no frames,
no source filenames, no Stage-5 semantic records, no `beat_info`, no sections, no tempo, no music
features and no current source state. A content-aware Director is a later milestone, and no dormant
abstraction was added for it.

It also does not read the Variation Seed, the six sliders, the preset or any Variant Lab state, so
the instruction is an **absolute** editing intention rather than a transformation of what is on
screen. A transform-current-settings mode is out of scope, not half-built. There is no Stage-5 cache
use, no proposal cache, no prompt history and no conversation state: every press is independent.

## Model assets are reused; the Stage-5 worker is not

```
MODEL_ASSETS_REUSED  = YES
STAGE5_WORKER_REUSED = NO
```

The same installed `Qwen3VL-2B-Instruct-Q8_0.gguf`, **text-only**: no `mmproj` is loaded and no
image argument is passed. `stage5_qwen_scene_worker.py` is deliberately not invoked and
`beatsync_fork.qwen_progress` is not used — those exist to batch frames through a persistent
`llama-server` and write a semantic response file, which is a different contract from one bounded
JSON answer. Reusing them would have meant teaching a media-semantics worker about creative intent,
which is exactly the leak `.claude/rules/stage5-worker.md` forbids. The worker is also not imported
for its paths: `gui.py` derives both from the one general `ROOT_DIR` constant.

## One bounded, one-shot subprocess — and the binary is `llama-completion.exe`

```
DIRECTOR_MODEL_STRATEGY = REUSE_QWEN3VL_TEXT_ONLY_ONE_SHOT
TIMEOUT_SECONDS         = 60
```

No `llama-server`, no port, no readiness polling, no persistent model process, no session. One
`subprocess.run` with a timeout, `CREATE_NO_WINDOW`, stdout and stderr captured separately. Success
requires `returncode == 0` **and** a strictly valid stdout payload. Every failure — executable
missing, model missing, timeout, non-zero exit, empty stdout, malformed JSON, invalid controls,
unexpected properties — produces a Director status and changes no execution widget. `run` rather
than `Popen` is the reason no process is left alive: it kills and reaps the child before raising,
and a test forbids `Popen` in the Director's call graph.

**Two measured deviations from the obvious invocation. Do not "simplify" either back.**

1. **`llama-completion.exe`, not `llama-cli.exe`.** On the installed build (`b9842-6f4f53f2b`)
   `llama-cli` is the interactive chat front end: it *rejects* `-no-cnv` outright
   (`--no-conversation is not supported by llama-cli / please use llama-completion instead`),
   ignores `--no-display-prompt`, and prints its banner, its command list, the echoed prompt and a
   timings line **into stdout** alongside the answer. Parsing that would mean the very regex the
   strict parser exists to refuse. `llama-completion.exe` ships in the same `bin` layout, is the
   binary `llama-cli` itself names, takes every argument, and emits the JSON object alone on stdout
   with the banner, logs and timings on stderr.
2. **`-cnv -st`, not `-no-cnv`.** `-cnv` is what applies the model's own chat template; `-st` runs
   exactly one turn and exits (non-interactively, because the turn is predefined by `-p`). There is
   still no chat history — the process dies. Raw completion mode skips the template, and on an
   *Instruct* model that is not a small difference: measured over five intents, `-no-cnv` collapsed
   every control to 0 or 1 and rambled past the token budget, while `-cnv -st` produced coherent,
   well-separated recipes (`20/10/70/60/30/50` for a cinematic intention against
   `100/100/50/100/50/50` for an aggressive one). The retained P0 probes could not distinguish the
   two: the server probe went through `/v1/chat/completions` (template applied) and the `llama-cli`
   probe's `-no-cnv` was silently rejected by the binary, so **both** measured template-applied
   output while one of them looked like a raw-completion result.

The rest: `-ngl 99`, `-c 2048`, `-n 320`, `--no-display-prompt`, `--no-perf`, `-co off`,
`--temp 0.0 --top-k 1`, `-sys`, `-p`, `--json-schema`. Greedy decoding matches the repository's
existing Qwen convention, so the same instruction proposes the same six controls — the honest
product, since the Director reads *words*. The Variation Seed is freshly minted every press
regardless, so two proposals from one instruction are still two different edits.

Generation settings are **hard-coded, not environment variables**, for the reason
`.claude/rules/stage5-worker.md` records for the recovery constants: a knob that changes a result
belongs under contract. They reach no cache key, and the Director has no cache at all.

The system prompt is asserted to be **pure ASCII** — it is a process argument to a native binary,
so a decorative em dash is a mojibake risk for no gain. UI copy is free to use them.

## Propose, then apply

```
DIRECTOR_APPLY_MODEL     = PROPOSE_THEN_APPLY
GENERATING_IS_NOT_APPLYING = YES
DIRECTOR_AUTO_RENDER     = NO
```

- **Generate Proposal** reads only the instruction and writes **zero** execution widgets — only
  `director_proposal_state`, the proposal read-out and the status. That absence is what makes
  generating-is-not-applying structural rather than careful, exactly as it is for
  `generate_variants_btn`. A failure clears the state rather than leaving the previous proposal
  behind a status line that contradicts it, and never replaces it with a fake valid object.
- **Apply Proposal** reads only `director_proposal_state` and writes exactly `variation_seed`, the
  six sliders, `creative_preset` and the Director status. No audio widget, no source widget, no
  Variant Lab master seed, no batch state, no report panel, no render.
- **Neither handler can reach a render entry point or the source gate**, walked structurally from
  both buttons so a rename cannot evade it.

### Apply is deliberately NOT stale-gated

Variant Lab's Apply has a live-declaration gate because a candidate describes a *base* the screen
may have moved away from. A Director proposal is an absolute set of seven values, as valid now as
when it was generated — so `director_proposal_state` survives an apply and the same explicit
proposal may be re-applied after manual experiments. **Do not add Apply-staleness semantics here,
and do not weaken Variant Lab's.** Generate Proposal replaces the prior proposal.

```
DIRECTOR_STATE_READER_COUNT = 1     # apply_director_btn.click, and nothing else
```

### `_variant_apply_outputs` is deliberately not reused

It is the right projection for Variant Lab and the wrong one here: it also writes the lab's Master
Seed (generator provenance the Director never had), the three `AudioRecipe` levels (which V1 does
not generate) and the lab report. Reusing it would have made the Director claim audio values it
never produced. `_director_apply_outputs` is a small Director-specific projection that reuses the
one semantic helper that matters — `presets.matching_preset` — so there is still exactly one
preset-label path. `DIRECTOR_PRESET_MATCHING_REUSED = YES`.

The Director **never emits a preset label**: which named recipe six numbers happen to match is a
GUI read-out, not something a model may assert. Programmatic slider writes do not fire `.input()`,
so Apply recomputes `matching_preset` from the proposal's numbers and returns it explicitly —
`50×6 -> Balanced`, anything unmatched `-> Custom`.

## Writer matrices, extended by exact list

```
the six creative sliders:  creative_preset.input, generate_variant_btn.click,
                           new_variant_btn.click, apply_variant_btn.click,
                           apply_director_btn.click
variation_seed:            randomize_btn.click, generate_variant_btn.click,
                           new_variant_btn.click, apply_variant_btn.click,
                           apply_director_btn.click
creative_preset:           the six <slider>.input handlers, generate_variant_btn.click,
                           new_variant_btn.click, apply_variant_btn.click,
                           apply_director_btn.click
```

Pinned as **exact sorted lists with list indirection resolved**, never relaxed to containment.
`generate_director_btn.click` is absent from all three, for exactly the reason
`generate_variants_btn.click` is. See `.claude/rules/creative-presets.md` and
`.claude/rules/creative-controls.md`.

## Isolation

`DIRECTOR_CHANGES_STAGE5_CACHE_IDENTITY = NO`,
`DIRECTOR_CHANGES_PERSISTED_MEDIA_SEMANTICS = NO`. `CACHE_CONTRACT_VERSION` stays
`stage5_cache_v3` and `ANALYSIS_VERSION` stays `auto_av_analysis_v8_llama_vulkan_batched`;
`video_analysis.py`, `stage5_qwen_scene_worker.py`, `src/auto_mode/*`, `video_processor.py`,
`ffmpeg_processing.py`, `creative.py`, `presets.py`, `creative_recipe.py`, `variant_lab.py`,
`variant_batch.py`, `render_batch.py`, `audio_mix.py` and `smart_mix.py` are **untouched**.

Director widgets are absent from `source_outputs`, `prep_outputs`, `live_declaration`,
`confirm_action`, `process_btn.click`, `render_selected_variants_btn.click`, `prep_scan_btn` and
`prep_analyze_btn`; Director events write neither `source_state` nor `prep_state`. Changing,
generating or applying Director intent cannot invalidate a source confirmation. The Director also
borrows no existing read-out: `variant_report`, `variant_batch_status`, `variant_batch_table`,
`audio_layers_report`, `smart_mix_report` and `render_batch_summary` keep their existing single
writers.

```
VARIANT_LAB_INTEROP                 = VIA_LIVE_SLIDERS_ONLY
DIRECTOR_DIRECT_C3_R0_INTEGRATION   = NO
```

There is no direct Director → Variant Lab wiring. After Apply, the recipe is on the visible
sliders, and Variant Lab reads those sliders as its base exactly as it always has — so
Director → Apply → Generate Variant(s) → C3-R0 works through existing execution truth.
`variant_lab.resolve`, `variant_lab.resolve_audio` and `variant_batch.resolve_batch` were not
modified, and the Director populates no `VariantBatch`, no render selector and no batch summary.

## No CLI flag

The CLI already exposes all seven resolved visual values, which are the reproducible execution
contract for the edit. A `--director "make it cinematic"` flag would add a non-deterministic,
model-dependent step to a headless entry point whose whole value is that its seven numbers *are*
the contract.

## UI copy must not over-claim

Two claims the copy may never make: that the instruction **reproduces** a result (a prompt is not a
recipe identifier, and the seed is minted fresh every press), and that the Director has **looked at
the footage** (V1 is media-blind). No "perfect", "best edit", "understands your footage" or "fully
automatic". A test pins the absence of all of them in the proposal read-out.

## Real-runtime acceptance

Out of scope for the portable suite, by the same rule as every other real-model path. The pure half
and the GUI runtime seam are both covered without a model: `tests/test_director.py` imports
`director.py` normally and AST-extracts the real `gui.py` handlers over a controlled `subprocess`
seam, so a timeout, a non-zero exit, a launch `OSError`, a missing asset and every malformed
payload are all exercised. The real-model run belongs in
`C:\tmp\BeatSync-Engine-DigitalUnion\tasks\...` against read-only assets.

Measured on the installed build, no mmproj, text-only: **2.54–2.57 s** per proposal end to end
(cold model load every time, because the process exits), `rc=0`, strict payload valid, recipe
`20/10/70/60/30/50` for the cinematic acceptance instruction, Variation Seed minted by the GUI,
`matching_preset -> Custom`, no orphan process, and the applied six sliders accepted unchanged as a
Variant Lab and `variant_batch` base. Well inside the 60-second bound.

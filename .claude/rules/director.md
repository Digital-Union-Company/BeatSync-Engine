---
paths:
  - "src/beatsync_fork/director.py"
  - "src/beatsync_fork/director_media.py"
  - "tests/test_director.py"
  - "tests/test_director_media.py"
---

> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

# AI Director V2 — semantic intent IR + one narrow media adaptation

```
natural-language editing intent
    -> local text-only Qwen3-4B generation      (gui.py runs the subprocess)
    -> strictly validated SemanticIntent        (beatsync_fork/director.py)
    -> deterministic semantic -> six-control BASE mapping
    -> optional narrow Source Diversity attenuation   (beatsync_fork/director_media.py)
    -> FINAL six controls
    -> the user reviews BASE, the adjustment and FINAL
    -> the user explicitly presses Apply Proposal
    -> the existing Variation Seed + six Creative Controls
    -> the user may edit, use Variant Lab, or render normally
```

**The Director proposes. It never renders, and it is not a second execution path.** It is a
*second producer* of the artifact Variant Lab already produces:

```
VARIANT LAB     = a recipe from a master seed, ranges and a spread
AI DIRECTOR     = a recipe from one sentence (+ one local media fact)
CREATIVE RECIPE = what WILL be rendered   (seven integers, the VISUAL execution artifact)
THE SIX SLIDERS + THE SEED = where it lands (unchanged execution truth)
```

## Why V2 exists: the model must not speak control names

V1 asked the model for the six internal controls directly. That shape was then measured three ways —
dense six values, sparse absolute values, and direction+strength on the internal names — and all
three failed the *same* ordinary sentence:

> "Keep scene choice relatively even across sections."

read as **neutral** by a 2B model, a 4B model and an 8B model, across all three contracts: six
measurements, one answer. The problem was never model size. The language-understanding task included
"which way does the `energy_response` slider move?", which is implementation trivia the user never
said.

V2 removes that from the model's job. The model classifies the user's meaning on six ordinary
**editing dimensions**, each with its own meaningful direction pair, and deterministic code alone
knows the mapping. With that one change the same 4B model read the sentence correctly.

**The frozen evidence this feature was authorized on** (selected model
`Qwen3-4B-Instruct-2507 Q8_0`, SHA256 `ae916ede…d5f1`, 4,280,403,520 bytes):

```
SCHEMA_VALID_RATE               100 %
ENERGY_CLUSTER                  6 / 6     (all six paraphrases, incl. the sentence above)
CONCEPT_PASS_COUNT              14 / 14
MEDIA_CONTROL_CONCEPT_PASS      10 / 10
WRONG_DIRECTION_TOTAL           0
UNSEEN_SINGLE_AXIS_HOLDOUT      12 / 12   (a matrix the model had never seen)
NON_TARGET_AXIS_EMISSION        2.50 %
DETERMINISM                     PASS      (byte-identical repeats)
MEDIAN_LATENCY                  ~3.5 s
```

**The prompt and the schema are hash-pinned to those exact bytes.**
`SEMANTIC_IR_SYSTEM_PROMPT_SHA256 = 2ef076e1693f08e0ac7a9d4f055fad88cf0ed3f5913b107882c52fd821e58ddd`
and `SEMANTIC_IR_SCHEMA_SHA256 = 411615315546af8707127d506375033c48369188b2b985b8df0fbab13889df35`,
asserted by `tests/test_director.py`. Drifting off them invalidates the evidence above, so a change
there is a re-measurement, not an edit.

## The model-facing surface carries no execution vocabulary

```
MODEL_FACING_INTERNAL_CONTROL_NAMES = 0
```

| semantic axis | directions | execution control |
|---|---|---|
| `cut_pacing` | `sparser` / `denser` | `cut_density` |
| `impact_accents` | `fewer` / `more` | `micro_cuts` |
| `scene_reading` | `visual` / `semantic` | `semantic_emphasis` |
| `section_reactivity` | `steadier` / `responsive` | `energy_response` |
| `motion_preference` | `calmer` / `dynamic` | `motion_bias` |
| `source_variety` | `reuse` / `diverse` | `source_diversity` |

None of the six right-hand names may appear in `system_prompt()` or `model_schema_json()` — enforced
by an import-time assertion in `director.py` **and** by a permanent test, because this is the whole
architectural distinction V2 rests on. Generic `up`/`down` are deliberately absent from the enums
too: the model classifies a *meaning*, not a slider direction.

**`SEMANTIC_AXES` is the one registry.** The schema properties, the prompt text and
`AXIS_TO_CONTROL` all derive from it, and import-time assertions pin that it has the same six
members as `presets.CREATIVE_CONTROL_FIELDS` and twelve distinct direction words.

**V1's control descriptions could not be reused.** They literally name `cut_density` /
`energy_response` / etc., so reusing them would leak exactly the vocabulary V2 removes. The axis
descriptions are written in ordinary editing language instead. That is the one place V2 could not
share text with V1, and it is deliberate.

## Strength is 1..100, and there is no neutral direction

Each reported axis carries `direction` plus `strength` (plain `int`, `1..100`). There is **no `0`**
and **no `neutral`** direction, because both are indistinguishable from omitting the axis — and the
sparse experiment measured exactly that failure mode: given a neutral option the model emitted no-op
entries (`{"source_diversity": 50}`) instead of omitting, and given absolute values it collapsed
every downward request onto 50.

```
omitted axis      -> that control is EXACTLY 50
reported axis     -> 50 ± magnitude_for_strength(strength)
{"intent": {}}    -> all six neutral; a valid answer, not an error
```

`magnitude_for_strength` is `(strength + 1) // 2` — explicit integer half-up, **never** `round()`.
Banker's rounding sends `0.5` to `0`, which would make the smallest possible request a silent no-op,
and sends `24.5` to `24`, breaking monotonicity at every half point. `director_media.half_up` is the
same discipline for the float product in the adapter.

## BASE and FINAL are two different truths

```
BASE  = what the user's semantic intent resolves to
FINAL = what will be proposed, after the optional media step
```

`DirectorProposal` carries **both**, plus the `SemanticIntent`, the model's explanation, the
instruction, an optional `MediaAdjustment` and a truthful `media_note`. `proposal.recipe` is always
FINAL — that is what Apply writes and what the preset label is derived from. BASE is **provenance
only** and is never overwritten in place. Both recipes share the one minted Variation Seed, so the
read-out cannot imply the media step re-rolled the clip selection.

Still frozen and deepcopy-safe: `gr.State` deep-copies its value, so every reachable value is an
`int`, `float`, `str` or a frozen record of those. A test walks the whole object graph.

## The one authorized media adaptation

```
MEDIA_ADAPTER_CONTROLS   = [source_diversity]
MEDIA_ADAPTER_DIRECTION  = BASE > 50 only
SUPPORT_FLOOR            = 0.20
EFFECTIVE_SOURCES_FULL   = 24.0
```

`support = 0.20 + 0.80 * clamp01(effective_sources / 24)`, then
`FINAL = 50 + half_up((BASE - 50) * support)`.

The invariant `50 <= FINAL <= BASE` holds for every reachable input and is asserted, not hoped for:
media may attenuate an unsupported request **toward** neutral, but never reverse intent, never
amplify it, and never invent a direction from a neutral BASE.

**Three of P1's four prototype adapters were measured and DROPPED.** All four were pure, bounded,
deterministic and cheap — necessary but, it turned out, not sufficient. P3 put each through the real
Stage-6 planner on real candidate pools across eight fixed Variation Seeds:

- *Semantic Emphasis* — **execution-inert** where it would matter. The control blends
  `det + factor * (full - det)`, and `full - det` is non-zero only where Stage 5 fused a Qwen
  reading, so at 0 % coverage attenuation changed no render outcome at all (character effect exactly
  `0.00000`). At 50 % coverage neither direction cleared the bar.
- *Energy Response* — the benefit **changed sign with the direction** of the request: `responsive`
  lost legacy score on all three target mixes while `steadier` gained.
- *Motion Bias* — discarded 71–78 % of the requested character for a +0.04–0.13 % score change whose
  sign was inconsistent across seeds, i.e. noise.

Only Source Diversity survived. On a prepared library with ~4 effective sources a strong diversity
request cannot buy a single extra source — the plan already uses all four at neutral — so the extra
reuse pressure is pure score cost. Relaxing it recovered **+1.2353 %** mean legacy score across
**8/8** seeds with the unique-source count identical every time.

**It is a trade-off, not a free win, and the UI must say so.** `adjacent_source_repeats` rose
(19.6 → 28.8 mean), so this is a P3 "meaningful trade-off" and explicitly **not** strict dominance.
Never describe it as better, optimal, free or a Pareto improvement; a test pins the absence of all of
those words from the provenance line, which states the benefit and the cost in one breath.

**Downward requests are never attenuated**, and that is measured too: on the same library,
attenuating reuse-direction requests was consistently *harmful* (4/4 cases, mean −0.4352 %), while
diverse-direction requests were consistently positive (7/7, mean +0.8653 %). The support function
measures *leverage* and is direction-agnostic; the **value** is directional, so the direction gate
lives in the adapter. Do not "simplify" it away.

**Do not implement the three rejected adapters, and do not ship their inputs.** `motion_spread`,
`action_spread`, `soft_spread`, `tension_spread`, the motion percentiles and the two Qwen coverage
fractions are absent from the production summary, and a test asserts they did not arrive as dead
weight.

## Media eligibility: a current, fully prepared scan or nothing

```
PARTIAL_SCAN_ADAPTATION = NO
STALE_SCAN_ADAPTATION   = NO
```

`gui._eligible_media_summary` gates on four things, in cheapening order: a recorded scan exists; the
**live** `LivePrepDeclaration` still `describes` it; `PrepScanResult.is_fully_prepared()`; and the
summary is provable. Anything else returns `None` plus a truthful note.

The live-declaration check is the same cheap one Analyze uses — practical equality on the normalised
folder and the exact recursive flag. It stats nothing, rescans nothing and probes no runtime
identity. It exists because Gradio delivers widget changes as separate queued events, so a user can
retype the folder and press Generate before the `change` handler has run; without it a proposal could
adapt to the *previous* library's concentration while the screen declared another folder.

The full-prepared requirement is not fussiness: P3's value evidence was produced on complete
candidate pools, so a partial scan's aggregate describes a subset and extrapolating from it is
reading a statistic the user never finished producing.

**An ineligible scan is not a failure and must never be called a fallback.** The semantic-IR proposal
is produced normally; only the deterministic step is skipped, and the read-out says which reason
applied (`MEDIA_NOTE_NO_SCAN`, `_STALE_SCAN`, `_NOT_PREPARED`, `_NO_SUMMARY`). It is still Director
V2 intent interpretation — simply `MEDIA_ADJUSTMENT_APPLIED = NO`.

## The model never receives media

```
DIRECTOR_MODEL_SEES_MEDIA = NO
```

The invocation receives the instruction, the system prompt and the schema. It receives no frames, no
filenames, no Stage-5 records, no `beat_info`, no sections, no tempo, no music features, no current
source state and **no media summary** — so there is no media prompt-injection surface at all. A test
inspects the real argv and asserts no summary field, no measured number and no `mmproj`/`--image`
argument appears.

It also does not read the Variation Seed, the six sliders, the preset or any Variant Lab state, so
the instruction is an **absolute** editing intention rather than a transformation of what is on
screen. There is no cache, no proposal history and no conversation state: every press is independent.

## Parse the whole of stdout strictly

Unchanged in philosophy from V1, and extended to the new shape. No regex fishes a `{...}` out of
prose — a broad `\{.*\}` search is how a truncated object, a code fence or a chatty preamble gets
half-accepted, and Stage 5's own truncation defect lived exactly there. The parse is: strip, remove
the one fixed `[end of text]` marker, `json.loads` the entire remainder, require a mapping, require
the exact top-level key set, require `intent` to be a mapping of known axes only, and require every
**present** axis to carry exactly `{direction, strength}` with an axis-specific enum value and a
plain in-range `int`.

**A malformed present axis rejects the whole intent.** It is not dropped and not salvaged: a producer
emitting `{"motion_preference": {"direction": "sideways"}}` disagrees with this contract about what
an intent is, and that disagreement is what is worth failing on. Missing axes are not malformed —
absence *is* the contract's way of saying "no preference".

`_END_OF_GENERATION_MARKER` remains a **constant, not a pattern**. Do not generalise it into a regex
or a list of tolerated suffixes.

## Strict execution, tolerant explanation

Two trust contracts, deliberately opposite:

```
valid intent + missing explanation      -> valid proposal, explanation ""
valid intent + non-string explanation   -> valid proposal, explanation ""
valid intent + overlong explanation     -> valid proposal, explanation bounded (280 chars)
one malformed present axis              -> the WHOLE proposal is rejected
```

The explanation is display-only and never enters `CreativeRecipe`, `CreativeProfile`, `beat_info`,
`render_info`, Stage 4/5/6, cache identity, a Stage-5 Qwen request or the planner. **It must never be
rewritten to pretend the model made the deterministic media change** — the model explains its own
intent reading, and the `MediaAdjustment` explains itself.

`maxLength` is advisory on the installed build (measured: declared limits of 60/160/280 all produced
identical ~460-character strings), so the schema asks and `normalize_explanation` guarantees.

## The model asset

```
DIRECTOR_MODEL          = bin\models\qwen3-4b-instruct-2507-q8_0.gguf   (text-only, no mmproj)
STAGE_5_MODEL           = bin\models\Qwen3VL-2B-Instruct-Q8_0.gguf + mmproj   (UNCHANGED)
LLAMA_CPP_BUILD         = b9842 (unchanged)
DIRECTOR_MODEL_FALLBACK = NONE
```

There are now **two** Qwen assets with two different jobs, and conflating them is the mistake worth
naming. Stage 5 keeps the vision model plus its projector for media semantics; the Director gets a
separate text-only 4B for intent. `DEFAULT_QWEN_GGUF_MODEL`, `DEFAULT_QWEN_MMPROJ_MODEL`, the Stage-5
request format, prompt, schema and cache identity are all untouched — `CACHE_CONTRACT_VERSION` stays
`stage5_cache_v3` and `ANALYSIS_VERSION` stays `auto_av_analysis_v8_llama_vulkan_batched`.

**There is deliberately no fallback to the 2B model.** It was measured against this contract and
failed it (8/14 overall, 0/4 downward requests). A missing Director model produces
`missing_runtime_status`, which names the file and points at the installer; silently substituting a
model that failed would produce confident wrong recipes instead of an honest error.

## One bounded, one-shot subprocess — and the binary is `llama-completion.exe`

Unchanged from V1, including both measured deviations. No `llama-server`, no port, no readiness
polling, no persistent model process, no session. One `subprocess.run` with a timeout,
`CREATE_NO_WINDOW`, stdout and stderr captured separately; `run` rather than `Popen` precisely so a
timeout kills and reaps the child.

1. **`llama-completion.exe`, not `llama-cli.exe`.** On build `b9842` `llama-cli` is the interactive
   chat front end: it rejects `-no-cnv`, ignores `--no-display-prompt`, and prints its banner, its
   command list, the echoed prompt and a timings line **into stdout**. Parsing that would mean the
   very regex the strict parser exists to refuse. The installer now requires
   `llama-completion.exe` by name, because it shipped in the same archive but was not previously
   verified — an otherwise "ready" install could satisfy the old check with no Director runtime.
2. **`-cnv -st`, not `-no-cnv`.** `-cnv` applies the model's own chat template; `-st` runs exactly
   one turn and exits. Raw completion mode skips the template, and on an *Instruct* model that is not
   a small difference — measured, `-no-cnv` collapsed every control to 0 or 1 and rambled past the
   token budget.

The rest: `-ngl 99`, `-c 2048`, `-n 320`, `--no-display-prompt`, `--no-perf`, `-co off`,
`--temp 0.0 --top-k 1`, `-sys`, `-p`, `--json-schema`. Greedy decoding, so the same instruction
proposes the same intent; the Variation Seed is minted fresh every press regardless. Generation
settings stay **hard-coded, not environment variables** — a knob that changes a result belongs under
contract. The system prompt is asserted pure ASCII: it is a process argument to a native binary.

## Propose, then apply

```
DIRECTOR_APPLY_MODEL       = PROPOSE_THEN_APPLY
GENERATING_IS_NOT_APPLYING = YES
DIRECTOR_AUTO_RENDER       = NO
```

- **Generate Proposal** takes `[director_instruction, prep_folder, prep_recursive, prep_state]` and
  writes **zero** execution widgets — only `director_proposal_state`, the read-out and the status.
  The three preparation inputs are *read* by the eligibility gate and reach deterministic local code
  only; Generate writes no preparation or source widget, so intent can never invalidate a scan or a
  confirmed source set. A failure clears the state rather than leaving a stale proposal behind a
  contradicting status line.
- **Apply Proposal** reads only `director_proposal_state` and writes exactly `variation_seed`, the
  six sliders, `creative_preset` and the Director status. It runs **no media logic at all** — a test
  pins that it mentions neither the eligibility helper nor the adapter.
- **Neither handler can reach a render entry point or the source gate**, walked structurally from
  both buttons so a rename cannot evade it.

### Order is the contract, and the seed is minted last

```
1. normalize instruction          7. resolve FINAL deterministically
2. verify runtime + model         8. validate the final controls
3. invoke the 4B model            9. mint the Variation Seed
4. strictly parse SemanticIntent  10. build BASE + FINAL with the SAME seed
5. resolve BASE six controls      11. build DirectorProposal
6. evaluate eligible media        12. return proposal / read-out / status
```

No seed is drawn before every execution-control truth is valid. A test counts the draws: an invalid
response must consume **zero**, so a malformed answer cannot produce a plausible half proposal.

### Apply is deliberately NOT stale-gated

Variant Lab's Apply has a live-declaration gate because a candidate describes a *base* the screen may
have moved away from. A Director proposal is an absolute set of seven values, as valid now as when it
was generated — so `director_proposal_state` survives an apply and may be re-applied after manual
experiments. **This is also true across a media change:** if the prepared library changes after
Generate, Apply remains allowed and the provenance tells the user what happened *at Generate time*.
Do not add media-snapshot staleness to Apply, and do not weaken Variant Lab's gate.

## The display contract

When an adjustment happened the read-out **visibly separates** the two authors, because they are
different and the user is entitled to know which is which:

```
Instruction: Showcase everything I have and keep the edit varied.

Base:  Clip seed … · Cut 50 · Micro 50 · Semantic 50 · Energy 50 · Motion 50 · Diversity 100
Director: <the model's own one-sentence reading>

Media adjustment: Source Diversity 100 -> 67 - this prepared library has only 4.0 effective
source(s), so stronger diversity pressure cannot spread the edit across any more of them.
Relaxing it may allow more adjacent source reuse.

Final: Clip seed … · … · Diversity 67
Proposal only - press Apply Proposal to move the controls. Nothing has been rendered.
```

Both halves are mandatory: the **benefit** (more pressure cannot buy more variety) and the
**trade-off** (relaxing it may allow more adjacent source reuse). With a fully prepared library and
nothing to change, the proposal says so concisely; with no eligible scan it names the reason.

## UI copy must not over-claim

Three claims the copy may never make: that the instruction **reproduces** a result (a prompt is not a
recipe identifier, and the seed is minted fresh every press); that the Director has **watched the
footage** or understands the media library; and that the adjustment is an **improvement**. The
distinction the copy must draw is between the two halves:

```
THE MODEL      reads the user's words only — no footage, frames, filenames, summary or record
LOCAL BEATSYNC may read one small aggregate a CURRENT FULLY PREPARED scan already produced
```

No "perfect", "best edit", "understands your footage", "fully automatic", "better" or "optimal". A
test pins the absence of all of them.

## Isolation

`DIRECTOR_CHANGES_STAGE5_CACHE_IDENTITY = NO`,
`DIRECTOR_CHANGES_PERSISTED_MEDIA_SEMANTICS = NO`. `stage5_qwen_scene_worker.py`,
`stage6_av_planner.py`, `creative.py`, `creative_recipe.py`, `presets.py`, `freestyle.py`,
`variant_lab.py`, `variant_batch.py`, `render_batch.py`, `render_worker.py`, `audio_mix.py`,
`smart_mix.py` and `stage_cache.py` are **untouched**. `video_analysis.py` changed only at the
preparation classifier seam, to count moments from records it had already loaded — no cache write, no
second completion rule, no identity input.

`CreativeRecipe.from_mapping` is reused **unmodified** as the trust boundary for both BASE and FINAL,
and `presets.matching_preset` remains the one preset-label path. The Director never emits a preset
label: which named recipe six numbers happen to match is a GUI read-out, not something a model may
assert.

## No CLI flag

The CLI already exposes all seven resolved visual values, which are the reproducible execution
contract for the edit. A `--director "make it cinematic"` flag would add a non-deterministic,
model-dependent step to a headless entry point whose whole value is that its seven numbers *are* the
contract — and the media half would additionally need a prepared-library scan the CLI does not have.

## Real-runtime acceptance

Out of scope for the portable suite, by the same rule as every other real-model path. The pure half
and the GUI runtime seam are both covered without a model: `tests/test_director.py` imports the pure
module normally, `tests/test_director_media.py` covers the adapter exhaustively, and
`tests/test_gui_guard_seam.py` AST-extracts the real `gui.py` handlers and executes them over a
controlled `subprocess` seam — so every media-eligibility branch, a timeout, a non-zero exit, a
launch `OSError`, a missing asset and every malformed payload are exercised.

Measured against the real 4B asset through the **production** `system_prompt()`,
`model_schema_json()`, `parse_semantic_intent()` and `resolve_semantic_intent()`: 3/3 on the frozen
smoke set (the A1 energy-down sentence → `section_reactivity: steadier/80`; the kinetic sentence →
`motion_preference: dynamic/100`; the cinematic broad instruction → a coherent three-axis intent),
3.54–4.00 s per proposal, with the concentrated-library adjustment firing correctly (Source Diversity
88 → 63). The real-model run belongs in `C:\tmp\BeatSync-Engine-DigitalUnion\tasks\...` against
read-only assets.

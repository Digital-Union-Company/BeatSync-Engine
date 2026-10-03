---
paths:
  - "src/audio_mixdown.py"
  - "src/beatsync_fork/audio_mix.py"
  - "src/beatsync_fork/smart_mix.py"
  - "tests/test_audio_mix.py"
  - "tests/test_audio_mixdown.py"
  - "tests/test_audio_layers_seam.py"
  - "tests/test_smart_mix.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

# Audio Layers (D) and Smart Mix (E): one mixdown graph

Both features are producers into the **same** FFmpeg graph and the same single master WAV, so they
share one rule file. D owns voice placement and the duck model; E adds SFX to D's graph.

## Audio Layers V1 adds voice without touching the edit (D)

`beatsync_fork/audio_mix.py` (pure placement + duck model) and `src/audio_mixdown.py` (ffprobe +
FFmpeg). The ordering is the whole architecture:

```
ORIGINAL MUSIC -> Stages 1-5 -> selected_beats + beat_info          (the video edit)
ORIGINAL MUSIC + voice + beat_info projection -> planner -> mixdown -> mixed master WAV
create_music_video(mixed master, SAME selected_beats, SAME beat_info)   (render audio ONLY)
```

**`analyze_beats_auto` always receives `local_audio_path`**, voice or no voice. The mixed master is
produced *from* the finished analysis and never fed back into it, so tempo, the beat grid, sections,
energy, cut selection, Qwen and the visual targets are all decided before any voice exists. A seam
test asserts this against the real call — it is the most important test in D.

- **No voice clips is the exact legacy path**, structurally: no ordering, no ffprobe, no planner, no
  FFmpeg, no temporary file, and the same `local_audio_path` object reaches the renderer. The only
  changed argument at the render boundary, ever, is `audio_file`.
- **`video_processor.py` and `ffmpeg_processing.py` are untouched** and must stay that way: the
  renderer already accepts an arbitrary audio path, and the CPU/NVENC/HEVC/ProRes branches all
  consume it. Audio Layers terminates *before* the renderer.
- **Deterministic filename order, never the browser's.** Voice clips are sorted with
  `input_manager.order_key` — the project's existing documented total order — because the HTML File
  API returns whatever the OS dialog supplies, which is not the user's click order. The help text
  says so and recommends `01_`, `02_`, `03_`.
- **The preflight validates the WHOLE selection and never returns a subset (R1-A).**
  `prepare_voice_inputs` receives the raw widget value, keeps every entry
  (`_selected_voice_paths` normalises a scalar/`PathLike`/iterable into a list and drops *nothing*),
  and raises `AudioMixError` on the first entry that is not a usable path value, not a supported
  extension, not an existing readable file, not probeable, or has a non-positive duration. Ordering
  is applied **after** validation and its length is re-checked, so nothing can disappear in the sort
  either. `_as_existing_source_paths` must **never** be used here: it filters out paths that no
  longer exist, which is correct for video sources (the confirmation gate vouched for them) and
  wrong for voice — it silently turned a 01/02/03 selection with a missing `02` into a two-clip
  render that looked deliberate. The failure is raised before `analyze_beats_auto`, so a bad voice
  file costs no analysis. A seam test asserts the call site passes `voice_files` and nothing else.
- **The placement report is a real read-out, written by exactly one event (R1-B).**
  `audio_layers_report` was declared with no writer, so the advertised report could never display
  anything. A successful plan now populates it from `audio_plan.report_lines()` — the planner's own
  lines, never recomputed in the GUI — via `session_state[AUDIO_LAYERS_REPORT_KEY]`, which
  `process_video_guarded` projects onto a fourth output. `process_video` keeps its three-value
  `StatusResult` contract; only the outer handler is `GuardedResult`. The key is cleared at the
  start of every attempt and before the gate, so **no voice, a refused render, a preflight failure
  and a mixdown failure all leave it blank rather than showing the previous render's placements.**
  It is pure diagnostics: nothing downstream reads it, and it stays absent from the render inputs,
  `source_outputs`, `prep_outputs`, every source and preparation handler and `live_declaration`.
  The five *configuration* widgets remain unwritable by any handler — that guard was split, not
  weakened, and the report has one permitted writer (`process_btn.click`) asserted by name.
- **Placement is deterministic and seedless.** Cursor starts at the start delay; for each clip in
  user order, take the earliest legal beat, then look at most `PLACEMENT_LOOKAHEAD_SECONDS = 4.0`
  past it for a preferred section start (`intro, verse, breakdown, bridge, outro`), then any other
  non-avoided section start. The bound is measured, not taste: preferring the best section start
  *anywhere* threw the first clip 27 s forward and the second 83 s forward on the real track.
- **Avoid drops is whole-interval**, and this is the rule most worth not weakening. A start-only
  test let a 12 s clip begin 0.44 s before a drop and put **11.56 s — 96 % of its speech — inside
  it**. Avoided types are exactly `drop` and `finale`, read from Stage 3's own vocabulary; `hook`
  and `chorus` are deliberately not avoided. Intervals are half-open `[start, end)` throughout, so
  boundary-touching is legal.
- **Never drop, truncate, overlap or overrun.** No legal placement is an explicit failure naming the
  clip, its duration and the cursor — raised before any video clip is extracted. A silent fallback
  to music-only would look deliberate and be wrong.
- **Music under voice is a linear gain.** 35 % means gain 0.35, *never* −35 dB, and the help text
  must keep saying "percent of the normal music level". Attack/release are fixed at 250/400 ms, so
  the music is already at the floor when the first syllable lands.
- **Overlapping duck windows take the MINIMUM gain**, so a short gap leaves the music continuously
  ducked. There is deliberately **no** hidden `min_gap >= attack + release` constraint. The FFmpeg
  expression is `max()` of duck amounts and must never become a chain of `if()`, where whichever
  event matched first would win.
- **The envelope is `aevalsrc` + `amultiply`, not `volume`.** `volume=eval=frame` measured as a
  staircase — ~5 steps across the 250 ms attack, largest 0.22 linear (~2.2 dB), an audible zipper —
  and `volume` has no per-sample mode (`eval` accepts only `once`/`frame`). Per-sample evaluation
  cut the deviation from the pure reference from **0.2245 to 0.0101**; a 280 s track mixes in 2.3 s.
  Commas inside the expression are escaped for the graph parser.
- **`alimiter` is a safety ceiling, not normalisation**, and `latency=1` is load-bearing. Full-scale
  music plus full-scale voice clips **7.9 % of samples** without it. With
  `limit=0.97:attack=1:release=50:level=0:latency=1` there is no clipping, impulses land on exactly
  the planned sample, and a non-clipping fixture is **byte-identical** to the unlimited mix. Without
  `latency=1` the whole master shifts 47 samples (0.979 ms). Verify any change to this on the
  portable build before freezing it. `amix` uses `normalize=0`: `normalize=1` moved the same mix
  from −21.28 to −27.09 dBFS, i.e. the music level would change with the voice count.
- **One 48 kHz / stereo / `pcm_s24le` master, exactly the music's duration** (`apad` then
  `atrim=end=D`). Measured delta 0.0000 ms across one voice, three voices, a voice ending at the
  music's end and one deliberately overrunning; enforced at ≤ 1 ms because `create_music_video`
  derives the frame-locked timeline from this file's duration.
- **The master lives in `session_dir`, never `get_processing_dir()`** — `create_music_video` clears
  that directory at startup and would delete the file moments after it was written. Fresh uuid path
  per render, discarded in a `finally` on success and failure. No persistent audio cache; voice
  sources are never copied or deleted.
- **Isolation.** Voice never reaches Stage 5 (`stage5_cache_v3` and
  `auto_av_analysis_v8_llama_vulkan_batched` unchanged), `CreativeProfile`, `CreativeRecipe`,
  `VariantLabConfig` or the presets. Variant Lab audio integration **shipped as E2 V1** and reaches
  exactly one Audio Layers control — `music_under_voice` — by writing the widget, which
  `AudioMixConfig` then normalises at the render boundary as it always has. `AudioMixConfig` itself
  was **not modified**. See the Variant Lab audio section below. **No CLI flag.**
- Seconds controls deliberately do **not** reuse `creative.normalize_control` (`2.5` is a valid
  delay) but keep its explicit type boundary; NaN/inf fall back to the default rather than clamping,
  so an infinite delay cannot become an enormous `adelay`.

## Smart Mix V1 adds SFX to the same master (E)

`beatsync_fork/smart_mix.py` (pure: roles, Amount mapping, percentile, five placement rules,
occupancy) and `src/audio_mixdown.py` (library scan, ffprobe, the FFmpeg streams). E is a **second
producer into D's single graph**, not a second pipeline:

```
ORIGINAL MUSIC -> Stages 1-5 -> selected_beats + beat_info          (the video edit)
ORIGINAL MUSIC + voice + SFX + beat_info projection -> planners -> ONE mixdown -> ONE master WAV
create_music_video(master, SAME selected_beats, SAME beat_info)     (render audio ONLY)
```

**`analyze_beats_auto` still receives `local_audio_path`.** SFX are planned *from* the finished
`beat_info` and never fed back, so tempo, the grid, sections, Stage 4 cuts, Stage 5, Qwen and the
visual planner are all decided before any SFX exists. `AudioMixPlan` gained exactly one trailing
defaulted field (`sfx_placements`); there is still **one `amix`, one `alimiter`, one exact-duration
master**, and `video_processor.py` / `ffmpeg_processing.py` are untouched.

- **Five frozen roles**, planned in this priority order: **riser → impact → transition →
  vocal_shot → atmosphere**. Every accepted *non-atmosphere* interval is pairwise disjoint under
  half-open `[start, end)`, so a riser ending exactly where a transition begins is legal. A
  colliding candidate is **skipped with a reason**; anchors are never moved and nothing but an
  atmosphere is ever trimmed. Atmospheres never enter occupancy — a bed underlays everything,
  including voice.
- **Folder = role, by an exact case-folded table** (`Impacts/ Risers/ Atmosphere|Ambience/
  Transitions/ VocalShots/`, plural and `vocal shot`/`vocal_shot` variants — 15 aliases in all). No
  `contains`, no `startswith`, no punctuation rewriting, no classifier. Unknown first-level folders
  and root-level files are **reported and ignored, never guessed**; files nested below a recognised
  role inherit it. Extensions are D's exactly — `.wav .mp3 .flac`, no `.m4a`.
- **"Reported" means reported to the user, and that took a correction (R1).** `prepare_sfx_inputs`
  collected `unknown_folders` / `root_level_files` / `unsupported` / `skipped_disabled` from the
  start, but the scan's return value was read for `library_root` and nothing else, so all four were
  dropped on the floor. A library with `Impats/` beside a valid `Risers/` still preflights — the
  user was simply told there "happened to be no impacts", which defeats the entire point of exact
  classification. The scan now returns the pure immutable `SfxLibraryDiagnostics`, `plan_sfx` carries
  it on `SmartMixPlan`, and **`SmartMixPlan.report_lines()` is the one place it is rendered** —
  `gui.py` threads the value and formats nothing, so there is still a single report formatter (a
  test asserts the four phrases appear in neither `gui.py` nor `audio_mixdown.py`). Lines appear
  only when non-empty, so a clean library gains no `Ignored …: 0` noise; unknown folder *names* are
  shown (the names are the useful diagnostic) ordered **case-folded then by the original name**, never
  by `os.walk`. Disabled-role files get the neutral `Files in disabled roles skipped: N` — disabling
  a role is a choice, not a mistake. **This is reporting, not validation tightening**: all four stay
  non-fatal and ignored, the fatal rules are untouched, and diagnostics reach no placement, no
  `AudioMixPlan`, no creative state and no cache.
- **Seedless.** Pools are ordered with `input_manager.order_key` and consumed round-robin, so the
  same library, track and settings always reproduce. The *planner* contains no `rng_for`, no
  Variation Seed and no Master Creative Seed, and `smart_mix.py` was **not modified** by E2: Variant
  Lab varies `sfx_amount` and `sfx_level` by writing those two widgets, and the planner still
  receives one already-normalised `SmartMixConfig`. Given a config, placement remains fully
  deterministic and seedless.
- **The asset cursor advances on every candidate ATTEMPT, not every success.** Candidate `k` takes
  `pool[k % N]` whether or not it lands. Without that, one asset too long to fit would be retried at
  every later anchor and permanently block the rest of its pool.
- **The four `beat_info` structure fields are numpy arrays and must NEVER be truth-tested.**
  `times`, `rhythm_data["impact_strength"]`, `["is_bar_anchor"]` and `["is_phrase_anchor"]` arrive
  as `ndarray` (566 long on the calibration track), and `bool(ndarray)` raises
  `ValueError: The truth value of an array with more than one element is ambiguous` above length 1.
  `project_structure` originally defaulted all four with `value or ()`, which **crashed every
  active Smart Mix render after Stage 5** — and the entire suite passed anyway, because every
  fixture fed lists and tuples, whose truth value is well defined. Missing-means-empty is now
  expressed by `_missing_as_empty` (`None` → `()`, *anything else returned untouched*), and the
  values are then consumed only by iteration and `len`. **Do not reintroduce `x or ()`, `if x` or
  `bool(x)` on these four**, and do not "fix" a future variant by importing numpy and calling
  `.size` — `beatsync_fork` is stdlib-only, which is exactly what makes the planner testable on a
  bare interpreter. Zero-length and `None` both still reach the existing "no usable beat times" /
  "misaligned" errors unchanged. The regression uses a stdlib `AmbiguousArray` whose `__bool__`
  raises like `ndarray`'s, covers each field **independently** (the original failure hit `times`
  first and would have masked the other three), and is backed by a structural guard that forbids
  these four keys inside any `BoolOp`, `bool(...)` or `not` in `project_structure`.
- **Malformed *scalar* structure input must fail through the structure boundary, not as an
  incidental `TypeError`.** Removing `value or ()` had a second, quieter consequence: that idiom
  was also absorbing a malformed falsey scalar (`0`, `0.0`, `False`) into the empty path, so
  without it a scalar reached `_finite_floats`, which iterates immediately, and escaped
  `project_structure` as a raw `TypeError: 'int' object is not iterable`. `_finite_floats` now
  answers `()` for a non-iterable argument — the same answer it already gave for a non-numeric
  element, `NaN` or `inf` — so `times = 0` reports `no usable beat times` exactly as it did before
  the ndarray fix. The guard is `try: for …`, **not** `if values`, so the container is still never
  truth-tested. It also covers the *pre-existing* truthy-scalar case (`times = 7`), which `or ()`
  never normalised either. `_finite_floats` has exactly two callers, both of them these two fields
  inside `project_structure`, so this cannot reach unrelated planner behaviour. The anchor fields
  were already covered by their own `try/except TypeError` (`rhythm anchors are not iterable`) and
  were deliberately **not** widened.
- **The impact percentile population is the WHOLE aligned beat array**; the bar-anchor mask is
  applied *after* the threshold. That is what the accepted calibration measured — restricting the
  population first silently shifts every threshold. The stdlib implementation reproduces
  `numpy.percentile`'s default `linear` method with a measured maximum difference of **0.0** on the
  real 566-beat array, which is how `beatsync_fork` stays numpy-free.
- **Amount is total over 0..100.** Amount 0 is a hard off-branch (no scan, no probe, no planner, no
  stream). Above it, continuous quantities interpolate piecewise-linearly between measured knots and
  **clamp** below the lowest one rather than extrapolating; integer caps interpolate from a real 0
  knot and quantise **half-up** (`floor(x+0.5)`, never `round()` — banker's rounding would collapse
  two control positions onto one cap). Risers and atmospheres are structural and ignore Amount.
  Pinned rows: 1 → `96/6.0/30.0/0/0`, 25 → `96/6/30/4/2`, 37 → `95.04/5.04/25.2/5/2`,
  50 → `94/4/20/6/3`, 62 → `92.08/3.52/16.16/7/3`, 75 → `90/3/12/8/4`, 91 → `88.72/3/12/8/4`,
  100 → `88/3/12/8/4`.
- **Per role:** impacts on bar anchors above the threshold, spaced, **2 per section** (keyed on
  section *identity*, not type) and `max(1, ceil(duration/20))` globally; risers end exactly on a
  **genuine** drop entry (one whose predecessor is not `drop`/`finale`), never truncated or shifted;
  transitions start exactly on a section-*type* change, no centring, no pre-roll; vocal shots sit on
  phrase anchors inside `chorus`/`verse`/`breakdown` with **no impact threshold** and must fit
  inside their own section; atmospheres fill the longest `intro`/`breakdown` sections ≥ 12 s, cap 2,
  **trimmed to the section** and never looped.
  The vocal-shot rule carries a measured reason: "high impact AND outside a drop" returned **0
  shots** on the calibration track, because on that material the high-impact beats *are* the drops.
- **SFX Level is a linear gain** — 50 % means 0.50, never −50 dB — applied as one `volume=` per SFX
  stream. It is **execution state, not plan data**: `SfxPlacement` is frozen and carries no gain, so
  the resolved level is threaded as a separate `sfx_level_percent` argument to `build_mixed_master`
  / `build_mix_command`. Level 0 is a valid mute and does **not** deactivate Smart Mix.
- **SFX are not ducked and do not duck.** The voice envelope multiplies the *music* only; voice is
  never attenuated by the SFX level. With no voice there are no duck events and **no unity envelope
  is synthesised** — the music routes straight to `[music]`, which an SFX-only mix needs anyway.
- **Every enabled, recognised, supported asset is probed before Stage 1**, not only the ones a plan
  selects: a corrupt file inside an enabled role is fatal before any analysis. Disabled roles,
  unknown folders and root-level files are never probed. Zero usable assets in total is fatal; **one
  empty enabled role while another has assets is not** — that role simply reports zero placements.
  Musical outcomes are never fatal: no drops, no anchors, a collision or an asset that does not fit
  all produce zero/skipped placements with reasons.
- **Measured on the calibration track** (synthetic R0 pools, frozen occupancy): amount 25 → 15 SFX
  (3/4/4/2/2), **50 → 19** (3 risers, 5 impacts, 6 transitions, 3 vocal shots, 2 atmospheres),
  75 → 24, 100 → 25, with **zero non-atmosphere overlaps** at every setting. The default impact
  count is **5, not 6**: the bar anchor at ~87.655 s falls inside the riser into the drop at
  88.143 s and is skipped. That is the accepted consequence of riser priority — do not retune it
  back. `tests/fixtures/nero_structure.json` holds the derived structure (no audio) so the suite
  reproduces this without the production media file.
- **Isolation.** `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3`, `ANALYSIS_VERSION` stays
  `auto_av_analysis_v8_llama_vulkan_batched`; `video_analysis.py`, `stage5_qwen_scene_worker.py`,
  `library_prep.py`, `creative*.py`, `variant_lab.py`, `presets.py` and `variation.py` are
  untouched. The SFX root is a runtime path argument and belongs to neither `CreativeProfile` nor
  `CreativeRecipe`. **No CLI flag.**
- **GUI: five components** in one collapsed `🔊 Smart Mix / SFX` accordion — folder Textbox, one
  `CheckboxGroup` (all five roles on, `(label, value)` choices so the value *is* the internal role
  name), Amount and Level sliders (0..100, step 1, default 50) and a read-only report. No Scan
  button; the library is validated by the render preflight. The report mirrors D's R1-B lifecycle:
  cleared before every attempt and before the gate, blank when inactive or on preflight failure,
  populated only from `SmartMixPlan.report_lines()`, and written by **`process_btn.click` alone**.

## What Variant Lab may touch here (E2 V1)

**Variant Lab writes exactly three audio widgets, and only from its own two buttons:**

```
music_under_voice   <-  generate_variant_btn.click, new_variant_btn.click
sfx_amount          <-  generate_variant_btn.click, new_variant_btn.click
sfx_level           <-  generate_variant_btn.click, new_variant_btn.click
```

**It writes nothing else.** `voice_files`, `voice_start_delay`, `voice_min_gap`,
`voice_avoid_drops`, `sfx_folder` and `sfx_roles` remain **zero-writer** configuration values that
no event in the app may set programmatically, and `audio_layers_report` / `smart_mix_report` keep
`process_btn.click` as their single writer. Those are the user's decisions and the render's
diagnostics respectively; neither is a value to vary.

Why those six are excluded is in `.claude/rules/variant-lab.md` — in short: files and folders are
resource identity, `avoid_drops` is a measured protective rule, roles are structural intent, and the
two seconds controls are deferred placement controls. **Do not add them here without reading that
rule first.**

Load-bearing test detail, because this is where a future change would go wrong quietly:

- **The writer guards were split, never weakened.** `test_no_audio_configuration_widget_is_ever_written`
  and `test_no_smart_mix_config_widget_is_ever_written` now assert **zero** writers for the excluded
  controls, and two new tests assert the permitted three have **exactly** the two Variant Lab writers
  — the writer *list* is pinned, so wiring one of them into any other event fails.
- **Those guards must resolve list indirection.** `variant_lab_outputs` reaches its widgets through a
  named sub-list (`variant_lab_audio_bases`), so a plain `names_in` sees the sub-list name and never
  `music_under_voice`. `expanded_names_in` exists for exactly this reason: without it every
  "is this widget ever written?" assertion in the suite would pass **vacuously** the moment a widget
  moved behind one level of indirection. A test asserts the non-expanded form genuinely does *not*
  see the widget, so the expander cannot be quietly dropped.
- **`audio_mix.py` and `smart_mix.py` were not modified**, and `tests/test_audio_mix.py` /
  `tests/test_smart_mix.py` are **unchanged** — they are independent regression controls for the
  placement algorithms, the duck model, the frozen Nero ladder and the occupancy policy. E2 changes
  which *values* reach those planners, never how they behave given a value.

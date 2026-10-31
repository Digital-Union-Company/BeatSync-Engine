---
paths:
  - "src/music_repeater.py"
  - "src/beatsync_fork/music_repeat.py"
  - "tests/test_music_repeat.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

# Repeat Music 1×–30× (FUN R10)

Play the selected song up to thirty times and render a **full-length music video whose later
repetitions contain newly planned shots**. Concatenating thirty copies of one render is explicitly
not the feature, and nothing here may become that.

```
ORIGINAL MUSIC ──▶ Stages 1-5 (ONCE)  ──▶ selected_beats + beat_info
ORIGINAL MUSIC ──▶ decode + concat N  ──▶ repeated master WAV, measured cycle length
beat_info + the MEASURED cycle         ──▶ extend_beat_info  ──▶ ONE extended timeline
create_music_video(repeated master, extended beats, extended beat_info)
```

Two modules, one direction:

| module | owns | may not |
|---|---|---|
| `beatsync_fork/music_repeat.py` | the control, the repeated geometry, the whole timeline extension, the read-out text | touch the filesystem, FFmpeg, a clock or randomness |
| `music_repeater.py` | the one FFmpeg decode-and-concatenate, the duration verification, the scratch lifetime | decide what a repeat *means* |

`music_repeater` imports the pure one; never the reverse. It is `audio_mixdown`'s structural
sibling — same pure/runtime split, same `session_dir` scratch ownership, same loud-failure
discipline — and deliberately **not** part of it: a repeat failure is about this render's own
generated artifact and the operator's repeat count, not about their voice clips or SFX library, so
`MusicRepeatError` is a plain `Exception` and the three `except audio_mixdown.AudioMixError`
handlers in the render body keep meaning exactly what they meant before.

## 1× is the legacy render structurally, not by value

**Every statement the feature adds to `_process_video_impl` sits behind a repeat guard** —
`music_repeat > 1` for the work, `repeated_master_path` for the cleanup and the panel line. At 1×
neither new module is even *resolved*, no file is built, no array is rebuilt and no added statement
runs. `tests/test_music_repeat.py::test_every_repeat_statement_in_the_render_body_is_behind_a_repeat_guard`
walks the real AST and asserts exactly that.

That is not stylistic. Several suites AST-extract `_process_video_impl`,
`_process_video_guarded_unlocked`, `process_video_guarded` and `render_selected_variants_guarded`
and execute them against synthesised namespaces that contain **neither** module; they pass
precisely because a 1× render never looks them up. The same constraint is why all three handler
defaults are the integer literal `1` rather than `fork_music_repeat.DEFAULT_REPEAT` — measured, not
assumed: the first draft used the constant in `process_video_guarded` and took the progress-seam
suite down at def-time. A test pins the literal equal to the real constant.

**Verified against pristine `main`** (`b90c265f`, a `--no-hardlinks` clone, the same portable
interpreter, a hash-identical FFmpeg, the identical argv): one `framemd5` digest
(`e123ef0a…`, 600 frames), one audio PCM hash (`efec99d1…`, 2 880 000 bytes) and an **empty diff**
over 83 normalised pipeline-fact lines. The *container* hash differs and a same-code control proves
why — Matroska writes a random SegmentUID, so two runs of pristine `main` differ from each other
too. **Do not use a container hash as the instrument here.**

## The control

`normalize_repeat` mirrors `creative.normalize_control`'s explicit type boundary: `bool` rejected
first, whole ints and whole floats clamped into 1…30, plain decimal strings accepted, and a
**fractional** value falling back to 1 rather than being floored — `4.5` is not a request for four
repetitions and truncating would render a length nobody chose. Nothing may raise mid-render.

Normalisation happens **once**, at the render boundary in `_process_video_guarded_unlocked`, beside
the creative profile and both mix configs, so `_process_video_impl` branches on a plain int it can
trust. The `!= 1` pre-check in front of it is the explicit legacy branch this repository prefers
over "the new code with a neutral value"; every other value, valid or malformed, goes through the
one normaliser, so there is no second interpretation of what a repeat count means. A test asserts
`gui.py` contains exactly one `normalize_repeat(` call.

## The audio master: decode once, concatenate, then verify

```
1. ffmpeg -i <music> -vn -map 0:a:0 -c:a pcm_s24le -ar 48000 -ac 2   cycle.wav
2. ffmpeg -f concat -safe 0 -i list.txt -c copy                      repeated.wav
3. probe repeated.wav; require |produced - N × cycle| <= 1 ms, or FAIL THE RENDER
```

- **`-stream_loop` was rejected.** It loops by seeking the *input* back to the start, so for a
  compressed source the decoder's delay/padding handling and the loop's timestamp continuity both
  become part of the result, and neither is something this feature can measure or promise. Decoding
  once to PCM and stream-copying that exact byte run makes every repetition bit-identical to the
  first by construction and leaves nothing between two cycles for a gap to live in.
  **Measured:** the three cycles of a real 3× master are byte-identical PCM (`892a2370…`).
- **`-vn -map 0:a:0` is load-bearing**, not tidiness: an MP3 with embedded cover art presents a
  video stream, and an MKV may carry several audio tracks. The first audio stream is the one
  BeatSync analysed, so it is the one that must be repeated.
- **Step 3 is a contract, not a sanity check.** The frame-locked timeline is derived from the
  rendered audio file's duration while the analysis is stamped at multiples of `cycle_seconds`, so a
  master that is not exactly `N` cycles long would shift every cut after the first repetition.
- **`cycle_seconds` is the MEASURED duration of the produced cycle**, never the analysis window's
  own `len(y) / sr`. The two differ by well under a millisecond, and thirty cycles is where that
  stops being nothing.
- **The master lives in `session_dir`**, never `get_processing_dir()`, which `create_music_video`
  clears at the start of every render — the same rule and the same reason as
  `audio_mixdown.master_path_for`. It is invocation-owned scratch with a fresh uuid path, discarded
  on success, failure and cancellation alike; the two intermediates are removed by the function's
  own `finally`. The operator's music file is opened read-only and nothing is ever written into
  their Media Library.
- **There is no fallback to one repetition.** Finishing a three-minute video for an operator who
  asked for fifteen minutes is a plausible-looking render that silently ignored the request, which
  is exactly what Audio Layers refuses to do for voice. A failure is `UNKNOWN_FATAL` — fail-closed,
  because this repository has no structured FFmpeg diagnostic classification able to tell a broken
  binary from a full disk.

## The timeline extension

Applied **after** `analyze_beats_auto` returns. Three consequences, and all three are the point:

- Stages 1–5 run **once** at every repeat count. Measured peak working set: **482.3 MB at 30×
  against 475.3 MB at 1×** on the same fixture.
- The repeat count **cannot reach Stage-5 cache identity, a Qwen request, the prompt or a persisted
  record** — not by policy but because it does not exist as a value in the render body until after
  Stage 5 has returned. `CACHE_CONTRACT_VERSION` and `ANALYSIS_VERSION` are untouched and no cache
  payload gained a field. A test asserts both names are absent from `video_analysis.py`, the Qwen
  worker, every `auto_mode` stage, `stage_cache.py`, `analysis_cache.py` and `library_prep.py`.
- `audio_visual_profile` is already finished and is **deliberately left describing the track**.

`extend_beat_info` returns **new** containers and mutates nothing it was handed, so the analysis
result and the process-local Stage-3 cache entry behind it stay exactly as they were. What it
replaces, and nothing else:

```
times            every beat of every cycle, at  times[k] + c * cycle_seconds
selected_times   the same MUSICAL cut positions, once per cycle
sections         re-stamped, re-indexed, each cycle closed onto [c*D, (c+1)*D)
rhythm_patterns  rebuilt from the extended sections
energy_profile   the four BEAT-SYNCHRONOUS series tiled
rhythm_data      all thirteen series tiled
selection_info   tiled and re-pointed at the extended sections
audio_duration   repeat * cycle_seconds
music_repeat     the diagnostics record -- reporting only, read by `summary_line` alone
```

Three details that are easy to get wrong:

- **Only arrays whose length equals the original beat count are tiled.** `energy_profile`
  legitimately carries three *frame-rate* curves (`rms`, `spectral_centroid`, `zcr`) that are
  already not beat-aligned and that the planner's own `_interp_feature` check already ignores.
  Tiling those would invent an alignment nobody has; dropping them would change what a 1× render
  carries. A mis-sized series is returned **untouched**, never forced.
- **Each cycle is closed onto the grid.** Stage 3's last section ends at the analysis window's own
  duration, a hair short of the rendered cycle, and `stage6_av_planner._section_at` does a
  half-open scan that falls back to the **last** section when nothing matches — so without closure
  every cycle boundary would leave a sub-millisecond hole in which every lookup resolved to the
  final cycle's final section. Only the first start and the last end of each cycle are moved, and
  only when that keeps the section non-empty; interior boundaries are never touched.
- **Section indices are unique across the whole extended list**, because `rhythm_patterns` is keyed
  by index and `selection_info` points at section objects.

**Musical cut positions repeat; the edit does not.** Stage 6 then plans one continuous timeline over
the whole duration, carrying its running state — the recent-candidate and recent-source windows, the
usage counter and the seeded selection stream — across cycle boundaries. Measured on the real
planner over a 3× timeline: cycle 1 differs from cycle 0 in **10 of 11** cuts and cycle 2 in
**11 of 11**, with 21 distinct candidate moments over 34 cuts, and the same inputs reproduce the
same plan exactly. With a one-candidate library the cycles *are* identical — a test pins that too,
so the variety is demonstrably a property of the library rather than of randomness.

## What the rest of the app does with it

- **`video_processor.py` is untouched by this feature.** `create_music_video` renders whatever audio
  file and whatever beat array it is handed, which is exactly why R10 needed no second render
  engine; a test asserts the three render functions mention none of its names. The frame-lock
  invariant is unchanged and the repeat is simply part of the one timeline it quantises.
- **Voice and SFX are placed against the EXTENDED timeline.** The mixdown's `music_path` is
  `render_audio_path` (`repeated_master_path or local_audio_path` — at 1× the original path
  *object*), and `music_duration`, `beat_times` and `sections` all come from the extended bus.
  Measured at 5×: five 6 s clips placed at 1.3 / 10.7 / 20.0 / 30.7 / 40.0 s, three of them past the
  first repetition, none skipped and none trimmed.
- **Progress uses the existing model.** Two Stage-6 `STATE` events under `phase="music_repeat"`
  report the preparation; Stage 6 then counts against the whole timeline. No new stage, no new
  counter, no ETA.
- **Cancellation uses the existing `RenderLifecycle`.** `build_repeated_music` has its own small
  cancellable runner (a process handle belongs to the frame that creates it — the same reason
  `audio_mixdown` does not share `ffmpeg_processing`'s), and `RenderCancelled` propagates
  **unchanged**: re-typing it would report a user's Stop as a repeat failure, and in a batch would
  describe a cancelled candidate as a problem with their repeat count. A cancelled preparation
  leaves no truncated WAV — measured at 0.05 / 0.10 / 0.18 s into a 30× concatenate, scratch empty
  every time.
- **It is a render request, not source identity and not a creative value.** The slider is wired into
  no source or preparation handler, so changing it cannot clear a confirmation (measured in the
  browser: the snapshot id survives 1 → 5 → 12 → 1 → 3). It has **zero writers** — Variant Lab must
  never vary it, because the length of the video is the operator's decision, not a value to
  randomise — and the names `repeat_music` / `music_repeat` appear in none of `creative.py`,
  `creative_recipe.py`, `variant_lab.py`, `variant_batch.py`, `presets.py`, `director.py`,
  `freestyle.py` or `render_batch.py`.
- **The Variant Lab batch freezes it once**, from its own submitted arguments, before the candidate
  loop — exactly as it freezes the audio file, the source set, the encoder and `voice_overflow`.
  Comparing candidates is only meaningful when the thing that differs between them is their creative
  settings. Measured: both candidates of a 2× batch rendered 1200 frames / 40.000000 s with
  different visual content.
- **`--repeat-music` is the headless parity**, with the six creative flags' discipline: optional,
  `default=None`, **no argparse `type=`**. It **refuses** to combine with `-s`/`-e`, because
  trimming the analysis window and repeating it have two incompatible readings (and
  `create_music_video` would re-apply the same window to the repeated master and clamp it back to
  one excerpt). The GUI has no trim controls, so the feature is unrestricted there.

## Two pre-existing defects this feature surfaced, and did not itself fix

> **Both have since been corrected, in the extractor / frame-lock contract rather than here.** The
> flush-at-EOF frame loss below was fixed by the FUN R10 Final Frame Fix, and the owner's real 3×
> render then exposed a second, unrelated one: the timeline's end is `round(audio_duration × fps)`,
> which for music that is not a whole number of frames plans up to half a frame past the audio, and
> `-shortest` discards the overhang — one frame on a stream without B-frames, **two** on an NVENC
> stream. Neither is caused by Repeat Music and neither was fixed inside it: both live in
> `.claude/rules/pipeline-core.md`. The repeat count is still not a parameter of either correction,
> and `video_processor.py`'s only change is the end-boundary clamp in
> `build_frame_aligned_cut_timeline`, which a 1× render takes identically.
>
> One consequence for this rule's own measurements: `cycle_seconds` is a *measured* duration and is
> almost never a whole number of output frames, so a repeated master's total is almost always in the
> clamped case. A repeated render's published frame count is therefore
> `floor(repeat × cycle_seconds × fps)` rather than `round(...)` whenever the two differ — at most one
> frame, and the same rule a 1× render of the same music follows.

A clip positioned **flush against the end of a source** loses its last frame:
`start=28.84s, dur=1.16s` on a 30.00 s source yields 28 frames instead of 29 at 25 fps, and 34
instead of 35 at 30 fps — **identically on pristine `main`'s own extractor**, so R10 does not cause
it. A longer timeline draws more clips from the same sources and therefore meets the case more
often (a 3× 720p25 render measured 1499 frames instead of 1500). The cause is
`extract_clip_segment_ffmpeg_detailed` plus a planner `start_time` clamped to
`video_duration - source_duration`, with `create_music_video` not verifying the frames a clip
actually produced. It belongs to the extractor / frame-lock contract in
`.claude/rules/pipeline-core.md`, and fixing it inside a creative feature would be exactly the kind
of unscoped change this repository's rule layout exists to prevent.

## Not implemented, and deliberately so

**Auto Repeat to Fit** — choosing the smallest repeat count up to 30 that makes a voice selection
placeable — is the next small integration and is **not** part of R10. It must be decided by actual
placement feasibility, never by summing durations, which is the correction `.claude/rules/audio-mixdown.md`
records for R7.3. Until that resolver exists, R7.3's fourth overflow mode stays unoffered rather
than offered and refused.

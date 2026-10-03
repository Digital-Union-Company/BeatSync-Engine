---
paths:
  - "src/video_processor.py"
  - "src/ffmpeg_processing.py"
  - "src/auto_mode/__init__.py"
  - "src/auto_mode/stage1_audio.py"
  - "src/auto_mode/stage2_features.py"
  - "src/auto_mode/stage3_sections.py"
  - "src/auto_mode/stage4_select.py"
  - "src/auto_mode/stage6_av_planner.py"
  - "src/logger.py"
  - "src/paths.py"
  - "src/gpu_cpu_utils.py"
  - "src/beatsync_fork/ffmpeg_diagnostics.py"
  - "tests/test_ffmpeg_diagnostics.py"
  - "tests/test_stage6_diagnostic_seam.py"
  - "tests/test_stage6_score_precompute.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

# Pipeline and rendering core

## Headless CLI

`src/video_processor.py` is the headless entry point and the fastest way to exercise the whole
pipeline — it runs the same `analyze_beats_auto` → `create_music_video` path as the UI, but it prints
everything instead of routing through the UI's quiet console.

```bat
set PY=bin\python-3.13.14-embed-amd64\python.exe

%PY% -X utf8 src\logger.py                          :: environment report: Python/CUDA/GPU/FFmpeg/NVENC/librosa
%PY% -X utf8 src\video_processor.py <audio> <video_dir> -o out.mkv --gpu --gpu-encoder h264_nvenc
%PY% -X utf8 src\video_processor.py <audio> <video_dir> -o out.mov --lossless --fps 30 -s 10 -e 45
%PY% -X utf8 src\video_processor.py <audio> <video_dir> -o out.mkv --seed 381944   :: creative variation
%PY% -X utf8 src\video_processor.py <audio> <video_dir> -o out.mkv --cut-density 70 --energy-response 80 --motion-bias 30
%PY% -X utf8 src\video_processor.py <audio> <video_dir> -o out.mkv --source-diversity 80 --micro-cuts 70
%PY% -X utf8 src\video_processor.py <audio> <video_dir> -o out.mkv --semantic-emphasis 80
```

The six 0–100 control flags are optional, `default=None`, and carry **no argparse `type=`** — the
values go through `creative.normalize_control`, so a malformed or out-of-range value clamps or falls
back exactly as in the UI instead of aborting the run inside argparse. Omitting them is today's
behaviour. There is no preset flag and no Variant Lab flag: the seven explicit values are the
reproducible execution contract.

## Pipeline

`gui.py` (or `video_processor.main`) → `auto_mode.analyze_beats_auto()` → `video_processor.create_music_video()`.

Stages 1–5 live in `src/auto_mode/`; stage 6 spans the planner and the renderer:

| Stage | File | Produces |
|---|---|---|
| 1 | `stage1_audio.py` | beat grid + tempo from the percussive HPSS component |
| 2 | `stage2_features.py` | beat-synchronous curves: wave/energy, kick/clap/bass/hihat, novelty, impact, bar & phrase anchors |
| 3 | `stage3_sections.py` | broad musical sections (intro/verse/chorus/drop/build/outro…) |
| 4 | `stage4_select.py` | the deliberate subset of beats that become cuts |
| 5 | `video_analysis.py` + `stage5_qwen_scene_worker.py` | the visual library: scored candidate moments per source video |
| 6 | `stage6_av_planner.py` + `video_processor.py` | candidate→segment assignment, then FFmpeg extract/concat/mux |

`analyze_beats_auto` returns `(selected_beats, beat_info)`. **`beat_info` is the pipeline's shared bus** —
it carries `times`, `sections`, `energy_profile`, `rhythm_data`, `audio_visual_profile`, `video_analysis`,
and is mutated downstream (`render_info`, `clip_plan_summary`) for the UI summary. Adding a signal usually
means adding a key here, not a new parameter.

## Import order is load-bearing

`logger.py` owns `setup_environment()`, which mutates `CUDA_PATH`/`PATH`/`PYTHONHOME` and reconfigures
stdout/stderr to UTF-8. Every entry module calls it **before** importing cupy/gradio/cv2. Preserve the
`from logger import setup_environment; setup_environment()` prologue and the position of later imports
when editing `gui.py`, `video_processor.py`, `ffmpeg_processing.py`, `video_analysis.py`, or
`auto_mode/__init__.py`.

`logger.py` also inserts `src/` into `sys.path`, so modules import each other flat (`from paths import …`,
not `from src.paths import …`). Importing `paths` creates `input/*` and `output/` as a side effect.

CUDA backend selection happens in `logger.py` by *detecting an installed `cuda-toolkit` wheel*:
`USING_CUPY_CTK` wins unless `BEATSYNC_FORCE_PORTABLE_CUDA=1`; `run.bat` mirrors the same decision in batch.
Legacy portable `bin/CUDA/v13.3` support is still present but the installer removes it.

`auto_mode/__init__.py` is both the package entry point and the home of `AutoWaveConfig` (all creative
tuning constants) plus shared numeric helpers. Stages import upward from it (`from . import _normalize`),
so the stage imports at the bottom of `__init__.py` must stay *after* the helper definitions.

## The processing directory is process-global, so renders are mutually exclusive

`create_music_video` **clears `get_processing_dir()` at the start of every render** — and
`paths.PROCESSING_DIR` is one module-level constant, not a per-session path. Two renders running
at once would therefore delete each other's in-flight clips.

Until C3-R0 this was latent: `process_btn.click` was the only render event. C3-R0 added a second
one, so the invariant is now enforced rather than merely true by accident:

```
_RENDER_LOCK            a process-global, non-reentrant threading.Lock in gui.py
                        acquired non-blockingly by BOTH render wrappers; the authority
concurrency_id          one shared Gradio group on both events; cooperative only
```

**And the bar is worker termination, not generator exhaustion.** No new render may begin until
the previous `create_music_video` worker has actually *terminated* — a returned generator proves
nothing, because `process_video` runs its render on a daemon thread. `process_video` therefore
joins that thread in a `finally`, and each wrapper closes the nested stream it owns before
releasing the mutex. Never weaken that join to a timeout, and never release the lock on a path
that has not closed its nested stream.

Any future render entry point **must** join both. Do not relax the lock to an `RLock`: a wrapper
re-entering it is exactly the bug the shared gate core exists to prevent, and a reentrant lock
would hide it. Do not clear the processing directory from anywhere else, and do not make it
per-session to "allow" parallel renders — the NVENC clip-extraction cap
(`_effective_clip_workers`) exists because one hardware encoder is already saturated by a single
render, so parallelism would buy nothing it did not also cost.

**C3-R0 changed no pipeline file.** It renders two candidates by calling the existing path twice,
sequentially; `video_processor.py`, `ffmpeg_processing.py`, `video_analysis.py` and
`src/auto_mode/*` are untouched, and no stage cache was invented. Stages 1–3 and a warm Stage-5
cache scan are legitimately repeated per candidate — Stage 4 and Stage 6 must re-run anyway
because Cut Density and Micro Cuts vary — and hoisting them would mean splitting
`analyze_beats_auto`, which is not worth a constant saving against render minutes.

## Durable output promotion depends on Windows `os.rename` semantics (H1)

A GUI render writes into `session_dir` and is only durable once it reaches `output/`. That final
step is `gui._promote_output_no_replace()`, and it is **one** `os.rename` — deliberately the whole
mechanism, not a check wrapped around a move:

```
os.rename, destination free       -> promotes
os.rename, destination exists     -> FileExistsError (WinError 183), BOTH files intact   [measured]
os.rename, different volume       -> OSError errno 18 / WinError 17, nothing written     [measured]
shutil.move, destination exists   -> REPLACES it silently                                [measured]
```

**This is a Windows guarantee and nothing else.** POSIX `rename(2)` replaces the destination
silently, so the same code on Linux would have the defect it was written to fix. That is acceptable
here only because the app is Windows-only by construction (`.claude/rules/platform-and-packaging.md`
— `.exe` paths, `CREATE_NO_WINDOW`, `chcp 65001`, the PowerShell installer, the patched embedded
`._pth`). Record the dependency rather than pretending it is portable, and if this project ever
grows a non-Windows target, this promotion is one of the first things that must be revisited.

The portable test suite therefore **stubs `os.rename`** and asserts how the helper *handles* each
outcome; one Windows-only case asserts the real primitive and skips cleanly elsewhere, so
CLAUDE.md's bare-CPython contract survives.

Do not "improve" this into `os.replace`, `shutil.move`, a delete-then-rename, or an EXDEV copy
fallback. The first three replace silently, and a copy is not atomic — an interrupted one leaves a
partial video at the final path, which reads as a finished render. Cross-volume **fails closed**:
destination untouched, temp retained and named in the message, `LAST_OUTPUT_PATH_KEY` empty.

## The frame-lock invariant

The whole sync story rests on `video_processor.build_frame_aligned_cut_timeline()`: **absolute** cut
positions are quantized to output frames once (`np.rint(cut_times * fps)`), and segment lengths are
frame *differences*. Never reintroduce per-segment `round(duration * fps)` — that is exactly the
cumulative drift this replaced. Downstream, `ffmpeg_processing` extracts with `-vframes` + `-fps_mode cfr`,
never with floating-point `-t` alone.

Corollary, in `create_music_video`: if any clip fails to extract, it **raises** rather than concatenating
a short timeline. Dropping one segment silently desynchronizes every later cut. Don't "fix" this by skipping.

## Rendering modes

`prores_proxy` takes a separate branch in `create_music_video`: sources are transcoded to intra-frame
ProRes 422 Proxy first, segments are cut serially, then concatenated with stream copy. The NVENC/CPU
branch extracts clips in a `ThreadPoolExecutor` — capped by `_effective_clip_workers()` because
simultaneous NVENC sessions contend for one hardware encoder. Final assembly tries concat stream-copy
first and falls back to a full re-encode (`BEATSYNC_FAST_CONCAT_COPY`). Audio is always re-laid as
`pcm_s24le` @ 48 kHz with `-shortest`, audio as master timeline.

**NVENC clips decode in software on purpose (Phase 3C).** `extract_clip_segment_ffmpeg_detailed` adds
**no** input `-hwaccel` when `use_nvenc=True`; the CPU-encode branch keeps `-hwaccel auto`. So the NVENC
path is software decode → the CPU filter chain (`trim,setpts,scale,fps`) → `h264_nvenc` encode. This is
measured, not stylistic: `-hwaccel cuda` failed to initialise on 48 of 60 sampled clips (33 nvdec decode
surfaces against a limit of 32) and FFmpeg fell back to software decode anyway; on the one source where
real NVDEC did engage it was ~20 % *slower*, because a CPU filter graph has to pull the frames back to
system memory. `-hwaccel cuda -threads 8` does produce positively confirmed hardware decode on every
sampled source — and was still slower than plain software decode on 20 of 20 clips, so it was rejected
on evidence rather than on feasibility. Removing the request left output **byte-identical** on 20/20
clips and gained ~10 % wall-clock at the 4-worker cap. Do not reintroduce `-hwaccel cuda`,
`-hwaccel auto`, `-threads`, `-extra_hw_frames` or `-hwaccel_output_format cuda` on the NVENC path
without new measurements; making real NVDEC pay off needs a GPU filter graph (`scale_cuda`/`hwdownload`),
which is a separate architecture task, not a flag tweak.

## A failed clip carries the reason FFmpeg gave (Phase 3B)

`extract_clip_segment_ffmpeg()` still returns a plain `bool` — that signature is a compatibility surface
and must stay. The implementation moved to `extract_clip_segment_ffmpeg_detailed()`, which returns
`(success, reason)`; the boolean function is a thin delegate. `create_clip_parallel` calls the detailed
variant and reports `f"FFmpeg extraction failed: {reason}"`, so the existing Stage 6 chain
(`clip_failures` → warning event → `first_failures` on the refusal → `ProgressView`) carries a real cause.
No new transport, no stdout scraping, and `gui.py` needed no change.

`beatsync_fork/ffmpeg_diagnostics.py` owns the summarising and is stdlib-only, so the ranking is testable
without FFmpeg or a GPU. Load-bearing details:

- **Only consulted when `returncode != 0`.** `rc=0` plus a non-empty output is success, full stop — a
  test pins the call inside the `returncode != 0` branch. The rule was written because, while the NVENC
  path still requested `-hwaccel cuda`, successful clips on driver 617.14 kept printing a scary nvdec
  fallback warning (`cuvidCreateDecoder … CUDA_ERROR_INVALID_VALUE`, `more than 32 (33) decode
  surfaces`), so summarising unconditionally would have attached a "reason" to most of a perfectly good
  render. Phase 3C later quantified what Phase 3B had only observed: CUDA decode initialisation failed
  on **48 of 60** measured old-path clips, which then fell back to software decode, while one sampled
  source family engaged real NVDEC and emitted no such warning. It was never *every* successful clip —
  it was most of the sampled ones. Phase 3C also removed the request, so current renders no longer emit
  it at all. The rule stands on its own either way: a warning a tool recovered from is not a failure,
  whatever produced it.
- **The selector anchors on FFmpeg's *consequence* lines, not on line order.** FFmpeg prints the root
  cause immediately before the wrapper it triggers (`Error while opening encoder` → `Task finished with
  error code` → `Conversion failed!`), so the specific lines nearest that boundary win. "Earliest
  diagnostic line wins" was the first implementation and it was wrong: in the real capture the recovered
  nvdec warning sits *four lines before* the fatal `Driver does not support the required nvenc API
  version. Required: 13.1 Found: 13.0`, and got reported instead of it.
- **Generic, not vendor-special-cased.** The ranking data carries no NVIDIA/NVENC/CUDA-specific tokens
  and no incident-specific version literals (`13.1`, `610.00`); a test asserts that vendor-token set
  against the marker tuples directly. Generic diagnostic words *are* allowed and `_SPECIFIC_MARKERS`
  does contain `"driver"` — that matches any vendor's driver complaint and is deliberately not treated
  as a vendor special case. The real pre-driver stderr is committed as the regression fixture — and it
  is **retained deliberately** even though Phase 3C removed the production trigger for its nvdec warning
  lines. The fixture's job is to prove the selector still tells a recovered warning apart from the fatal
  encoder cause sitting four lines after it; that property is independent of whether current renders
  happen to produce those lines. Do not "modernise" it by stripping the CUDA/NVDEC text.
- **Bounded at 240 chars**, one line, control characters stripped and heap addresses collapsed
  (`[h264_nvenc @ 000001c3…]` → `[h264_nvenc]`) so the same failure produces the same string twice. A
  1 MB stderr still yields a ~60-char reason. The unabridged stderr still goes to the console exactly
  as before.
- Missing/empty output and exceptions get their own honest wording rather than a fabricated stderr
  quote; a `TimeoutExpired` is described by its timeout, not by its 4000-character argv.

## Environment variables

Tuning knobs, all optional. Most useful when debugging performance or an unstable GPU path:

| Variable | Effect |
|---|---|
| `BEATSYNC_DISABLE_QWEN=1` | skip semantic tagging entirely (deterministic analysis only) |
| `BEATSYNC_FORCE_PORTABLE_CUDA=1` | ignore the CuPy CTK wheel, use `bin/CUDA/v13.3` |
| `BEATSYNC_QWEN_LLAMA_DIR` / `_MODEL` / `_MMPROJ` | point at a different llama.cpp build or GGUF pair |
| `BEATSYNC_QWEN_LLAMA_DISABLE_SERVER=1` | force the slow `llama-mtmd-cli` path |
| `BEATSYNC_QWEN_LLAMA_DEVICE`, `_SLOTS`, `_CTX`, `_CTX_FALLBACK` | Vulkan device pick, parallel slots, context sizes |
| `BEATSYNC_QWEN_MAX_WINDOWS`, `_FRAME_WIDTH`, `_MAX_NEW_TOKENS` | how much the VLM sees and generates |
| `BEATSYNC_QWEN_BATCH_VIDEOS=0` | one worker process per video instead of one shared |
| `BEATSYNC_NVENC_CLIP_WORKERS` | override the NVENC clip-extraction worker cap |
| `BEATSYNC_VIDEO_ANALYSIS_WORKERS`, `BEATSYNC_CANDIDATE_METRIC_WORKERS`, `BEATSYNC_OPENCV_FFMPEG_THREADS` | CPU analysis parallelism |
| `BEATSYNC_GPU_SCENE_DETECTION=1`, `BEATSYNC_GPU_CANDIDATE_METRICS` | opt into the CUDA decode / CuPy metric paths (scene detection defaults off) |
| `BEATSYNC_FAST_CONCAT_COPY=0` | always re-encode on final assembly |
| `GRADIO_SERVER_PORT` | pin the UI port instead of searching from 7860 |

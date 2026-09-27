# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **Windows-only, portable** Gradio app that turns one audio track + one or more source videos into a
beat-synchronized music video (AMV/GMV). Python 3.13, no PyTorch/Transformers. GPU work is split three
ways: **CuPy CTK** (CUDA analysis), **llama.cpp Vulkan** (Qwen3-VL semantic tagging), **FFmpeg NVENC**
(encoding). All three are optional — every path degrades to CPU.

This repo contains **source only**. `bin/` (portable Python, FFmpeg, llama.cpp, GGUF models), `input/`
and `output/` are runtime directories created by the installer or at import time; they are not tracked.

## Commands

```bat
install.bat                     :: one-time: downloads portable Python/FFmpeg/llama.cpp/Qwen GGUF via scripts\install.ps1
run.bat                         :: launches the Gradio UI on http://127.0.0.1:7860 (auto-steps to 7861+ if busy)
```

Running modules directly (portable interpreter; `run.bat` sets `PYTHONPATH=src`, `PYTHONUTF8=1`,
`PYTHONDONTWRITEBYTECODE=1` and puts `bin\ffmpeg` on `PATH`):

```bat
set PY=bin\python-3.13.14-embed-amd64\python.exe

%PY% -X utf8 src\logger.py                          :: environment report: Python/CUDA/GPU/FFmpeg/NVENC/librosa
%PY% -X utf8 src\gui.py                             :: UI without run.bat
%PY% -X utf8 src\video_processor.py <audio> <video_dir> -o out.mkv --gpu --gpu-encoder h264_nvenc
%PY% -X utf8 src\video_processor.py <audio> <video_dir> -o out.mov --lossless --fps 30 -s 10 -e 45
%PY% -X utf8 src\auto_mode\stage5_qwen_scene_worker.py --request req.json --response resp.json
```

`src/video_processor.py` is the **headless entry point** and the fastest way to exercise the whole
pipeline — it runs the same `analyze_beats_auto` → `create_music_video` path as the UI, but prints
everything instead of routing through the UI's quiet console.

### Tests

```bat
python -m pip install -r requirements-dev.txt    :: pytest only; NOT installed into bin/ by install.ps1
python -m pytest                                 :: whole suite
python -m pytest tests/test_input_manager_large.py -v
```

The suite covers `src/beatsync_fork/` only, and runs on **any** recent CPython — no portable runtime,
no CUDA, no FFmpeg, no models — because fork modules are stdlib-only by rule (see below).

`tests/test_qwen_stream.py` is the one suite that spawns **real child processes**: it writes a tiny fake
Qwen worker into `tmp_path` and runs it with `sys.executable`. That is deliberate — the defect Phase 2B
fixed is a property of the process boundary, and a mocked `Popen` would "stream" happily under the old
`capture_output=True` code too. It still needs nothing but CPython, and it is a few seconds slower than
the rest of the suite because several cases wait on real child timing.

Two suites verify code that **cannot be imported** on a bare interpreter (`video_analysis.py` needs the
whole runtime; the Qwen worker needs cv2/PIL) by inspecting it with `ast` instead:
`tests/test_qwen_worker_protocol.py` and `tests/test_gui_guard_seam.py`. Prefer AST assertions over grep
there — they match call sites by function and keyword, so e.g. the `llama-mtmd-cli --version` probe is
distinguished from the streaming launches by its argv rather than by a line number.

The upstream pipeline modules have no tests and cannot even be imported without the portable runtime;
verification there is still end-to-end (run the CLI on a short audio file plus one source video and
check the console stage timings and the output file). There is no linter or formatter config. The
installer's own smoke checks (`scripts/install.ps1`, bottom) are the closest thing to a runtime health
check — they import gradio/librosa/cv2/cupy/numba, run a CuPy kernel, assert PyTorch is *absent*,
verify the llama.cpp binaries and GGUF files, and run `pip check`.

## Architecture

### Pipeline

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

### Import order is load-bearing

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

### The frame-lock invariant

The whole sync story rests on `video_processor.build_frame_aligned_cut_timeline()`: **absolute** cut
positions are quantized to output frames once (`np.rint(cut_times * fps)`), and segment lengths are
frame *differences*. Never reintroduce per-segment `round(duration * fps)` — that is exactly the
cumulative drift this replaced. Downstream, `ffmpeg_processing` extracts with `-vframes` + `-fps_mode cfr`,
never with floating-point `-t` alone.

Corollary, in `create_music_video`: if any clip fails to extract, it **raises** rather than concatenating
a short timeline. Dropping one segment silently desynchronizes every later cut. Don't "fix" this by skipping.

### A failed clip carries the reason FFmpeg gave (Phase 3B)

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

### Stage 5 runs out-of-process

`video_analysis.py` never loads a model in-process. It writes a JSON request into
`input/video_analysis_cache/`, spawns `stage5_qwen_scene_worker.py` with `sys.executable`, and reads a JSON
response back. The worker prefers a persistent **`llama-server`** (model loads once, parallel slots) and
falls back to **`llama-mtmd-cli`** per frame. Multiple source videos are batched into one worker process by
default (`BEATSYNC_QWEN_BATCH_VIDEOS`) for the same reason.

The request/response JSON files are **intentionally left behind** for debugging — the `finally:` blocks
that would delete them are deliberately empty. Do not "clean that up".

**The parent streams the worker's stdout** (Phase 2B). `_run_qwen_worker_batch()` and
`_run_qwen_worker()` go through `beatsync_fork.qwen_progress.run_qwen_worker()`, which uses `Popen` and
drains stdout and stderr on **two separate threads** while the main thread owns `wait(timeout=…)`.
Draining both concurrently is mandatory, not tidiness: a failing llama.cpp run emits megabytes of Vulkan
diagnostics, and reading stdout to EOF first deadlocks the moment the stderr pipe buffer fills. stderr is
kept as a bounded tail (2400 chars batch / 1800 single — the same limits the old code printed), so a
loudly-failing worker cannot grow the parent's RAM.

Three boundaries here are load-bearing:

- **Only the two long-running Python-worker launches were converted.** The short
  `llama-mtmd-cli --version` probe in `_llama_version_token()` is still a plain `subprocess.run` — it
  feeds the cache signature and has nothing to stream.
- **The worker's own `subprocess.Popen` is a different thing.** `LlamaServerClient._start` has managed
  the `llama-server` child since long before Phase 2B, handing it dedicated log-file handles
  (`*_llama_server_ctx*_stdout.log`), not the worker's pipes. Never state a repo-wide "no `Popen`"
  invariant — it is false and it describes the wrong boundary. A test asserts the worker still contains
  exactly one `Popen` and that it is still `_start`'s.
- **A reader thread owns closing its own pipe.** Closing a pipe from the waiting thread blocks on the
  buffer's internal lock while its reader sits in `readline()`; with a grandchild holding the write end
  that turned a 2s timeout into a 120s return (measured). `stream_worker_process` therefore joins both
  readers against **one shared deadline** and closes only pipes whose reader has already finished.

Timeout and failure semantics are unchanged from `subprocess.run`: bounded wait, then `terminate()` →
short grace → `kill()`; a non-zero exit prints the bounded stderr tail and returns `{}`; deterministic
visual tags stay active either way. Measured process-tree boundary on a timeout: the direct Python worker
is killed, and an already-started `llama-server` is **orphaned** — identically to the pre-Phase-2B path,
because both kill only the direct child and neither runs the worker's `finally: client.close()`. That gap
is pre-existing; do not "fix" it with `taskkill /T` or a process-group redesign as a side effect of
unrelated work.

**The response JSON remains the only semantic authority.** Nothing is reconstructed from stdout and no
tag is ever parsed out of a progress line, so the same request returns the same semantics whether a
progress callback exists or not. A test compares the streaming result against the old capture path for
identical response JSON.

Qwen is advisory, not authoritative: `_merge_semantic()` keeps deterministic motion/quality metrics
dominant (e.g. action = 0.72·deterministic + 0.28·semantic·motion_gate) so a pretty static frame can't be
hallucinated into an action shot. Keep that weighting shape when adding semantic fields.

### Analysis cache

`input/video_analysis_cache/*.json` is keyed by `ANALYSIS_VERSION` + video path/size/mtime + a backend
signature (model/mmproj/server/mtmd stat + `llama-mtmd-cli --version`). **Bump `ANALYSIS_VERSION` in
`video_analysis.py` whenever candidate scoring, window building, or the candidate schema changes** —
otherwise stale candidates silently survive. Swapping the GGUF model or llama.cpp build invalidates
automatically.

#### Durability invariants (D1)

Before D1 the only save site was a terminal loop at the end of `analyze_video_sources`, so an
interruption *anywhere* earlier discarded every newly analysed source — measured: 3 sources and all
their Qwen tags completed, **0** durable cache entries. The rules that replaced it:

- **Every completion point checks checkpoint eligibility**, rather than Stage 5 saving once at the
  end. `_checkpoint_cache` is called after each serial `_analyze_single_video` (inline AI), after
  each parallel deterministic result, after `_complete_deferred_qwen`, and after **each per-job
  merge** inside `_complete_deferred_qwen_batch`; the terminal loop survives only as a backstop. A
  *call* is not a write — the completion rule decides, so what each shape actually persists is:

  | shape | at that point | durable? |
  |---|---|---|
  | non-AI parallel or serial | complete on arrival | **yes, immediately** |
  | AI-deferred parallel result | `ai_deferred=True` | **no** — only after its Qwen result completes |
  | candidate-less with scoring evidence | no Qwen work exists | **yes** (the explicit exception) |
  | shared Qwen batch, per job | after the worker's final response returns | **yes, per job** |

  So a failing or missing sibling job, and a parent interruption during the post-response merge loop,
  cannot discard jobs already written. **While the shared worker is still in flight its per-job
  results are not durable at all** — they exist only inside that process until its final response is
  written, and the streamed progress channel carries no semantic result authority. Closing that gap
  would need a two-phase deterministic-only record, which D1 deliberately does not introduce.
- **`_cache_entry_is_complete()` is the single completion rule.** Never re-answer "is this reusable?"
  anywhere else — the scattered version is exactly how a failed Qwen run became a permanent
  AI-complete hit. It rejects a non-dict payload, a wrong `analysis_version`, a missing/non-string
  `video_file`, non-list `candidates`, and **anything with `ai_deferred` truthy**; under
  `require_ai` it additionally demands `ai_enabled` unless there are no candidates *and* the
  deterministic scoring pass is shown to have run (see the candidate-less rule below).
- **`_checkpoint_cache()` is the only thing that may start a write.** It consults the rule first, so
  checkpointing early can never publish a deferred or failed-AI record. `analyze_video_sources`
  must not call `_save_cache` directly; a test asserts that.
- **`ai_enabled` is a fact, not a convenience.** Set it `True` only when Qwen genuinely completed —
  and that applies to **every** path: the deferred single, the deferred batch, *and* the serial
  inline one. The facade reports completion through the private `_QWEN_COMPLETED_KEY`, which each
  caller **pops before `timings.update(...)`** so it never reaches a cached payload. Never restate
  the request as the result: `ai_enabled = enable_ai and not defer_ai` was exactly the R2 defect —
  the inline path reported AI-complete for a Qwen run that had failed, timed out or been skipped.
- **Completion comes from the worker's response envelope, never from tag count.** The worker writes
  `timings_by_job[<job id>]` once a job has finished, while every `_run_qwen_worker` failure path
  (launch error, non-zero exit, timeout, unreadable response) returns `{}`. So membership in that
  map is the evidence: single-job runs check `"single"`, the batch path checks each `job_id`. A
  *finished* worker that produced zero usable tags is a **success with nothing to merge** —
  treating it as failure made the next run repeat the whole Qwen pass for nothing. A globally
  non-empty batch response is still not proof that *this* job completed, and one missing job must
  not fail its siblings.
- **A candidate-less source is complete only if the deterministic pass actually ran.** Two very
  different outcomes both end with `candidates == []`: a source whose windows yielded no usable
  moments, and a source OpenCV could not open (`"Warning: OpenCV could not open …; candidate
  analysis skipped."`). `_deterministic_analysis_completed()` tells them apart using existing
  durable evidence — `timings["candidate_scoring_seconds"]`, written once immediately after
  `_measure_windows` inside the `cap.isOpened()` branch and nowhere else. The genuine case is
  reusable (with `ai_enabled` left honestly `False`, not falsified); the open failure is not cached
  at all, so the source is retried. An empty candidate list **alone** is not evidence of success —
  D1 accepted it and would have retired a readable source permanently on one transient decode
  failure. This check applies under both `require_ai` modes.
- **`BEATSYNC_QWEN_MAX_WINDOWS=0` means no Qwen work was completed**, so `ai_enabled` is `False` and
  no AI-keyed checkpoint is written. That is deliberate: `QWEN_MAX_WINDOWS` is *not* part of cache
  identity, so caching a knowingly Qwen-less record under the AI model key would poison it for a
  later run that does want tags. The supported way to cache deterministic-only results is
  `BEATSYNC_DISABLE_QWEN=1`, which `auto_mode/__init__.py` turns into `enable_ai=False` and which
  therefore produces the separate `no_ai` cache identity.
- **The writer publishes through a unique same-directory temp**
  (`tempfile.mkstemp(prefix=<name>., suffix=.tmp, dir=<cache dir>)`), `flush` + `os.fsync`, then
  `os.replace`, with best-effort temp cleanup in `finally`. The old shared `path + ".tmp"` let two
  processes collide: measured, one writer's `os.replace` published the *other* writer's payload
  while reporting success, and the loser failed with `FileNotFoundError` into a warning
  `QuietConsole` discards. Never reintroduce a temp name derived from the final path.
- **`PROCESS_CRASH_ATOMICITY` is provided** (a reader sees the old entry or the new one, never a
  partial final file — verified at all four boundaries). **`POWER_LOSS_DURABILITY` is not claimed**:
  the temp is fsynced, the containing directory is not. Do not upgrade that claim without testing it.
- D1 deliberately **did not** change `_video_signature`, `_path_signature_token`,
  `_qwen_backend_signature_token`, `_cache_path` or `ANALYSIS_VERSION`, so all pre-existing entries
  stay addressable and reusable (verified read-only against 200 real entries). Source/backend
  identity hardening — `int(st_mtime)` collides for any in-place rewrite inside the same second — is
  **deferred to D2** because it re-keys the whole cache.
- **Never run a destructive cache test against the real runtime cache.** Mutation tests belong in
  `C:\tmp\BeatSync-Engine-DigitalUnion\tasks\...`; the runtime cache is read-only for study work.

#### The portable-Python `._pth` provenance hazard

`bin\python-3.13.14-embed-amd64\python._pth` is patched to include the runtime checkout's `src`, and
**that entry wins over `PYTHONPATH`**. A probe that simply does `import video_analysis` under the
portable interpreter can therefore load a *different checkout's* module and, because
`VIDEO_ANALYSIS_CACHE_DIR` is derived from `logger.ROOT_DIR`, point straight at the **real runtime
cache**. This has actually happened during a study. Any script that imports pipeline modules for
inspection must first strip that path entry, then assert `video_analysis.__file__` is the intended
file and that the redirected cache directory is not the runtime one — and refuse to run otherwise.

### Console vs. UI output — structured progress

In GUI runs `gui.process_video` still redirects stdout/stderr into `QuietConsole` (discarded), so **a
plain `print()` inside the pipeline is invisible in the UI.** Emit a structured event instead.

```
pipeline  --ProgressEvent-->  queue.Queue  -->  Gradio generator  -->  status textbox
```

`src/beatsync_fork/progress.py` defines the immutable `ProgressEvent(stage, kind, message, current,
total, elapsed_seconds, rate, data)`; `progress_view.ProgressView` folds events into the panel text.
Both are stdlib-only, so the whole progress path is testable without Gradio.

**Stage identity is `event.stage`, an integer.** The old path recovered it with
`re.search(r"Stage (\d+) is processing", message)` — that regex is gone from `gui.py` and must not come
back; a test asserts its absence.

Rules that are load-bearing:

- **`emit()` never raises.** It swallows callback exceptions and ignores a `None` event, which is what
  makes `emit(cb, counter.advance())` safe — `StageCounter.advance()` returns `None` when throttled.
  A broken status widget must not lose hours of analysis. `KeyboardInterrupt` still propagates.
- **`StageCounter` is monotonic, bounded and throttled** (~2 updates/sec, final update always sent).
  Stage 5 retries a failed video serially and Stage 6 collects clips out of order via `as_completed`;
  neither may make the displayed count go backwards or exceed the total.
- **Monotonicity is per `(stage, phase)`, never per stage.** A stage number is not a counter: Stage 6's
  ProRes path counts *sources* while converting and *clips* while extracting, with different
  denominators, then runs an uncounted assembly phase; Stage 5 counts sources then runs Qwen. The phase
  comes from `data["phase"]` (`prores_convert`, `prores_extract`, `assembly`, `qwen`); events without
  one belong to the stage's main counter. Merging them produced `758 / 100 (758%)` and an extraction
  that looked 62% done before it started. Nothing may carry across a phase boundary — count, total,
  unit, rate or elapsed.
- **An uncounted active phase shows no percentage.** Assembly and Qwen must not inherit the previous
  counter; `ProgressView` shows their state plus a history line (`· 1216 clips completed`) instead.
- **`ProgressEvent.data` is read-only *recursively*.** `frozen=True` alone allowed
  `event.data["x"] = ...`, and a shallow `MappingProxyType` still allowed
  `event.data["section_types"].append(...)` — which matters because Stage 3 and the Stage 6 refusal
  really do emit nested lists. `_freeze`/`_thaw` handle mappings and sequences only; scalars, `str`
  and `bytes` pass through. `as_dict()` thaws recursively, so the payload shares no mutable container
  with the event; it is built field by field because `dataclasses.asdict` deep-copies and cannot
  handle a mappingproxy.
- **A rate must describe what it measures.** `StageCounter` tracks its rate basis separately from the
  completion count: `advance(..., counts_toward_rate=False)` counts a Stage 5 cache hit without
  billing it as throughput, and `begin_rate_window()` re-bases the clock after the cache scan. Without
  that, 420 instant cache hits made the panel claim ~840 sources/s. Pass `rate_unit` when the rate
  measures something narrower than the count (Stage 5 counts `sources`, its rate is
  `analyzed sources`). Stage 6 opens no window, so its clip rate is unchanged. **Never add an ETA.**
- **A stale straggler is ignored for everything, not just the count.** `ProgressView` drops
  out-of-order events for `rate`, `elapsed` and `message` as well, and never overwrites a known
  measurement with `None` — otherwise one late Stage 6 event erased `4.8 clips/s`.
- **Never touch a Gradio component from a worker thread.** `event_callback` only calls `queue.put`;
  the generator does all widget updates. A test asserts the callback's only method call is `put`.
- **Stage 5 seeds the counter with cache hits**, so a fully cached run shows completion instead of
  sitting at 0 while doing nothing.
- **No invented ETAs.** Stages 1-4 are seconds long; Stage 5's per-video cost varies with clip
  duration. Rendering publishes a *measured* rate, which is not a prediction.

The legacy `progress_callback` / `console_callback` remain for CLI and headless callers;
`event_callback` is optional everywhere. `StageConsoleLogger.apply_event()` drives the CMD log from the
same events, so console and GUI cannot disagree.

**Qwen live progress is streamed (Phase 2B).** The worker emits a namespaced one-line JSON protocol on
stdout — `BEATSYNC_QWEN_PROGRESS\t{"v":"beatsync.qwen-progress/1","kind":…}` — **in addition to** its
existing human-readable lines, which are untouched because they are what a CLI user reads.
`beatsync_fork/qwen_progress.py` owns the wire format (`encode`/`decode`), the translator, and the
streaming runner; `stage5_qwen_scene_worker.py` calls a guarded `_emit_progress()` that swallows
everything, so a status line can never cost a Qwen run that has already spent GPU minutes.

Kinds: `worker_state` (`loading_model`, `backend_ready`, `prefetch` — the model load is the longest
silent stretch of a run, ~8s even for the 2B model), `job_start`, `job_progress`, `job_end`. Rules:

- **Do not regex worker prose.** Phase 2A deleted prose parsing from the GUI; re-adding it at the
  subprocess boundary would repeat the mistake one layer down. The machine channel is explicit and
  versioned, and unknown `kind`s, malformed JSON, and non-object payloads are counted and ignored — never
  raised. Ordinary stdout stays ordinary and goes to the console sink.
- **`job_progress` carries the worker's own numbers**: `current` is the inference loop index, `total` is
  `len(frame_items)` — what frame prefetch *actually* decoded — and `candidates_per_second` is the
  worker's measured `idx / elapsed`. Never recompute the rate from source counts, cache counts or total
  Stage 5 elapsed time, and label it **`candidates/s`**, never `sources/s`.
- **No global candidate denominator is invented.** The parent knows requested candidate counts, not the
  live per-job denominator, so the panel shows `Qwen job 17 / 300 · 64 / 120 candidates (53.3%)` and
  never a cross-job `1840 / 3620`. `job_end` accumulates `candidates_done` from worker-reported tag
  counts only.
- **Per-job progress is an uncounted `STATE` event under `phase="qwen"`**, deliberately. A counted phase
  would fight `ProgressView`'s per-`(stage, phase)` monotonicity — job 18 restarting at `1 / 130` after
  job 17 finished at `120 / 120` looks exactly like the stale straggler that rule rejects, freezing the
  panel on the old job — and it would also hide the `· 758 sources completed` history line, which
  `ProgressView` shows precisely when the active phase has no counter. The numbers live in the message
  *and* in `event.data`.
- Both execution modes stream: the shared batch worker and the single/legacy path. A test asserts both
  call sites go through the streaming runner and that `event_callback` reaches every orchestrator seam,
  so live progress cannot silently become batch-only.

### Rendering modes

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

## Platform notes

Windows-only by construction: `.exe` paths, `CREATE_NO_WINDOW`, `chcp 65001`, PowerShell installer,
embedded-Python `._pth` patched to include `..\..\src`. `gui.py` also patches asyncio's Proactor
transport to swallow benign `WinError 10054` pipe resets — that filter is intentional, not dead code.

Runtime folders are created inside the repo root, so `.gitignore` covers `bin/`, `input/`, `output/`
and Python caches. Ignoring is not deleting: `output/` and `input/video_analysis_cache/` are still
retained on disk by the housekeeping policy below.

Licensed AGPL-3.0. This repository is a **modified fork** — see `CHANGELOG-FORK.md` for the
modification record required by AGPL-3.0 §5(a), and the README licence section for the §13
(network-use) consequence of ever exposing the Gradio UI beyond localhost.

## Fork-specific code (`src/beatsync_fork/`)

All Digital Union additions live in this package. Upstream never creates this directory, so features
land here with almost no merge-conflict surface; upstream modules are touched only at small call sites
marked `# [FORK]`. Grep for `[FORK]` to enumerate the entire divergence surface.

**Hard rule:** nothing in `beatsync_fork` may import the upstream runtime (`logger`, `paths`, `gradio`,
`cupy`, `cv2`, `librosa`, `numpy`). `logger` imports librosa at module scope and mutates
`PATH`/`CUDA_PATH`, and importing `paths` creates directories as a side effect — depending on either
would make the fork package untestable on a bare interpreter. `tests/test_no_runtime_dependency.py`
enforces this both dynamically (subprocess module-table check) and statically (AST import inspection).

| Module | Purpose |
|---|---|
| `__init__.py` | fork identity: `FORK_NAME`, `FORK_VERSION`, `UPSTREAM_BASELINE_COMMIT`, `fork_identity()` |
| `input_manager.py` | `scan_folder()` — deterministic local-folder source discovery with exact accounting |
| `input_report.py` | `InputReport` — renders and serialises the counts from an `InputSet` |
| `input_confirmation.py` | `SourceSnapshot` identity + `evaluate_gate()` — what "confirmed" means |
| `input_session.py` | the source-input state machine and `resolve_for_render()` — the actual gate |
| `progress.py` | `ProgressEvent` + `StageCounter` + `emit()` — structured pipeline progress |
| `progress_view.py` | `ProgressView` — folds events into the status panel text |
| `qwen_progress.py` | Qwen worker stdout protocol + translator + the streaming `Popen` runner |
| `ffmpeg_diagnostics.py` | bounded, vendor-neutral summaries of FFmpeg stderr for failed clips |

### Video source modes and the confirmation gate

```
INPUT MANAGER CORE EXISTS
GUI INTEGRATION = IMPLEMENTED (local folder + browser, confirmation gate)
```

`gui.py` has a **Video Source** block with two modes:

- **Local folder** (default, recommended for large libraries) — `scan_folder()` enumerates the folder
  server-side, so `Discovered / Supported / Rejected / Ready / Duplicates / Total size` are exact by
  construction. Files are used **in place**: nothing is copied, and the paths handed to the pipeline
  are the user's original files.
- **Browser files** — upstream's `gr.File` multi-upload, unchanged. Gradio still copies these into
  `input/gradio_uploads/`.

**Browser mode reports only `Backend ready: N`** — the count the server has actually received. The
number of files the user picked in the browser dialog is frontend state that is never transmitted, so
a "selected" or "pending" figure would be fabricated. `tests/test_input_gate.py` asserts no such
number is ever printed. Do not add JS to scrape it; the fix does not depend on knowing it.

**Create Music Video is disabled until the source set is explicitly confirmed**, and confirmation is
over a `SourceSnapshot` — an ordered identity of path + size + mtime_ns per file, plus mode, scan root
and the recursive flag — not a count. Two different lists of the same length are different
confirmations. Identity deliberately does **not** hash file contents; the head+tail fingerprint in
`input_manager` is for duplicate candidacy and must not become a per-render cost.

Any source change clears the confirmation (mode switch, folder path, recursive toggle, re-scan, browser
list change). Non-source settings — FPS, encoder, output filename, audio — must **not**: they are not
wired to these transitions, and a test asserts the confirmation survives them.

**`process_video_guarded()` in `gui.py` is the real gate**, and it validates the **live** source
controls — `source_mode`, `source_folder`, `source_recursive`, `video_input` are render-request inputs,
not just `gr.State`. That is load-bearing, not defensive padding: Gradio delivers widget changes as
separate queued events, so at click time the state can lag behind the widgets (a late upload, a retyped
folder). Trusting the state alone allowed a render for a source set the user was no longer declaring.
**Never reduce this handler's inputs back to `source_state` alone.** Its parameter names mirror the
widget names because Gradio passes them positionally; `tests/test_gui_guard_seam.py` asserts the two
lists line up, so a silent reordering fails the suite.

`check_declaration()` compares declared intent first (mode / folder / recursive), so a folder the user
has navigated away from is never scanned. Then folder mode re-scans with `detect_duplicates=False`
(duplicate grouping is reporting, not identity) and browser mode snapshots the **live** `gr.File` list —
re-stat'ing `confirmed.paths` instead would only re-verify files already approved and would never notice
a late upload. On success the handler hands the existing `process_video()` the same `List[str]` it always
consumed, so Auto Mode and the renderer remain unaware that input modes exist.

**Folder identity covers the supported *scope*, not just the ready subset.** `SourceSnapshot.excluded`
records supported-extension files the scan could not use (`empty_file`, `unreadable`, `not_a_file`) as
`(path, reason)` pairs inside the digest. A live library gains `.mp4` files that are momentarily 0 bytes
— the real `Cuts` folder gains roughly one per minute — and a ready-only identity reported "no change"
while a new source entry had appeared. Record only the stable reason code, never OS error text, or the
digest stops being reproducible. Unsupported files (`.mp3`, `.txt`) are deliberately outside identity so
they cannot cause false invalidation. The render list stays ready-only.

What the core does: enumerates `.mp4`/`.mkv` under a folder (optionally recursive); rejects entries
with a recorded reason (`unsupported_extension`, `empty_file`, `not_a_file`, `unreadable`); collapses a
file reachable by two paths into a single entry with a recorded collision; orders results
case-insensitively with a total-order tie-break so two scans agree exactly; and reports duplicate
*candidates* using a cheap fingerprint (size + SHA-256 of the first and last 1 MiB, whole file when
≤ 2 MiB). Files whose size is unique within the set are never opened at all. It copies nothing,
transcodes nothing, and deletes nothing — duplicates are reported, never removed.

**Traversal is all-or-nothing, and must stay that way:** a complete walk returns an `InputSet`; any
directory that cannot be listed raises `InputScanError`. `os.walk` silently skips `scandir` failures
unless an `onerror` callback is passed, so never drop the one in `_walk_error_raiser()` and never
"soften" it into a partial result — a quietly smaller library that still reports READY is the exact
bug this module exists to prevent. Unreadable *files* are different: they stay `unreadable`
rejections, because a named rejection is not a silent loss.

**No entry may be dropped by a probe in `_iter_candidate_paths()`.** It decides only "directory or
not"; everything else goes to the classifier, which owns extension, `stat`, regular-file, empty-file
and `UNREADABLE` handling. Never reintroduce `os.path.isfile()` / `os.path.isdir()` as the filter:
they *suppress* stat errors and return `False`, so an unreadable entry vanishes from `ready`,
`rejected` **and** `discovered_count` — invisible even to the counting invariant. (Note for tests:
since Python 3.13 on Windows `os.path.isfile` is `nt._path_isfile`, a C builtin that never calls
`os.stat`, so patching `os.stat` alone cannot simulate an unreadable path.)

---

# Repository Workflow Policy: DU-REPO-WORKFLOW-v1

Repository-local rule (authored here, not synced from any machine-local file). It governs what a task
must do to count as finished.

## Mandatory task closeout — commit and push

**For every task that makes authorized repository changes, the default is: complete the work, commit it,
push it, and verify the remote.** Authorized work left only on local disk is not finished. The purpose of
this rule is that every completed implementation task leaves a durable GitHub state that can be
independently reviewed afterwards.

1. **Complete the requested work fully.** Not the easy part of it — all of it.

2. **Before committing, verify all of the following:**
   - the current branch;
   - `git status`;
   - the **complete** diff, not a summary of it;
   - that only files authorized by the task are included;
   - that no unrelated pre-existing user changes are staged or committed;
   - that all tests/checks applicable to the task have been run;
   - that every mandatory acceptance gate the task defines has passed.

3. **Commit** the completed authorized work.

4. **Push** the resulting commit to the appropriate remote branch.

5. **Verify the push succeeded** by comparing the local HEAD against the *actual* remote branch HEAD —
   query the remote, do not trust a possibly stale remote-tracking ref.

6. **End the task report with at least:**
   - branch name
   - commit SHA
   - remote branch SHA
   - changed paths
   - tests/checks run, and their result
   - whether the working tree is clean
   - whether push verification passed

## Exceptions — when NOT to commit or push

**An explicit task instruction always overrides the default above.** Do not commit or push when the
current task says anything equivalent to: READ ONLY · NO EDITS · NO COMMIT · NO PUSH · NO PR ·
local experiment only · investigation/audit only.

**Never commit or push merely to make a failed task appear complete.** STOP and report the exact blocker
instead of forcing a commit or push when:

- mandatory tests fail;
- an acceptance gate fails;
- the authorized scope cannot be satisfied;
- unexpected unrelated working-tree changes create ambiguity about what would be committed;
- the remote branch changed in a way that makes the planned push unsafe.

## Branch / remote safety

- Push the branch the authorized task was done on.
- Do **not** switch to `main` merely to satisfy this rule.
- Do **not** push implementation work directly to `main` when the task requires a feature-branch / PR
  workflow.
- Do **not** merge a PR unless the task explicitly authorizes the merge.
- **Never force-push**, and never rewrite published history, unless the task explicitly authorizes it.
- Before pushing, fetch or otherwise measure the target remote branch well enough to avoid blindly
  overwriting concurrent work.
- For documentation/governance tasks explicitly authorized directly on `main`, a normal fast-forward
  commit + push to `main` is allowed.

## Commit hygiene

- One completed task normally produces **one coherent commit**, unless the task asks for more.
- Commit messages describe the actual change.
- **Never `git add .`** when unrelated files may exist — stage explicit authorized paths.
- Preserve user-created or pre-existing uncommitted work that lies outside task scope.
- Runtime, cache and output files are not committed unless explicitly authorized — see the housekeeping
  policy below, whose *Git discipline* and *Source-control awareness* rules apply in full here.

## Post-push verification

A task is not durably complete until the pushed remote ref is verified. At minimum:

```
LOCAL_HEAD == REMOTE_TASK_BRANCH_HEAD
```

If they differ, report the mismatch and do **not** claim completion. Use the final state

```
TASK_COMPLETE_AND_PUSH_VERIFIED = YES
```

only when the requested work, the checks, the commit, the push, and the remote verification have all
succeeded.

---

# Housekeeping Policy: PCBUS-HK-v1

Status: `LOCAL_POLICY_DURABLE` — this file is committed on the authoritative main line
(`origin/main` of `Digital-Union-Company/BeatSync-Engine`), so the policy travels with the repository
across machines. Keep the `PCBUS-HK-v1` marker; it is a synchronisation marker, not a version number —
do not bump it for wording changes.

## Temporary work goes outside the repository

Standard temp workspace: **`C:\tmp\BeatSync-Engine-DigitalUnion\`**. Per-task scratch goes in
`C:\tmp\BeatSync-Engine-DigitalUnion\tasks\<TASK_NAME>\`; create only the subdirectories a task needs
(`work\`, `downloads\`, `exports\`, `extracted\`, `logs\`, `backups\`, `quarantine\`).

Put there: downloads, intermediate outputs, extracted archives, debug exports, scratch scripts, one-off
logs, temporary patches, throwaway test payloads, comparison copies, temporary screenshots.

**Plan the location before creating the file.** Do not invent other temp roots — not `I:\tmp`, not
`<repo>\tmp`, `<repo>\temp`, `<repo>\old`, `<repo>\backup`, `<repo>\scratch`, not `Desktop\temp`. If a
tool requires a repo-internal temp directory while it runs, remove it after the run succeeds. Create
`C:\tmp\` if missing. Temporary storage is a workspace, never an archive: it must never be the only
place an evidence, acceptance or release artifact exists.

## Repository hygiene

Do not leave disposable material in the repo: `*.tmp`, `*.bak`, `*.old`, `*.orig`, `*.copy`,
hand-numbered variants (`_v2`, `_final2`, `_new`, `_old`), temporary JSON/CSV/XLSX exports, saved API
responses, download artifacts, temporary screenshots, extracted archives, spent test payloads, debug
output, one-off SQL, disposable helper scripts, intermediate build output, superseded local candidates.

Filename variants are not version control — historical versions belong in Git history, not beside the file.

## Every task cleans up after itself

Substantial tasks run **work → verify → establish final state → housekeeping → report**. At closeout,
inspect the material your task created: delete demonstrably disposable artifacts, retain what is still
required, move uncertain material to `C:\tmp\BeatSync-Engine-DigitalUnion\quarantine\<YYYY-MM-DD>\` with
a note on origin and why it is uncertain, and leave no unnecessary repo-local scratch behind. Keep this
quiet and routine; call it out only when cleanup was significant, something was quarantined, a recurring
clutter pattern appeared, cleanup could not be completed safely, or the owner must decide.

## Never clean what you did not create

Clean up **your own** task artifacts, and only those. Do not sweep `C:\tmp\BeatSync-Engine-DigitalUnion\`
merely because your task finished. Never touch files that may belong to another running session, another
developer or process, another active task, or an unknown producer. If files appear or change concurrently
and ownership cannot be established, leave them alone and **do not infer staleness from age**. The same
applies inside the repo.

## Classify before removing — conservative by default

- **DELETE** only when demonstrably temporary, reproducible, superseded and no longer needed: extracted
  archives whose original remains, download copies, intermediate conversions, scratch output, test files
  created solely for the finished task, regenerable caches.
- **MOVE** to `C:\tmp\BeatSync-Engine-DigitalUnion\` when still useful but not repo material: diagnostic
  exports, manual backups, downloaded source material, large comparison files, ad-hoc reports.
- **QUARANTINE** to `C:\tmp\BeatSync-Engine-DigitalUnion\quarantine\<YYYY-MM-DD>\` whenever deletion
  safety is not established. Never guess.
- **KEEP** — source-controlled files, canonical project files, active source, current config, test
  fixtures, documentation, production/deployment definitions, anything referenced by code or docs,
  accepted or frozen candidates, hash-bound artifacts, authoritative release artifacts, audit /
  acceptance / verification evidence, rollback material still in force, and anything whose ownership or
  purpose is unclear.

**Never destroy evidence just because a milestone completed.**

## Project-specific retention (overrides the generic rules above)

- `input/video_analysis_cache/` is a **deliberately preserved** cache — including the
  `qwen_batch_request_*.json` / `qwen_batch_response_*.json` worker files. Do not clean it as scratch.
  `gui.cleanup_on_startup()` already protects it, along with `input/audio/` and `input/video/`.
- `output/` holds the user's rendered videos. Never clear it as part of housekeeping.
- `input/processing/` and `input/gradio_uploads/` are app-managed runtime scratch — the app clears them
  itself; do not race it while a render may be running.

## Git discipline

Policy edits to a tracked `CLAUDE.md` are legitimate commits. Do not commit temp files in order to delete
them later, do not bundle unrelated source changes, and do not rewrite history. A small targeted
`.gitignore` addition is appropriate only for a genuinely recurring clutter pattern — never a large
generic ignore list.

Worktrees, additional clones and orphaned checkouts are **not** clutter by default. Never remove, prune,
reset, stash or relocate one as a housekeeping action, and never disturb an active development worktree.
If a pass uncovers state that exists nowhere else (untracked run output, acceptance evidence), stop:
cleanup of that area is blocked until the material has a second verified copy in durable retention.

## Source-control awareness

Before deleting or moving anything in the repo, establish whether it is tracked, intentionally ignored,
untracked, generated, referenced, part of the current task, or historical evidence. Never remove a tracked
file just because it looks stale. Housekeeping is workspace hygiene: it never changes application behaviour
and grants no new authority. Project security, retention, evidence and deployment rules override it.

---

# Monitoring Policy: PCBUS-WATCHDOG-v1

When waiting on an external async result (CI, deploys, long remote jobs), never rely indefinitely on a
single background monitor. After at most **20 minutes** without a surfaced terminal result: run a fresh,
independent, **read-only** query against the authoritative source, surface an interim status to the user,
and continue with another bounded interval. A fresh authoritative query beats a silent monitor — silence
is not evidence of "still running".

Before arming a monitor: verify every binary in the pipeline exists (`jq` is **not** installed in this
machine's Git Bash — use `gh --jq`), never suppress the monitor's own stderr, emit a heartbeat so total
silence is distinguishable from a healthy wait, and sanity-run the poll command once in the foreground.

Watchdog rechecks are read-only: they never authorize a rerun, retry, `workflow_dispatch`, empty commit or
force push. Do not end a turn with a monitor running and no mechanism to surface its terminal result.

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

### Stage 5 runs out-of-process

`video_analysis.py` never loads a model in-process. It writes a JSON request into
`input/video_analysis_cache/`, spawns `stage5_qwen_scene_worker.py` with `sys.executable`, and reads a JSON
response back. The worker prefers a persistent **`llama-server`** (model loads once, parallel slots) and
falls back to **`llama-mtmd-cli`** per frame. Multiple source videos are batched into one worker process by
default (`BEATSYNC_QWEN_BATCH_VIDEOS`) for the same reason.

The request/response JSON files are **intentionally left behind** for debugging — the `finally:` blocks
that would delete them are deliberately empty. Do not "clean that up".

Qwen is advisory, not authoritative: `_merge_semantic()` keeps deterministic motion/quality metrics
dominant (e.g. action = 0.72·deterministic + 0.28·semantic·motion_gate) so a pretty static frame can't be
hallucinated into an action shot. Keep that weighting shape when adding semantic fields.

### Analysis cache

`input/video_analysis_cache/*.json` is keyed by `ANALYSIS_VERSION` + video path/size/mtime + a backend
signature (model/mmproj/server/mtmd stat + `llama-mtmd-cli --version`). **Bump `ANALYSIS_VERSION` in
`video_analysis.py` whenever candidate scoring, window building, or the candidate schema changes** —
otherwise stale candidates silently survive. Swapping the GGUF model or llama.cpp build invalidates
automatically.

### Console vs. UI output

In GUI runs, `gui.process_video` redirects stdout/stderr into `QuietConsole` (discarded) and the worker
thread streams status through two callbacks:

- `progress_callback(str)` → the Gradio status box, and starts a stage in `StageConsoleLogger`
- `console_callback(stage, msg)` → the CMD window, capped at **5 lines per stage**

So a plain `print()` added inside the pipeline is invisible in the UI. Emit through `_notify_console`
(auto_mode) or the `_stage5_summary`/`_stage6_summary` helpers in `gui.py` instead.

### Rendering modes

`prores_proxy` takes a separate branch in `create_music_video`: sources are transcoded to intra-frame
ProRes 422 Proxy first, segments are cut serially, then concatenated with stream copy. The NVENC/CPU
branch extracts clips in a `ThreadPoolExecutor` — capped by `_effective_clip_workers()` because
simultaneous NVENC sessions contend for one hardware encoder. Final assembly tries concat stream-copy
first and falls back to a full re-encode (`BEATSYNC_FAST_CONCAT_COPY`). Audio is always re-laid as
`pcm_s24le` @ 48 kHz with `-shortest`, audio as master timeline.

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

### Input manager status

```
INPUT MANAGER CORE EXISTS
GUI INTEGRATION = NOT YET IMPLEMENTED
```

`scan_folder()` is currently reachable only from tests and from Python. **`gui.py` is untouched** and
still uses upstream's `gr.File` multi-upload path, so the failure this module was written to prevent —
a partially-uploaded selection rendering silently from a truncated source set (observed: 329 of ~701
files reaching Stage 5) — is **not yet fixed in the app**. Wiring the folder-mode UI, the READY state
and the confirm-before-render gate is the next slice.

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

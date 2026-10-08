# CLAUDE.md

Always-loaded constitution for Claude Code (claude.ai/code) in this repository. It is deliberately
short. **Subsystem detail lives in path-scoped rules under `.claude/rules/`**, declared with YAML
`paths:` frontmatter; durable history and acceptance evidence live in `docs/claude/`. See
`docs/claude/README.md` for the map, and `docs/claude/instruction-architecture.md` for why.

**How a path-scoped rule actually loads.** A `paths:` rule is conditional: it enters context when
Claude works with a matching file through the file operations that trigger rule matching — currently
documented as **Read, Write and Edit**. It is *not* triggered by every way a file can be reached. A
`grep`, a `git show`, a build command or a one-off script that happens to mention a matching path does
**not** activate its rule.

**So: before changing a repository file through Bash, a script, `git apply`, a patch or any other
non-Read/Write/Edit path, first Read the target file with the Read tool, or Read the applicable
`.claude/rules/…` file explicitly, so that file's path-scoped contract is in context before you
mutate it.** The rules in this repository are load-bearing contracts, not style notes — editing
`smart_mix.py` or `video_analysis.py` with an unreviewed shell patch is how a documented invariant
gets silently broken.

## What this is

A **Windows-only, portable** Gradio app that turns one audio track + one or more source videos into a
beat-synchronized music video (AMV/GMV). Python 3.13, no PyTorch/Transformers. GPU work is split three
ways: **CuPy CTK** (CUDA analysis), **llama.cpp Vulkan** (Qwen3-VL semantic tagging), **FFmpeg NVENC**
(encoding). All three are optional — every path degrades to CPU.

This repo contains **source only**. `bin/` (portable Python, FFmpeg, llama.cpp, GGUF models), `input/`
and `output/` are runtime directories created by the installer or at import time; they are not tracked.

Licensed AGPL-3.0; this repository is a **modified fork** — `CHANGELOG-FORK.md` is the §5(a)
modification record.

## Commands

```bat
install.bat                     :: one-time: portable Python/FFmpeg/llama.cpp/Qwen GGUF via scripts\install.ps1
run.bat                         :: Gradio UI on http://127.0.0.1:7860 (auto-steps to 7861+ if busy)
```

Running modules directly (portable interpreter; `run.bat` sets `PYTHONPATH=src`, `PYTHONUTF8=1`,
`PYTHONDONTWRITEBYTECODE=1` and puts `bin\ffmpeg` on `PATH`):

```bat
set PY=bin\python-3.13.14-embed-amd64\python.exe

%PY% -X utf8 src\logger.py           :: environment report: Python/CUDA/GPU/FFmpeg/NVENC/librosa
%PY% -X utf8 src\gui.py              :: UI without run.bat
%PY% -X utf8 src\video_processor.py <audio> <video_dir> -o out.mkv --gpu --gpu-encoder h264_nvenc
%PY% -X utf8 src\auto_mode\stage5_qwen_scene_worker.py --request req.json --response resp.json
```

`src/video_processor.py` is the **headless entry point** and the fastest way to exercise the whole
pipeline — same `analyze_beats_auto` → `create_music_video` path as the UI, but it prints everything
instead of routing through the UI's quiet console. It also exposes every creative control as a flag
(`--seed`, `--cut-density`, `--micro-cuts`, `--semantic-emphasis`, `--energy-response`,
`--motion-bias`, `--source-diversity`); those seven values are the reproducible execution contract.

### Tests

```bat
python -m pip install -r requirements-dev.txt    :: pytest only; NOT installed into bin/ by install.ps1
python -m pytest                                 :: whole suite
```

The whole suite runs on **any** recent CPython — no portable runtime, no CUDA, no FFmpeg, no models —
but it is not limited to the fork package. Three distinct tiers, and conflating them is how a false
claim gets made in either direction:

1. **Directly importable, bare-CPython core** — `src/beatsync_fork/*`. Imported normally and tested
   as ordinary code, which is exactly what the stdlib-only hard rule buys.
2. **Upstream seams verified without importing the runtime** — `video_analysis.py`, `gui.py`,
   `stage5_qwen_scene_worker.py`, `stage6_av_planner.py`, `stage4_select.py` and others are reached
   by `ast` inspection, by path-based `importlib` loading, and by AST-extracting real function or
   class bodies and executing them against a synthesised parent package. These are **real
   assertions about real upstream source**, not mocks — but they are seam and structure assertions,
   not integration runs.
3. **Real portable-runtime / end-to-end verification** — not in the suite. Run the CLI on a short
   audio file plus one source video and check the stage timings and the output file. A small
   opt-in tier sits between 2 and 3: `tests/test_audio_mixdown.py` exercises real FFmpeg when
   `BEATSYNC_TEST_FFMPEG` points at the portable build, and skips otherwise.

So: the upstream pipeline modules **cannot be imported** on a bare interpreter, and they are
nevertheless **not untested**. There is no linter or formatter config. Harness conventions and the
deliberate loading tricks: `.claude/rules/test-harness.md`.

## Architecture map

`gui.py` (or `video_processor.main`) → `auto_mode.analyze_beats_auto()` → `video_processor.create_music_video()`.

| Stage | File | Produces |
|---|---|---|
| 1 | `auto_mode/stage1_audio.py` | beat grid + tempo from the percussive HPSS component |
| 2 | `auto_mode/stage2_features.py` | beat-synchronous curves: energy, kick/clap/bass/hihat, novelty, impact, anchors |
| 3 | `auto_mode/stage3_sections.py` | broad musical sections (intro/verse/chorus/drop/build/outro…) |
| 4 | `auto_mode/stage4_select.py` | the deliberate subset of beats that become cuts |
| 5 | `video_analysis.py` + `auto_mode/stage5_qwen_scene_worker.py` | the visual library: scored candidate moments per source |
| 6 | `auto_mode/stage6_av_planner.py` + `video_processor.py` | candidate→segment assignment, then FFmpeg extract/concat/mux |

`analyze_beats_auto` returns `(selected_beats, beat_info)`. **`beat_info` is the pipeline's shared
bus** — `times`, `sections`, `energy_profile`, `rhythm_data`, `audio_visual_profile`,
`video_analysis`, `creative` — and is mutated downstream (`render_info`, `clip_plan_summary`) for the
UI summary. Adding a signal usually means adding a key here, not a new parameter.

## Globally load-bearing invariants

These apply in almost every session. Each has a scoped rule with the full contract.

- **Import order is load-bearing.** `logger.py` owns `setup_environment()`, which mutates
  `CUDA_PATH`/`PATH`/`PYTHONHOME` and reconfigures stdout/stderr to UTF-8. Every entry module calls it
  **before** importing cupy/gradio/cv2. Preserve the
  `from logger import setup_environment; setup_environment()` prologue and the position of later
  imports. `logger.py` also inserts `src/` into `sys.path`, so modules import each other flat
  (`from paths import …`). Importing `paths` creates `input/*` and `output/` as a side effect. →
  `.claude/rules/pipeline-core.md`
- **The hard rule for `src/beatsync_fork/`: nothing in it may import the upstream runtime** (`logger`,
  `paths`, `gradio`, `cupy`, `cv2`, `librosa`, `numpy`). That is what keeps the fork package testable
  on a bare interpreter; `tests/test_no_runtime_dependency.py` enforces it statically and
  dynamically. Several modules cite "CLAUDE.md's hard rule" in their docstrings — this is it. →
  `.claude/rules/fork-package.md`
- **The frame-lock invariant.** `build_frame_aligned_cut_timeline()` quantizes **absolute** cut
  positions to output frames once; segment lengths are frame *differences*. Never reintroduce
  per-segment `round(duration * fps)`. If a clip fails to extract, `create_music_video` **raises**
  rather than concatenating a short timeline. → `.claude/rules/pipeline-core.md`
- **Stage 5 runs out-of-process** and never loads a model in-process; the response JSON is the only
  semantic authority. The retained request/response JSON files are **intentionally** left behind for
  debugging. → `.claude/rules/stage5-worker.md`
- **Stage-5 cache contract constants.** `CACHE_CONTRACT_VERSION = "stage5_cache_v3"` owns cache
  identity *and* the persisted contract; `ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"`
  owns candidate scoring, window building and the candidate schema. Bump the right one, never a new
  constant. → `.claude/rules/stage5-cache-identity.md`, `.claude/rules/stage5-cache-durability.md`
- **Persisted Stage-5 semantics are media-neutral.** Stage 5 records intrinsic media truth
  (persistent); Stage 6 and the Director layers own creative interpretation (ephemeral, per render).
  Nothing about the music, the edit style or any creative control may reach a Qwen request, the
  prompt, a persisted record or Stage-5 cache identity. → `.claude/rules/stage5-worker.md`
- **Creative state is a render request, never source or media identity.** The seven resolved controls
  ride on `beat_info["creative"]`; changing one re-plans and never re-analyses. →
  `.claude/rules/creative-controls.md`
- **Cancellation is boundary-only (C3-R1A).** Exactly one `RenderLifecycle` per top-level render
  event — one Create Music Video click, or the **whole** 2–4-candidate batch. FFmpeg-class children
  are terminated *and reaped* before `RenderCancelled` propagates; an in-flight Stage-5/Qwen call is
  never hard-killed. `RenderCancelled` is an ordinary `Exception`, so every broad `except Exception`
  on the render path must name it **before** the generic handler. Abandoning a stream is **not** a
  Cancel, only a plain string enters `gr.State`, and the durable promotion is the one SUCCESS commit
  point. → `.claude/rules/variant-lab.md` (C3-R1A), `.claude/rules/pipeline-core.md`
- **Progress is structured, not printed.** In GUI runs stdout/stderr go to a discarded `QuietConsole`,
  so a plain `print()` inside the pipeline is invisible in the UI — emit a `ProgressEvent`. Never
  touch a Gradio component from a worker thread, and never invent an ETA. →
  `.claude/rules/progress-events.md`
- **Create Music Video is gated on an explicitly confirmed source set**, identified by a
  `SourceSnapshot` (ordered path + size + mtime_ns, mode, scan root, recursive flag), not a count. →
  `.claude/rules/input-gate.md`
- **Windows-only by construction**: `.exe` paths, `CREATE_NO_WINDOW`, `chcp 65001`, PowerShell
  installer, embedded-Python `._pth` patched to include `..\..\src`. →
  `.claude/rules/platform-and-packaging.md`
- **Upstream files are touched only at small call sites marked `# [FORK]`.** Grep for `[FORK]` to
  enumerate the entire divergence surface.

## Hard safety floor

The full, operative policies are `.claude/rules/operating-policies.md` (unscoped, always active:
`DU-REPO-WORKFLOW-v1`, `PCBUS-HK-v1`, `PCBUS-WATCHDOG-v1`). Read it before any repository mutation or
any wait on an external result. The non-negotiables, restated here deliberately because getting one
wrong is expensive:

- **Never force-push and never rewrite published history** unless the task explicitly authorizes it.
  Push the branch the task was done on; never push implementation work directly to `main`; never merge
  a PR unless explicitly authorized.
- **Authorized work left only on local disk is not finished.** Complete it, verify the full diff and
  the applicable checks, commit, push, then verify `LOCAL_HEAD == REMOTE_BRANCH_HEAD` against the
  *actual* remote. Never commit or push merely to make a failed task look complete — stop and report
  the blocker. An explicit READ ONLY / NO COMMIT / NO PUSH instruction overrides this default.
- **Never `git add .`** — stage explicit authorized paths, and preserve pre-existing uncommitted work
  outside task scope.
- **Temporary work goes outside the repository**, in `C:\tmp\BeatSync-Engine-DigitalUnion\` (per-task
  scratch under `tasks\<TASK_NAME>\`, uncertain material under `quarantine\<YYYY-MM-DD>\`). Plan the
  location before creating the file. Temp storage is a workspace, never an archive.
- **`input/video_analysis_cache/` and `output/` are preserved by policy**, `.gitignore`
  notwithstanding — the cache is deliberately retained (including the retained Qwen request/response
  JSON) and `output/` holds the user's rendered videos. Never clear either as housekeeping, and never
  destroy audit / acceptance / verification evidence because a milestone completed.
- **Clean up only what your own task created.** Never infer staleness from age, never disturb another
  session's files, and never remove, prune or reset a git worktree or clone as housekeeping.
- **Waiting on an external result**: after at most **20 minutes** without a surfaced terminal state,
  run a fresh, independent, **read-only** query against the authoritative source and surface an
  interim status. Silence is not evidence of "still running". A recheck never authorizes a rerun,
  retry, `workflow_dispatch`, empty commit or force push. (`jq` is **not** installed in this machine's
  Git Bash — use `gh --jq`.)

## Environment variables

All optional tuning knobs; the table is in `.claude/rules/pipeline-core.md`. The ones that matter most
often: `BEATSYNC_DISABLE_QWEN=1` (deterministic analysis only), `BEATSYNC_QWEN_MAX_WINDOWS` /
`_FRAME_WIDTH` / `_MAX_NEW_TOKENS` (the three settings that **do** re-key the Stage-5 cache), and
`GRADIO_SERVER_PORT`.

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

### A persistently rejected candidate gets one targeted recovery (R1)

A candidate whose semantics `_normalize_semantic` rejects used to make its **whole source permanently
uncacheable**, because `_qwen_job_completed` requires `returned_ids == requested_ids`, so one missing
tag means no checkpoint and the source is re-analysed on every run forever. Two sources in the real
845-file library were in exactly that state (measured: 10 requested, 10 decoded, 9 tagged), costing a
~51 s Stage 5 tax on every warm run.

Measured root cause — **truncation**, not a field-level rejection. The primary request budgets
`_max_new_tokens()` (default 128) and leaves `description` an unbounded string. llama-server returns
`finish_reason="length"` with non-empty but truncated text; `_parse_json_object`'s `\{.*\}` finds no
closing brace, `json.loads` fails, and `_normalize_semantic` rejects at its not-a-dict guard. All 8
numeric keys, both enums and substantial description content were already present in the raw text;
what was missing was **JSON termination** — generation stopped mid-description, so neither the
description string's closing quote nor the object's closing brace was emitted (the captured output
has an odd quote count). The description value is therefore *not* syntactically complete, which is
why no amount of lenient brace-matching would rescue it. Because decoding is greedy
(`temperature 0`, `top_k 1`) every retry re-issues the identical request and gets byte-identical
output, which is why it never resolves.

**Raising the token budget alone is not a fix, and that is measured rather than assumed.** One of the
two cases is a degenerate repetition loop (`lips moving, lips open, lips closed, …`) that simply
consumes a larger budget too: still truncated at 160, 192 **and 256** tokens, growing 350 → 470 → 614
→ 880 characters. The grammar bound is the half that stops the loop; the extra budget is only needed so
the bounded JSON can close (131 and 129 tokens observed). The bound alone also fails — it leaves the
other case one token short. Smallest variant recovering both 3/3 deterministically:
**160 tokens + `description.maxLength = 96`**. Larger bounds (112, 128) also pass but retain *more* of
the repetition, which is the argument for 96.

Load-bearing details:

- **The primary path is untouched.** `_max_new_tokens()` still governs the ordinary request, still
  keyed into the D2 signature through `BEATSYNC_QWEN_MAX_NEW_TOKENS`; `SEMANTIC_SCHEMA` still carries
  `description = {"type": "string"}` with no bound; the prompt and greedy sampling are unchanged.
  `_recovery_semantic_schema()` deep-copies rather than mutating the global. Proven cross-branch:
  primary output is **byte-identical** to merged main on all four measured candidates (342/347/350/306
  chars) against one shared llama-server instance.
- **Recovery is deliberately LAST in the control flow**, and every *applicable* pre-existing primary
  retry/fallback path stays ahead of it. The tiers are conditional, not a fixed sequence every
  candidate walks: the initial attempt always runs; the server retry applies to failed candidates
  while a server is still available; the reduced-slot restart fires only on its existing condition
  (`valid_ratio < 0.70`, server active, `batch_size > 1`) and `return`s recursively, so only the
  innermost wave reaches recovery; the serial/CLI fallback applies per the existing backend state.
  Recovery does **not** force any of those tiers to run — it simply sits after whichever ones did.
  Only then does a still-unresolved candidate get **exactly one** recovery generation. No recursion,
  no second attempt.
- **Eligibility is recomputed from `semantics`, not from `failed`.** The serial fallback tier resolves
  candidates without rewriting `failed`, so trusting `failed` would re-ask for semantics that already
  arrived.
- **Semantic rejection only, never transport failure.** `_is_semantic_rejection(semantic, text)`
  requires a falsy semantic *and* non-empty text. An HTTP error, dead server, CLI timeout, non-zero
  exit or empty generation produces no truncated output to rescue, and re-asking would paper over a
  broken backend — those paths report `False` as the 4th tuple element. The classification is internal
  to the inference wave and **never reaches a cached payload**.
- **`max_tokens`/`semantic_schema` overrides default to `None` on all three `generate()` primitives**
  (`LlamaServerClient`, `LlamaMtmdClient`, `QwenLlamaClient`), so every existing caller is unchanged.
  The CLI client's ctx-fallback self-retry forwards them too — without that, a recovery hitting a ctx
  error would silently retry as an ordinary 128-token unbounded request and truncate again while
  appearing to have run.
- **The constants are hard-coded, not environment variables.** A `BEATSYNC_QWEN_RECOVERY_*` knob would
  be result-affecting Qwen configuration absent from `_qwen_config_token()` — exactly the defect D2
  fixed for `MAX_WINDOWS`. They are contract-governed instead, like the prompt and the schema.
- **No cache re-key and no contract bump for this first introduction**, and the argument is structural:
  a source current main caches had every requested candidate tagged on the primary path, so recovery
  never runs and the persisted semantics are identical; a source that missed a candidate fails
  `_qwen_job_completed`, so current main wrote **no complete record at all** — recovery can only turn
  an absence into a record, never contradict a stored one. The 843 existing D2 records stay reusable.
  A *future* change to these constants does not inherit that argument, because fallback-generated
  records will exist by then: default policy is to bump `CACHE_CONTRACT_VERSION` unless
  persisted-output compatibility is explicitly proven.
- **The completion contract is unchanged.** `_qwen_job_completed`, `_stored_ai_cache_is_consistent`,
  `ai_enabled`/`ai_deferred` semantics and checkpoint eligibility are all untouched. Recovery merely
  supplies one more candidate semantic; the existing machinery still decides whether the source may be
  checkpointed. A failed recovery leaves the candidate absent and the job incomplete, exactly as before.
- **Recovery does not cure repetition.** The recovered degenerate description is still partially
  repetitive — it is merely valid JSON, schema-valid, normalization-valid and bounded to ≤ 96 chars.
  The win is that one runaway description no longer makes an entire source permanently uncacheable.
  Prose quality is out of scope.

Measured cost: ~0.61–0.72 s per recovery call (mean ~0.65 s), so 2 calls ≈ 1.3 s for the known library.
Prediction only until a production run confirms it: once both sources recover and checkpoint, a
subsequent identical warm run should show 845/845 cache hits and launch no Qwen worker at all.

### Analysis cache

`input/video_analysis_cache/*.json` is keyed by `CACHE_CONTRACT_VERSION` + `ANALYSIS_VERSION` + source
identity (absolute path, size, `st_mtime_ns`, bounded content fingerprint) + a backend token (or
`no_ai`) + an effective Qwen config token covering `MAX_WINDOWS`, `FRAME_WIDTH`, `MAX_NEW_TOKENS` and
`smart_preset`. **The exact contract — including the fingerprint windows, backend identity,
fail-closed behaviour and the persisted `cache_contract` marker — is the D2 section immediately
below; read that rather than this summary before changing anything.** Swapping the GGUF model or
llama.cpp build still invalidates automatically.

`ANALYSIS_VERSION` keeps its own narrower job and does **not** own cache-generation semantics: **bump
it in `video_analysis.py` whenever candidate scoring, window building, or the candidate schema
changes**, otherwise stale candidates silently survive. Cache identity and the contract generation
belong to `CACHE_CONTRACT_VERSION`.

#### Cache identity and the contract generation (D2)

**`CACHE_CONTRACT_VERSION = "stage5_cache_v2"` is the single constant owning cache identity *and* the
persisted contract.** It is the first component of every signature *and* the value stored as
`cache_contract` in every record, so a key and its payload can never disagree about their generation.
There is deliberately no second version constant. **Bump it whenever a change alters what a cached
result means** — the identity algorithm, the Qwen prompt, the semantic normalisation/output contract,
or a result-affecting Qwen setting not already in the signature. Do *not* hash the worker source into
the key: a progress or performance-only worker edit must not invalidate semantic cache.
`ANALYSIS_VERSION` keeps its own narrower job (candidate scoring, window building, candidate schema)
and D2 does not touch it.

**Source identity** = `CACHE_CONTRACT_VERSION | ANALYSIS_VERSION | abspath | st_size | st_mtime_ns |
bounded content fingerprint | backend token (or `no_ai`) | Qwen config token`. Pre-D2 it was
`abspath + size + int(st_mtime)`, which reproducibly gave the *same* signature to a file rewritten
inside the same second. `st_mtime_ns` fixes that truncation but **not** an exact-mtime restore — the
fingerprint is what catches that, which is why mtime_ns alone was not enough. The absolute path stays
in identity on purpose: identity is *location + content*, so a moved file re-keys, preserving the
pre-D2 contract. Content-only identity would deduplicate copies; that is a semantic change D2 does not
make.

**Bounded fingerprint**: BLAKE2b, 16-byte digest, over `str(size)` then content. Files ≤ 3 MiB are read
whole; larger files contribute exactly three non-overlapping 1 MiB windows — `[0, 1 MiB)`,
`[mid, mid + 1 MiB)` with `mid = max(1 MiB, min(size // 2 - 512 KiB, size - 2 MiB))`, and
`[size - 1 MiB, size)`. The clamp guarantees no byte is hashed twice, so bytes read = `min(size, 3 MiB)`.
Measured on the real 702-source library: **5.1 ms/source → ~3.6 s per 700, ~2.1 GB read**. BLAKE2b
rather than SHA-256 because it measured faster and this is **accidental** stale-cache prevention — no
cryptographic or adversarial claim is made.

**Backend identity** covers all four components by *absolute path* + size + `st_mtime_ns` + content:
full BLAKE2b for `llama-server.exe` and `llama-mtmd-cli.exe` (9 KB and 83 KB — exact identity for
~13 ms), the bounded fingerprint for the two GGUFs (1.83 GB and 0.82 GB). Pre-D2 the token used the
*basename*, so same-name/same-size/same-second files in different directories collided and an override
pointing at another copy did not re-key. The `llama --version` string stays as extra evidence but is no
longer load-bearing alone, so a failed version probe remains non-fatal.

**The backend token is computed once per `analyze_video_sources` invocation — on success *and* on
failure — and threaded into every source signature** (`backend_token=` / `config_token=` /
`audio_profile=` on `_video_signature`/`_cache_path`). It used to be reached *from* `_video_signature`,
i.e. once per source; with content fingerprints that is 702 reads of a ~2.65 GB backend, measured at
**61.7 minutes**. Threaded it is ~9–20 ms once. It is invocation-scoped, not module-cached, so a later
call in the same process still observes a swapped model or llama build.

**If the invocation-level backend identity fails, AI caching is off for that entire run.** The
orchestrator holds an explicit `ai_cache_disabled` state and then does not call `_cache_path` at all —
because down in `_video_signature` a `None` `backend_token` means *"not supplied, compute it now"*, so
handing the failed `None` onward made every source retry the fingerprinting (measured **1 + N** calls)
and let a transient later success re-enable caching *mid-run*. Never overload `None` as both "not
supplied" and "supplied but failed" at that boundary, and **do not** claim a per-source retry can
restore caching: it must not. Analysis, Qwen and rendering continue normally; only the cache is off.

**Qwen identity keys four things**, on *effective* values mirroring the runtime's own parsing and
clamps, so behaviourally identical configurations key identically (unset == explicit default; a
malformed value == the default the worker actually uses):

- `BEATSYNC_QWEN_MAX_WINDOWS` — default 120, then `max(0, …)`. D1 proved this changes how many
  candidates get semantics while being absent from identity.
- `BEATSYNC_QWEN_FRAME_WIDTH` — 512, clamp 224–768. Changes the image the VLM sees.
- `BEATSYNC_QWEN_MAX_NEW_TOKENS` — 128, clamp 32–256. Can truncate the semantic JSON.
- `audio_profile["smart_preset"]` — **prompt context**. `analyze_video_sources` forwards the audio
  profile into the worker request, and the worker's `_build_prompt` interpolates this value directly
  into the Qwen prompt (`"The music edit style is {style_hint}."`), defaulting to `rhythmic_gmv_amv`.
  `_qwen_prompt_style_hint` mirrors that default in one place; a seam test reads the worker's own
  `audio_profile.get("smart_preset", …)` call and asserts the parent agrees, so a worker-side change
  to the key or default fails the suite.

The **whole `audio_profile` is deliberately not hashed** — almost all of it drives beat and render
decisions, not the prompt; only fields proven to reach the persisted result belong in identity.
Runtime-only knobs stay **excluded**: slots, device, timeouts, batching, ctx, prefetch. A `no_ai` run
gets a canonical no-AI config token, so neither Qwen settings nor `smart_preset` perturb a
deterministic key.

**Unprovable identity means no cache, never a weak key.** If a stat or fingerprint fails —
source *or* backend — `_video_signature` and `_cache_path` return `None`: that source gets no lookup and
no write for the run, and analysis/rendering continue normally. There is no `ai_missing`-style
placeholder any more, because a *stable* token for an unprovable input is exactly what lets a stale
entry be reused. `_checkpoint_cache(None, …)` is already a no-op, which is the seam this uses.

**The contract marker is stamped where records are born**, in `_analyze_single_video`, so AI,
deterministic/`no_ai` and candidate-less records all carry it before the completion rule sees them.
`_save_cache` stays a pure transport primitive and never injects semantic truth.

**D2 costs exactly one cold rebuild, by design** (~0.86–1.27 h for the current 702-source library) and
there is **no migration and no cleanup**: new inputs produce new filenames, so pre-D2 records are simply
never looked up. There is deliberately no D1→D2 compatibility loader. Old and new generations coexist at
roughly 55 MB each (~110 MB peak); `input/video_analysis_cache/` is preserved by policy, so do not add
automatic deletion. Warm runs thereafter pay only the ~3.6 s identity scan plus ~20 ms of backend work.

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
- **A submitted Qwen job completed only if every *requested* candidate came back tagged.**
  `_qwen_job_completed(timing, envelope_present, requested_ids, returned_ids)` is the one rule, shared
  by the single and batch paths. It requires **all** of: the expected per-job envelope exists;
  `timing` is a dict; `frame_count` and `tag_count` are real integer counts; the requested set is
  non-empty; `frame_count == len(requested_ids)`; `tag_count == frame_count`; and
  `returned_ids == requested_ids`. `requested_ids` is what `_select_ai_candidates` actually submitted,
  not every deterministic candidate.

  Three weaker definitions were tried here first, and each is worth remembering:

  1. **emptiness of `semantics`** — conflated a finished worker with a dead one.
  2. **envelope membership alone** — `_run_semantics_for_video` always returns a timings dict and
     `main` always records it under the job id, so the envelope only proves *the job loop returned*.
     Inside it, `_normalize_semantic` returns `{}` for any candidate whose semantic content is invalid
     (missing numeric key, disallowed emotion/use, empty description), `_run_inference_wave`
     classifies `{}` as **failed** and retries it (server retry → reduced-slot restart → serial
     fallback), and a candidate still failing is simply **absent** from the returned semantics.
  3. **`tag_count == frame_count`** — proves every *decoded* frame was tagged, but
     `_prefetch_candidate_frames` returns only frames it could read
     (`ready = [p for p in plans if p["image"] is not None]`), so `frame_count` can be **smaller than
     the requested set**. requested 10 / decoded 8 / tagged 8 looked complete while two candidates had
     no semantics at all.

  So **no single signal is sufficient**: not the envelope, not a count, not tag presence. The ids
  matter too — a foreign semantic id is not evidence that one of *our* candidates completed, and an
  extra id means the response does not match the request, so the match is exact. Tags that *did*
  arrive are still merged; only the verdict changes, so the next run retries instead of inheriting a
  silent gap. A globally non-empty batch response is still no proof that *this* job ran, and one
  failing job never fails its siblings.

  Worker counts are read defensively because the response is parsed JSON from a subprocess:
  `_coerce_count` stops a non-numeric count raising `ValueError` out of the whole analysis, `_is_count`
  rejects `bool` (which subclasses `int`, so `frame_count: true` would otherwise have compared equal
  to 1), and `_reported_count` uses **presence** rather than truthiness — `timing.get(k) or default`
  silently rewrote a genuine `frame_count = 0` into the requested candidate count in stored timings.
- **A *stored* AI record must also be self-consistent, not just flagged.**
  `_stored_ai_cache_is_consistent()` runs whenever `require_ai` reuse depends on `ai_enabled`, and it
  is deliberately **not** the live rule: `_qwen_job_completed` needs `requested_ids`, which legacy
  payloads never stored (nor the `BEATSYNC_QWEN_MAX_WINDOWS` value in force), so replaying it against
  an old record would mean inventing evidence. **Never call `_qwen_job_completed` from the loader.**

  It asks only whether the persisted fields contradict each other: `timings` is a dict with real
  integer `qwen_frame_count`/`qwen_tag_count` (not bools), `frame_count > 0`,
  `tag_count == frame_count`, `frame_count <= len(candidates)`, candidate ids are usable strings, and
  the `ai_analyzed` ids are unique, a subset of the candidate ids, and number exactly `tag_count`.

  It deliberately does **not** require `frame_count == len(candidates)` — a smaller value is the
  normal result of `_select_ai_candidates` limiting the submitted set.

  This exists because a read-only audit of the real 2196-entry cache found **4** records claiming
  `ai_enabled=True` with `qwen_frame_count=10`, `qwen_tag_count=9` and 9 `ai_analyzed` candidates —
  exactly the false-complete shape D1 exists to prevent — which the pre-R6 loader accepted because it
  only checked `bool(data["ai_enabled"])`. Measured effect *in the D1-era cache*: 2192 of 2196 accepted,
  those 4 rejected and naturally recomputed on next encounter. Two further records (398 candidates/114
  tagged, 525/119) were internally coherent but historically unverifiable, and remained reusable under
  the D1 loader pending the D2 decision — which the D2 generation transition has since settled by
  orphaning them. **No runtime cache file was ever edited** — the audit and the verification are
  read-only, and rejection simply becomes an ordinary cache miss.
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
- **Historical, at the D1 merge boundary:** D1 deliberately did **not** change `_video_signature`,
  `_path_signature_token`, `_qwen_backend_signature_token`, `_cache_path` or `ANALYSIS_VERSION`.
  `_video_signature` still used `int(stat.st_mtime)` at that point, so all pre-existing entries
  remained addressable, and 2192 of the 2196 real records remained reusable once the selective legacy
  guard landed, with 4 intentionally rejected as self-contradictory (see the stored-consistency rule
  above). Source/backend identity hardening was intentionally deferred *from* D1, because closing
  `int(st_mtime)`'s same-second collision re-keys the whole cache.

  **Current state:** the D2 identity contract above supersedes all of that. Identity now uses
  `st_mtime_ns` plus a bounded content fingerprint under `CACHE_CONTRACT_VERSION = "stage5_cache_v2"`,
  so pre-D2 records are naturally orphaned and are never reachable by a D2 lookup — including the two
  records whose completeness D1 could not prove, which that transition retires without a judgement
  call. The D1 figures above describe the D1-era loader and cache, not current behaviour.
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

### Stage 5 returns two different truths (R1)

`analyze_video_sources` returns **current-run execution facts** and **cached-library aggregates**, and
they must never be presented as the same thing. Production proved why: a fully warm run — 845/845
cache hits, **zero** sources re-analysed, **zero** Qwen workers launched, zero inference — printed
`visual workers: 1`, `Qwen performance: … 3.12 candidates/s` and `Qwen tags: 8704/8704 in 3031.9s`.
Every one of those numbers came from cached records. This is a reporting defect only; the cache and
the Qwen runtime behaved correctly throughout.

- **Library aggregates** (`qwen_tag_count`, `qwen_frame_count`, `qwen_seconds`,
  `qwen_inference_seconds`, `qwen_model_id`, `qwen_concurrency`, `qwen_peak_vram_gb`) sum or select
  over `videos`, which contains every cache hit. They are **historical by construction** and are
  preserved unchanged for compatibility. A warm library legitimately carries large Qwen counts and
  timings with no inference having happened this run.
- **Current-run facts** carry the `_this_run` suffix (`sources_analyzed_this_run`,
  `analysis_workers_used`, `qwen_jobs_this_run`, `qwen_completed_jobs_this_run`,
  `qwen_incomplete_jobs_this_run`, `qwen_frame_count_this_run`, `qwen_tag_count_this_run`,
  `qwen_seconds_this_run`, `qwen_inference_seconds_this_run`). **Any claim about work performed or
  performance achieved must use these.**

Load-bearing details:

- **Current-run counts come from `_new_run_stats()`**, an invocation-scoped dict threaded only into
  the paths that analyse an *uncached* source. It is never populated from a cached record, so a cache
  hit cannot inflate it, and a second call in the same process starts from zero. It is ephemeral
  top-level metadata: a separate object from `video_data`, so there is no path by which it reaches
  `_checkpoint_cache`. **No cache field, no cache identity, no contract bump.**
- **A Qwen job is counted only where a request is genuinely issued**, which is why
  `qwen_jobs_this_run` is *not* `len(deferred_jobs)`. That would be wrong in both directions: the
  serial path (`_analyze_single_video` with `defer_ai=False`) runs Qwen **inline** and never appears
  in `deferred_jobs`, while a deferred source whose candidate list is empty passes through the batch
  orchestration and never reaches the worker. The counters therefore live at the two places that
  actually submit work — after `_run_qwen_worker` in the facade, and in the batch's per-job merge
  loop, which only runs for sources that made it into `request_jobs`.
- **Both recording sites are written inline rather than through a shared helper.**
  `_annotate_candidates_with_qwen` and `_complete_deferred_qwen_batch` are AST-extracted and executed
  by `tests/test_stage5_cache_completion.py`, so they must stay self-contained with respect to
  helpers outside that suite's extraction list. Do not "tidy" this into a module-level function
  without also updating that suite.
- **A job that ran but did not complete is still a job.** It is counted and separately tallied as
  incomplete, so a failed pass can neither be reported as success nor silently vanish. On a failure
  path where elapsed time is not provable, the UI **omits** the timing rather than understating it.
- **Zero uncached jobs means zero analysis workers used.** `_video_analysis_workers` keeps its
  `video_count <= 1 → 1` contract untouched — the genuine single-source case needs it — and the call
  site supplies the truth (`… if jobs else 0`). The analysis block is guarded by `if jobs:`, so
  nothing is ever submitted when the library is fully warm.
- **The Stage-5 START event is pre-cache-classification.** It fires before the cache scan, so it
  cannot know how many sources need analysing; it says `Checking N source video(s)`. The post-scan
  metric (`H cached, J to analyze, W worker(s)`) remains the authority on real work.
- **The five-line CMD budget is not the bug and must not be raised.** `_stage5_summary` is ordered so
  current-run truth wins: sources/cache/analysed → current-run Qwen status → `Analysis time …,
  cache H/N` → library summary → optional *explicitly labelled* cached metadata. The authoritative
  `Analysis time` line used to be the one dropped; a test now pins that it survives.

Not fixed here, and deliberately out of scope: production measured **~69 s** inside Stage 5 on that
warm run (`total_elapsed` brackets the whole `analyze_video_sources` body and flows to the END
event's `elapsed_seconds`), while later profiling — run after the same ~2.52 GB of bounded
fingerprint windows were already in the OS cache — measured **~3.2–3.4 s**. That discrepancy is
**unresolved**. This work fixes reporting truth, not Stage-5 performance.

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

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
%PY% -X utf8 src\video_processor.py <audio> <video_dir> -o out.mkv --seed 381944   :: creative variation
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
`no_ai`) + an effective Qwen config token covering `MAX_WINDOWS`, `FRAME_WIDTH` and `MAX_NEW_TOKENS`.
**The exact contract — including the fingerprint windows, backend identity, fail-closed behaviour and
the persisted `cache_contract` marker — is the D2 section immediately below; read that rather than
this summary before changing anything.** Swapping the GGUF model or llama.cpp build still invalidates
automatically. Nothing about the *music* is in there — see the media-neutral contract (P2) below.

`ANALYSIS_VERSION` keeps its own narrower job and does **not** own cache-generation semantics: **bump
it in `video_analysis.py` whenever candidate scoring, window building, or the candidate schema
changes**, otherwise stale candidates silently survive. Cache identity and the contract generation
belong to `CACHE_CONTRACT_VERSION`.

#### Cache identity and the contract generation (D2)

**`CACHE_CONTRACT_VERSION = "stage5_cache_v3"` is the single constant owning cache identity *and* the
persisted contract.** It is the first component of every signature *and* the value stored as
`cache_contract` in every record, so a key and its payload can never disagree about their generation.
There is deliberately no second version constant. **Bump it whenever a change alters what a cached
result means** — the identity algorithm, the Qwen prompt, the semantic normalisation/output contract,
or a result-affecting Qwen setting not already in the signature. Do *not* hash the worker source into
the key: a progress or performance-only worker edit must not invalidate semantic cache.
`ANALYSIS_VERSION` keeps its own narrower job (candidate scoring, window building, candidate schema)
and neither D2 nor P2 touches it.

**It has been bumped once, `stage5_cache_v2` → `stage5_cache_v3`, by P2** — the media-neutral prompt
changed what a persisted semantic record *means*. See the media-neutral section for why there is no
migration.

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
failure — and threaded into every source signature** (`backend_token=` / `config_token=` on
`_video_signature`/`_cache_path`; there is no `audio_profile=` there any more, see P2). It used to be
reached *from* `_video_signature`,
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

**Qwen identity keys three things** — `_qwen_config_token()` takes no arguments — on *effective*
values mirroring the runtime's own parsing and clamps, so behaviourally identical configurations key
identically (unset == explicit default; a malformed value == the default the worker actually uses):

- `BEATSYNC_QWEN_MAX_WINDOWS` — default 120, then `max(0, …)`. D1 proved this changes how many
  candidates get semantics while being absent from identity.
- `BEATSYNC_QWEN_FRAME_WIDTH` — 512, clamp 224–768. Changes the image the VLM sees.
- `BEATSYNC_QWEN_MAX_NEW_TOKENS` — 128, clamp 32–256. Can truncate the semantic JSON.

There was a fourth, `audio_profile["smart_preset"]`, because the worker interpolated it into the
prompt. **P2 retired it**, along with `_qwen_prompt_style_hint` and the whole notion of an edit style
in persisted semantics; that is the media-neutral section below, and the D2 R2 tests asserting the
opposite were deliberately replaced. Runtime-only knobs stay **excluded**: slots, device, timeouts,
batching, ctx, prefetch. A `no_ai` run gets a canonical no-AI config token, so Qwen settings never
perturb a deterministic key.

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
  `st_mtime_ns` plus a bounded content fingerprint under `CACHE_CONTRACT_VERSION = "stage5_cache_v3"`,
  so pre-D2 records are naturally orphaned and are never reachable by a current lookup — including the
  two records whose completeness D1 could not prove, which that transition retires without a judgement
  call. P2's `v2 → v3` bump orphans the D2 generation the same way, by the same mechanism. The D1
  figures above describe the D1-era loader and cache, not current behaviour.
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
  `qwen_incomplete_jobs_this_run`, `qwen_requested_count_this_run`, `qwen_frame_count_this_run`,
  `qwen_tag_count_this_run`, `qwen_seconds_this_run`, `qwen_inference_seconds_this_run`). **Any
  claim about work performed or performance achieved must use these.**

**Requested, decoded and tagged are three different facts** and must not be conflated:
`qwen_requested_count_this_run` is what was *submitted*; `qwen_frame_count_this_run` is what the
worker *proved* it decoded; `qwen_tag_count_this_run` is what was *actually merged*. Only the first
is knowable without a usable worker response, which is what makes a failed attempt reportable at
all. **The UI's `N/M tags` denominator is the requested count**, never the decoded count — a job
that requested 10 and decoded 8 must not render as a flawless `8/8`. Current-run decoded frames are
counted only when the worker reported a real integer: the persisted `timings["qwen_frame_count"]`
falls back to the requested count for source-record compatibility, and **that fallback must never
leak into current-run truth** (the legacy field keeps it unchanged).

Load-bearing details:

- **Current-run counts come from `_new_run_stats()`**, an invocation-scoped dict threaded only into
  the paths that analyse an *uncached* source. It is never populated from a cached record, so a cache
  hit cannot inflate it, and a second call in the same process starts from zero. It is ephemeral
  top-level metadata: a separate object from `video_data`, so there is no path by which it reaches
  `_checkpoint_cache`. **No cache field, no cache identity, no contract bump.**
- **Batch accounting is two-phase, and submission truth comes first.** `_complete_deferred_qwen_batch`
  records jobs, requested candidates and the shared-worker wall time **immediately after
  `_run_qwen_worker_batch` returns — before the empty-response branch**. A worker that timed out,
  exited non-zero or produced an unreadable response still consumed a real attempt on real sources;
  recording only in the per-job merge loop reported `0 jobs`, which the UI rendered as "no inference
  this run". Every submitted job starts *incomplete* and is promoted only by its own returned
  evidence, so an empty response leaves them all incomplete with no extra bookkeeping. The per-job
  loop therefore must **not** increment `qwen_jobs` — that would double-count every success.
- **Current-run wall time counts each worker invocation once.** `qwen_seconds_this_run` adds the one
  measured `batch_seconds` for a shared batch, and the one measured call duration for a
  single/inline invocation. Never sum the amortized per-source figures — they scale with source
  count and would inflate the total. Worker-reported inference time is added only where it is
  present and numeric; missing timing is absence of evidence, so it contributes zero and is never
  invented. Accounting is observability and must stay non-fatal: it may not introduce an exception
  on malformed timing data that the runtime would otherwise survive.
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

### Counts and optional telemetry have different trust contracts (T1)

Everything Stage 5 reads out of a Qwen response arrived as parsed JSON from a subprocess, and
everything it reads out of a cache record arrived off disk. Those values split into two kinds, and
conflating them is what produced a class of crashes:

- **Counts are load-bearing.** `frame_count`/`tag_count` decide semantic completion, so `_is_count`,
  `_coerce_count` and `_reported_count` keep their D1 semantics **frozen**. Do not redefine them.
- **Durations, VRAM, batch size and the model id are optional telemetry** — pure observability.
  `_qwen_job_completed`, `_stored_ai_cache_is_consistent` and `_cache_entry_is_complete` never read
  them, and a test asserts those bodies reference neither the fields nor the telemetry helpers.

**Semantic completion must never depend on optional telemetry.** 10 requested / 10 decoded / 10
returned for exactly the requested ids is a *complete* job even if `inference_seconds` is `"bad"`. The
old code got this wrong in two different ways: the shared-batch call site is unguarded, so a malformed
duration aborted Stage 5 outright; the inline facade *is* wrapped in `except Exception`, so a malformed
`batch_size` or `peak_vram_gb` silently produced `ai_enabled=False` — permanent re-analysis of a fully
tagged source, caused by a field no completion rule reads.

**`NaN`/`Infinity` are a real hazard here, not a hypothetical.** `json.loads`/`json.load` accept the
bare `NaN`, `Infinity`, `-Infinity` tokens (Python's default non-standard `parse_constant`) and
`json.dump` emits them (`allow_nan=True` by default). So a non-finite value survives the worker
response file *and* a full cache round trip, `float()` will not reject it, and one of them makes every
sum it enters non-finite silently and permanently. Two tests demonstrate this rather than assert it.
Do not "simplify" that away, and do not try to fix this by turning `allow_nan` off in `_save_cache` —
the writer is a transport primitive, not the semantic-policy layer.

The telemetry seam (`_is_real_number`, `_optional_telemetry_number`, `_telemetry_seconds`,
`_telemetry_total`, `_is_nonnegative_count`, `_bounded_count`, `_telemetry_text`, `_as_mapping`,
`_record_telemetry`, `_record_candidate_count`) is stdlib-only and deliberately field-specific rather
than one blind coercer. Load-bearing details:

- **An accepted telemetry number is a real `int`/`float`, not a `bool`, finite and `>= 0`.** A numeric
  string is **not** accepted — `float("2.5")` succeeding is no reason to believe a string in a numeric
  field, and laundering it hides a broken producer. `int(2.7)` → 2 and `int(True)` → 1 were exactly
  that kind of fabrication for `batch_size`. Negatives are rejected because every field here is
  physically non-negative *and* because `_fmt_seconds` already clamps display with `max(0.0, …)`, so a
  negative stayed invisible on screen while corrupting the total.
- **`None` means unknown and is not the same as `0.0`.** An unprovable `peak_vram_gb` persists as
  `None`, because the worker reports a literal `0.0` — coercing malformed to `0.0` would make a corrupt
  record indistinguishable from every healthy one in the library. A genuine `0` stays `0` everywhere;
  never write `value or default` on a field where zero is a measurement.
- **No aggregate may be non-finite, including from individually finite parts.** Enough finite values
  overflow a running total, so `_telemetry_total` checks the accumulator and degrades to a neutral
  `0.0` rather than reporting `inf`. Never fabricate a plausible measurement to keep a number finite.
- **Validating the parts is necessary but not sufficient: *every* telemetry sum goes through
  `_telemetry_total`.** R1 validated each scalar and made only the final library aggregate
  overflow-safe, which left two earlier sums on raw floating-point addition — and `1e308 + 1e308` is
  `inf` from two values that each passed finite-and-non-negative. Measured on the R1 head: the
  per-job `prefetch + inference + amortized_model` wrote **`"qwen_seconds": Infinity` into a
  checkpointed record whose semantics were complete**, so the source stayed reusable while carrying
  exactly the value the contract excludes; and `qwen_inference_seconds_this_run` reached `inf` from
  two jobs in the batch path and from two successive calls in the inline path. So the rule is about
  the *operation*, not just its inputs: any place telemetry quantities are combined — a per-job sum
  or a running `run_stats` total — uses the shared aggregator, never `+` or `+=`. The parent-measured
  `run_stats["qwen_seconds"]` accumulations were routed through it too; they are `perf_counter`
  deltas and were never at risk, but the invariant is then structural rather than resting on an
  argument about how large a monotonic-clock delta can get. A test walks both orchestration bodies
  and fails on any augmented assignment to a float telemetry key, because a *future* site is the
  failure mode that got through R1's own review. **Do not add a second summation helper** — reuse
  `_telemetry_total`; a test asserts it is the only one.
- **Counts respect their natural bound.** `_is_count` admits negatives (`_is_count(-5)` is `True`),
  which is how a worker-reported `frame_count: -5` reached `qwen_frame_count_this_run`; the sign and
  bound checks therefore live in `_bounded_count`, not in `_is_count`. Current-run decoded frames must
  be `<= ` the candidates that job submitted, and library totals are bounded by the record's own
  candidate count, so a candidate-less record with hand-edited huge counts cannot inflate them.
- **There is deliberately no wall-time ceiling on durations.** Bounding a worker-reported duration by
  the parent's measurement of the call makes the value depend on how long the surrounding call happened
  to take, so a stubbed or replayed worker — the only way this code is testable without a GPU — has
  every legitimate duration rejected. Finiteness and sign are what make the aggregate safe; the ceiling
  added nothing there and cost testability. Counts keep a bound because they have a real one in scope.
- **Nested containers are untrusted; the top-level response is not re-checked.**
  `beatsync_fork.qwen_progress` already proves the loaded payload is an object — do not duplicate that.
  A truthy non-dict `semantics_by_job` used to pass the empty-response branch and then raise
  `AttributeError` on `.get`; it now degrades to the same outcome as no usable semantics (job attempted,
  job incomplete, nothing invented). `_as_mapping` is safe at load-bearing containers too, and that is a
  property rather than luck: collapsing a non-dict to `{}` can only make a completion check *fail*.
- **The library aggregation reads cache hits, so it must be tolerant rather than strict.** On a warm run
  every value in that block comes off disk. A record can pass `_cache_entry_is_complete` *and*
  `_stored_ai_cache_is_consistent` while carrying malformed optional telemetry, stay reusable — so
  nothing recomputes it — and crash or poison the aggregate on every later warm run. The fix is tolerant
  reading, **not** a stricter loader: old cache files are never rewritten and never migrated, and a
  valid entry round-trips byte-identically. Malformed-record *order* must not change the outcome either;
  the old `next(...)` converted only up to the first truthy record, so whether Stage 5 crashed depended
  on where a malformed source sorted. (Which *valid* `model_id` wins is still first-come and legitimately
  order-sensitive.)
- **Sanitise at ingestion, before a record is written.** Never by rejecting the whole record in
  `_save_cache`, and never by changing `json.dump(allow_nan=…)`.
- **This is robustness at a trust boundary, not a claim about the shipping worker.** It emits
  `perf_counter` deltas, `max(1, min(32, int(slots)))` and a literal `0.0`, so it cannot produce these
  shapes; `stage5_qwen_scene_worker.py` was **not** modified and producer-side validation was shown not
  to be required. The exposure is the retained request/response and cache JSON under
  `input/video_analysis_cache/`, a future worker change, or a regression. Concurrency gets
  non-negative-integer robustness only, because its real ceiling (`min(32, …)`) lives in the worker and
  restating it here would be a drifting magic number.
- **No cache-contract or analysis-version bump *for T1*.** T1 changed neither semantic meaning,
  candidate scoring, the candidate schema, cache identity nor the completion contract, and tolerant
  reading never contradicts a stored value. (The contract constant later moved to `stage5_cache_v3`
  for an unrelated reason — P2's media-neutral prompt; `auto_av_analysis_v8_llama_vulkan_batched` is
  still unchanged.)
- **The extraction lists are part of this contract.** `_complete_deferred_qwen_batch` and
  `_annotate_candidates_with_qwen` are AST-extracted and *executed* by
  `tests/test_stage5_reporting_truth.py` and `tests/test_stage5_cache_completion.py`. Any module-level
  helper those bodies call must be added to both suites' extraction tuples, or they fail with
  `NameError` — that coupling is the price of testing the real bodies on a bare interpreter, and it is
  the reason those two suites list helper names explicitly.

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

### The creative variation seed (Phase A)

The user picks a **Variation Seed**; it changes which clips the planner chooses, and nothing else.

- **Seed 0 is legacy, and that is a product contract, not an implementation detail.** `_choose_candidate`
  keeps its original argmax branch for seed 0 — same penalties, same `rng.random() * 0.015`, and
  critically the same RNG stream: `_stable_rng(index, target, start)` with **no** seed component.
  Prepending a `0` would change the hash input, change the jitter and silently change every default
  render. A positive seed uses `_stable_rng(seed, index, target, start)` instead. A test recomputes
  the pre-seed algorithm independently and pins the legacy plan against it.
- **A positive seed changes the winner rule, not the scoring.** `_adjusted_score` is the old inline
  arithmetic lifted out verbatim so both branches score identically; `variation.select_index` then
  takes the best `TOP_K = 6`, drops anything more than `SCORE_WINDOW = 0.12` below the best, and makes
  a weighted draw. The window is deliberately smaller than the planner's own 0.28 "seen recently"
  penalty, so variation can never undo a repeat penalty the planner applied on purpose.
- **The seed rides on `beat_info["creative"]`.** `analyze_beats_auto(creative={"seed": n})` normalises
  it once and stores it; Stage 6 is the only reader. No analysis signature changed, and
  `create_music_video` did not change at all.
- **It must never touch cache identity.** `video_analysis.py` was unmodified by Phase A, both version
  constants were unchanged, and the seed is absent from `_video_signature` / `_cache_path` /
  `_qwen_config_token` and from `audio_visual_profile`. At the time, that last clause mattered because
  `audio_visual_profile["smart_preset"]` *was* keyed into the Qwen config token, so the profile was the
  one dict a future creative control could accidentally re-key 845 Qwen records through. **P2 closed
  that route entirely**: no `audio_profile` field reaches Stage-5 identity any more. Keeping creative
  state off the profile is still right — it describes the track — but it is no longer the last line of
  defence. Tests assert all of it by AST. Changing the seed re-plans; it never re-analyses.
- **It is not source identity either.** The widget is outside the Video Source group and is wired into
  no source handler and no `source_outputs`, so changing it cannot clear a confirmed source set. It is
  a render-request input alongside FPS and the encoder, and `test_gui_guard_seam.py` still pins the
  positional alignment between the click `inputs` list and the handler's parameters.
- **Anything not a positive whole number normalises to legacy**, and the boundary is an explicit type
  check rather than `int(value)`: `None`, `""`, a negative, `NaN`, `inf`, **`7.9` and `True`** all
  become 0. Truncating `7.9` to 7 would render a seed the user never chose and would make two
  different inputs reproduce as the same "reproducible" variation; `bool` has to be rejected first
  because it subclasses `int`. A number box can produce all of these and none may raise mid-render.

### Scale diagnostics (L0)

The roadmap targets 5,000+ sources and ~100,000 candidate moments with **no source-count cap**, so the
rule is *measure first*. L0 adds timings and counts around the four places that grow with the library
and **optimises nothing**. There are no thresholds, no warnings, no caps and no metrics store — a
number that would be a gate is out of scope by design.

- **Folder scan** — `InputReport.render_text()` shows the `scan_seconds` the scan already recorded
  (`Scan time:   4.1s`). `format_seconds` is total: non-numeric, `NaN`, `inf` and negatives all render
  `0.0s`, because a report must never raise inside a UI callback.
- **Render-time source verification** — `process_video_guarded` times its existing
  `resolve_for_render` call and hands the figure to `process_video`, which reports it as a completed
  **Stage 0** (`Stage.INPUT` already existed for pre-Stage-1 source work; L0 is its first producer).
  The gate is timed, never changed: same call, same live declaration, same allow/deny, and no
  verification result is ever reused between renders. `StageConsoleLogger.end_stage` gained an
  optional elapsed override used only for a stage whose END is the first event the logger sees —
  otherwise Stage 0 would print "ended in 0 seconds" for work that already happened.
- **Stage 5 cache scan** — `cache_identity_seconds` (the `_cache_path`/`_video_signature` side,
  including the D2 bounded content fingerprint) and `cache_lookup_seconds` (the `_load_cache` side)
  are accumulated around the *existing* calls with `perf_counter`, plus `cache_lookups`. They are
  deliberately separate: the two costs scale differently, and combining them would hide which one
  grows. Never add them to deterministic or Qwen analysis time.
- **Stage 5 counts** — `candidate_count` is `len(all_candidates)` directly, never re-derived from
  telemetry, so it cannot disagree with the list Stage 6 receives. `source_count`, `cache_hits` and
  the R1 `*_this_run` fields are unchanged, and the current-run / historical-aggregate separation
  still holds: the summary line order is sources → current-run Qwen → cache check → `Analysis time` →
  visual library, with the optional *cached-library* line yielding to the five-line budget. **The
  budget is not raised.**
- **Stage 6 planner** — `create_music_video` times only `build_planned_clip_sequence` and records
  `planner_seconds`, `planner_candidate_count` and `planner_segment_count` in `render_info`.
  `planner_candidate_count` is the candidate **moments** presented to the planner, never the source
  file count — that distinction is the whole point of the number, since the planner cost is
  segments × candidates. Existing render diagnostics (`render_cuts`, `timeline_frames`, `encoder`,
  `audio_duration`, `final_assembly_seconds`) are untouched and not duplicated.

**All of it is ephemeral invocation metadata.** Nothing here enters a cache payload, a cache key or
the completion contract: `CACHE_CONTRACT_VERSION` and `ANALYSIS_VERSION` are unchanged, and
`tests/test_scale_diagnostics.py` asserts the new names are absent from `_video_signature`,
`_cache_path`, `_qwen_config_token`, `_cache_entry_is_complete`, `_checkpoint_cache` and the
record-producing functions. One name collides on purpose: a per-source record has carried its own
`candidate_count` since long before L0 — different scope, same word, and the test pins the old one
unchanged rather than forbidding the name.

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
| `variation.py` | creative variation seed: normalisation + the seeded top-K selection rule |
| `library_prep.py` | media library preparation: classification vocabulary, scan state, report text, bounded analysis batches (trackless since P2) |

### Media Library Preparation (P V1 + P2)

Preparing a library means running the Stage-5 work for new/changed sources **before** a render, so
the render finds a warm cache. `video_analysis.classify_library_sources()` answers "which sources
already have reusable cache?" and `beatsync_fork/library_prep.py` holds the state and the report.

**It prepares the library, full stop — not a track and not an edit style.** P V1 required an audio
file and derived `smart_preset` from it, because that preset was the one `audio_profile` field
reaching the cache key. P2 made persisted semantics media-neutral, so preparation is now **trackless**:
no audio control, no Stage 1–4 pass during a scan, no `TrackIdentity`, no stored profile. One
preparation serves every track, every preset and every creative seed. The report says
`Semantic mode: media-neutral`; do not reword anything here to imply track or style dependence.

That removal is also where the measured **~15–20 s** per-scan track-profile component went. A scan
now costs folder enumeration + source identity (the D2 bounded fingerprints) + cache-record lookups.

- **`classify_library_sources` reuses the production primitives and adds no key formula.** Identity
  is `_cache_path`/`_video_signature`; reuse is `_load_cache`, i.e. the one completion rule. It
  splits a miss three ways purely for wording — key file absent → `new_or_changed`, key file present
  but rejected → `incomplete_or_invalid`, `_cache_path is None` → `source_identity_unavailable`. A
  brand-new file and a changed same-path file are deliberately **one** label: path+content identity
  cannot tell them apart, and inventing the distinction would need a content-addressed index.
- **It mirrors the orchestrator's once-per-invocation identity block rather than extracting it.**
  `tests/test_stage5_cache_identity.py` asserts that structure *inside* `analyze_video_sources`, and
  the expensive part (fingerprints, key formula) is shared through the helpers regardless. Do not
  "DRY" the six-line pattern without rewriting those assertions.
- **Unprovable backend identity blocks preparation entirely.** When AI is available but
  `_qwen_backend_signature_token` returns `None`, Stage 5 runs with caching off — a preparation run
  would then spend GPU hours and persist nothing, and the next scan would show the same counts. The
  classifier skips per-source work (no verdict could exist) and Analyze stays disabled. This is
  *narrower* than "no AI": a legitimately AI-disabled run uses the existing `no_ai` identity and
  classifies normally.
- **Scan is read-only with respect to cache *records*.** It creates the cache directory, because
  `_cache_path` always has; it never writes, updates or checkpoints a record. Do not "fix"
  `_cache_path`'s `os.makedirs` to make the claim tidier.
- **Scan classifies the whole library once; Analyze sends only `subset_for_analysis()`.** On the
  measured 902-source library that is 4 paths, not 902. Analyze then calls the *existing*
  `analyze_video_sources`, so all persistence stays with `_checkpoint_cache`; preparation never
  calls `_analyze_single_video`, `_checkpoint_cache` or `_save_cache` itself. Per-source staleness
  between the two clicks is deliberately unchecked — the analyzer re-derives each selected source's
  own identity anyway.
- **What invalidates a scan:** the folder, the recursive flag, and — rechecked at Analyze time — the
  backend token, the config token and the effective Qwen mode. That is the whole list: `config_token`
  covers the three Qwen env knobs, and after P2 there is nothing else in identity for a preparation
  to bind. The P V1 track check (path/size/`mtime_ns` plus the derived preset) is **retired**, and a
  test asserts `analyze_refusal` acquired no substitute for it. **Analyze batch size is deliberately
  not on that list** — see the bounded-batch section below.
- **Analyze takes the live preparation controls, not just `gr.State`.** Gradio delivers widget
  changes as separate queued events, so a user can retarget the folder or the recursive flag and click
  Analyze before the `change` handler has run — which would analyse the *previous* library's
  classification while the screen declared something else. `declaration_refusal` compares the live
  declaration against the recorded scan **first**, before the runtime identity is recomputed and
  before anything is analysed; a mismatch drops the scan and asks for a rescan rather than silently
  re-targeting it. It is practical equality only (normalised folder, exact `recursive`) and stats
  nothing. Never reduce `prep_analyze_btn`'s inputs back to `prep_state` alone; a test pins the click
  inputs against the handler's parameter order.
- **Strictly separate from the Create Video gate.** Its own `gr.State`, its own `prep_outputs`, and
  no overlap with `source_state` / `source_outputs` / `confirm_action` / `process_btn`. Local folder
  only: browser uploads live under `input/gradio_uploads/`, which `cleanup_on_startup` clears, so a
  path-keyed preparation of them would be worthless.
- **Nothing new in identity.** Preparation adds no field to any cache payload and no input to any
  key; a test asserts the identity and completion functions never mention it. (P2's `v2 → v3`
  contract bump is about the Qwen prompt, not about preparation.)
- **Progress:** a trackless scan has no Stages 1–4 to report, so it shows only Stage 0 under the
  `library_classify` phase; Analyze uses the existing Stage 5 events. Do not fake the removed stages.

#### Analyze submits one bounded batch

Scan still classifies the **whole** library — that is the P.1 rule and it is unchanged. What one
Analyze click submits is bounded by `library_prep.DEFAULT_ANALYZE_BATCH_SIZE` (**100**), exposed as an
`Analyze batch size` number box and taken as a prefix of the already-frozen
`subset_for_analysis()` ordering via `subset_for_analysis_batch(limit)`.

**The reason is a property of Stage 5's shared worker, not a deficiency of the cache.** Stage 5
batches multiple videos into one Qwen worker process, and that worker writes its response JSON only
after its **entire** job loop finishes — so the parent can checkpoint individual completed records
only once the whole batch returns (this is the same D1 boundary already documented above: "while the
shared worker is still in flight its per-job results are not durable at all"). Submitting a cold
1107-source library therefore exposed all of it to a single all-or-nothing worker invocation.
Bounding the submission bounds that exposure.

Two claims this explicitly does **not** make:

- **It does not make an in-flight worker resumable.** If a batch's worker dies before producing its
  response, that batch may still need repeating; the bound only limits what that costs. The worker
  was **not** modified — incremental worker responses remain possible future work, and a test asserts
  no such machinery was invented here.
- **It is not a library cap.** A 5000-source scan still reports 5000 needing analysis; the batch size
  only decides how many of them one click submits. `normalize_batch_size` therefore has **no upper
  bound** and the `gr.Number` carries no `maximum`. Tests pin both.

Load-bearing details:

- **Batch size is execution policy, never classification identity.** It is absent from
  `LivePrepDeclaration`, from `PrepScanResult` and from `RuntimeIdentity`, so changing it never
  invalidates a scan: `set_batch_size` is the one preparation transition that does **not** route
  through `_invalidated`, and the retuned state keeps the *same* scan object. Folder and recursive
  still invalidate. That asymmetry is the whole design — a scan valid at 100 is exactly as valid at
  50, and re-fingerprinting 1107 sources to act on a different bound would be pure waste.
- **Keeping the scan is not the same as keeping its rendered text.** The report quotes the batch size
  (`Analyze batch: 100 per run`, `This run submits the next 100`), so `set_batch_size` re-renders it
  from the scan already in hand; otherwise the screen contradicted itself — widget 50, button
  "Analyze next 50", report still claiming 100. That was only ever a *reporting* bug, since the
  handler always used the live value, but a report disagreeing with the button is what a user
  believes. Re-rendering reads only counts already recorded in the scan: no filesystem access, no
  classification, no identity probe. With **no** scan recorded the existing text is preserved
  verbatim, because it is then the intro, a failure message or a finished batch's summary — none of
  which a batch-size change may overwrite.
- **Analyze reads the live widget, Scan does not receive it at all.** `prep_analyze_btn` inputs are
  `[prep_folder, prep_recursive, prep_batch_size, prep_state]` and a test pins that against
  `_on_prep_analyze_click`'s parameter order; `prep_scan_btn` keeps `[prep_folder, prep_recursive,
  prep_state]`, because how much of a classification one click later consumes cannot affect how any
  source was classified. A live batch size differing from the stored one is **not** a stale scan and
  must never be refused as one.
- **`normalize_batch_size` mirrors `variation.normalize_seed`.** `bool` rejected first (it subclasses
  `int`), whole positive ints and whole positive floats accepted, plain decimal strings accepted, and
  **fractional values fall back to the default rather than being floored** — `100.5` is not a request
  for 100, and truncating would submit a batch the user never chose. Nothing here may raise mid-run.
- **After a batch the scan is dropped, exactly as before.** Its counts describe the library as it was
  before the run. The summary reports `Submitted this batch` and `Sources analyzed this batch` and
  then asks for a re-scan; it **never** prints `old outstanding − N` as a remaining count, because the
  library can also change on disk between clicks. A test asserts `summarize_analysis_run` contains no
  subtraction at all. Preparation never silently re-scans 1107 sources on the user's behalf.
- **No cache change whatsoever.** `CACHE_CONTRACT_VERSION` and `ANALYSIS_VERSION` are untouched, no
  cache payload gained a field, and `video_analysis.py` was **not modified by this feature at all** —
  a test asserts the batch names appear nowhere in it. Persistence stays entirely with the existing
  `analyze_video_sources` → `_checkpoint_cache` path; bounding the input adds no write path.

### Persisted Stage-5 semantics are media-neutral (P2)

**This is an architectural boundary, not an optimisation:**

```
STAGE 5                      = INTRINSIC MEDIA TRUTH      (persistent)
STAGE 6 / FUTURE DIRECTOR    = CREATIVE INTERPRETATION    (ephemeral, per render)
```

Stage 5 records what is visually present — motion, character focus, visual quality, beauty, action
intensity, tension/softness, semantic content. It must **not** answer "what should I do with this shot
for this particular song?" That interpretation belongs downstream, and it may vary per render, per
section and per seed without invalidating or rewriting a single cache record.

A source therefore needs one semantic analysis per compatible **source identity** + **Qwen backend
identity** + **result-affecting media-semantic Qwen configuration** — not one per `smart_preset`.

**The prompt is the whole mechanism.** `stage5_qwen_scene_worker._build_prompt()` takes no arguments
and carries the validated instruction *"Assess the moment only from what is visually present; do not
adapt the tags to music, song energy, edit style, or desired pacing."* The old
`"The music edit style is {style_hint}."` conditioning, `_qwen_prompt_style_hint` and the
`smart_preset` component of `_qwen_config_token` are all gone. Do **not** replace the removed hint
with a fake constant style: after P2 there is no edit style in persisted Stage-5 semantics at all.

Load-bearing details:

- **Nothing about the music crosses the worker boundary.** Neither request JSON (single or batch)
  carries `audio_profile`; `_build_prompt` was its only consumer. Tests execute the real
  request-building bodies and assert the exact key sets.
- **`audio_profile` survives on `analyze_video_sources` as a retained integration signature with zero
  effect.** `auto_mode.analyze_beats_auto` still passes it, so that call site needed no change, but it
  reaches no cache key, no Qwen request, no prompt and no persisted record — a test asserts the
  parameter is absent from the function body, and that two arbitrarily different profiles produce the
  identical cache path. Every *private* seam that existed only to forward it
  (`_analyze_single_video`, `_annotate_candidates_with_qwen`, `_complete_deferred_qwen`,
  `_complete_deferred_qwen_batch`, `_run_qwen_worker`, `_run_qwen_worker_batch`,
  `_video_signature`, `_cache_path`, `classify_library_sources`) lost the parameter outright. Do not
  find it a new use.
- **The three media-semantic settings still re-key.** `BEATSYNC_QWEN_MAX_WINDOWS`, `_FRAME_WIDTH` and
  `_MAX_NEW_TOKENS` change how many candidates get semantics, what the model sees and whether the JSON
  truncates, so they stay in `_qwen_config_token()`. Runtime-only knobs stay out.
- **`stage5_cache_v2 → stage5_cache_v3`, and the cold rebuild is intentional.** A v3 record *means*
  something different from a v2 one, so the one generation constant was bumped. There is deliberately
  **no migration**: v2 filenames are never produced or looked up again, nothing reuses their Qwen
  semantics, and the old files are left on disk untouched by the retention policy — no compatibility
  loader, no rewriter, no automatic deletion. `ANALYSIS_VERSION` is unchanged, because deterministic
  candidate scoring, window building and the candidate schema are.
- **The schema was deliberately *not* redesigned.** The real-material A/B validated the *existing*
  schema under a media-neutral prompt, so `recommended_use`, `emotion`, all eight numeric fields and
  the description contract are untouched; changing them here would have invalidated that evidence and
  widened the rebuild boundary. Under P2, read `recommended_use` as a media-neutral suggestion from the
  model, **not** an instruction tied to the current song — a future planner may weight it, ignore it or
  reinterpret it without touching the cache. Schema evolution is separate, later work.
- **The evidence.** Validated on the user's real material (`J:\New folder`): 23 analysed sources, 17
  source groups, 10,913 deterministic candidates, 115 identical A/B semantic moments; 115/115
  decoded/tagged with 0 failures on both sides; post-`_merge_semantic` mean score differences
  ≤ ~0.023; planner seeds 0/101/202 with 0 fallbacks and near-identical editorial scores; blind human
  review of 40 frames giving B a 10/16 decisive win rate with fewer editorial-leak (4 → 1) and
  false-action (2 → 1) flags. The reading is *not* "B is dramatically better" — it is that
  media-neutral semantics are not materially worse on real content, remain equally usable by Stage 6,
  and stop music/edit intent leaking into persisted media semantics. The earlier hockey human-scoring
  experiment is **superseded** and must not be cited as implementation evidence.
- **Normal Create Video is unchanged and still music-aware.** Stages 1–4 still run, Stage 6 still
  receives `beat_info`, sections, energy, targets, the creative seed and the candidate tags/scores, and
  its scoring and planning logic were not touched. The only difference is that Stage-5 semantics no
  longer change because Stage 1–4 resolved a different edit style.
- **Future creative modes must not undo this.** Neutral / music-aware / hybrid / freestyle / AI
  Director interpretation, section-specific weighting, a Master Seed, or an optional second style-aware
  pass over a *small shortlisted* candidate set are all legitimate future work — **none of it is
  implemented here**, and no speculative abstraction was added for it (a test asserts no
  director/freestyle/shortlist machinery exists). The permanent constraints are: creative state never
  enters Stage-5 cache identity; no per-render interpretation overwrites persistent semantics; and a
  future second pass stays run-scoped rather than becoming the library's durable truth.

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

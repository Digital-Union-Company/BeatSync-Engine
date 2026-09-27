# Fork modification log

This file records the modifications this fork makes to the upstream project, as required by
**AGPL-3.0 §5(a)** ("cause the modified work to carry prominent notices stating that you modified it,
and giving a relevant date").

| | |
|---|---|
| **Fork** | [Digital-Union-Company/BeatSync-Engine](https://github.com/Digital-Union-Company/BeatSync-Engine) |
| **Upstream** | [Merserk/BeatSync-Engine](https://github.com/Merserk/BeatSync-Engine) |
| **Upstream baseline** | `06679c1f28c6d0b56c901495e52497cccebc744a` |
| **Fork modifications began** | 2026-09-26 |
| **Licence** | GNU Affero General Public License v3.0 (unchanged from upstream) |

Fork-specific code lives in `src/beatsync_fork/`. Upstream modules are touched only at small call sites
marked `# [FORK]`, so upstream synchronisation stays straightforward. Upstream Auto Mode creative
behaviour is preserved as the default.

---

## Unreleased

### Changed — 2026-09-27 (Phase 3C: NVENC clips decode in software)

The NVENC extraction path no longer asks FFmpeg for CUDA input decoding. **`h264_nvenc` still does the
encoding** — only the input-side `-hwaccel cuda` request is gone, so the architecture is now software
decode → the existing CPU filter chain → NVENC encode. The CPU-encode branch keeps its `-hwaccel auto`
and is untouched.

A read-only decode-path study measured three real paths on RTX 3080 / driver 617.14 over 5 real sources
× 4 deterministic windows (2.0 s, 1280×720, 30 fps, 60 frames per clip), in balanced interleaved order:

- **`-hwaccel cuda` was not actually decoding on hardware for most sources.** It failed to initialise on
  48 of 60 clips (33 requested nvdec decode surfaces against a limit of 32) and FFmpeg fell back to
  software decode anyway — the warning was the only thing it reliably produced.
- **Where real NVDEC *did* engage, it was slower.** On the one sampled source whose surface count fits,
  hardware decode was positively confirmed from FFmpeg's own verbose decoder log and ran ~20 % slower
  than software decode (0.530 s vs 0.436 s per clip), because this CPU filter chain
  (`trim,setpts,scale,fps`) has to pull the frames back to system memory.
- **Genuine CUDA decode is achievable but still not worth it.** `-hwaccel cuda -threads 8` produced
  confirmed hardware decode on all 5 sources with zero failures, exact frame counts and byte-identical
  output — and was slower than plain software decode on 20 of 20 clips. (`-threads 16` and
  `-extra_hw_frames 0` did not resolve the surface count at all.) It is not adopted.
- **Output is unchanged, byte for byte.** All 20 A/B pairs produced identical output *files* — not
  merely equal frame counts or equivalent content — and each variant was deterministic across
  repetitions. The post-change smoke reproduces the study's Variant-B files exactly: 5/5 SHA-256 match.
- **Modest, consistent speedup.** Sequential median 0.434 s vs 0.449 s (faster on 18/20 clips, and in
  every balanced repetition); at the real 4-worker NVENC cap, 12.17 s vs 13.46 s wall for 40 clips
  (3.286 vs 2.972 clips/s), with 0 decode-init failures instead of 32 and ~500 MiB lower peak VRAM.

The FFmpeg argv delta is exactly the removal of the two tokens `-hwaccel cuda`, proven by diffing the
argv the production function really constructs, before and after, with every other token in the same
order. No filter graph, encoder, quality-argument, frame-lock, worker-policy, resolution or FPS change;
`get_nvenc_quality_args`, `get_cpu_h264_quality_args`, `seconds_to_frame_count`,
`frame_count_to_seconds`, `build_frame_aligned_cut_timeline` and `_effective_clip_workers` are all
AST-identical to the base commit, and `video_processor.py`, the Phase 3B diagnostic module,
`ANALYSIS_VERSION` and the analysis-cache identity are unmodified.

The Phase 3B regression fixture that carries the old `cuvidCreateDecoder` / decode-surface warning text
is **deliberately retained**. Phase 3C removes the production trigger, not the requirement that the
diagnostic selector keep telling a recovered warning apart from the fatal encoder cause.

### Fixed — 2026-09-27 (Phase 3B: Stage 6 FFmpeg failure diagnostics)

A Stage 6 render of 701 sources lost every clip and reported `283 clip(s) failed; refusing to
concatenate an incomplete timeline`. FFmpeg had already said exactly why —
`Driver does not support the required nvenc API version. Required: 13.1 Found: 13.0` — but
`extract_clip_segment_ffmpeg()` printed that to stdout and returned a bare `False`, `create_clip_parallel`
turned the `False` into the fixed string `"FFmpeg extraction failed"`, and the GUI redirects stdout into
`QuietConsole`. Identifying the cause took a dedicated forensic phase. This change makes the reason
travel with the failure. Diagnostics only: no encoder, command, timing or render behaviour changed.

- **New fork module `src/beatsync_fork/ffmpeg_diagnostics.py`** (stdlib-only). `summarize_ffmpeg_failure`
  ranks stderr lines and returns one bounded line; `describe_output_problem` and `describe_exception`
  cover the cases where there is no stderr worth quoting. Vendor-neutral by construction: the marker
  tuples contain no NVIDIA/NVENC/CUDA-specific tokens and no incident-specific values such as `13.1` or
  `610.00`, and a test asserts exactly that token set. Generic diagnostic terms stay intentionally
  allowed — `_SPECIFIC_MARKERS` does include `"driver"`, which matches any vendor — so the next failure
  family benefits too.
- **`extract_clip_segment_ffmpeg()` keeps its `-> bool` signature** and becomes a thin delegate to the
  new `extract_clip_segment_ffmpeg_detailed() -> Tuple[bool, str]`. The call graph shows exactly one
  in-repo caller, but the boolean function is a module-level API in an upstream file, so it was left
  compatible rather than converted. The FFmpeg command construction is **byte-identical** to the base
  commit (all 17 `cmd`/`filters`/frame-arithmetic statements compare equal by AST).
- **`create_clip_parallel` reports `f"FFmpeg extraction failed: {reason}"`**, keeping the historical
  wording as a prefix so existing expectations still match. Everything downstream already worked:
  `clip_failures` → Stage 6 warning → `first_failures[:3]` on the refusal → `ProgressView`. No GUI, no
  `progress.py` and no `ProgressView` change was required; `gui.py` is untouched.
- **A successful clip stays successful.** The summariser is only reached inside the
  `returncode != 0` branch, asserted by a seam test. This mattered concretely *at the time of Phase 3B*,
  when the NVENC path still requested `-hwaccel cuda`: on driver 617.14 every successful NVENC clip
  emitted `cuvidCreateDecoder … CUDA_ERROR_INVALID_VALUE` / `more than 32 (33) decode surfaces` while
  FFmpeg fell back to software decode, and the validated 150-clip render would otherwise have acquired
  150 spurious "reasons". (Phase 3C later removed that request, so current renders no longer emit it —
  the rule itself is unchanged and still load-bearing.)
- **Bounded**: 240 characters, one line, control characters stripped, heap addresses collapsed so the
  same failure yields a reproducible string. A 1 MB stderr produced a 61-character reason in test. The
  full text still reaches the console through the pre-existing print.
- **The complete-timeline refusal is untouched.** Measured against real `create_music_video` with two
  clips forced to fail out of nine: `RuntimeError: 2 clip(s) failed; refusing to concatenate an
  incomplete timeline.`, no output file written, `failed_clips=2 total_clips=9`, two warning events and
  two `first_failures` entries each carrying the decisive driver/API text within 215 characters.

One defect the fixture caught before commit: the first selector took the *earliest* diagnostic line,
which in the real capture is the recovered nvdec warning four lines above the fatal encoder error. The
selector now anchors on FFmpeg's consequence lines and picks the specific lines nearest that boundary.

Verified on the current environment (RTX 3080, driver 617.14): real NVENC extraction returns `True` with
2,964,751 bytes and exactly 60 frames, its detailed reason empty; real CPU H.264 extraction returns
`True` with 11,944,372 bytes and exactly 60 frames. `ANALYSIS_VERSION`, cache identity, the frame-lock
timeline builder, NVENC/CPU quality arguments, `_effective_clip_workers`, `-hwaccel cuda` and
`logger.check_nvenc()` are all unchanged; 21 protected files are blob-identical to the base commit.

### Added — 2026-09-26 (Phase 2B: live Qwen worker progress)

Closes the last observability hole in the pipeline: the parent → Python-worker subprocess boundary.
Observability only — no creative, inference, scoring, cache-identity or render behaviour changed.

- **New fork module `src/beatsync_fork/qwen_progress.py`** (stdlib-only, like the rest of the package).
  Three pieces: a namespaced one-line JSON wire protocol (`encode`/`decode`/`is_protocol_line`), a
  `QwenProgressTranslator` that folds payloads into Stage 5 `phase="qwen"` events, and
  `stream_worker_process()` / `run_qwen_worker()`, the `Popen`-based runner.
- **The worker emits machine-readable progress** (`src/auto_mode/stage5_qwen_scene_worker.py`).
  `BEATSYNC_QWEN_PROGRESS\t{…}` lines for `worker_state` (`loading_model`, `backend_ready`,
  `prefetch`), `job_start`, `job_progress` and `job_end`, **additive** to the existing human-readable
  lines, which are byte-for-byte unchanged. Emission goes through a guarded `_emit_progress()` that
  swallows every failure — including a missing fork package — because a status line must never cost a
  run that has already spent GPU minutes. The only other change to this file is an optional
  `job_context` parameter on `_run_semantics_for_video()` carrying
  `job_index`/`job_total`/`job_id`/`source_name` so emitted lines can be attributed. Prompt, schema,
  vocabularies, device/slot/context selection, frame prefetch, inference waves and the
  `LlamaServerClient`/`LlamaMtmdClient`/`QwenLlamaClient` classes are all AST-identical to the base
  commit.
- **The parent streams instead of capturing** (`src/video_analysis.py`).
  `_run_qwen_worker_batch()` and `_run_qwen_worker()` now call `fork_qwen.run_qwen_worker()` with the
  same argv, environment, UTF-8-with-replacement decoding, timeout and `{}`-on-failure contract they
  had with `subprocess.run(capture_output=True)`. `event_callback` is threaded through
  `analyze_video_sources` → `_analyze_single_video` / `_complete_deferred_qwen` /
  `_complete_deferred_qwen_batch` → `_annotate_candidates_with_qwen` → both worker launches, so live
  progress works in **both** execution modes rather than silently only in batched runs.
- **stdout and stderr are drained concurrently** on two threads while the main thread owns
  `wait(timeout=…)`. Required, not decorative: a failing llama.cpp run emits megabytes of Vulkan
  diagnostics, and draining stderr only after `wait()` deadlocks once the pipe buffer fills. stderr is
  retained as a bounded tail (2400 chars batch / 1800 single — the limits the old code already
  printed), so RAM stays flat however loudly the worker fails.
- **Reported truthfully.** Per-job progress is an *uncounted* `STATE` event: the only provable
  denominator is the current job's `len(frame_items)`, so the panel shows
  `Qwen job 17 / 300 · 64 / 120 candidates (53.3%) · 2.1 candidates/s · batch 8` and never invents a
  cross-job total. The rate is the worker's own measured `idx / elapsed`, labelled **`candidates/s`**.
  Staying uncounted also keeps `· 758 sources completed` visible and avoids fighting `ProgressView`'s
  per-`(stage, phase)` monotonicity, which would otherwise reject job 18's restart at `1 / 130` as a
  stale straggler.
- **The response JSON is still the only semantic authority.** Nothing is reconstructed from stdout; a
  test asserts the streaming path returns exactly what the old capture path returned for identical
  response JSON, and that results are identical with and without a progress callback. Request/response
  files remain retained per the project's PCBUS-HK-v1 override (the `finally:` blocks are still bare
  `pass`).
- **Scope of `Popen`.** `stage5_qwen_scene_worker.py` already used `subprocess.Popen` for its internal
  `llama-server` lifecycle long before Phase 2B, handing it dedicated log-file handles rather than the
  worker's pipes; that code is untouched and a test pins it to exactly one `Popen` inside
  `LlamaServerClient._start`. The short `llama-mtmd-cli --version` probe remains a plain
  `subprocess.run`. A repo-wide "no `Popen`" invariant would be both false and about the wrong
  boundary.
- **Measured, not assumed.** Red evidence first: under `capture_output=True` a worker emitting its
  first progress line at ~0s delivered **0 callbacks before exit**, first observed at 0.857s — the exit
  timestamp. After the change, the real worker with the real Qwen3VL-2B GGUF on Vulkan reported
  `loading model` at +0.17s and `backend ready` at +8.98s of a 10.38s run. Timeout process-tree
  boundary was measured both ways: the direct Python worker is killed and an already-started
  `llama-server` is **orphaned**, identically to the pre-Phase-2B path, because both kill only the
  direct child. That gap is pre-existing and was deliberately not "fixed" with `taskkill /T`.

Two defects found by the new tests and fixed before commit: a job's first `job_progress` was being
swallowed by the throttle window (the update that replaces "starting" with a real count), and closing a
pipe from the waiting thread blocked on the reader's buffer lock, turning a 2s timeout into a 120s
return whenever a grandchild held the write end — reader threads now own their own close and are joined
against one shared deadline.

`ANALYSIS_VERSION` is unchanged (`auto_av_analysis_v8_llama_vulkan_batched`): a progress-only change must
not invalidate a 758-video analysis cache. `tests/test_no_runtime_dependency.py` now discovers fork
modules from disk instead of a hardcoded list, so a future module cannot escape the stdlib-only guard.

### Fixed — 2026-09-26 (Phase 2A review remediation, round 3)

- **`ProgressEvent.data` is now immutable *recursively*** (`src/beatsync_fork/progress.py`). R2's
  `MappingProxyType` was shallow, and the pipeline genuinely emits nested mutable values —
  `section_types=[...]` from Stage 3, `first_failures=[...]` from the Stage 6 refusal — so
  `event.data["section_types"].append("evil")` still succeeded, and `as_dict()`'s shallow copy handed
  the *same* nested objects to every consumer. Small stdlib-only `_freeze`/`_thaw` pair: mappings
  become `MappingProxyType` over recursively frozen copies, lists and tuples become tuples, scalars
  (including `str`/`bytes`) are untouched. `as_dict()` recursively thaws back to plain
  `dict`/`list`, so the payload shares no mutable container with the event and stays JSON
  serialisable. Only the container types the pipeline actually emits are handled; no serialization
  framework was added.
- **Stage 5 no longer bills cache hits as analysis throughput** (`progress.py`,
  `src/video_analysis.py`). `StageCounter.rate` was `current / elapsed`, and the source counter
  advances for cache hits too, so 420 instant cache hits plus one slow real analysis reported
  **839.9 sources/s** in the reproduction. The rate basis is now tracked separately from the
  completion count: `advance(..., counts_toward_rate=False)` records a cache hit without billing it,
  and `begin_rate_window()` re-bases the clock once the cache scan finishes. The count still reads
  `421 / 758` with cache hits visible; the rate now reads `0.1 analyzed sources/s`, labelled via a
  `rate_unit` so a bare `sources/s` cannot be misread next to a count that includes cache hits.
  **Stage 6 clip throughput is unchanged** — it opens no rate window, so its basis is the whole
  counter exactly as before. No ETA was added.
- **A stale straggling progress event no longer wipes a good measurement** (`progress_view.py`).
  Monotonicity protected `current` but `rate`/`elapsed`/`message` were overwritten unconditionally, so
  an out-of-order Stage 6 event carrying no rate silently degraded
  `612 / 1216 · 4.8 clips/s · elapsed 2m 07s` to `612 / 1216`. An event ignored for the count is now
  ignored for those fields too, and a known measurement is never replaced by `None`. Found by the
  portable UI smoke, not by the unit tests.
- **28 new tests** (`tests/test_progress_truth.py`), 16 of which were confirmed failing against
  `95150de` first, including AST/source assertions that `video_analysis.py` really opts cache hits out
  of the rate, really re-bases the window, and really labels the rate unit.

### Fixed — 2026-09-26 (Phase 2A review remediation)

- **Progress counters are now subphase-aware** (`src/beatsync_fork/progress_view.py`,
  `src/video_analysis.py`). Monotonicity was enforced per *stage*, but Stage 6's ProRes path contains
  two counted subphases with different units and denominators — conversion counts sources, extraction
  counts clips — so the conversion count leaked into extraction. Reproduced before the fix:
  758 sources followed by 100 segments rendered **`758 / 100 (758.0%)`**; with 1216 segments,
  extraction appeared to begin **62.3% complete**. The monotonicity key is now `(stage, phase)`, taken
  from the `data["phase"]` marker the pipeline already emitted. Nothing carries across a phase
  boundary — not the count, total, unit, rate or elapsed time — while monotonicity *within* each phase
  is unchanged, so a straggling `4 / 10` after `7 / 10` still shows `7 / 10`.
- **No fake percentage for uncounted phases.** `Final assembly started` previously still displayed
  `1216 / 1216 (100.0%)` as though the assembly itself were complete; the Qwen phase likewise wore the
  deterministic pass's `758 / 758 (100.0%)`. An uncounted active phase now shows its own state plus a
  history line (`· 1216 clips completed`) instead of inheriting a counter. The Qwen state events carry
  `phase="qwen"` so the view can tell the two apart. **Qwen live N/T progress remains NOT implemented
  (Phase 2B); no `Popen` was introduced.**
- **`ProgressEvent.data` is genuinely read-only.** `frozen=True` only prevented field rebinding, so
  `event.data["x"] = ...` succeeded and an event handed to several consumers could be edited under the
  others. `data` is now a `MappingProxyType` over a private copy; `as_dict()` is built field by field
  because `dataclasses.asdict` deep-copies and cannot handle a mappingproxy. Stdlib only, no
  serialization framework.
- **23 new tests** (`tests/test_progress_phases.py` plus immutability regressions), including AST
  assertions that `video_processor.py` really emits `prores_convert` / `prores_extract` / `assembly`
  and `video_analysis.py` really emits `qwen`, so the suite cannot drift into testing an invented
  event shape.

### Added — 2026-09-26 (Phase 2A — structured progress / observability)

- **Structured progress core** (`src/beatsync_fork/progress.py`, `progress_view.py`): an immutable
  `ProgressEvent(stage, kind, message, current, total, elapsed_seconds, rate, data)` with a `Stage`
  enum, `EventKind` (start/progress/metric/state/warning/error/end), JSON-friendly `as_dict()`, and a
  `StageCounter` that is monotonic, bounded and throttled (~2 updates/sec, always emitting the final
  one). `emit()` swallows any callback exception — and a `None` event — so observability can never
  fail a render; `KeyboardInterrupt` still propagates. Stdlib-only, so the whole core is testable on a
  bare interpreter. `ProgressView` accumulates events into the status panel.
- **The GUI no longer parses prose for stage identity.** `gui.py` previously recovered the current
  stage with `re.search(r"Stage (\d+) is processing", message)`; that regex is gone. Stage identity is
  now the integer `event.stage`. The existing architecture is preserved — worker thread → `queue.Queue`
  → generator → widgets — and the `event_callback` only ever enqueues, so no Gradio component is
  touched from a worker thread (asserted by a test).
- **Stages 1-4** emit start/end boundaries with useful metrics (beats + tempo, features profiled,
  section count, cuts/beats/ratio/interval/preset). No ETAs: these stages take seconds, so a projection
  would be noise.
- **Stage 5 deterministic analysis** reports real source progress — `total` is the actual source count,
  **cache hits count as completed work** (a fully cached run shows completion rather than sitting at
  zero), and a video that fails in parallel and is retried serially advances the counter exactly once.
  Worker count and cache-hit count are reported as metrics.
- **Stage 6 rendering** reports `current / total` clips against the frame-locked segment count, with
  measured rate and elapsed. `current` advances only for clips actually created, individual failures
  surface as concise warnings (first three in detail, then a running count), and the
  incomplete-timeline refusal — unchanged in behaviour — is now visible in the UI instead of only on a
  discarded stdout.
- **ProRes** reports both of its serial loops: sources converted and segments extracted.
- **Final assembly** emits start/finish states and a concise error on failure. No fake percentage for a
  single FFmpeg call.
- **Backward compatible.** `progress_callback` and `console_callback` are untouched and
  `event_callback` is optional, so CLI/headless callers keep working. Legacy string statuses are still
  accepted on the same queue, but only shown before any structured event arrives, so they cannot
  overwrite richer output.
- **Qwen live progress is NOT implemented.** `video_analysis.py` still launches the worker with
  `subprocess.run(capture_output=True)`, so the worker's own `Qwen llama.cpp tagged N/T` lines remain
  invisible to the parent until it exits. Only honest high-level states are emitted
  (started / tags N/T / finished / failed). Live streaming is **Phase 2B**; no `Popen` was introduced.
- **100 new tests** (`test_progress_core.py`, `test_progress_sequence.py`, `test_gui_progress_seam.py`)
  plus the fork no-runtime-dependency guard extended to both new modules.

### Fixed — 2026-09-26 (Phase 1B review remediation)

- **The render gate now validates the LIVE source controls, not only the stored session state**
  (`src/beatsync_fork/input_confirmation.py`, `src/beatsync_fork/input_session.py`, `src/gui.py`).
  Gradio delivers widget changes as separate queued events, so at the moment Create is clicked the
  `gr.State` can lag behind the widgets. Reproduced before fixing: a browser confirmation of 2 files
  was **allowed** while the live `gr.File` value already held 3, because `current_snapshot_for_render`
  re-stat'ed `confirmed.paths` instead of the live list; likewise a confirmed folder was allowed while
  the folder textbox already pointed elsewhere. `process_video_guarded` now receives `source_mode`,
  `source_folder`, `source_recursive` and `video_input` as render-request inputs and builds a
  `LiveSourceDeclaration`; `check_declaration()` compares declared intent (mode / folder / recursive)
  before any filesystem work, so a folder the user has navigated away from is never even scanned. The
  event-driven invalidation is unchanged and still provides immediate UX feedback. Handler parameter
  names mirror the widget names because Gradio supplies them positionally, and a test asserts the two
  lists line up name-for-name.
- **Folder identity now covers the whole supported scope, not just the ready subset** (same modules).
  Reproduced before fixing: after confirming 1 ready `.mp4`, an external writer created a 0-byte
  `new.mp4`; the scanner rejected it as `empty_file`, the ready list was unchanged, and the gate
  **allowed** the render even though a new supported source entry had appeared. This is not
  hypothetical — the real `Cuts` library was observed gaining about one MP4 per minute. `SourceSnapshot`
  gained `excluded`, an ordered tuple of `ExcludedEntry(path, reason)` for supported-extension files the
  scan could not use, and it participates in the digest (`SNAPSHOT_VERSION` → v2). Only the stable
  reason code is recorded, never OS error text, which would make the digest unstable. Unsupported files
  (`.mp3`, `.txt`, `.jpg`) are deliberately excluded from identity, so adding one does not invalidate a
  confirmation. The render list remains the **ready** files only; no media is hashed.
- **35 new tests** (`tests/test_input_gate_live.py`, `tests/test_gui_guard_seam.py`): live browser list
  +1 / −1 / same-count replacement / reorder / empty / missing file, live folder / recursive / mode
  changes, "a folder outside the declaration is never scanned", new empty and new unreadable supported
  files, rejected↔ready transitions, unsupported additions ignored, and a dependency-injected seam test
  proving `process_video` is never reached after a denial.

### Added — 2026-09-26 (Phase 1B — GUI source modes + confirmation gate)

- **Local folder video-source mode** (`src/gui.py`, `src/ui_content.py`), the new default and the
  recommended mode for large libraries. It calls the existing `beatsync_fork.input_manager` — no
  duplicate enumeration logic in the UI — and shows exact
  `Discovered / Supported / Rejected / Ready / Duplicates / Total size`. Source files are referenced
  **in place**: no Gradio upload, no copies. Browser-files mode remains available and unchanged.
- **Authoritative confirmation gate.** `Create Music Video` now starts disabled and is enabled only by
  an explicit `Confirm N files` action.
- **`src/beatsync_fork/input_confirmation.py`**: `SourceSnapshot` — an ordered identity over
  (normalised path, size, mtime_ns) per file plus mode, scan root and recursive flag, digested with
  SHA-256. Confirmation is over the **set**, not the count, so two different lists of equal length are
  never interchangeable. File contents are not hashed: the head+tail fingerprint in `input_manager` is
  for duplicate candidacy and must not become a per-render cost. Also provides `describe_change()` for
  actionable abort messages and `evaluate_gate()`.
- **`src/beatsync_fork/input_session.py`**: the source state machine (`set_mode`, `set_folder_path`,
  `set_recursive`, `scan_folder_action`, `set_browser_files`, `confirm_action`) and
  `resolve_for_render()`. Pure, Gradio-free and stdlib-only, so the gate is fully testable without a
  web server.
- **Render-time re-verification.** `process_video_guarded()` in `gui.py` re-derives source identity
  from the filesystem on every click and refuses before Stage 1 if it no longer matches the
  confirmation. UI disablement alone is treated as a courtesy, not a guarantee.
- **Invalidation rules**: mode switch, folder path change, recursive toggle, re-scan, and any browser
  file-list change all clear the confirmation. FPS, encoder, output filename and audio deliberately do
  not — they are not source identity, and a test asserts the confirmation survives them.
- **Honest browser semantics.** Browser mode reports only `Backend ready: N`. The browser-side
  selected/pending count is never transmitted to Python, so it is never displayed; a test asserts no
  such number is printed.
- **64 new tests** (`tests/test_input_confirmation.py`, `tests/test_input_gate.py`) covering snapshot
  identity (added / removed / replaced-same-count / renamed / size / mtime / order / root / recursive /
  mode), empty-set refusal, and the full gate matrix. The fork no-runtime-dependency guard now covers
  the two new modules as well.

### Added — 2026-09-26

- **Repository workflow policy** (`CLAUDE.md`): `DU-REPO-WORKFLOW-v1` task-closeout rules.
- **Fork identity surface** (`src/beatsync_fork/__init__.py`): `FORK_NAME`, `FORK_VERSION`,
  `UPSTREAM_BASELINE_COMMIT`, `fork_identity()`.
- **Local input manager core** (`src/beatsync_fork/input_manager.py`,
  `src/beatsync_fork/input_report.py`): deterministic local-folder scanning with explicit
  discovered / supported / rejected / duplicate / ready accounting and cheap duplicate-candidate
  fingerprinting. **Not yet wired into the GUI** — see `CLAUDE.md`.
- **Test harness** (`pytest.ini`, `requirements-dev.txt`, `tests/`): first automated tests in the
  project, including a 1000-file regression test proving the input manager does not truncate large
  source lists.
- **`.gitignore`**: runtime/build artefacts (`bin/`, `input/`, `output/`, `__pycache__/`).

### Changed — 2026-09-26

- **`README.md`**: added a fork notice and a licence section. Upstream content is otherwise unmodified.

### Fixed — 2026-09-26

- **Input scan no longer silently skips an unscannable directory** (`src/beatsync_fork/input_manager.py`).
  `os.walk` ignores `scandir` failures unless an `onerror` callback is supplied, so an unreadable,
  vanished or disconnected nested directory was skipped in silence while `scan_folder()` still
  returned an `InputSet` that could report INPUT READY — the same silent-truncation failure the module
  exists to prevent, sourced from the filesystem instead of the browser. Traversal is now
  all-or-nothing: a complete walk returns an `InputSet`, and any directory that cannot be listed
  raises `InputScanError` naming the failing path and preserving the original `OSError` as `__cause__`.
  `followlinks=False` and all ordering, classification and accounting semantics are unchanged.
  Individual files that cannot be stat'ed remain `unreadable` rejections rather than scan failures,
  because a named rejection is not a silent loss.
- **Non-recursive scans no longer drop an entry whose metadata probe fails**
  (`src/beatsync_fork/input_manager.py`). The non-recursive iterator selected entries with
  `os.path.isfile()`, which *suppresses* stat/access errors and returns `False`. A locked, offline or
  vanished top-level source file therefore disappeared before the classifier saw it — absent from
  `ready`, absent from `rejected`, and missing from `discovered_count`, so even the
  `discovered == ready + rejected + path_collisions` invariant could not detect the loss. Reproduced:
  a three-file folder with one unreadable `.mp4` reported `discovered=2, ready=2, rejected=0` and
  INPUT READY. The iterator now decides only "directory or not" from the `scandir` entry and passes
  everything else to the classifier, which accounts for it as `unreadable`. This also removes a
  redundant `stat` per entry. Ordinary directories are still excluded; recursive behaviour,
  ordering, duplicate and collision semantics are unchanged. Side effect: a broken symlink at the top
  level is now reported as an `unreadable` rejection instead of vanishing.

### Removed — 2026-09-26

- Untracked the stale compiled artefact `src/auto_mode/__pycache__/stage5_qwen_scene_worker.cpython-313.pyc`
  from Git. The file was left on disk (it is regenerable, and removing files on disk is not this
  change's business).

### Unchanged

No change to generated video output. Auto Mode stages 1-6, `AutoWaveConfig` defaults, the analysis
cache format, seed/randomness behaviour, `gui.py` and the render path are untouched.

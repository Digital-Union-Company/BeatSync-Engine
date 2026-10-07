---
paths:
  - "src/beatsync_fork/progress.py"
  - "src/beatsync_fork/progress_view.py"
  - "src/beatsync_fork/qwen_progress.py"
  - "tests/test_progress_core.py"
  - "tests/test_progress_phases.py"
  - "tests/test_progress_sequence.py"
  - "tests/test_progress_truth.py"
  - "tests/test_qwen_progress_view.py"
  - "tests/test_gui_progress_seam.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

## Console vs. UI output — structured progress

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

**`process_video` owns its worker thread, and its generator lifetime IS that worker's lifetime.**
Everything after `thread.start()` is inside a `try/finally` whose finalizer joins the thread, so
closing the generator early does not return until the render has actually stopped. A UI or event
cancellation may stop *consumption*; it may not orphan a render worker behind a returned
generator, because the caller's render mutex is released the moment that generator returns. This
changes no `ProgressEvent`, no stage, no phase and no schema — it is purely about who is still
running when the stream ends.

**C3-R1A's Cancel is a different mechanism and adds no progress protocol.** It sets one flag that
the render thread reads at safe boundaries; it emits **no** `ProgressEvent`, adds no stage, no
phase, no counter and no ETA, and the Cancel handler's only output is the status line. Two things
follow that are easy to get wrong. A cancellation is **not narrated as a failure**: the two
`concatenate_videos_ffmpeg` call sites in `create_music_video` catch `RenderCancelled` and re-raise
it *before* the generic handler that would otherwise emit `error(6, "Final assembly failed: …")`,
because an event must describe what actually happened. And abandonment is still not cancellation —
`process_video`'s finalizer joins its worker with no timeout and no kill, exactly as above.

**The C3 batch prefixes; it never replaces.** The 2–4-candidate batch wrapper emits no
`ProgressEvent` of its own, adds no stage, no phase, no counter and no ETA, and parses nothing out of
the rendered text. It yields `f"Rendering candidate {position} / {request.count}"` — the count is the
user's own selection, 2 to 4 — then a blank line, and then today's `ProgressView` output verbatim, so
the panel reads exactly as it always has with one line of context above it. A status-only yield uses
`gr.skip()` for the video so a finished candidate's preview is never blanked by the next candidate's
progress, which also keeps an earlier success visible when a later candidate fails locally and the
batch carries on. Re-adding prose parsing at this seam would repeat the
mistake Phase 2A deleted, one layer up.

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

---
paths:
  - "src/gui.py"
  - "src/ui_content.py"
  - "tests/test_gui_guard_seam.py"
  - "tests/test_gui_progress_seam.py"
  - "tests/test_creative_controls_seam.py"
  - "tests/test_audio_layers_seam.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

# GUI integration seams

`src/gui.py` wires every subsystem together, so attaching all the subsystem rules to it would
reproduce the context explosion this layout exists to avoid. This file therefore carries **only the
cross-cutting GUI invariants** plus a routing table: when you touch a particular seam, open the named
subsystem rule. Each subsystem rule is scoped to its own implementation and **its seam test**, so
editing `tests/test_gui_guard_seam.py`, `tests/test_gui_progress_seam.py`,
`tests/test_creative_controls_seam.py` or `tests/test_audio_layers_seam.py` loads the right rule
automatically.

## Cross-cutting invariants

- **Preserve the import prologue.** `from logger import setup_environment; setup_environment()` runs
  before gradio/cupy/cv2 are imported, and the position of later imports is load-bearing. See
  `.claude/rules/pipeline-core.md`.
- **A plain `print()` inside the pipeline is invisible in the UI.** `gui.process_video` redirects
  stdout/stderr into `QuietConsole` (discarded). Emit a structured `ProgressEvent` instead.
- **Never touch a Gradio component from a worker thread.** `event_callback` only calls `queue.put`;
  the generator does all widget updates. A test asserts the callback's only method call is `put`.
- **Stage identity is `event.stage`, an integer.** The old
  `re.search(r"Stage (\d+) is processing", message)` recovery is gone from `gui.py` and must not come
  back; a test asserts its absence.
- **One render gate core, two mutex-owning wrappers, one render at a time (C3-R0).**
  `_process_video_guarded_unlocked()` is the single authoritative live-source-gate + render body;
  `process_video_guarded()` wraps it for a single render and `render_selected_variants_guarded()`
  for the two-candidate batch. Both acquire the **process-global `_RENDER_LOCK`** non-blockingly
  and refuse cleanly when it is held — `create_music_video` clears one process-global processing
  dir per render, so two overlapping renders would delete each other's in-flight clips. Both
  events also share one `concurrency_id` with `concurrency_limit=1`, but that is cooperative: the
  lock is the authority, because it is provable without Gradio.

  **The mutex protects the render WORKER's lifetime, not the Gradio generator frame (R1).**
  `process_video` starts a daemon thread; if its generator is abandoned — `close()`, a dropped
  event, an exception while draining — the frame unwinds immediately. Without a finalizer the
  worker kept running, the wrapper's `finally: _RENDER_LOCK.release()` ran anyway, and a second
  render could clear the processing dir out from under the first. So the chain is explicit and
  ordered: closing a wrapper closes the nested stream it **owns**, which closes `process_video`,
  whose finalizer **joins** the worker — and only then does the mutex release. Both wrappers own
  their nested stream (`render_stream` / `candidate_stream`) and close it in a `finally` nested
  *inside* the lock-holding `try`, so the ordering is structural rather than incidental. No
  worker is ever terminated and there is no timeout: with no cancellation, abandoning a stream
  means waiting for the render in flight. The lock is a plain non-reentrant
  `Lock` on purpose — a batch that re-entered the single-render wrapper would refuse itself on its
  own first candidate, and an `RLock` would hide that instead of exposing it. Never wire an event
  directly to the core, and never let a wrapper rebuild the gate.
- **Diagnostics the batch reads rather than writes.** C3-R0 records each candidate's durable
  output in `session_state[LAST_OUTPUT_PATH_KEY]` — cleared before every attempt, set only after
  the promotion into `output/` succeeds, and never the ProRes preview — and reads the two report
  keys after each candidate. It becomes a writer of **neither** report panel; the batch summary
  owns multi-render diagnostics so no panel can describe a candidate the user is not looking at.
- **One universal durable-output policy: no GUI render may ever replace an existing file (H1).**
  There is exactly one promotion into `output/`, `_promote_output_no_replace()`, and it is a single
  no-replace `os.rename` — not `shutil.move`, not `os.replace`, and not an `os.path.exists` check
  followed by a move. The check *is* the operation, so nothing can appear in a window between
  them. Ordinary Create Music Video and the C3-R0 batch get the identical guarantee: C3-R0's
  keyword-only `refuse_existing_output` is **gone**, and no boolean may take its place — a
  per-caller overwrite policy is how the single render stayed destructive while the batch was
  safe. The exact final path is also checked once before `analyze_beats_auto`, purely so a doomed
  render costs no analysis; that check is a courtesy and the rename is the authority. Every
  failure is **fail-closed**: the destination is never deleted to make room, the new render stays
  in `session_dir` and is named in the message, `LAST_OUTPUT_PATH_KEY` stays empty, and a ProRes
  preview is not generated at all. A cross-volume destination (`errno.EXDEV`) refuses rather than
  copying — a copy is not an atomic promotion, and an interrupted one leaves a partial video
  looking like a finished render. See `.claude/rules/pipeline-core.md` for why this rests on
  Windows-specific `os.rename` semantics.
- **The gate validates the **live** source controls**
  (`source_mode`, `source_folder`, `source_recursive`, `video_input`), not just `gr.State`. Gradio
  delivers widget changes as separate queued events, so the state can lag the widgets at click time.
  **Never reduce this handler's inputs back to `source_state` alone.**
- **Gradio passes handler arguments positionally**, so every click `inputs` list must stay aligned
  with its handler's parameter order. `tests/test_gui_guard_seam.py` and
  `tests/test_creative_controls_seam.py` pin those alignments; a silent reordering fails the suite.
- **Source identity vs. render request.** Non-source settings — FPS, encoder, output filename, audio,
  the seven creative controls, the Variant Lab widgets, the audio/SFX controls — must never be wired
  into a source or preparation handler, into `source_outputs` or into `prep_outputs`, so changing one
  cannot clear a confirmation, disable Create Video or trigger a scan.
- **Media Library Preparation is strictly separate from the Create Video gate**: its own `gr.State`,
  its own `prep_outputs`, no overlap with `source_state` / `source_outputs` / `confirm_action` /
  `process_btn`.
- **Diagnostic read-out panels have exactly one writer, `process_btn.click`**, and are cleared at the
  start of every attempt and before the gate — so no voice, a refused render, a preflight failure and
  a mixdown failure all leave them blank rather than showing the previous render's result.
- **Configuration widgets are unwritable, with exactly three audited exceptions.** Since E2 V1,
  `music_under_voice`, `sfx_amount` and `sfx_level` may be written — and since C3 V1 the permitted
  writer list is `generate_variant_btn.click`, `new_variant_btn.click` and
  `apply_variant_btn.click`, and nothing else. The six creative sliders add `creative_preset.input`
  to those three. Every other audio/SFX configuration widget still has **zero** writers. Split seam
  guards pin the whole matrix by exact writer list; extend it under review, never relax one to make
  a new handler fit.
- **Generating is not applying.** `generate_variants_btn.click` (C3 V1) writes **no** execution
  widget — only the batch state, the comparison table, the selector, the status and the root master
  seed. That absence is load-bearing: it is what makes candidate chaining structurally impossible.
  Only `apply_variant_btn.click` writes execution widgets, and only after re-deriving the live
  declaration and requiring it to equal the one its batch was generated from.
- **The asyncio Proactor patch that swallows benign `WinError 10054` pipe resets is intentional**, not
  dead code.

## Routing table

| Seam you are editing | Open |
|---|---|
| progress panel, `ProgressView`, event plumbing, Qwen live progress | `.claude/rules/progress-events.md` |
| Video Source block, scan/confirm/gate, the shared gate core | `.claude/rules/input-gate.md` |
| C3-R0 render-two-candidates seam, render mutex, batch summary | `.claude/rules/variant-lab.md` **+** `.claude/rules/input-gate.md` **+** `.claude/rules/pipeline-core.md` |
| the six creative sliders, Variation Seed, Randomize | `.claude/rules/creative-controls.md` |
| the Creative Preset selector and its `.input()` graph | `.claude/rules/creative-presets.md` |
| Variant Lab **visual** widgets, master seed, Spread, Generate handlers | `.claude/rules/variant-lab.md` |
| Variant Lab **audio** subsection, or any of its three audio outputs (E2) | `.claude/rules/variant-lab.md` **+** `.claude/rules/audio-mixdown.md` |
| Variant Lab **comparison / Apply Selected** seam, candidate count, batch state (C3) | `.claude/rules/variant-lab.md` **+** `.claude/rules/creative-presets.md` **+** `.claude/rules/audio-mixdown.md` — Apply writes both creative and audio widgets, so all three writer matrices apply |
| voice clips, Smart Mix folder/roles/Amount/Level, the report panels | `.claude/rules/audio-mixdown.md` |
| Media Library Preparation scan/analyze, batch size | `.claude/rules/library-preparation.md` |
| Stage 0 / scan / planner timings surfaced in the UI | `.claude/rules/scale-diagnostics.md` |
| render modes, encoder branches, frame lock | `.claude/rules/pipeline-core.md` |

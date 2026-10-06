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
  worker is ever terminated and there is no timeout: **abandoning a stream means waiting for the
  render in flight**, and that stays true under C3-R1A — abandonment is not an explicit Cancel, and
  a finalizer that cancelled would turn every dropped Gradio event into a Stop nobody pressed. The
  lock is a plain non-reentrant
  `Lock` on purpose — a batch that re-entered the single-render wrapper would refuse itself on its
  own first candidate, and an `RLock` would hide that instead of exposing it. Never wire an event
  directly to the core, and never let a wrapper rebuild the gate.

  **The wrappers also own the render lifecycle (C3-R1A).** Each constructs exactly **one**
  `RenderLifecycle` per top-level render event — the batch's one spans *both* candidates — installs
  it in the capacity-one active-render slot, publishes its `invocation_id` in a control-plane-only
  yield **before** the gate, and marks the lifecycle terminal exactly once after everything it ran.
  Terminal-marking never happens inside `process_video.worker()`, which the batch calls once per
  candidate against that shared lifecycle. Both wrappers' yields gained one trailing element, the
  invocation id, and `process_video` keeps its 3-value contract untouched. The Cancel button is a
  **third** event that is deliberately not a render: its own concurrency lane, no `_RENDER_LOCK`, no
  `session_state`, no report widget, and only a plain string in `gr.State`. Full contract:
  `.claude/rules/variant-lab.md` (C3-R1A section).
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

  **AI Director V1 repeats that split exactly**, and the symmetry is deliberate rather than
  stylistic:

  ```
  generate_director_btn.click  ->  director_proposal_state, director_proposal, director_status
                                   and NOTHING else — no execution widget at all
  apply_director_btn.click     ->  variation_seed, the six sliders, creative_preset,
                                   director_status — and nothing else
  ```

  `generate_director_btn.click` is absent from every writer matrix (sliders, Variation Seed,
  `creative_preset`), exactly as `generate_variants_btn.click` is. `apply_director_btn.click` is
  the **one** Director execution-widget writer, and each of those three matrices was extended by
  precisely that one entry — by exact list, never relaxed to containment.
- **`director_proposal_state` has exactly one reader**, `apply_director_btn.click`, pinned as an
  exact list (the same contract `variant_batch_state` carries, which now has exactly two). It is
  absent from `process_btn.click`, `render_selected_variants_btn.click`, `source_outputs`,
  `prep_outputs`, `live_declaration`, `CreativeProfile` and both mix configs.
- **The Director does not render and cannot reach the source gate.** Neither handler's call graph
  may touch `process_video_guarded`, `_process_video_guarded_unlocked`, `process_video`,
  `_process_video_impl`, `analyze_beats_auto`, `create_music_video`,
  `render_selected_variants_guarded`, `_promote_output_no_replace`, `resolve_for_render` or
  `live_declaration` — walked structurally from both buttons, so a rename cannot evade it. Neither
  takes `_RENDER_LOCK`, joins `RENDER_CONCURRENCY_ID` or touches the processing directory: a
  proposal is not a render. Director widgets are also absent from `source_outputs`, `prep_outputs`
  and every source/preparation handler, so generating or applying intent cannot invalidate a
  confirmed source set.
- **Unlike Variant Lab's Apply, Director Apply is deliberately NOT stale-gated.** Variant Lab has a
  live-declaration gate because a candidate describes a *base* the screen may have moved away from;
  a Director proposal is an absolute set of seven values, as valid now as when it was generated. So
  the proposal state survives an apply and may be re-applied. Do not conflate the two, and do not
  weaken Variant Lab's gate.
- **One new subprocess, bounded and windowless.** `_run_director_model` is one
  `subprocess.run(..., timeout=60, creationflags=CREATE_NO_WINDOW)` with stdout and stderr captured
  separately — `run` rather than `Popen` precisely so a timeout kills and reaps the child. `gui.py`
  now has exactly two `subprocess.run` call sites (the pre-existing ProRes preview and this one),
  pinned by test. It uses `llama-completion.exe` rather than `llama-cli.exe` on measured grounds;
  see `.claude/rules/director.md` before touching the argv.
- **The Director owns its own read-outs.** `director_proposal` and `director_status` are written
  only by Director handlers, and no Director handler writes `variant_report`,
  `variant_batch_status`, `variant_batch_table`, `audio_layers_report`, `smart_mix_report`,
  `render_batch_summary` or `status_output`. Their existing single writers are unchanged.
- **Freestyle's eleven widgets are render-request inputs with exactly one read-out between them
  (Freestyle V1).** The checkbox and the ten section dropdowns each register one `.change()`, and
  all eleven write **only** the read-only `freestyle_summary` textbox — never a slider, the Variation
  Seed, `creative_preset`, the lab's master seed or an audio level. They are appended
  **explicitly** (not behind a list concatenation) and **last** to both `process_btn.click` and
  `render_selected_variants_btn.click`, so the positional seam tests can inspect them and every
  pre-existing parameter keeps its index.

  What crosses the boundary is a **plain inline frozen tuple** `(enabled, style × 10)` in
  `SECTION_TYPES` order, not the fork record — because the frozen preservation suites AST-extract
  these render bodies and execute them against a synthesised namespace, so the bodies must not name
  a fork module. `auto_mode._resolve_freestyle` is the one conversion to a `FreestyleDeclaration`,
  and the success line reads the resolved declaration back off `beat_info` **duck-typed** for the
  same reason. A C3 batch builds its tuple **once before the candidate loop**, so both candidates
  provably get equal declarations. See `.claude/rules/freestyle.md`.
- **The asyncio Proactor patch that swallows benign `WinError 10054` pipe resets is intentional**, not
  dead code.

## Routing table

| Seam you are editing | Open |
|---|---|
| progress panel, `ProgressView`, event plumbing, Qwen live progress | `.claude/rules/progress-events.md` |
| Video Source block, scan/confirm/gate, the shared gate core | `.claude/rules/input-gate.md` |
| C3-R0 render-two-candidates seam, render mutex, batch summary | `.claude/rules/variant-lab.md` **+** `.claude/rules/input-gate.md` **+** `.claude/rules/pipeline-core.md` |
| the **Cancel Active Render** button, `render_invocation_state`, the active-render slot, `RenderLifecycle`, or any `lifecycle=` parameter on a pipeline function (C3-R1A) | `.claude/rules/variant-lab.md` (C3-R1A section) **+** `.claude/rules/pipeline-core.md` **+** `.claude/rules/progress-events.md` |
| the six creative sliders, Variation Seed, Randomize | `.claude/rules/creative-controls.md` |
| the **AI Director** group, its instruction box, either Director button, `director_proposal_state`, or the one-shot model invocation | `.claude/rules/director.md` **+** `.claude/rules/creative-controls.md` **+** `.claude/rules/creative-presets.md` — Apply writes the seed, the six sliders and the preset label, so all three writer matrices apply |
| the Creative Preset selector and its `.input()` graph | `.claude/rules/creative-presets.md` |
| the **Freestyle** accordion, its checkbox, any of the ten section dropdowns, the summary textbox, or the tuple threaded through the render wrappers | `.claude/rules/freestyle.md` **+** `.claude/rules/creative-controls.md` — the rules modulate five of the six global controls, so both apply |
| Variant Lab **visual** widgets, master seed, Spread, Generate handlers | `.claude/rules/variant-lab.md` |
| Variant Lab **audio** subsection, or any of its three audio outputs (E2) | `.claude/rules/variant-lab.md` **+** `.claude/rules/audio-mixdown.md` |
| Variant Lab **comparison / Apply Selected** seam, candidate count, batch state (C3) | `.claude/rules/variant-lab.md` **+** `.claude/rules/creative-presets.md` **+** `.claude/rules/audio-mixdown.md` — Apply writes both creative and audio widgets, so all three writer matrices apply |
| voice clips, Smart Mix folder/roles/Amount/Level, the report panels | `.claude/rules/audio-mixdown.md` |
| Media Library Preparation scan/analyze, batch size | `.claude/rules/library-preparation.md` |
| Stage 0 / scan / planner timings surfaced in the UI | `.claude/rules/scale-diagnostics.md` |
| render modes, encoder branches, frame lock | `.claude/rules/pipeline-core.md` |

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
- **`process_video_guarded()` is the real render gate** and it validates the **live** source controls
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
  `music_under_voice`, `sfx_amount` and `sfx_level` may be written — by `generate_variant_btn.click`
  and `new_variant_btn.click` and by nothing else. Every other audio/SFX configuration widget, and
  every creative-control configuration widget, still has **zero** writers. Split seam guards pin the
  whole matrix by exact writer list; do not relax one to make a new handler fit.
- **The asyncio Proactor patch that swallows benign `WinError 10054` pipe resets is intentional**, not
  dead code.

## Routing table

| Seam you are editing | Open |
|---|---|
| progress panel, `ProgressView`, event plumbing, Qwen live progress | `.claude/rules/progress-events.md` |
| Video Source block, scan/confirm/gate, `process_video_guarded` | `.claude/rules/input-gate.md` |
| the six creative sliders, Variation Seed, Randomize | `.claude/rules/creative-controls.md` |
| the Creative Preset selector and its `.input()` graph | `.claude/rules/creative-presets.md` |
| Variant Lab **visual** widgets, master seed, Spread, Generate handlers | `.claude/rules/variant-lab.md` |
| Variant Lab **audio** subsection, or any of its three audio outputs (E2) | `.claude/rules/variant-lab.md` **+** `.claude/rules/audio-mixdown.md` |
| voice clips, Smart Mix folder/roles/Amount/Level, the report panels | `.claude/rules/audio-mixdown.md` |
| Media Library Preparation scan/analyze, batch size | `.claude/rules/library-preparation.md` |
| Stage 0 / scan / planner timings surfaced in the UI | `.claude/rules/scale-diagnostics.md` |
| render modes, encoder branches, frame lock | `.claude/rules/pipeline-core.md` |

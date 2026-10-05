---
paths:
  - "src/beatsync_fork/**/*.py"
  - "tests/test_no_runtime_dependency.py"
  - "tests/test_fork_identity.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

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
| `creative.py` | the resolved `CreativeProfile`: seven controls, their normalisation and their mappings (seed handling delegated to `variation.py`) |
| `presets.py` | the four named Creative Preset recipes: immutable data plus three total helpers. UI-only — the name reaches no stage, no profile and no cache |
| `freestyle.py` | Freestyle V1: per-**section** creative rules. Owns `FREESTYLE_CONTROL_FIELDS` (the six controls minus `micro_cuts`, **derived** from `presets.CREATIVE_CONTROL_FIELDS`), the ten-entry Stage-3 section vocabulary, the frozen deepcopy-safe `SectionOverride` / `FreestyleDeclaration`, the preset→numbers projection, the sparse `effective_profile` composition and the one truthful summary formatter. The Variation Seed and Micro Cuts are **absent by construction** — there is no field for either, so a rule cannot touch them. Depends on `creative.py` and `presets.py` only; it is the **only** fork module restating an upstream vocabulary, and that tuple is pinned against the real `stage3_sections.py` by test. Renders nothing, analyses nothing, caches nothing |
| `creative_recipe.py` | the seven-integer `CreativeRecipe`: the exact resolved **visual** creative configuration for one render (Variation Seed + the six creative controls), strictly validated as a whole, bridged to `CreativeProfile`. Complete for the visual interpretation it stands for — **not** for the whole physical render: E2's three generated audio levels are the sibling `AudioRecipe` in `variant_lab.py`, never a field here and never routed through `CreativeProfile` |
| `variant_lab.py` | Variant Lab: named stable RNG sub-streams (`clips`, `controls` and, since E2, `audio`), the frozen spread formula, range/config normalisation, and one Generate resolving a visual `CreativeRecipe` **plus** a sibling `AudioRecipe` from one shared master seed and one shared Spread. It resolves; it renders nothing |
| `variant_batch.py` | Variant Lab C3 V1: multi-variant generation and comparison. Derives one candidate master per index under the `batch` RNG domain, then calls `variant_lab`'s frozen `resolve` / `resolve_audio` **unchanged** — it orchestrates, it never re-implements. Owns the candidate-count bounds, the deepcopy-safe `VariantBatchDeclaration` / `VariantCandidate` / `VariantBatch` session records and the one comparison-table formatter. Depends on `variant_lab`; `variant_lab` must never depend on it. Renders nothing |
| `render_batch.py` | Variant Lab C3-R0: rendering exactly two compared candidates. Owns the selection contract (exactly two, canonical ascending index), the candidate output identity (index + master + request tag — never the Variation Seed, whose uniqueness C3 does not guarantee), and the batch outcome/summary formatter. Pure: it decides, `gui.py` performs every side effect. No Gradio, no filesystem, no clock, no randomness, no renderer |
| `audio_mix.py` | Audio Layers V1: `AudioMixConfig`, deterministic voice placement and the duck model. Pure — no FFmpeg, no probing, no filesystem |
| `smart_mix.py` | Smart Mix V1: the five SFX roles, the exact folder-alias table, `SmartMixConfig`, the Amount mapping, a stdlib percentile, all five placement rules and the cross-role occupancy policy. Pure — no FFmpeg, no probing, no filesystem walking, no numpy |
| `deterministic_view.py` | reconstructs a candidate's pre-Qwen deterministic scores from the raw CV primitives Stage 5 never fuses |
| `library_prep.py` | media library preparation: classification vocabulary, scan state, report text, bounded analysis batches (trackless since P2) |
| `director.py` | AI Director V1: the pure stdlib-only Director **schema / prompt / parser / proposal boundary** — the model's six-control JSON schema (derived from `presets.CREATIVE_CONTROL_FIELDS`, with no `seed` property), the system and user prompts, the strict whole-stdout parser, the tolerant explanation policy, and the frozen deepcopy-safe `DirectorProposal` with its one read-out formatter. **No subprocess, no model runtime, no media** — and no model path, no clock and no randomness either: `gui.py` runs the bounded one-shot invocation and mints the Variation Seed through the existing `variation.random_seed()`. A second *producer* of `CreativeRecipe`, never a second interpretation of one; it reuses `creative_recipe.from_mapping` unmodified as its trust boundary. Renders nothing |
| `stage_cache.py` | L2 V1 process-local post-Stage-3 audio-analysis cache: one most-recent entry, strong track/window/config/use_gpu identity, defensive-copy isolation, no persistence; Stage 4/5/6 remain outside the cache |

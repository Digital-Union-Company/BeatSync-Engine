# Claude instruction set — map

Three layers, by how often Claude needs them:

| Layer | Location | Loaded |
|---|---|---|
| Constitution | root `CLAUDE.md` | always |
| Operating policies | `.claude/rules/operating-policies.md` | always (deliberately unscoped) |
| Subsystem rules | `.claude/rules/*.md` with `paths:` frontmatter | only when you touch matching files |
| Reference / history | `docs/claude/*.md` | on demand, never injected |

## Subsystem rules

| Rule | Scoped to (abridged) |
|---|---|
| `pipeline-core.md` | `src/video_processor.py`, `src/ffmpeg_processing.py`, `src/auto_mode/stage{1,2,3,4,6}*.py`, `src/auto_mode/__init__.py`, `src/logger.py`, `src/paths.py`, `src/gpu_cpu_utils.py`, `src/beatsync_fork/ffmpeg_diagnostics.py` + their tests |
| `stage5-worker.md` | `src/video_analysis.py`, `src/auto_mode/stage5_qwen_scene_worker.py`, `src/beatsync_fork/qwen_progress.py`, `tests/test_qwen_*.py`, `tests/test_media_neutral_semantics.py` |
| `stage5-cache-identity.md` | `src/video_analysis.py`, `tests/test_stage5_cache_identity.py`, `tests/test_fingerprint.py` |
| `stage5-cache-durability.md` | `src/video_analysis.py`, `tests/test_stage5_cache_{durability,completion}.py` |
| `stage5-reporting.md` | `src/video_analysis.py`, `tests/test_stage5_reporting_truth.py`, `tests/test_qwen_scalar_boundary.py` |
| `creative-controls.md` | `src/beatsync_fork/{creative,variation,deterministic_view}.py`, `src/auto_mode/stage{4,6}*.py` + their tests |
| `creative-presets.md` | `src/beatsync_fork/presets.py`, `tests/test_creative_presets.py` |
| `variant-lab.md` | `src/beatsync_fork/{variant_lab,creative_recipe}.py` + their tests |
| `audio-mixdown.md` | `src/audio_mixdown.py`, `src/beatsync_fork/{audio_mix,smart_mix}.py` + their tests |
| `progress-events.md` | `src/beatsync_fork/{progress,progress_view,qwen_progress}.py`, `tests/test_progress_*.py`, `tests/test_gui_progress_seam.py` |
| `input-gate.md` | `src/beatsync_fork/{input_manager,input_report,input_confirmation,input_session}.py`, `tests/test_input_*.py`, `tests/test_gui_guard_seam.py` |
| `library-preparation.md` | `src/beatsync_fork/library_prep.py`, `tests/test_library_preparation.py` |
| `scale-diagnostics.md` | `src/beatsync_fork/input_report.py`, `src/video_processor.py`, `src/video_analysis.py`, `tests/test_scale_diagnostics.py` |
| `fork-package.md` | `src/beatsync_fork/**/*.py`, `tests/test_no_runtime_dependency.py`, `tests/test_fork_identity.py` |
| `gui-integration.md` | `src/gui.py`, `src/ui_content.py` |
| `test-harness.md` | `tests/**/*.py`, `pytest.ini`, `requirements-dev.txt` |
| `platform-and-packaging.md` | `install.bat`, `run.bat`, `scripts/install.ps1`, `src/logger.py`, `src/paths.py`, `README.md`, `CHANGELOG-FORK.md` |

`src/gui.py` deliberately loads **only** `gui-integration.md`, which carries the cross-cutting GUI
invariants and a routing table to the subsystem rules. Each subsystem rule is scoped to its own
implementation **and its GUI seam test**, so editing `tests/test_gui_guard_seam.py`,
`tests/test_gui_progress_seam.py`, `tests/test_creative_controls_seam.py` or
`tests/test_audio_layers_seam.py` loads the right subsystem contract without attaching all of them to
`gui.py`.

## Reference documents

| Document | Contents |
|---|---|
| `instruction-architecture.md` | why this layout exists, the classification of every original section, and the old → new mapping |
| `stage5-cache-history.md` | the D1-era cache state the D2 identity transition superseded |
| `removed-and-superseded.md` | what was dropped rather than relocated, and why it is no longer authoritative |

`CHANGELOG-FORK.md` remains the AGPL-3.0 §5(a) modification record and the per-feature engineering
history; it is not part of the instruction set.

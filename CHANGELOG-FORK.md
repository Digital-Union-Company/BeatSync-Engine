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

### Removed — 2026-09-26

- Untracked the stale compiled artefact `src/auto_mode/__pycache__/stage5_qwen_scene_worker.cpython-313.pyc`
  from Git. The file was left on disk (it is regenerable, and removing files on disk is not this
  change's business).

### Unchanged

No change to generated video output. Auto Mode stages 1-6, `AutoWaveConfig` defaults, the analysis
cache format, seed/randomness behaviour, `gui.py` and the render path are untouched.

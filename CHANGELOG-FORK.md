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

---
paths:
  - "src/beatsync_fork/input_manager.py"
  - "src/beatsync_fork/input_report.py"
  - "src/beatsync_fork/input_confirmation.py"
  - "src/beatsync_fork/input_session.py"
  - "tests/test_input_confirmation.py"
  - "tests/test_input_gate.py"
  - "tests/test_input_gate_live.py"
  - "tests/test_input_manager_large.py"
  - "tests/test_input_manager_paths.py"
  - "tests/test_input_manager_scan.py"
  - "tests/test_input_manager_traversal_errors.py"
  - "tests/test_input_report.py"
  - "tests/test_gui_guard_seam.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

## Video source modes and the confirmation gate

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

**One gate core, two mutex-owning wrappers (C3-R0 → C3-R1B-b).** The authoritative gate is
`_process_video_guarded_unlocked()` in `gui.py`; `process_video_guarded()` is the single-render
wrapper that `process_btn.click` calls and `render_selected_variants_guarded()` is the **2–4**-candidate
batch wrapper. (Two *wrappers* counts entry points, not candidates.) Both take the process-global
render mutex and then reach the **same** core — the batch once per candidate, so **every** selected
candidate is freshly re-verified, however many were chosen:

```
_process_video_guarded_unlocked   = the ONE live source gate + render core
process_video_guarded             = single-render mutex wrapper   -> core
render_selected_variants_guarded  = batch mutex wrapper           -> core, per candidate
```

**Never create a second source-gate implementation.** The core was extracted precisely so the batch
could reuse the gate instead of copying it; a wrapper that rebuilt `resolve_for_render`,
`live_declaration` or the config normalisation would be two gates one refactor from disagreeing. A
seam test asserts exactly one `resolve_for_render` exists in the module, and that neither wrapper
contains it. The core is **not** a public entry point: only those two wrappers may call it, and no
Gradio event may name it as its `fn`.

**The gate validates the **live** source controls** — `source_mode`, `source_folder`, `source_recursive`, `video_input` are render-request inputs,
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

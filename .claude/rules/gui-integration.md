---
paths:
  - "src/gui.py"
  - "src/ui_content.py"
  - "tests/test_gui_guard_seam.py"
  - "tests/test_gui_progress_seam.py"
  - "tests/test_creative_controls_seam.py"
  - "tests/test_audio_layers_seam.py"
  - "tests/test_cut_handoff.py"
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
- **One render gate core, two mutex-owning wrappers, one render at a time (C3-R0 → C3-R1B-b).**
  `_process_video_guarded_unlocked()` is the single authoritative live-source-gate + render body;
  `process_video_guarded()` wraps it for a single render and `render_selected_variants_guarded()`
  for the **2–4-candidate** batch. (Two *wrappers* is a count of entry points and is unrelated to
  the candidate count, which is 2–4.) Both acquire the **process-global `_RENDER_LOCK`** non-blockingly
  and refuse cleanly when it is held — `create_music_video` clears one process-global processing
  dir per render, so two overlapping renders would delete each other's in-flight clips. Both
  events also share one `concurrency_id` with `concurrency_limit=1`, but that is cooperative: the
  lock is the authority, because it is provable without Gradio.

  **`_RENDER_LOCK` has a THIRD non-blocking acquirer since S2-A: `_on_verify_sources_click`.**
  Read-only source verification issues up to four concurrent FFprobe reads against the same drive a
  render extracts from, and D0 traced clip-extraction timeouts to exactly that contention, so the
  two are mutually exclusive. The mutex is *reused* rather than duplicated precisely because the
  render side then needs no new check — it already refuses when the lock is held and does not care
  who holds it — and because one `acquire(blocking=False)` is a test-and-set, whereas
  `if not lock.locked(): start()` is a check-then-act race. The refusal message now comes from
  `_busy_message()` so it names the real holder; `RENDER_BUSY_MESSAGE` remains the default.
  Verification holds no `RenderLifecycle`, is not cancellable, and is not a render. See
  `.claude/rules/source-verification.md`.

  **A mutex cannot refuse an event the queue never delivers (S2-A R3).** `concurrency_id` grouping
  is described above as "cooperative", and that is exactly why the Verify event must **not** join
  `RENDER_CONCURRENCY_ID`. S2-A did, with `concurrency_limit=1`; measured on the pinned
  `gradio==6.19.0`, `Queue.get_events()` skips a group already at its limit and leaves the event
  queued, so the handler was not entered until the render finished — then it ran automatically
  against the folder captured at *click* time and overwrote the newer invalidation notice. The
  mutex was never reached and both busy messages were unreachable. `prep_verify_btn.click`
  therefore carries no `concurrency_id` and `concurrency_limit=None` (unlimited — a lane of 1 would
  queue a *second* Verify click instead of letting the mutex refuse it). Both render events keep
  `concurrency_id=RENDER_CONCURRENCY_ID, concurrency_limit=1`, and `CANCEL_CONCURRENCY_ID` is
  untouched. **Before claiming any Gradio queuing behaviour in this repository, measure it against
  the pinned version** — this is the second time a plausible reading of a `concurrency_*` parameter
  turned out to describe something the operator could not observe.

  **`gr.Request` is an injected per-request value, not a widget and not session state.**
  `_on_verify_sources_click(folder_path, recursive, request: gr.Request = None)` and
  `_on_verify_invalidate(request: gr.Request = None)` both take it; neither lists it in `inputs`,
  both default it to `None` so they stay callable without Gradio, and both read only
  `session_hash`. It keys a bounded `str -> int` map (`_verify_revisions`, cap 512, LRU eviction)
  that exists solely so a verification finishing after a folder change can withhold its now-stale
  report. Session-scoped deliberately: a process-global counter would let one browser session
  invalidate another's legitimate run. Nothing live — no lock, thread, handle or lifecycle — may
  enter that map, and it is not a persistence layer.

  **The running verification's own entry is exempt from that eviction (R5).** An absent key reads
  back as revision `0`, so evicting the entry of a verification that is still running made "I was
  invalidated" indistinguishable from "nothing changed" and published a stale report as current —
  reproduced by independent review at exactly 512 competing sessions. `_RENDER_LOCK` already
  permits only one verification, so `_verify_active_session` pins that one session for the span of
  its run and `_bump_verify_revision` skips it; the cap still holds including the pinned entry.
  `_begin_verify_session` pins and captures in one locked step **after** the mutex is taken, and
  `_end_verify_session` compares and unpins in one locked step from the `finally`, so a refused
  click pins nothing and no terminal path can leak a pin. The pin is a plain string and never
  reaches `gr.State`. Full contract: `.claude/rules/source-verification.md`.

  **Three holders share that one mutex, and the refusal may not guess (S2-B P2).** Sample Decode
  joined renders and verification on `_RENDER_LOCK`, which broke the two-holder boolean:
  `_verification_running == False` used to mean "a render" **by elimination**, so a render refused
  during a sample would have claimed another render was running. `_lock_holder_now()` replaces it
  and reports `render` / `verify` / `sample` / unknown from two **positive** sources — the explicit
  diagnostic token, and the **existing** `_active_render_slot` as evidence of a render. Reusing the
  slot is what keeps `process_video_guarded` and `render_selected_variants_guarded`
  **byte-identical** to the pre-S2-B implementation, so every render lifecycle, cancellation and
  generator-shape regression in `tests/test_gui_progress_seam.py`,
  `tests/test_render_failure_classification.py` and `tests/test_delivery_share.py` stays valid
  unchanged — adding holder marks to those wrappers instead was measured to break 62 tests across
  those harnesses. No token and no slot is reported as unknown with neutral text, never as a
  render; the two attribution locks are taken sequentially and never nested; and the lookup is
  diagnostic only, so `_RENDER_LOCK` remains the sole admission authority. The sample event has its
  own unlimited lane for the same R3 reason as Verify, writes only `sample_report`, and passes no
  frame/timeout/budget argument to the core at all. Full contract:
  `.claude/rules/source-verification.md`.

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
  the promotion into `output/` succeeds, and never the delivery preview or the share file — and reads the two report
  keys after each candidate. It becomes a writer of **neither** report panel; the batch summary
  owns multi-render diagnostics so no panel can describe a candidate the user is not looking at.

  **Since C3-R1B-a the batch also reads the FULL typed class** off
  `session_state[RENDER_OUTCOME_KEY]` and passes it to `RenderCandidateOutcome.outcome_kind`
  **unchanged** — `outcome_kind=candidate_kind`, with no filter in between. R1A read that key only
  to spot `CANCELLED`, so `CANDIDATE_LOCAL` was produced and discarded and every other class
  collapsed to `None`. The class comes **only** from that key: never from the status text, the
  durable path, a message prefix or an emoji.

  **An explicit contradiction must be loud, and the batch must not sanitize it.** `durable` remains
  the sole success authority, and `RenderCandidateOutcome.__post_init__` raises `ValueError` when
  `success` and `outcome_kind` disagree. A draft of R1B-a wrapped the hand-off in an agreement test
  and substituted `None` on disagreement; that defeated the very invariant the model exists to
  enforce *and* then let the conservative derivation publish a different class than the producer
  named — laundering a broken producer contract into a plausible-looking outcome, and leaving
  C3-R1B-b to decide continuation on a class nobody verified. The filter is gone and a structural
  test forbids it returning. Conservative derivation is reserved for the one case that proves
  nothing: `candidate_kind is None`, a producer that never classified itself.

  **Since C3-R1B-b exactly one class continues.** The handler constructs the outcome, appends that
  exact object, and then reads the policy off `candidate_outcome.outcome_kind` — never the raw
  `candidate_kind`, so the *model* decides what an unclassified `None` means. `CANDIDATE_LOCAL`
  `continue`s; `SHARED_FATAL`, `UNKNOWN_FATAL` and `CANCELLED` reach `if not
  candidate_outcome.success:` and stop. The **loop-head cancellation check still comes first in
  every iteration**, so a `continue` cannot outrun a Stop: a Cancel landing after a local failure
  leaves the remaining candidates NOT ATTEMPTED. Tests pin exactly one `continue`, guarded by
  exactly that one class, plus measured call order for every scenario.
- **`_promote_output_no_replace()` returns `(message, outcome_kind)`** since C3-R1B-a — `('', None)`
  on success, otherwise the unchanged user-facing text plus the typed cause
  (`FileExistsError` → `CANDIDATE_LOCAL`, `errno.EXDEV` → `SHARED_FATAL`, any other `OSError` →
  `UNKNOWN_FATAL`). Those three branches were already structurally separate, so this added no logic:
  it only stops the caller recovering the cause by reading prose. Everything else about the helper is
  untouched — one atomic no-replace `os.rename`, no deletion, no copy fallback, no `exists()`
  pre-check — and the SUCCESS commit point did **not** move: it is still written only after the
  promotion returned success. The call site reads `promotion_kind or UNKNOWN_FATAL`, so a future
  branch that forgets to name a class still fails closed.
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

  **The AI Director repeats that split exactly**, and the symmetry is deliberate rather than
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

  **Director V2 widened Generate's `inputs`, and only its `inputs`.** They are now
  `[director_instruction, prep_folder, prep_recursive, prep_state]`, matched positionally against
  the handler's parameter order. The three preparation values are **read** so the eligibility gate
  can tell a current, live-declared, fully prepared scan from a stale or partial one — the same
  queued-widget hazard `prep_analyze_btn` already takes live values for. Generate's `outputs` are
  unchanged, so it is still a reader and never a preparation or source writer: intent cannot
  invalidate a scan or a confirmed source set. No creative state reaches Generate — not a slider,
  not the Variation Seed, not the preset, not a Variant Lab widget — so the instruction stays an
  absolute intention rather than a transform of the screen.
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
- **A successful render yields one guaranteed artifact and up to two best-effort ones (Deliverable
  Export V1-A + V1-B-S1).** Only the **master** is guaranteed: it is promoted exactly once, as
  above, and once that promotion returns success the render is SUCCESS no matter what follows. The
  other two are attempted after the commit point and either may be absent:

  ```
  HIGH-QUALITY MASTER   output/<stem>.<ext>              durable, authoritative, GUARANTEED
  SHAREABLE MP4         output/<stem>_share.mp4          durable, subordinate, best-effort
  PLAYABLE PREVIEW      <session_dir>/<stem>_preview.mp4  session-scoped, best-effort
  ```

  Do not restate this as "every render produces three artifacts". A preview failure skips the
  share file entirely (it is a copy of the preview), and a share failure leaves the preview
  working — the panel must say which of the three actually exist.

  The **delivery preview** is built by `ffmpeg_processing.build_delivery_preview_command` and
  handed to `gr.Video`; it is generated for **all four** processing modes, strictly after the
  SUCCESS commit point, and is **never** promoted into `output/`. It stays in `session_dir` under
  `GRADIO_TEMP_DIR` and that location is load-bearing: **measured against the running app, a path
  under `GRADIO_TEMP_DIR` is served `206` with `video/mp4` and range support, while a raw
  `output/` path is served `403`** (`route_utils.file_fetch` → `is_allowed_file`, whose
  `created_paths` is `[upload_dir, cache_folder]`). So moving the preview into `output/` and
  handing `gr.Video` that path is not a simplification, it is broken — and widening
  `allowed_paths` to make it work would expose the whole folder to every user of the app. Never do
  either.

  Why it exists: every master muxes `pcm_s24le`, which lands in MP4 as `ipcm` (ISO/IEC 23003-5) and
  is not decodable by an ordinary browser, so the pre-V1-A non-ProRes path previewed a finished
  render silently; ProRes and HEVC masters are not AVC at all. Measured on 18 real renders,
  stream-copying the `-crf 0` master and swapping only PCM→AAC saves 4.7 % — so the preview
  **re-encodes** (one CPU `libx264 -crf 21` + `aac 192k` profile for every mode, so it never depends
  on NVENC). Resolution and frame rate are preserved by omitting `-vf`/`-s`/`-r`, and no H.264 level
  is pinned.

  **The preview is selected only when FFmpeg exited 0 AND the file exists AND it is non-empty.**
  Assigning `preview_path` before the subprocess is exactly how the old block returned a path to a
  file that was never created. Any failure, timeout or cancellation is contained: the master stays
  SUCCESS, `LAST_OUTPUT_PATH_KEY` still names it, the display path falls back to it, and the panel
  says no shareable MP4 was produced — it must **never** claim the master is browser-playable. See
  `.claude/rules/render-worker.md` for the swallow exemption and why a `TimeoutExpired` is now
  caught.

- **H1-b: the subordinate durable publication (Deliverable Export V1-B-S1).** H1 still owns the
  **one authoritative** promotion. `_publish_share_no_replace()` adds **at most one subordinate**
  durable artifact per render, and subordinate is a contract, not a description:

  ```
  runs strictly after the SUCCESS commit point, and only from a VALIDATED preview
  writes neither RENDER_OUTCOME_KEY nor LAST_OUTPUT_PATH_KEY  -> invisible to batch continuation
  never reassigns preview_path                                -> gr.Video keeps the servable file
  never raises                                                -> an escape would downgrade SUCCESS
  failure is a delivery warning, never a render outcome class
  ```

  It is **a separate helper and must never be merged into `_promote_output_no_replace`**. That one
  carries authoritative semantics — its `CANDIDATE_LOCAL`/`SHARED_FATAL`/`UNKNOWN_FATAL` classes
  feed `RenderCandidateOutcome` and continuation — and a share failure must produce none of them.
  Routing both through one helper behind a policy flag is exactly the per-caller boolean that
  `refuse_existing_output` was deleted for.

  **No second transcode:** it copies the already-encoded preview (5.5–9.2 % of the master,
  measured). Staging lives **inside** `output/.beatsync-staging/`, so the final step is always a
  same-directory `os.rename` and `errno.EXDEV` is structurally impossible rather than merely
  unlikely — `EXDEV` is still handled fail-closed anyway. The staging file is **attempt-unique and
  exclusively created** (`O_CREAT | O_EXCL`), never a predictable `<name>.part`: a predictable
  name is a latent collision in which the second attempt truncates the first's in-flight copy.
  Containment is measured (`realpath` + `commonpath` plus an explicit same-volume check) rather
  than tested by string prefix, which would accept `output-evil/` for `output/`.

  **Reparse points are resolved and containment is measured on the resolved location — they are
  not refused on sight.** `realpath` runs before `_is_reparse`, so a junction or symlink is
  followed and the attribute check sees an already-resolved path; it fires only in the residual
  case where `realpath` could not resolve. Measured against real Windows junctions: one escaping
  `output/` is refused by `commonpath`, one on another volume by `splitdrive`, and one resolving
  back inside `output/` is allowed — containment holds in every case. **Never change this to
  inspect the unresolved paths:** an `output/` that is itself a junction to a larger drive is a
  legitimate, measured-working setup, and refusing reparse points on sight would break it.

  Verification **re-reads the staged bytes and compares them to the bytes read during the copy** —
  that is the exact scope. It detects a corrupt or short write, which trusting the write-time
  digest could not. It does **not** prove equality with the source at publication time: a
  same-size in-place mutation mid-copy stages a torn read that still matches (measured), while a
  size-changing one is caught by the pre-copy size comparison. Source stability comes from the
  runtime instead — `_RENDER_LOCK` serialises renders, `session_dir` is a per-session `mkdtemp`,
  and the preview's FFmpeg child is reaped before publication starts.

  **Retention:** an interrupted process may leave its own unique `*.part` in the staging folder.
  That is acceptable and must not block later attempts. Nothing may sweep that folder — not
  `cleanup_on_startup`, not housekeeping — because another invocation's `.part` may be in flight;
  each attempt discards only the file it exclusively created.

  `os.fsync` is called before the rename, but **atomicity here means final-path visibility, not
  power-loss durability**: a partial file never appears under the final name. Claiming crash
  consistency would need filesystem-level durability testing that has not been done.

  **A breach of "never raises" would cost the panel, not the outcome.** If the helper ever did
  raise, `_process_video_impl`'s generic `except Exception` leaves the already-written SUCCESS
  standing (its `is None` guard does not overwrite), so the committed outcome and batch
  continuation stay correct — but the function returns `(None, '❌ Error: …')`, clearing `gr.Video`
  and showing an error for a finished, promoted render. The generic handler protects the
  **outcome**, not the user-visible result; the helper's structural wrapper is what makes the
  breach unreachable. Do not describe the two as interchangeable.

  **Delivery progress is DEFERRED BY PRODUCT OWNER** for S1, and the deferral is deliberate rather
  than an oversight. `video_processor` emits Stage 6's END before the preview begins, and
  `EventKind.STATE` does not clear `finished` — only `START` does — so a "Delivery" STATE event on
  Stage 6 renders **nothing at all**. Do not emit one to look busy, do not restart Stage 6 (its
  closing `end()` would overwrite the planner summary carrying the P1/P2 shot diagnostics), and do
  not invent Stage 7 without separate authorization.
- **S2 is Open Output Folder, and a download control was REJECTED (Deliverable Export V1-B-S2).**
  The product owner replaced the planned `gr.DownloadButton` with one button that opens the local
  output folder. Do **not** reintroduce a download button, a download endpoint, a browser file
  transfer, a widened `allowed_paths`, a download cache, a duplicate video or a share-file state
  machine. The rejection rests on the same measurement as the preview's location: a raw `output/`
  path is served **403**, so a download control would need either a wider `allowed_paths` — which
  exposes the whole folder to every user — or a third copy of an artifact that is already durable
  on that disk.

  ```
  _on_open_output_folder_click()   zero parameters, returns None, writes no component
  path = get_output_dir()          the fixed process-global folder, never LAST_OUTPUT_PATH_KEY,
                                   never RENDER_OUTCOME_KEY, never the status text or the widget
  os.startfile                     stdlib Windows shell verb -- NOT a subprocess site
  gr.Info / gr.Warning             feedback is a toast, so `outputs` stays exactly []
  ```

  `inputs=[]` and `outputs=[]` are both exact and both load-bearing. Nothing on the screen decides
  which folder to open, so there is nothing to pass in, and no user-controlled text reaches the OS
  call — which is why there is no quoting or shell involvement to get wrong. **The folder button
  adds no new writer to `status_output`; feedback uses Gradio notifications.** That is the
  invariant — not "the panel has one writer", which it does not: `process_btn.click`,
  `render_selected_variants_btn.click` and `cancel_render_btn.click` all write it, and
  `tests/test_open_output_folder.py` pins exactly that set. Writing `status_output` from the folder
  button would make a **fourth** writer and let opening a folder overwrite a render's result panel;
  the toast exists precisely to avoid that. (An earlier version of this bullet said "a second
  writer, which the invariant above forbids" — wrong on both counts: the count, and the invariant,
  which at *Diagnostic read-out panels* above governs the diagnostic panels rather than
  `status_output`.)

  The button takes no `concurrency_id` (it is not a render and must not queue behind one), is
  always interactive, and is absent from `source_outputs` and `prep_outputs` — the folder is a
  fixed property of the installation, so no state makes pointing at it wrong, and it works before
  the first render of a session.

  Platform support is read with `getattr(os, 'startfile', None)` because that attribute is
  Windows-only and the suite runs on bare CPython anywhere; a missing directory is **reported, never
  created**, since a read-only user action must not write to disk. **The success message says the
  request was made, not that Explorer appeared** — `startfile` returns as soon as the shell accepts
  the path, so visual confirmation is a human step and must be recorded as one, never asserted.

- **One new subprocess, bounded and windowless.** `_run_director_model` is one
  `subprocess.run(..., timeout=60, creationflags=CREATE_NO_WINDOW)` with stdout and stderr captured
  separately — `run` rather than `Popen` precisely so a timeout kills and reaps the child. `gui.py`
  now has exactly two `subprocess.run` call sites (the delivery preview encode and this one),
  pinned by test. It uses `llama-completion.exe` rather than `llama-cli.exe` on measured grounds;
  see `.claude/rules/director.md` before touching the argv.
- **The Director owns its own read-outs.** `director_proposal` and `director_status` are written
  only by Director handlers, and no Director handler writes `variant_report`,
  `variant_batch_status`, `variant_batch_table`, `audio_layers_report`, `smart_mix_report`,
  `render_batch_summary` or `status_output`. Their existing writer sets are unchanged — and "set"
  is the accurate word: `status_output` has **three** writers (`process_btn.click`,
  `render_selected_variants_btn.click`, `cancel_render_btn.click`), not one. Corrected 2026-10-13
  under V1-B-S2-R2 (accepted review finding MINOR-B); no writer was added or removed.
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
  same reason. A C3 batch builds its tuple **once before the candidate loop**, so every selected
  candidate provably gets an equal declaration. See `.claude/rules/freestyle.md`.
- **The Raw Cutter's Activity & Performance read-out is a FRAGMENT of the existing progress panel,
  not a component (R12).** `_cut_progress_html` gained one keyword-only `activity: str = ''` which
  it appends as the last child of its own container; omitting it leaves the output byte-identical
  to the pre-R12 panel. That shape is load-bearing twice over: the three Raw Cutter handlers'
  emitted arities are pinned against their `outputs` lists by `tests/test_raw_cutter_seam.py`, so a
  second component would have widened `_on_cut_analyze`, `_on_cut_run` and `_on_cut_batch_run_ui`
  together; and `_cut_progress_html` is AST-extracted and executed against a **two-name** namespace
  (`html_lib` + `_CUT_TONES`), so it must stay dependency-free — which is why what crosses into it
  is a pre-rendered string rather than a record.

  All policy lives in the pure, **clock-injected** `beatsync_fork/raw_cutter_activity.py`; `gui.py`
  supplies `time.perf_counter()` readings and the facts, through `_cut_activity` (a running frame:
  declares the phase, then renders — one reading for both) and `_cut_activity_final` (a terminal
  frame: freezes both timers and does **not** restart the phase timer on the terminal label).

  Four rules that are easy to break by accident:

  * **A refusal carries no activity panel.** `refuse(...)` in `_on_cut_run` performed no work —
    no mutex, no directory, no clip — so a timer there would be counting a job that never began.
  * **The batch's terminal phase comes from `_CUT_BATCH_ACTIVITY`**, keyed by
    `fork_cutter_batch`'s own `TERMINAL_*` vocabulary, so the panel, the headline and the manifest
    are three renderings of one classification. Never derive it from the status text. The missing
    key is a run that never started and renders `PHASE_NOT_RUN`, which is deliberately not spelled
    as a cancellation.
  * **The browser tick is anchored, not incremented.** `CUT_ACTIVITY_JS` is a **third** authorised
    `app.load(js=…)` installer (`tests/test_open_output_folder.py` holds the identity allowlist),
    registers no server call, selects by **class** because both modes render the same markup into
    two components, and makes no network request of any kind. A Gradio update replaces the element
    and loses per-element state, so the anchor is keyed by the panel's `data-job-token` /
    `data-phase-token` and survives the re-render.
  * **Nothing new may probe the GPU.** `gui.py` has exactly two audited `subprocess.run` call
    sites; the panel reports `GPU usage: Unavailable` and names the encoder instead, and Qwen is
    reported as *not used* rather than as offline. Adding a sampler to fill in a percentage would
    break the pinned site count and buy a cosmetic number.
- **The Raw Cutter's "Review these clips" handoff is a RECORD, and the gate lives in the record
  (R11).** `cut_handoff_state` is session state holding a plain dict of primitives —
  `{directory, recursive, clips, mode, outcome}` — or `None`. All the policy is in the pure
  `beatsync_fork/cut_handoff.py`; `gui.py` supplies the facts.

  ```
  _cut_handoff(record)  ->  (record, gr.update(interactive=fork_handoff.is_offer(record)))
  ```

  Returned as one pair for exactly the reason `_cut_buttons` is: writing the state and the control
  that reads it separately is how a record outlives the button that offers it, or a button stays
  lit over a record that was just cleared. Six rules, each of which something used to get wrong:

  * **`build()` answers `None` unless a directory was named AND at least one clip reached disk.**
    That is the "never offer an empty folder" gate, and it is a property of the record rather than a
    check each of the eight emitting paths has to remember. `read()` re-applies it to a record
    coming back out of session state — untrusted input on the way in — by *rebuilding* through
    `build`, so a tampered `recursive` is corrected to what its mode implies rather than honoured.
  * **The count is PUBLISHED clips.** `done` / `written` / `completed` for a single run (the
    callback's list, because a cancellation discards `export_segments`' return value);
    `summarise(...).clips_written` for a batch, carried out through `publish()`. `plan.count` and
    `clips_planned` must stay unreachable from every `fork_handoff.build` call site.
  * **The exact directory is published by the handler that created it.** `sink['directory']` is
    written where `started = bool(batch_directory)` is already known, and popped when the run never
    obtained one. A wrapper that rebuilt it from the output-root text plus a timestamp would be
    guessing. There is **no fallback** — not the parent, not the output root, not `abspath('')`.
  * **Every non-terminal and every refused frame emits `_cut_handoff(None)`**, which is what makes
    "a new, failed or refused cut cannot present the previous job's folder" structural.
  * **The batch's outcome comes from `_CUT_BATCH_HANDOFF`**, keyed by `fork_cutter_batch`'s own
    `TERMINAL_*` vocabulary — a fourth rendering of the one classifier the headline, the manifest
    and the activity panel share. A missing key resolves to no outcome, which `build` refuses.
  * **Pressing it does exactly one `os.path.isdir`.** A single `stat`, asked *then* rather than
    cached, because a record describes a finished job and not a disk that stayed still. No walk, no
    probe, no FFmpeg, no move, no rename, no decision write. `inputs=[cut_handoff_state]` and
    **not** `cut_output`: with the widget as input, editing it after a job finished silently
    redirected that job's review.

  The two appended outputs sit **between the payload and the three button states** on both handlers
  (`_on_cut_run` 7, `_on_cut_batch_run_ui` 8) because the button set is the tail of every emitted
  expression — appending after it misaligns all three updates on every frame. `_on_cut_batch_run`
  keeps its name, signature, body and 2-tuple arity; it gained only sink writes.
- **The asyncio Proactor patch that swallows benign `WinError 10054` pipe resets is intentional**, not
  dead code.

## Routing table

| Seam you are editing | Open |
|---|---|
| progress panel, `ProgressView`, event plumbing, Qwen live progress | `.claude/rules/progress-events.md` |
| the Raw Cutter's **Activity & Performance** panel, `_cut_activity` / `_cut_activity_final`, `CUT_ACTIVITY_JS` or `raw_cutter_activity.py` (R12) | the R12 bullet above **+** `ROADMAP.md` R12 for the product contract — and `tests/test_raw_cutter_activity.py`, which executes the pure timer rules rather than asserting about them |
| the **"Review these clips"** button, `cut_handoff_state`, `_cut_handoff`, `_on_cut_open_in_library`, `_CUT_BATCH_HANDOFF` or `cut_handoff.py` (R11) | the R11 bullet above **+** `ROADMAP.md` R11 for the product contract — and `tests/test_cut_handoff.py`, which executes the real `_on_cut_run` on every terminal path rather than asserting about it. `.claude/rules/clips-preview.md` too if you touch what Video Review does with the folder it receives |
| Video Source block, scan/confirm/gate, the shared gate core | `.claude/rules/input-gate.md` |
| the C3 2–4-candidate render seam, render mutex, continuation policy, batch summary | `.claude/rules/variant-lab.md` **+** `.claude/rules/input-gate.md` **+** `.claude/rules/pipeline-core.md` |
| the **Cancel Active Render** button, `render_invocation_state`, the active-render slot, `RenderLifecycle`, or any `lifecycle=` parameter on a pipeline function (C3-R1A) | `.claude/rules/render-worker.md` **+** `.claude/rules/variant-lab.md` (C3-R1A section) **+** `.claude/rules/pipeline-core.md` **+** `.claude/rules/progress-events.md` |
| the six creative sliders, Variation Seed, Randomize | `.claude/rules/creative-controls.md` |
| the **AI Director** group, its instruction box, either Director button, `director_proposal_state`, or the one-shot model invocation | `.claude/rules/director.md` **+** `.claude/rules/creative-controls.md` **+** `.claude/rules/creative-presets.md` — Apply writes the seed, the six sliders and the preset label, so all three writer matrices apply |
| the Creative Preset selector and its `.input()` graph | `.claude/rules/creative-presets.md` |
| the **Freestyle** accordion, its checkbox, any of the ten section dropdowns, the summary textbox, or the tuple threaded through the render wrappers | `.claude/rules/freestyle.md` **+** `.claude/rules/creative-controls.md` — the rules modulate five of the six global controls, so both apply |
| Variant Lab **visual** widgets, master seed, Spread, Generate handlers | `.claude/rules/variant-lab.md` |
| Variant Lab **audio** subsection, or any of its three audio outputs (E2) | `.claude/rules/variant-lab.md` **+** `.claude/rules/audio-mixdown.md` |
| Variant Lab **comparison / Apply Selected** seam, candidate count, batch state (C3) | `.claude/rules/variant-lab.md` **+** `.claude/rules/creative-presets.md` **+** `.claude/rules/audio-mixdown.md` — Apply writes both creative and audio widgets, so all three writer matrices apply |
| voice clips, Smart Mix folder/roles/Amount/Level, the report panels | `.claude/rules/audio-mixdown.md` |
| Media Library Preparation scan/analyze, batch size | `.claude/rules/library-preparation.md` |
| the **Verify Sources** button, its report box, the FFprobe metadata probe, or render↔verification exclusion (S2-A) | `.claude/rules/source-verification.md` **+** `.claude/rules/library-preparation.md` (the cache question goes through `classify_library_sources`) **+** `.claude/rules/pipeline-core.md` (the probe shares `_run_media_command` and `FFPROBE_PATH`) |
| Settings → **Video Analysis Cache**, the shared cache folder, the Import button, or Cache Management's analysis category | `.claude/rules/shared-analysis-cache.md` **+** `.claude/rules/stage5-cache-identity.md` (the directory is deliberately not part of identity) |
| Stage 0 / scan / planner timings surfaced in the UI | `.claude/rules/scale-diagnostics.md` |
| render modes, encoder branches, frame lock | `.claude/rules/pipeline-core.md` |

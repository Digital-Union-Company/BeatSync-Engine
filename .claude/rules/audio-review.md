---
paths:
  - "src/beatsync_fork/audio_review.py"
  - "src/beatsync_fork/audio_cutter.py"
  - "src/beatsync_fork/audio_library.py"
  - "src/audio_media_server.py"
  - "tests/test_audio_review.py"
  - "tests/test_audio_media_server.py"
  - "tests/test_audio_cutter.py"
  - "tests/test_audio_preview_tab.py"
  - "src/beatsync_fork/cut_handoff.py"
  - "tests/test_r15c_audio_review_consolidation.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`. GUI seams are in `.claude/rules/gui-integration.md`. The
> video sibling of most of this is `.claude/rules/clips-preview.md` — read it before changing
> anything about authorization, because R7.2 **reuses** that model rather than restating it.

# R7 Raw Audio Cutter and R7.2 Audio review

**ONE surface reviews exported audio clips: Media Library → Audio Review.** That is R15-C, and it
is the first thing to know about this subsystem, because much of the text below was written when
there were two and still describes the shared machinery in the plural.

Everything that is not Gradio and not HTTP lives in `beatsync_fork/audio_review.py`: enumeration,
paging, the tile markup, the decision vocabulary, the sort plan and the destination rules. It is
still **parameterised by an `_AudioSurface`** rather than inlined into its one caller — deliberately,
because R15-B's Folder Batch is expected to want it — so a behaviour that is wrong is still wrong in
one place.

## R15-C: the Audio Cutter exports and hands off; it does not review

The Raw Audio Cutter used to carry a duplicate of the whole workflow: its own destination pair,
gallery, Confirm, sweep gate and sort. Reading any claim below about "both surfaces" or "two
galleries", understand it as history — the second surface is gone, and with it
`_AUDIO_SURFACE_CUT`, `_on_audio_confirm`, `_on_audio_proceed`, `_on_audio_cancel`,
`_on_audio_destinations_change` and three `gr.State` slots. What is left of the cutter's review half
is **one button**, `audio_open_preview_btn`.

Rules that are now load-bearing:

- **The shared core is untouched and must stay that way.** Removing a surface is not licence to trim
  the engine under it. `_audio_confirm_core`, `_audio_sort`, `_audio_render`, `_audio_load_view`,
  `_audio_merge_payload`, and every module under `beatsync_fork/` named in this file, are exactly
  what they were. `tests/test_audio_cutter.py` pins their survival by name.
- **The removed widget names are PREFIXES of surviving ones.** `audio_library_browse` is a substring
  of `pref_audio_library_browse`; `audio_preview_library` begins with the removed
  `audio_library_folder`'s stem. Assert removal over the **syntax tree**, never as a substring —
  the first attempt at that guard reported two live widgets that had genuinely been deleted.
- **`_AUDIO_CUT_REGISTRY` and the `/r7audio` mount are kept on purpose.** Nothing publishes into
  that registry now, so the prefix answers `403 unknown_session` for everything — but
  `MEDIA_PREFIX_CUTTER` is still the module-level **default** prefix for `media_url` and
  `gallery_html`, so un-mounting it would leave that default pointing at a route Gradio would 404.
  Narrowing that boundary is a separate change with its own evidence.
- **The handoff is a `cut_handoff` record, not a path.** See "The handoff" below.

## The audio is NOT in the page, and the budget is gone rather than raised

R7.1 embedded each clip as a base64 data URI under a cumulative ~40 MiB budget. It failed in real
use exactly as a budget must: a **valid** clip became unplayable because *earlier* clips had spent
the page's allowance, and the tile said "not loaded for preview on this page" — a sentence about a
page limit rather than about that clip.

A tile now carries an **opaque URL and no bytes**, and the client creates **one** `<audio>` element
on hover and re-points it as the pointer moves. Measured in Chrome: a page of six clips totalling
**50.4 MiB** renders as **3,629 bytes** of markup, and the *last* clip — the one past the old
budget — plays unmuted at `readyState 4`.

Consequences that are requirements:

- **`MAX_EMBEDDED_BYTES` must stay deleted, not raised.** A bigger number is the same defect with a
  later threshold. A parametrized test asserts the name is absent from `audio_cutter` entirely.
- **There is no "could not load" state any more**, so the only remaining failures are genuine. The
  client reads `MediaError` and distinguishes a refused fetch (code 2) from an undecodable clip
  (3) from an unsupported format (4). A failed tile goes **amber, not red** — red is the Discard
  decision, and "I could not play this" must not look like "you rejected this" — and **stays
  classifiable**, because an unplayable clip is still one the operator may want to keep.
- `preview_markup` survives as the single-clip fallback with its own small cap. That is not a
  grid and is not the thing that failed.

## Authorization is reused, not reimplemented

`src/audio_media_server.py` is the audio sibling of `src/clips_media_server.py`. It claims **one**
prefix, serves GET/HEAD, is byte-range capable, and streams in bounded 64 KiB chunks. Everything
about *authorization* — the registry, the six gates, the opaque `(session, page, clip)` triple and
the **descriptor-identity revalidation** — is `beatsync_fork.clips_review`, shared byte for byte.

**A filesystem path never travels through the browser**, and `allowed_paths` is still untouched.
The module contains no path join at all; a structural test asserts that over the **syntax tree**,
never as a substring, because the module's own docstring explains the rule and a substring ban
fires on the prose documenting it. (That trap has caught this repository repeatedly — see
`.claude/rules/clips-preview.md`.)

**Why a separate middleware rather than a fifth prefix on `ClipMediaMiddleware`.** That class
claims exactly four prefixes and a test pins both halves of it. Audio needs no poster route, no
decision route and no state route, so adding a prefix there would widen a reviewed security
boundary to carry a feature that uses none of it.

### Two registries — historically necessary, now kept for a different reason

`ClipRegistry.new_session` revokes **every previous session that owner holds**. That is correct
within one review surface and was wrong across two: a shared registry would have let an Audio Review
scan silently revoke the Cutter's just-cut gallery, leaving every tile answering 403 with nothing on
screen to explain it. So `_AUDIO_CUT_REGISTRY` and `_AUDIO_PREVIEW_REGISTRY` are separate
instances, each **bound to its own prefix at middleware construction time** — the pairing is a
construction-time fact, not a lookup that could resolve a token against the wrong map. Verified in
the browser: a live token presented on the other surface's prefix answers `403 unknown_session`.

**[R15-C] That revocation conflict no longer exists, because the cutter's gallery does not.** Both
registries and both mounts are nevertheless kept, for the reason given at the top of this file:
`MEDIA_PREFIX_CUTTER` is the module-level default prefix, so un-mounting it would point that default
at an unmounted route. The practical state today is that nothing publishes into
`_AUDIO_CUT_REGISTRY`, so `/r7audio` answers `403 unknown_session` for every request — which is the
safe direction, and is the *reason* removing it needs its own change rather than being free.

`ClipRegistry.publish_page` gained a `servable` **allowlist** parameter defaulting to the video
`SERVABLE_SUFFIXES`, so every R5 call site is unchanged. It is an allowlist in both uses — never
widened to "anything", never derived from the request — and an **empty** one raises rather than
meaning "anything goes".

## Decisions are keyed by file identity, which is what makes paging lossless

The browser holds the per-page `clip_id`; the server maps it to
`decision_identity(realpath | size | mtime_ns)` and stores decisions and edited names against
*that*. Two consequences, both load-bearing:

- turning a page and coming back preserves every decision, with no per-page bookkeeping;
- a **re-recorded** clip hashes differently and therefore does not inherit the old clip's
  decision. Invalidation is structural rather than a check somebody has to remember to run.

`_audio_merge_payload` folds the hidden field into that record. An unreadable payload merges
**nothing** — it does not *clear* — because the opposite choice would let one malformed update
discard a page of real decisions. A clip id that is not on the current page is ignored.

**Decisions still live in the page until Confirm**, so a reload loses them. The Clips Preview's
durable journal is deliberately not duplicated for audio; do not half-implement one.

## Grid-relative lookup, which is why a second gallery was ever safe

Each grid carries `data-store` and `data-counts` naming **its own** hidden field and counts box,
and the client reads them off the grid the tile belongs to. R7.1 looked both up by document-wide
id; two grids under that scheme would both write the Audio Cutter's field, so Audio Review's
decisions would land on the cutter's clips — silently, and only once both tabs had been used.

**[R15-C] One gallery exists now, and none of this is relaxed.** The mechanism is what lets a future
surface — R15-B's Folder Batch — be added with **no client change at all**, so keep it: do not
re-key anything on a document-wide id just because there is currently one grid to find. Likewise
`AUDIO_GALLERY_CSS` is rendered **once**, in Audio Review; the cutter's duplicate copy went with its
gallery. Every CSS selector is a **class**, not `#r7-grid`, which is exactly why deleting one copy
left the surviving grid fully styled.

The grid also carries a **nonce**: a `gr.HTML` update whose markup is byte-identical to the previous
value is not delivered by the pinned Gradio, so Cancel, the empty state **and the R15-C handoff
retirement** repaint deliberately.

The **"Enable hover audio"** banner and every button that can move a file are matched by class
(`.r7-enable-hover`, `.r7-release-audio`). Releasing the player before a move is the difference
between a clip filing and winerror 32 — Windows will not rename a file a player still holds open —
and `removeAttribute('src')` + `load()` is the mechanism, because `src=''` re-requests the page URL
in Chrome.

**[R15-C] Two buttons carry `.r7-release-audio` now, not four**: Audio Review's Confirm and Proceed.
The cutter's pair went with its gallery, and with them the only controls outside Audio Review that
could move an operator's clips. The matching is still by class, so nothing in the script changed and
nothing needs to — which is the property to preserve, not the number two.

## Confirm is admitted by one KEPT clip, and the sweep is gated

R7.1 refused Confirm while anything was undecided, which made the operator click Reject on every
clip they did not want. The rule now:

```
>= 1 SELECTED            -> Confirm is admitted
explicit REJECTED        -> Discarded
unmarked (PENDING)       -> Discarded, but ONLY after an explicit, batch-bound confirmation
```

- **The counts are shown BEFORE anything moves**, naming keep / explicitly rejected / unmarked, and
  stating that nothing will be permanently deleted. Proceed and Cancel are server-revealed; the
  page cannot show a Proceed that is not attached to a specific set of counts.
- **`SortPlan.signature`** binds that confirmation to one batch: a sha256 over every clip id, its
  decision and **both destinations** (case-normalised, so a Windows path spelling does not re-arm
  it). It is a **change detector, not a credential** — nothing is authorised by presenting it, and
  Proceed re-derives the whole plan and re-validates both folders before moving a byte.
  `_on_audio_proceed` *is* `_on_audio_confirm` with the signature supplied, so there is no path on
  which Proceed moves a file Confirm would not have.
- **`sort_clips(unmarked_as=...)` is explicit**, so an unmarked clip can never be discarded by
  omission. A caller without confirmation passes `PENDING` and unmarked clips are left alone.
- The report keeps **all four counts distinct** — kept, explicitly discarded, automatically
  discarded, failed. "14 discarded" cannot tell an operator whether they rejected 14 clips or
  forgot to look at 12.

Filing is `audio_library.file_clip`, unchanged: one `os.rename`, which **refuses** an existing
destination rather than overwriting and **raises** across volumes rather than degrading into
copy-then-delete. Nothing in `audio_review` deletes anything — a test asserts the module reaches no
`os.remove`, `os.unlink`, `shutil.rmtree` or even `os.replace`. Identity is re-checked immediately
before each move, so a clip that changed after the scan is skipped rather than sorted on a stale
decision. One failure does not stop or undo the others; the partial batch is reported as one.

## Destinations are validated as typed AND again at Confirm

Reported from real use: both destination fields set to the same folder, and sorting proceeded.
`validate_destinations` is the one place that decides which check applies — the full pairwise
`validate_roots` when both will be written, `validate_one_destination` when only one will, so an
operator who keeps everything is never asked to name a quarantine folder.

`compare_destinations` exists because "are these two the same folder?" is answerable **without an
input folder**, which the full check cannot do: it identifies all three roots and returns on the
first failure, so before a scan it refuses with `empty_path` and never reaches the comparison the
operator needs. Verified in the browser as typed.

All of it compares **resolved real paths** with `commonpath` plus the Windows `(st_dev, st_ino)`
cross-check — never a string prefix, so `lib-evil` is not inside `lib`, while a trailing separator,
a `.` segment, a junction, a `subst` drive and a difference of letter case all *are* the same
folder. Refused: identical destinations, either nested in the other, either inside the review
folder, the review folder inside either, and the protected runtime directories.

**Re-validation at Confirm is not redundant.** The folder that was checked and the folder that is
written must be the same one, and a folder can be renamed between the two events. **The gallery
stays fully usable while the folders are being corrected** — only sorting is withheld.

## MP3 output

`FORMAT_MP3` is **appended** to `FORMATS`; WAV stays the default and stays first. `libmp3lame` at
192 kbps CBR, `-write_xing 1`, and **no `-ac` in any branch** so mono stays mono — forcing stereo
on a mono voice clip would double the bitrate it needs for the same quality.

- **`normalize_format` is total.** The old `FLAC if fmt == FLAC else WAV` form silently mapped MP3
  onto WAV, so adding a format to the widget alone would have written `.wav` files from an MP3
  selection.
- **The encoder is probed before anything is written.** A missing `libmp3lame` refuses in words
  rather than producing a folder of unplayable files, and a probe that cannot be *run* refuses too.
- **The selector is pinned against `FORMATS` by value.** The first browser smoke test of this phase
  found the core, the handler, the argv, the manifest and the probe all supporting MP3 while the
  `gr.Radio` still offered two choices — every unit test passed and the operator had no way to
  select it. Adding a format to the core without offering it now fails a test.
- **State the timing honestly.** An MP3 frame is 1152 samples, so the encoder cannot end on an
  arbitrary sample; LAME records delay and padding in the Xing/LAME header and a decoder that
  honours it reproduces the original length. Measured on the portable build: **+0.000 s** drift per
  clip at millisecond resolution, from WAV, MP3 and video sources alike. That is close, and it is
  **not** sample-exact — which is why WAV remains the default.
- **MP3 is a lossy re-encode, including from an MP3 source**, because the silences are found by
  decoding and there is no stream copy across an arbitrary cut point. The UI says so. Measured:
  26.2 % of the WAV size for a mono source (about an eighth for stereo); the advantage is file
  size, never quality.

`.mp3` is already in `audio_mix.SUPPORTED_VOICE_EXTENSIONS`, so the clips work in Audio Layers; a
test asserts **every** format the cutter can write is accepted there, so the two cannot drift.

## An unnamed folder is refused before it is made absolute, by the CORE's normaliser

`os.path.abspath("")` is the working directory, which for this app is its own install folder, so
folder text that normalises to nothing used to mean "review every render in `output/`".
`discover_audio` had the same ordering defect its video sibling had — emptiness tested on
`folder.strip()`, quotation marks removed only afterwards — and the same seven input shapes
(`""`, `"`, `""""`, `"   "`, `  "  "  `, `" "`, and `%VAR%` for an empty variable) enumerated the
install directory. Measured on `ffb828a1`.

It now calls **`core.normalize_folder_text`** and defines nothing of its own, the same way it
already shares the registry, the six authorization gates and `Discovery` — so a refusal cannot be
right on one tab and wrong on the other. The full contract, and the two orderings that are the
whole correction, are in `.claude/rules/clips-preview.md`; a test asserts this module contains no
`def normalize_folder_text` and that both tabs answer identically. Each surface keeps its **own**
refusal wording ("…the clips." / "…the audio clips."), its `input_manager.scan_folder` ordering and
its media allowlist.

`validate_destinations` was already correct here — it strips before it tests, and answers
`empty_path` — and is untouched. Nothing about serving, authorization, the sort plan or filing
changes: the refusal happens before the scan.

## The handoff  [R15-C]

`_on_audio_open_in_library` takes the finished cut's **`cut_handoff` record** — the Raw Cutter's own
type, reused rather than copied — and not a path string. Four properties, each closing something:

- **`build()` answers `None` below one published clip.** `audio_run_dir_state` was written whenever
  a run directory had been *created*, so an export in which every clip failed still offered its
  empty folder. `clips=` must therefore be `len(written)`, never `len(segments)`: a planned count
  makes the record's only gate unreachable.
- **`read()` re-validates it** coming back out of `gr.State`, where it is untrusted input.
- **One `isdir`, asked at press time**, never cached. A record describes a job that finished, not a
  disk that has stayed still. A folder that has gone is reported — never repaired, never silently
  replaced by a parent that still exists.
- **A successful handoff retires Audio Review's page; a refused one retires nothing.** This is R11's
  F-1 hazard in audio: a new folder in the box while the gallery, counts and an armed sweep still
  described the previous one meant Confirm sorted the previous folder. The asymmetry matters: a
  mis-press must not cost an operator the page they were working on.

## Retiring an audio page: revoke, do not repaint  [R15-C / F-1, F-2]

**A `gr.State` write is not a retirement, and getting this wrong cost a measured defect.** R15-C
first implemented the handoff's retirement by handing an empty view back into
`audio_preview_state` and calling that the mechanism. Independent review reproduced the
consequence deterministically on `77184e44`: a Confirm **already in flight** — Gradio gives a
handler its arguments at submit time, so it still carried the previous folder's page — sorted that
folder anyway. Three real clips left the retired folder under a terminal frame reading
`✅ Sorted all 3 clip(s).`, and the retired page stayed fetchable over the media route.

The rule, which is the one `.claude/rules/clips-preview.md` already states for the video side —
*"anything that needs to retire a page must call this rather than add a second notion of 'the
current page' beside it"*:

- **`_audio_retire_page` is the mechanism.** It calls the registry's own `revoke_owner`, the audio
  sibling of the Clips Preview's `invalidate(owner)`. Per-owner, so another browser session keeps
  its page; **not a decision**, so Keep/Discard marks survive untouched; and it **clears the page's
  clip map**, which is why one revocation closes the decision path *and* the HTTP path.
- **`_audio_page_is_live` is the gate**, and it asks the **registry** — never the view, a widget or
  a module flag. `_audio_confirm_core` consults it as gate 1b, **before** the payload is merged,
  before the plan is derived, before the destinations are validated and before any sort. A view
  carrying no tokens is not live either; treating the empty view as live would re-open the hole.
- **Revoke before repainting.** The repaint is the slow part to reach the browser; authority must
  be gone when the handoff is handled, not when its response lands.
- **The refusal paths revoke nothing**, and that is asserted against the registry rather than
  against widget values.

**What this does not promise — stated narrowly, do not widen [R15-C / F-3].** A *scan* already in
flight finishes by minting a **fresh** session for the folder it was scanning, so it can still
republish that folder as a live page after the handoff. This surface has no generation counter to
order a scan against a retirement, and adding one would be exactly the second notion of "the
current page" the rule above forbids. The residual is bounded to presentation, and the bound is the
reason it is acceptable: the republished page is **live**, is the page the gallery is actually
showing, and its own report names the folder it scanned — so a later Confirm acts on what is on
screen. **Authority is consistent; the folder textbox can be stale.** Do not restate this as "the
handoff fully orders a concurrent scan", and do not claim a browser-level proof that was not taken.

`loaded_text(record, destination=...)` names the section. The default stays `Video Review` so R11's
wording and its pinned tests are byte-identical; the audio caller passes `DESTINATION_AUDIO_REVIEW`.
Passing nothing would send the operator a confidently wrong instruction.

## Nothing scans as a side effect

An explicit requirement, and the easy thing to get wrong once a folder box has a remembered value.
Only the Scan button walks a folder. Loading preferences fills the box and reads nothing; the
destination `.change()` handlers validate and write one read-only textbox; "Open results in Media
Library" fills Audio Review's box, sets its recursion tick from the record's shape and selects both
the tab and the section. Paging reuses the list the scan captured —
re-enumerating per page turn would renumber pages under the operator and charge a disk walk for a
keystroke — and a **successful sort** re-enumerates, because files genuinely moved.

`_audio_confirm_core` returns `sorted_any` rather than letting the caller infer it from an empty
signature: every refusal returns an empty one too, so inferring would make a refused Confirm
trigger a folder walk and replace its own message.

## Isolation

No audio-review handler touches `process_video`, `analyze_beats_auto`, `create_music_video`,
`_RENDER_LOCK`, `resolve_for_render`, `live_declaration`, `RenderLifecycle`, `session_state`,
`source_state`, `prep_state`, `status_output`, `video_output` or `process_btn`; no registration
writes a render, source, preparation or creative widget. Every registration carries
`concurrency_limit=None` and joins no concurrency group — the measured S2-A R3 rule: a full group
leaves the event **queued** rather than delivering it. None of them launches a process; durations
go through the fork core's runner, like the poster pass, because two suites pin this module's
process-launch site count.

**Register event handlers explicitly per widget, never in a loop.** A loop body produces
`_field.change(...)` in the syntax tree, so every per-widget seam assertion becomes unable to find
the binding and the guard silently stops guarding. This suite hit that on its first run.

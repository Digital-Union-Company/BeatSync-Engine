# R8 UX Refresh — audit and record

Reference document. Never injected into context; read on demand.

The owner's complaint was not that any one feature was wrong — audio cutting quality and Comfort
Mix were explicitly accepted — but that BeatSync had become "a collection of independently added
tools" rather than an application. This records what was actually observed in the running UI, what
changed, and what deliberately did not.

## How the audit was done

A playtest instance was launched from the R8 candidate worktree on a free port (7880) using the
provenance-gated isolated launcher: production's embedded interpreter, the candidate's `src` first
on `sys.path`, and a hard assertion that `gui`, `logger`, `paths` and `raw_cutter_export` all
resolve inside the candidate and that `logger.ROOT_DIR` is the candidate root. Production (7860)
and the R7.2 playtest (7872) were never started or modified by this work; neither was listening
when the task began.

The UI was then inspected in real Chrome through the DOM rather than from screenshots, because
`Page.captureScreenshot` times out on this page — the rendered document is ~820 KB and the
renderer does not answer within 30 s. Reading the accessibility-relevant DOM per tab turned out to
be *better* evidence than pixels anyway: it enumerates every control, its widget type, its range
and its default, which is what the audit questions were actually about.

## Audit findings

Priority is the owner's stated priority — a coherent desktop-style experience — not defect
severity.

| # | Problem (observed) | User impact | Change | Priority |
|---|---|---|---|---|
| 1 | Seven top-level tabs named after features, not jobs: `Quick Render` implied a second slower render that does not exist; `Advanced Tools` was a drawer holding three unrelated tenants (voice layering, SFX, library preparation) | No way to predict where a function lives; the name teaches nothing | Seven workflow-named tabs in a fixed order: Render · Creative Settings · Raw Cutter · Audio Cutter · Audio Mix · Media Library · Settings | HIGH |
| 2 | Three views of one library in three unrelated places: `Clips Preview` tab, `Audio Preview` tab, and a *collapsed accordion* at the bottom of `Advanced Tools` | Reviewing and preparing a library meant knowing three locations, one of them hidden | One **Media Library** tab with three sections: Video Review · Audio Review · Prepare & Verify | HIGH |
| 3 | The Creative Preset radio sat on `Quick Render`; the six sliders it writes sat on `Creative Studio` | The one control and the six values it sets were on different screens | Preset moves to Creative Settings, beside its sliders (Commit C — coupled to finding 6) | HIGH |
| 4 | The remembered-folders panel was the last block of the `Clips Preview` tab | Nobody looks for application settings at the foot of a review screen | Dedicated **Settings** tab | HIGH |
| 5 | Video settings were one free-text `Custom FPS` number and nothing else. Render resolution is taken from the **first** source video, and the extraction path uses a direct `scale=width:height` | A 4:3, portrait or anamorphic source is **stretched**, not padded; and there is no way to pin an output format | Output Video Profile selector (Auto / 1080p · 720p at 25 · 30) using scale **+ pad** | HIGH |
| 6 | Two read-outs each claimed to describe the next render and neither was truthful: the preset radio can read `Balanced` while Freestyle, Surprise Me, a Variation Seed or an applied Director proposal are live, and the separate `Active optional settings` box listed overrides without relating them to the named profile | The displayed creative profile could be confidently wrong | One truthful effective-profile summary (`Custom — Balanced base + Freestyle`) | HIGH |
| 7 | Only the Audio Cutter offered `Open results in …`; the Raw Cutter offered nothing | Reviewing cut video meant copying a path by hand | Both cutters offer **Open results in Media Library** | MED |
| 8 | `Prepare & Verify`'s content was a single collapsed accordion | An operator who navigates deliberately to a section is shown one closed drawer | Opened — see Commit A | MED |
| 9 | Render had no way to use the configured default video Media Library as a source | The library path is configured once and then retyped every render | `Use Media Library` source mode | MED |
| 10 | Destination collisions were reported but the only resolution was manual renaming | Filing a reviewed clip could stall on a name clash | Collision policy: skip (default) or auto-suffix `_01`, `_02`… | MED |
| 11 | No visibility of, or control over, application caches | Disk fills invisibly; the only remedy was deleting folders by hand, which risks the deliberately-preserved Stage-5 cache | Settings → Cache Management, inspect and clear by category | MED |
| 12 | The Audio Cutter's exports have hard edges | Speech and SFX do not blend under music | Optional fade in/out, bounded by the configured Padding | MED |
| 13 | Every tab opened with multiple permanent explanatory paragraphs; the Render tab's own controls needed two side-by-side sub-columns to keep Create Music Video above the fold at all | Vertical scrolling, and real settings pushed below prose | Trim permanent text to a label plus one line | MED |

### Observations that were checked and did *not* become work

* **Widget duplication.** The whole tab restructure was verified mechanically to be a pure
  re-parenting: the complete set of `name = gr.X(` assignments in `gui.py` is identical before and
  after — 233 names, none added, lost, duplicated or retyped. This matters because a re-layout that
  copies a control instead of moving it gives one execution setting two sources of truth.
* **Raw Cutter already had the right export-profile set** (`Same as source`, 1080p/720p at 30/25).
  Its *pure* resolution and padding helpers are the ones to reuse for finding 5 — but the two
  workflows stay distinct, and the Raw Cutter's export profile is **not** a render setting.

## Commit A — navigation, Media Library and Audio Mix

| Was | Is |
|---|---|
| `⚡ Quick Render` | `⚡ Render` |
| `🎨 Creative Studio` | `🎨 Creative Settings` |
| `🔧 Advanced Tools` | `🎚️ Audio Mix` (library preparation moved out) |
| `🖼️ Clips Preview` (top-level) | `🖼️ Video Review` (Media Library section) |
| `🎧 Audio Preview` (top-level) | `🎧 Audio Review` (Media Library section) |
| Library Preparation accordion in Advanced Tools | `📚 Prepare & Verify` (Media Library section), **open** |
| Remembered folders at the foot of Clips Preview | `⚙️ Settings` tab |

There are now **7 top-level tabs, 10 `gr.Tab` calls and 2 `gr.Tabs` containers**. The second
container is named `library_tabs`, because reaching a section from a cutter's "Open results"
button needs **two** selections — the outer tab and the inner section. Returning only the inner one
applies an invisible selection underneath whatever tab the operator is already on.

### Two traps this commit hit, recorded so the next one does not

* **Indentation arithmetic.** A re-parented top-level tab body moves from column 16 to column 24
  (+8), not +4: the section header sits at 20 inside `library_tabs` at 16. The first attempt used
  +4 and produced an `IndentationError` — which is the benign outcome. The dangerous version of
  this mistake is one that still parses.
* **Prose tripping a substring guard, for the sixth time.** A comment explaining *why* the Raw
  Cutter's new button is registered outside the Video Review event span named both of that span's
  anchor tokens literally. `str.index` found the comment first, relocated the span, and the guard
  failed with `assert 1 == 4`. The comment now describes the anchors instead of spelling them. See
  `prose-mentioning-a-ban-trips-substring-bans`.

### What the structural tests became

The suites were updated to the new architecture **without weakening a behavioural check**. The
pattern throughout: where a test asserted a *position*, it now asserts the *property the position
was standing in for*, and spans are computed from whichever tab actually comes next rather than
from a named neighbour — so the next re-order cannot leave an assertion silently measuring the
wrong region.

| Test | Was | Is |
|---|---|---|
| `test_the_tab_set_is_exactly_the_documented_one` | 7 labels, 1 `gr.Tabs` | 7 tabs **and** 3 sections asserted separately, 2 containers — a section promoted to a tab now fails |
| `_tab_span(source, index)` | index into `_TABS` | `_tab_span(source, label)`, bound = next tab by source position |
| `test_the_clips_preview_tab_is_last_and_top_level` | "top-level, and LAST" | `test_video_review_hides_none_of_its_controls` — the property that mattered |
| `test_the_tab_is_top_level_and_sits_before_clips_preview` | a position among seven tabs | `test_audio_review_is_the_middle_media_library_section` |
| `test_the_occasional_tools_are_on_the_advanced_tab` | one list over two unrelated tenants | split into Audio Mix and Media Library assertions, so neither can drift into the other |
| `test_library_prep_is_the_last_block_in_the_advanced_tab` | ordering inside one tab | `test_the_sample_policy_span_still_holds` — states how the guarantee is now bought |
| `test_the_three_bulky_blocks_stay_collapsed` | four labels | three, plus an explicit positive test that preparation is **open** |

**The sample-policy span is the one invariant that needed a new argument rather than a new
anchor.** `test_media_sample.py` enforces the approved sample resource policy by *omission*: no
`gr.Slider` may appear between `INFO_PREP_SAMPLE` and the sample button's registration. That used
to hold because the preparation block was the last thing in the third of four tabs. It now holds
because **Prepare & Verify is the last Media Library section** — so Audio Mix's sliders sit
earlier in the source — and because only the Settings tab follows it, which is why every numeric
control in Settings is a `gr.Number`. That constraint is now written down in both the source and
the test instead of being an accident of ordering.

## Commit B — Settings, central paths, collision policy, cache management

Phases 3, 7 and 8. Two new stdlib-only fork modules, one new preference key, one new preference
*section*, and the end of a `shutil.rmtree` that ran before the operator had done anything.

### Preferences stayed backward-compatible, and that is measured

`state/preferences.json` keeps its schema string, `beatsync-folder-preferences-1`, deliberately.
`folders` keeps exactly the shape it had; `render_output` is **appended** to the key list, which
`load_preferences` fills with `""` when absent, so a file written by any previous version loads
with no migration step. Non-folder settings went into a separate top-level `options` section, so
an older build reads the file and ignores them — the correct outcome, since an older build has no
feature to apply them to.

The one real hazard this created is a writer that drops a section it does not own.
`save_preferences(path, preferences)` replaces the whole file, so the two-argument form every
pre-R8 call site uses would have deleted the options — the operator would have watched a setting
reset itself every time they pressed Save defaults. It now reads the stored options back and
writes them out again, and that is asserted directly: a two-argument save after an options-only
write must leave the options intact.

### Where the Render output folder does and does not reach

`get_render_output_dir()` resolves the configured folder at **call time** (so saving takes effect
on the next render, not the next restart), uses it only if it **already exists** (it never creates
a folder — a remembered path is a suggestion, and a folder that has gone away is reported through
the existing `unavailable_notice`, for free, because `RENDER_OUTPUT` is a destination key), and
governs the **published** render only. `session_dir`, the processing directory, the Gradio upload
folder and every cache keep resolving through their own constants. The share-staging folder stays
a child of whatever the resolver returns, so the final step is still a same-directory rename and
`EXDEV` remains structurally impossible rather than merely unlikely.

It has exactly two callers — the render's publication and Open Output Folder — and a test pins
that count, because a second resolver is precisely how the button could start opening a folder the
render no longer writes to.

### The collision policy is opt-in, and the default is provably unchanged

`beatsync_fork/collision.py` is one small module both review workflows now share. Four properties
make the opt-in safe, and each is asserted:

* **The rename is the authority, never an `exists()` check.** `publish()` advances to the next
  candidate when the caller's no-replace rename *reports the name is taken*. There is no window in
  which another process could create the file between a check and a move, because the check **is**
  the move. A test asserts the module contains no `os.path.exists`, `os.stat`, `listdir` or
  `scandir` at all.
* **It retries.** Two review sessions filing into one library will race, and the loser should take
  `_02`, not report an error.
* **`SKIP` attempts exactly one name.** That is what makes the default *provably* identical to the
  pre-R8 behaviour rather than merely similar to it — and `NoFreeName.attempts == 1` is the signal
  both callers branch on to reproduce their original wording verbatim. Callers branch on the
  attempt count, never on the policy they passed.
* **The original is never lost.** Exhaustion raises with nothing moved.

One pre-existing pre-check had to **go** rather than be kept: `move_decided_clip` used to test
`os.path.exists(target)` to produce its `OCCUPIED` message. Keeping it would have short-circuited
the suffix sequence before the rename could advance it. The message now derives from the rename's
own `FileExistsError`, which is strictly more accurate — it reports the state at the instant of the
move rather than at the instant of an earlier look.

Measured on real files through real `os.rename`, three clips all wanting one name:

| Policy | Published | Library afterwards | Sources left |
|---|---|---|---|
| `skip` (default) | 1 of 3 | `clip.wav` | the other 2, bytes intact |
| `suffix` | 3 of 3 | `clip.wav`, `clip_01.wav`, `clip_02.wav` | none |

Under `suffix` the three written files held three distinct payloads, so nothing was overwritten on
the way; and the clip that kept its own name carries **no** rename note, which is what stops the
report annotating lines that did not change.

### Cache management: containment is the whole mechanism

`beatsync_fork/cache_admin.py` never discovers a directory. `gui.py`'s `_cache_categories()` is the
only function that chooses what may be cleared, and it names four roots; the module proves every
path it touches lies inside the root it was handed, by `realpath` + `commonpath` rather than a
string prefix. So the never-delete list is enforced by **construction**, not by a blocklist a
future edit could get wrong — and a test asserts the absence of `get_output_dir`,
`get_render_output_dir`, the two input-media directories, `PREFS_PATH` and `CLIPS_STATE_DIR` from
that builder.

`CLIPS_POSTER_DIR`, never `CLIPS_STATE_DIR`, is the subtlety worth stating: the review decision
journals live in the *parent* of the poster cache, so a category pointed one level up would delete
the operator's decisions.

Two further properties, both measured against a real filesystem:

* **`os.walk` descends an NTFS junction** (measured previously in this repository), so a junction
  inside a cache would turn "clear 40 MB of thumbnails" into "delete whatever it points at".
  Reparse-point entries are pruned from `dirnames` in place, `followlinks=False`, and both are
  pinned by test.
* **A file an active job holds open is reported and kept, and so are its parent folders.** Files
  are removed individually and only *empty* directories are pruned with `os.rmdir`, which refuses
  a non-empty one — so anything the clear declined to delete keeps its parents alive instead of
  being swept away inside a recursive removal. `shutil.rmtree` appears nowhere in the module.

Verified in the running GUI: with 3 posters (150 KB), 3 processing files (500 KB) and a 400 KB
Stage-5 cache entry seeded, Inspect reported all three accurately with the expensive warning on
the analysis cache; Clear with the two cheap categories selected and the confirmation **unticked**
refused and deleted nothing; ticked, it freed 650 KB and 6 files and pruned one empty folder. The
decision journal, the unselected Stage-5 cache, a finished render in `output/`, the operator's
`input/audio` track and both cache roots all survived.

### `cleanup_on_startup` no longer deletes browser uploads

It used to `shutil.rmtree` everything under `input/` except three protected names — including
`gradio_uploads`, before the operator had done anything. A browser upload is a copy the operator
made *into* the app, and for some of them it is the only copy they still have a path to.

The replacement is the simplest thing that is reversible: one `os.rename` into
`input/retired_uploads/<timestamp>/`. No index, no manifest, no restore command, no second copy —
the files are simply still there under a name that says what they are. `retired_uploads` joins the
protected set, because otherwise the next start would delete it and reproduce the old behaviour one
run later. An empty `gradio_uploads` is skipped, so no empty folder accumulates.

It is bounded only by the operator's own clearing — one subdirectory per start that had uploads —
which is exactly why it is a first-class Cache Management category with its size on screen rather
than something that silently grows.

This is `gui.py`'s **third** `os.rename`, where a test previously pinned exactly two. The
invariant was never "two renames"; it was "no further durable publication into `output/`", and
that is preserved exactly: the retirement writes under `input/`, writes neither
`RENDER_OUTCOME_KEY` nor `LAST_OUTPUT_PATH_KEY`, is unreachable from the render body, and the two
publication renames are still asserted to be exactly two. The test now says all of that instead
of counting to two.

## Commit C — one truthful Creative Profile, and the Media Library as a source

Phases 4 and 5. These are one commit because they are one change: the preset selector could only
leave the Render tab once something truthful replaced it.

### The defect, precisely

The Creative Preset selector is an honest read-out of the six sliders — `matching_preset`
recomputes it from their live values, so it is never wrong *about them*. The "Active optional
settings" box was an honest list of the extras. Each was individually true, and **together they
were misleading**: the screen could say `Balanced` while Freestyle rules, a Variation Seed and SFX
were armed, and nothing anywhere said the render would not be a plain Balanced render.

### What replaced them

One resolved line on the Render tab, built by `beatsync_fork/effective_profile.py`:

```
Balanced                                 an untouched screen
Cinematic                                a different recipe, nothing else on
Custom                                   the six match no recipe
Balanced + Freestyle (1 section rule)    the reported defect, fixed
Custom + Variation Seed 7                measured live in the running app
```

### Why there is no "Custom — based on Balanced"

The task's examples included it. It is **not implementable truthfully**, and the reason is a
deliberate pre-existing decision rather than an oversight: this application stores no "last
selected preset" anywhere, by rule (`.claude/rules/creative-presets.md`) and by test. Six integers
describe a render completely, so a preset name is recomputed from them and then stops existing —
which is what lets an old render reproduce from *its recorded values* rather than from a label
whose recipe may since have been retuned.

A remembered base would reintroduce exactly the second source of truth that decision removed, and
would then be able to **disagree with the sliders** — the same class of defect this commit exists
to remove. So a base is named only when it is verifiable: when the six live values match a recipe,
that recipe *is* the base and is named; when they match none, there is no base to name and the
answer is `Custom`. Both are recomputed from live state, so neither can go stale. A test asserts
the module acquired no remembered-base state.

### Why the six sliders came back into the read-out's inputs

R1 had deliberately removed them, and the reason it gave is exactly the reason it is now safe to
undo. R1 removed them because the preset radio sat on the Render tab and could change all six
while the box — refreshed only on tab selection — went on showing the old answer. That was the one
staleness hole the read-out had.

Commit C moves the preset to Creative Settings, so **every** control the box describes now lives
on another tab, and the `.select()` that fires on the way back is sufficient for all of them. The
hole is closed by the move, not by the omission — and the omission was what let the screen claim a
plain `Balanced` render while Freestyle was armed. No creative widget gained a second
registration, which is the guard R1 refused to relax and this commit also does not.

### "Reset Creative Overrides" was deferred here — and the reason I gave was WRONG

Phase 5 offered it as optional ("if useful"), and Commit C did not build it. That decision was
defensible; **the justification written for it was not, and it is corrected here rather than
quietly edited away.**

What this document and `ROADMAP.md` originally said was that selecting the `Balanced` preset
"already does it". That is false. A preset writes **the six base sliders and nothing else** — by
rule, by test, and by the design note in `.claude/rules/creative-presets.md` that a preset is a
name for six numbers. It does **not** clear the Variation Seed, it does **not** disable Freestyle,
and it does **not** touch anything in Audio Mix. So the thing the operator was told already
existed did not exist, and the half of the state they most likely wanted to clear was exactly the
half `Balanced` leaves alone.

The standing prohibition is also narrower than I implied: it forbids a reset **for the six global
controls**. An action that resets the seed and Freestyle and deliberately *leaves the sliders
alone* is not that reset, and never was.

**R8 Final Polish therefore builds it**, as `🧹 Clear creative variations`, with the two actions
disjoint by construction:

```
Balanced preset            ->  the six sliders, and only those
Clear creative variations  ->  the Variation Seed and Freestyle, and only those
```

Neither is a superset of the other, so neither can honestly be called "reset everything" — which
is the property that keeps a second source of truth from appearing. It writes no slider (pinned by
test), no preset, and no Audio Mix widget, and it **reads** the ten section dropdowns rather than
clearing them, so turning Freestyle off leaves the rules intact and ticking the box again restores
them. It is the seventh writer of `variation_seed`; that matrix was extended by exactly one, with
the reason recorded beside it.

### "Use Media Library" is not a third source mode

It is a local folder whose path the operator configured once instead of typing every time, so it
maps onto the **existing** `LOCAL_FOLDER` mode and reuses the whole engine: the same scan, the
same `SourceSnapshot` identity, the same confirmation gate, the same invalidation, the same media
analysis. A third `SourceMode` member would have been a second source concept for the snapshot,
the session state and every test that enumerates modes to carry forever — for a convenience.

The required invalidation behaviour then comes **by construction rather than by a new rule**:
selecting it *writes* `source_folder`, and that textbox's own `.change` already invalidates a
confirmation. Measured in the running app with a configured library: selecting the option filled
the folder in and the report immediately read `Folder changed. Press Scan Folder.` with Create
Music Video disabled and the status `NOT CONFIRMED` — identical to editing the folder by hand,
because it *is* editing the folder.

An unconfigured library changes nothing and says where to set it. Clearing the box would silently
destroy a path the operator had already typed; inventing one would point the render at a directory
nobody chose. A configured-but-missing folder is filled in anyway **and reported**, so it can be
corrected — and nothing is created, the same rule the whole preference store follows.

## Commit D — Output Video Profiles, and soft edges on audio clips

Phases 6 and 9.

### The stretching defect, and why Auto keeps it

The render took its resolution from the **first source video** and fitted every other clip to it
with a bare `scale=width:height`. A portrait phone clip in a 1920×1080 timeline came out
*squashed*, not pillarboxed; a 4:3 or anamorphic source the same. The only video setting was a
free-text "Custom FPS" box — a frame rate with no resolution beside it, which is half a setting,
and the missing half is where the defect lived.

The Output Video Profile **replaces** that box, in the same position and at the same index in
every handler signature it feeds — so the positional alignments several suites pin are preserved
by *arity* rather than by renumbering.

**Auto is the untouched historical path, including the stretch.** That is deliberate: fixing the
geometry unconditionally would silently change every render an operator has already tuned and
compared. Auto is structurally the same code, not merely equivalent — `resolve` answers `None`,
every helper returns its caller's fallback unchanged, and `geometry_filter` returns `""`, which
the extractor treats as "emit what you always emitted". The corrected geometry is what *choosing*
a profile buys.

### The geometry is reused, not reimplemented

The Raw Cutter already had a measured scale-and-pad chain. It was extracted as
`raw_cutter_profiles.geometry_filter(width, height)` and `video_filter` now composes it —
**byte-identical output for all four cutter profiles**, verified before anything else changed. The
two workflows stay distinct (each owns its own `trim` and `fps`); only the pixel geometry is
shared.

In the extractor the chain **replaces** the bare scale rather than being appended to it: the chain
contains its own `scale`, and running both would fit the picture twice. A test pins the `if/elif`.

### FFprobe verification — 4 profiles × 5 source shapes = 20 renders

Every one produced exact dimensions, exact CFR, and SAR 1:1, with the picture's own aspect ratio
preserved inside the padded frame:

| Source | → 1080p | → 720p |
|---|---|---|
| portrait 1080×1920 | 608×1080 picture, pillarboxed | 405×720, pillarboxed |
| 4:3 640×480 | 1440×1080, pillarboxed | 960×720, pillarboxed |
| **anamorphic 720×576 SAR 16:15** | **1440×1080, pillarboxed** | **960×720, pillarboxed** |
| 720p 1280×720 | 1920×1080, exact fit | 1280×720, exact fit |
| 1080p 1920×1080 | 1920×1080, exact fit | 1280×720, exact fit |

The anamorphic row is the one that proves the SAR-squaring step: 720×576 is a **4:3** picture, not
5:4, and fitting it by coded size would letterbox it wrongly *and* squash the content. It comes
out as a true 4:3 1440×1080 picture.

### Two signature contracts I had to respect rather than renumber

* `test_every_seam_takes_lifecycle_last_and_defaulted` requires the cancellation token to be the
  **final** parameter of every cancellable entry point. So `geometry` and `output_profile` go
  *in front of* `lifecycle` in `extract_clip_segment_ffmpeg_detailed` and `create_music_video`.
  Every call site passes both by keyword, so nothing shifts.
* `test_the_extract_kwargs_bundle_carries_only_the_command_plus_the_cancellation_token` pins the
  kwargs bundle by exact list. `geometry` **is** command-shaping, so it joined the command-shaping
  assertion rather than sitting beside `lifecycle` — the prefix grew from eight keys to nine, and
  the structure of the guard (exact list + explicit prefix) is unchanged.

### Soft edges: a fade may consume only the padding, never the sound

The distinction that makes Phase 9 safe is between the pad **setting** — a request — and the
padding a clip **actually received**. `plan_segments` gives each side at most half the silence it
is reaching into, so a clip in a crowded passage may get far less than asked for, or none at all.
`Segment` now records the applied padding, and the fade is bounded by *that*. "Never attenuate a
spoken syllable" is therefore a computation, not a hope: a clip with 10 ms of lead-in fades for at
most 10 ms, and one with none does not fade in at all.

Two further clamps protect short clips: neither fade may exceed 40 % of the clip, and the two
together can never overlap (which would leave a clip quiet in the middle).

The planner is untouched — fades change what is *written*, never where a clip begins or ends, so
the non-overlap guarantee is preserved by construction. Off by default, and when off **no `-af`
argument is emitted at all**, so an already-accepted export is bit-identical.

Measured through real FFmpeg on a **continuous tone** (the first fixture used silence either side,
which made the test vacuous — the head measured −91 dB whether or not a fade was applied, so both
branches would have "passed"):

| Format | hard cut | with fade |
|---|---|---|
| WAV | head = body, 0.00 dB | head 15.30 dB quieter |
| FLAC | head = body, 0.00 dB | head 15.30 dB quieter |
| MP3 | head ≈ body, −0.40 dB | head 14.80 dB quieter |

The report shows the **effective** fade range across the planned clips, not the setting, and names
how many edges had no padding and were left sharp — because those two numbers differ exactly when
the operator needs to know.

## Commit E — text, duplication, and what was left undone

Phase 10, plus the verification pass.

### Permanent text

The audit's finding 13 was visible in the very first screenshot: the Render tab's single largest
block of text was the *Cancel Active Render* explanation, taller than the button it sat under.
Four of the longest permanent paragraphs were cut without losing a claim anyone acts on:

| Constant | Was | Is |
|---|---|---|
| `INFO_CANCEL_RENDER` | 530 chars | 150 |
| `INFO_OPEN_OUTPUT_FOLDER` | 501 | 140 |
| `INFO_TAB_CLIPS_PREVIEW` | 606 | 200 |
| `INFO_TAB_AUDIO_PREVIEW` | 678 | 240 |

The Render tab went from **1686 px to 1418 px** at a 1440-wide viewport — 16 % shorter, measured.

Two rules were followed rather than invented. `test_open_output_folder.py` pins three claims in
that help text (whose machine opens it, that it is Explorer, that nothing is downloaded) and all
three survive. And the supported-format list left the Audio Review section intro for the folder
field's own hint, because it matters at the moment of choosing a folder rather than as a
permanent paragraph above everything.

### Duplication

`Video Source` and `Processing Mode` each printed their name twice — once as a group heading and
again as the radio's own label. Both radios now carry `show_label=False`; the label **constant is
still passed**, so assistive technology and every test that reads it are unaffected.

### Accordions were NOT the tool for this

The obvious move — put long text in a collapsed help accordion — is unavailable in exactly the
places that needed it most. `test_the_raw_cutter_is_a_top_level_tab_not_an_accordion` and
`test_video_review_hides_none_of_its_controls` both assert `"gr.Accordion" not in <span>`, and
they are right to: those tabs exist because burying controls behind a click is the problem this
refresh is fixing. A help accordion would have tripped guards written against hiding *controls*.
Shortening the text is the better answer anyway — an accordion keeps the words and adds a click.

### What a fresh pair of eyes should look at first

Recorded honestly rather than left for the owner to discover:

* **Settings' Browse buttons sit in their own narrow column**, so the row reads as a wide text
  box with a small detached button. It works; it is not pretty.
* **The Creative Settings tab is 3516 px tall** — by far the longest screen in the app. Variant
  Lab, the Director and Freestyle are all legitimately large, and the three bulky ones are
  collapsed, but this tab is the next candidate for the same treatment the Render tab just had.
* **`output_profile` is now a parameter name in two unrelated handler families** — the Raw
  Cutter's export profile and the render's output profile. They are in separate function scopes
  and the *widgets* are distinct (`cut_output_profile` versus `output_profile`), so there is no
  defect, but the name is ambiguous to a reader.
### The first-Confirm Proceed/Cancel visibility issue: checked, and it does NOT reproduce

The brief asked for this to be checked and fixed if reproducible. It was driven end to end in the
running app — three real WAV clips, Media Library → Audio Review, Scan, mark one Keep, press
Confirm with two still unmarked — and the gate behaves correctly:

```
proceed : flex 913x40  "➡️ Proceed — sort it as listed"
cancel  : flex 913x40  "↩️ Cancel — go back and mark m…"
report  : ⚠️ Some clips are still unmarked. Nothing has moved yet — here is what Confirm would do:
clips   : all three still in the review folder; library and discarded both EMPTY
```

The server was observed sending both `{"visible":true,"__type__":"update"}` objects, and the
buttons are 913×40 on screen.

**I first recorded this as reproduced, and that was my measurement error — worth writing down
because it is an easy one to repeat.** The first probe polled with
`for (…) { btn = document.getElementById(…); if (btn) break; }` and then read `display`.
Existence and visibility are two different transitions here — the same button was observed
**absent** from the DOM before any interaction, and **present with `display: none`** shortly after
the click — so `if (btn) break` exits on the first of the two and the subsequent `display` read
lands in the window before the visible state settles.

Worse, the follow-up looked like corroboration: pressing Confirm a second time swept the unmarked
clips, which reads as "the gate armed invisibly and the only visible button did the destructive
thing". It is in fact the documented behaviour — *"with a signature that still matches, the sweep
proceeds"* — and the second press was a legitimate confirmation of a gate that had been shown
correctly.

**The rule: when a control's visibility is the thing under test, never poll for the element's
existence.** Poll for the property itself (`getComputedStyle(el).display !== 'none'`), or wait on
the network response. An element that exists in both states makes existence a constant, and a
probe whose condition is already true measures nothing.

---

## Commit F — the final polish pass

A separate brief, after the owner reviewed the shipped screenshots. Four items, all practical:
keep-only classification for Video Review, a shorter Creative Settings tab, a readable Settings
layout, and a compact Render input area. No new audit, no architecture change.

### Video Review now matches Audio Review — and the plan model is now written once

Confirm used to require *every* clip decided. It now enables as soon as **one clip is marked
Keep**, and if anything is still unmarked it shows a second step with exact counts, both
destinations and Proceed / Cancel. Proceed treats the unmarked clips as Discarded **for this
batch only**; Cancel changes nothing.

The important part is what was *not* written. Audio Review already had this exact mechanism —
`SortPlan`, `plan_sort`, `plan_preview_lines` — so rather than build a second one, those three
were **moved verbatim into `beatsync_fork/clips_review.py`** and re-exported from
`audio_review.py` as plain aliases. `audio_review.SortPlan is clips_review.SortPlan` is literally
true. Two copies of a safety rule is one copy too many, and the brief said not to introduce
another transfer engine.

Three properties are pinned by tests because each is a way this could go wrong quietly:

* **Proceed is the same handler as Confirm.** It re-derives the plan, re-validates the
  destinations and re-checks the signature; the only difference is that by then the arming state
  matches. A second entry point could have skipped one of those.
* **The sweep writes no decision to the durable record.** PENDING→REJECTED happens in a local
  `decision_of`, so "for this batch only" is structural rather than remembered.
* **A signature from a different batch cannot authorise a sweep.** Change a decision or a folder
  and the operator is asked again.

Runtime, four real clips, one marked Keep: Confirm enabled at `Kept 1 · Undecided 3`; the gate
listed `1 keep → library`, `3 unmarked will ALSO be moved to Discarded`; **Cancel left all four
files in the source folder**; Proceed produced `library: 1`, `discarded: 3`, **4 files still on
disk**, original filenames intact.

### Creative Settings: 3516 px → 1375 px

The previous pass listed this tab as the next candidate. Measured again after: **1375 px**, a 61%
cut, achieved almost entirely by ordering rather than deletion. The controls reached on every
render come first (preset → seed → six sliders) and the three large panels — AI Director,
Freestyle, Variant Lab — are collapsed below them. All three were verified to still expand.

`test_all_three_creative_panels_are_collapsed` **replaces**
`test_the_ai_director_and_freestyle_are_no_longer_collapsed`, and the reversal is deliberate:
GUI Refresh A opened those panels because they were easy to miss in a single long column. That
was right then. Discoverability costs one tab visit; scrolling costs every visit.

### "Clear creative variations" — and the claim it replaces

The previous pass deferred a reset action with the justification that *selecting Balanced already
does it*. **That was wrong, and the owner corrected it.** Selecting Balanced writes the six base
sliders and nothing else — not the Variation Seed, not Freestyle, not Audio Mix. The false claim
has been removed from this document and from `ROADMAP.md`.

The action that now exists is the disjoint half: **Variation Seed → 0 and Freestyle → off, with
the six base sliders and Audio Mix untouched.** Two actions that write disjoint sets of widgets
are not two sources of truth for one state — `test_clear_creative_variations_is_disjoint_from_the_preset`
pins exactly that, and the seed-writer matrix went 6 → 7.

Runtime: seed 42 + Freestyle on → press → seed 0, Freestyle off, `cut_density` and `micro_cuts`
unchanged at 50.

### Settings: nine Browse buttons that now belong to a field

The previous pass flagged this as "works; it is not pretty". The cause was structural: Browse
buttons were collected into their **own Row below** a Row of two text fields, so each button sat
under the gap between two boxes. Now one field per Row with its own Browse beside it — measured
on the page, all nine at `sameRow=true`, 1 px to the right of their field.

The nine paths are also grouped by purpose (render/video, audio/voice, review destinations), and
**Cache Management is the one collapsed block** on the tab: it is occasional maintenance with a
destructive button in it, not setup. The deletion logic is untouched — no defect was found and
the brief said not to change it without one. Preference keys, values and the save/reset handlers
are unchanged; a `preferences.json` written by an earlier build still loads, and the defaults
were verified to survive a restart.

### Render: the drop zone is bounded, and the native picker is NOT available here

The music drop zone claimed roughly 190 px for a single-file control; it is now 113. With two
redundant wrapper headings removed (`Video Settings` and `Output Settings` each named the single
control beneath them) and the profile help cut from three lines to one, **Create Music Video
moved from y=903 to y=826 at 1440 CSS px** — from just below a 900 px fold to above it.

The brief also asked whether the native Browse used by every other path field could be offered
here. It was built, measured and **reverted**. A `gr.File` refuses to display a path outside
Gradio's allowed roots:

```
gradio.exceptions.InvalidPathError: Cannot move <chosen path> to the gradio cache dir because it
was not created by the application or it is not located in either the current working directory
or your system's temp directory.
```

The dialog opened and returned the operator's choice correctly — the refusal is downstream, in
`processing_utils._check_allowed`, and the component renders "Error". The three ways around it
are all worse than the button: widening `launch(allowed_paths=…)` exposes a whole tree to any
request the browser can make (the exact thing R5's authorized middleware exists to avoid);
copying the file into an accepted directory is the duplication of large local files the brief
ruled out; replacing `gr.File` with a path textbox rewrites the render source pipeline and loses
drag-and-drop. `tests/test_render_music_input.py` carries the measurement and pins the revert.

### End-to-end, after all four items

720p · 30 fps selected in the UI, four 320×240 sources, real render through the running app:
`width=1280 height=720 r_frame_rate=30/1`. Fixed profiles still produce exactly their dimensions.

### A defect found in the playtest harness, not the app

`launch_playtest.py` called `app.launch()` without `app_kwargs`, so none of the three media
middlewares were attached. Posters still drew, but every clip decision POSTed to a route that did
not exist and came back **404**, surfaced only in `window.__r5Last`. A silent dead click in a
harness is indistinguishable from a broken feature. The launcher now mirrors `gui.main()`'s
launch arguments exactly.

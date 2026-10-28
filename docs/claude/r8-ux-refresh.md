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

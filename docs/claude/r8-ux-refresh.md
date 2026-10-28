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

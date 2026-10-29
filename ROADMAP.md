# BeatSync Engine — Roadmap

What is built, what is next, and what is only an idea. This is the **product** roadmap; the
record of what actually changed, and when, is [`CHANGELOG-FORK.md`](CHANGELOG-FORK.md), which is
also the AGPL-3.0 §5(a) modification record. The two answer different questions and neither is a
substitute for the other.

**Nothing here is a scheduled release.** There are no dates, no committed deliveries and no
versions promised. `PLANNED` means *we know what we would build and roughly how* — not that anyone
has started, and not that it will be built at all.

---

## Raw Cutter — preparing usable source footage

Historically tracked as **R — Raw Pre-Cutter / Ingest Splitter**. The purpose has not changed: take
long, unedited source video and turn it into clean, individually usable shots **before** Media
Library Preparation and Stage-5 analysis ever see it. A library of good short shots produces better
music videos than a library of long takes, and it analyses faster.

That concept has since become a dedicated **✂️ Raw Cutter** tab in the GUI rather than a background
ingest step, because the decisions it makes — which shots are worth keeping — are the operator's,
not the pipeline's.

### The principle that governs all of it

> **Raw Cutter optimizes for clean, usable footage — NOT for preserving 100% of the source video.**

Discarding unsuitable intervals is correct behaviour. A clip that spans a scene change is worse
than no clip, so material that cannot form a clean continuous shot is skipped and the report says
so. Anything in this roadmap that appears to conflict with that principle is wrong.

---

### Current priority (product-owner decision)

Automatic dissolve detection is **no longer the next feature**. The R2-A research measured the
candidate signals on real labelled footage and neither was reliable enough to ship; the findings are
preserved under R2 below and must not be read as a detector that exists.

The priority is now **processing many source videos into usable clips with minimal operator
interaction** — R4's batch processing, promoted from "when feedback justifies it" to the active
piece of work. A missed gradual transition is an accepted limitation: the operator cleans up
afterwards if needed.

> **Detect what we can, cut what we can, skip short unusable shots, and keep going.**

A **Review Assistant Cutter** — any workflow requiring per-cut manual confirmation — was considered
and **rejected by the product owner**. Do not reintroduce one.

**R5's Clips Preview Selector is not that, and the distinction is load-bearing.** It reviews clips
**after** the Raw Cutter has already produced them, in its own tab, as a separate pass the operator
chooses to run. Cutting itself stays uninterrupted and uninteractive. A design that put clip
approval *inside* the cut loop would be the rejected idea wearing a new name.

With batch processing shipped and deployed (R4-A) and live progress now merged and deployed (R4-B),
and output naming merged and deployed (R4-C), the active work is
**triaging the result** (R5 — the read-only review phase is implemented; moving the reviewed
clips is not). The ordering is in the status summary at the
end of this document.

---

### R1 — Raw Cutter V1 · **DONE (merged in PR #68)**

Mechanical, deterministic, quality-first shot selection. No AI involved anywhere.

- Independent **fourth GUI tab**, beside Quick Render, Creative Studio and Advanced Tools.
- **One local video file** by absolute path, read in place — nothing is uploaded or copied.
- **User-selected output directory**; each run writes into its own fresh timestamped subfolder.
- **FFmpeg scene detection** with a configurable sensitivity slider (default 50, which is the same
  scene-score threshold the rest of BeatSync already uses).
- **Minimum and maximum exported clip length** (defaults 5 s and 300 s).
- **Quality-first shot selection**: a shot is the interval between two confirmed scene changes, and
  it is either exported or skipped as a whole.
- **Short shots are skipped, never merged** with an unrelated neighbour to reach the minimum.
- **Analyze / Preview before export** — it writes nothing, and reports kept shots, skipped shots,
  the reason for each skip, and the expected exported and skipped durations.
- **Remove Audio** (default on), which removes the audio *stream*, not just the signal.
- **NVENC or CPU H.264** export, preserving the source's resolution, aspect ratio and frame rate.
- **Safe staged publication**: clips are written to a task-owned staging file and published by an
  atomic no-replace rename, so an existing file is never overwritten and a failure never deletes a
  file the run does not own.
- **Cancellation between clips**, keeping everything already completed.

**Playtest coverage.** `D:\BeatSync-Playtest-dd059fb1\` was built from `dd059fb1`, which predates
this work, so the Raw Cutter is **not** in that package. `D:\BeatSync-Playtest-77b854a1\` was built
from `77b854a1` and **does** contain Raw Cutter V1 but not batch mode. Batch mode is in the later
packages, built from `dd2e08e4` (PR #69) and `894814aa` (scene guards and VFR-safe exports).
**No Playtest package contains R4-C**: it merged and deployed after the latest package was
built, and no Playtest has been built from `0aed35d6`.

---

### R2 — Transition Detection + Edge Cleaner · **DEFERRED, NOT IMPLEMENTED · non-blocking**

**Deferred by the product owner.** It was the next priority; the R2-A research then established
that the signals tested are not reliable enough on real footage, so nothing is built and nothing
is scheduled. This does **not** block R4 batch processing, which ships without it.

What the research established, kept because a negative result is the expensive part:

- A frozen **contrast-dip** rule found 3 of 5 gradual transitions on a freshly labelled real video
  — **60 % recall against an 80 % target** — at 0.48 false positives per minute. Ordinary footage in
  that same video produced *deeper* contrast dips than the two transitions it missed, so no
  threshold separates them. Not a calibration shortfall.
- A complementary **blend-linearity** signal scored 6/6 with zero false positives on synthetic
  fixtures and then produced **zero events** on both real videos. On real footage ordinary content
  fits the linear-blend model *better* than a true cross-dissolve does — the ordering is inverted,
  and the synthetic fixtures had encoded the model's own assumption.
- Both signals fail on the **same** class, dissolves between visually similar shots, for unrelated
  reasons. They are therefore not complementary where it matters.

**No transition detector exists anywhere in the product**, and no speculative scaffolding for one
was left in the code. Reviving R2 needs more independently labelled footage, with the candidate rule
frozen before it is measured.

The earlier edge-only measurement that started all this is still accurate and still worth reading.

Ordinary hard-cut detection misses **gradual** transitions. A cross-dissolve, a fade, or any blend
between two shots leaves *mixed frames* — frames containing part of the outgoing shot and part of
the incoming one — and those frames currently end up inside an exported clip.

**The measurement that defines this work is already done** and is recorded in
[`docs/claude/raw-cutter-edge-cleaner.md`](docs/claude/raw-cutter-edge-cleaner.md).

> That document records a **negative** result. FFmpeg's adjacent-frame `scene` score was measured
> against four synthetic fixtures and found **insufficient** to separate a slow dissolve from
> ordinary camera movement: a 2-second cross-dissolve peaked at `0.140` while continuous motion
> with no edit at all reached `0.045` and peaked at `0.061`. The ranges overlap, so no threshold
> divides them — and because `scene` compares *adjacent* frames, a longer and smoother dissolve
> scores *lower*, which is backwards for this purpose.
>
> **No transition detector was shipped.** No speculative scaffolding was left in the code for one.
> Do not read that document as implementation evidence; it is the reason R2 is still planned.

R2 has two distinct components, and they are worth keeping apart because the first is research and
the second is mostly plumbing.

#### R2-A — Detect gradual transitions across the source

A coarse pass over the **whole source**, not only clip edges. This is required rather than
preferred: an edge-only pass cannot find a dissolve in the middle of an interval the primary
detector never split, because it never looks there.

Research candidates, none of them proven:

- **Long-baseline frame differences** — compare frame *N* with frame *N+k* where `k ≈ 1 second`,
  rather than with *N+1*. A dissolve should show a large long-baseline change while adjacent-frame
  change stays small; a pan should show both.
- **Accumulated visual change** over roughly one second, compared against the direct endpoint
  difference.
- **Contrast, variance or histogram evidence** — a linear blend of two shots tends to have lower
  contrast than either source.
- **Low-resolution, modest-FPS sampling** throughout, to keep the pass affordable on long sources.
- **Conservative rejection** of camera pans, zooms and ordinary motion. A false positive trims real
  footage, so the default must be to leave material alone when the evidence is weak.

**No single heuristic may be treated as proven.** Whatever is chosen needs calibration against both
synthetic fixtures and representative real footage before it is enabled.

#### R2-B — Clean clip boundaries

Once transition regions are identified:

- Trim the **outgoing** shot to end before the mixed frames begin.
- Start the **incoming** shot after the mixed frames end.
- Allow the transition interval itself to be **discarded** — that is the intended outcome.
- If cleaning makes a clip shorter than the minimum, **skip that clip**. It is not rescued.
- **Never** force adjacent shots together to recover discarded seconds — the R1 principle holds.
- Inspect roughly **2–3 seconds** at each edge where appropriate.
- Apply the trim **before the final export**, so no clip is encoded twice to clean its edges.

Do not claim precise knowledge of blend opacity. A flattened video does not carry the alpha value
that produced it, and any report that implies it does is wrong.

#### R2 acceptance goals

- Detects representative cross-dissolves of **different lengths**, not just one calibrated case.
- Does **not routinely trim ordinary camera motion**.
- Source files remain untouched.
- Reports transition regions and removed durations **honestly**, including uncertainty.
- Stays within a measurable, reasonable processing-time budget on long sources.

**Qwen is not required for R2.** This is a mechanical detector.

---

### R3 — AI Director Cutter · **PLANNED for future consideration, NOT IMPLEMENTED**

A future **smart mode of the existing Raw Cutter tab** — not a separate top-level tool, and not a
replacement for the mechanical cutter.

```
Standard Cutter — Fast     deterministic, mechanical, no AI required   (this is R1/R2)
AI Director Cutter — Smart mechanical candidates, then Qwen-assisted selection
```

**Qwen is not integrated into the Raw Cutter today.** Nothing in R1 sends a frame to any model.

#### Proposed architecture

**Step 1 — Mechanical candidate detection.** FFmpeg and the existing video analysis establish the
real intervals and transition boundaries. Every timestamp the later steps use originates here.

**Step 2 — Qwen3-VL semantic evaluation.** Reuse the existing local Qwen3-VL capability, sampling
**representative frames from candidate shots** rather than pushing every frame of a long source
through the model. Candidate signals: visual and technical suitability; subject matter and action;
motion and visual interest; redundancy against other shots; obviously poor or contaminated footage;
relevance to the operator's instruction.

These are **fallible estimates, not facts**, and the UI must present them that way.

**Step 3 — AI Director selection.** An instruction field, for example:

> *"Select dynamic action scenes with clear subjects. Avoid blurry footage, subtitles, long dialogue
> shots and repetitive moments."*

With a strictness control: **Relaxed** (retain more), **Balanced** (default), **Strict** (fewer,
stronger clips). The model proposes keep/skip decisions, a ranking and a short explanation each.

**Step 4 — Structured timeline proposal.** The selection is converted into a deterministic,
validated export plan. **The model must never invent timestamps or produce approximate
string-based cuts.** All interval boundaries come from actual video analysis against the verified
source timeline, and the resulting plan must satisfy every existing Raw Cutter safety requirement:
source containment, clip-length limits, non-overlap, and no clip spanning a confirmed scene change.

**Step 5 — Human review and export.** Show the proposed kept intervals, the skipped intervals, the
reason for each decision, the expected output count and total duration, and anything the model was
uncertain about. **Export only after the operator explicitly approves.** Source footage is never
modified.

#### Reuse and caching

- Investigate reusing existing **Stage-5 analysis** where it is genuinely compatible, to avoid
  paying for the same Qwen work twice.
- If a cutter-specific AI cache turns out to be necessary, **design its identity and provenance
  before implementing it**.
- **Do not modify or weaken Stage-5 cache identity as a shortcut.** That contract exists for the
  music-video pipeline and is not a convenience to be borrowed.
- The **Standard Cutter must stay fully operational** when Qwen is disabled or unavailable.
- The text-only **AI Director model** and the **Qwen3-VL visual model** do different jobs. They are
  not interchangeable and must not be conflated.

#### R3 acceptance goals

- Better usable-shot selection than mechanical rules alone — demonstrated, not assumed.
- Evidence-based, **explainable** keep/skip decisions.
- Deterministic export from an approved plan.
- Reasonable analysis time on long source videos.
- Explicit operator control and review before anything is written.
- **No impact on the music-video rendering pipeline.**

---

### R4 — Raw Cutter usability and scale · **ACTIVE DEVELOPMENT PRIORITY**

Promoted from "worth doing when Playtest feedback justifies it" by the product-owner decision above.
Batch processing is the active piece; the rest of R4 stays unscheduled.

#### R4-A — Automatic folder batch mode · **DONE — merged in PR #69, deployed to production**

A second input mode inside the existing ✂️ Raw Cutter tab — not a fifth tab — that processes a whole
folder with one press and no per-file preview or confirmation.

- **Single Video** mode is unchanged: Analyze / Preview, then Cut Video, with the staleness gate.
- **Folder Batch** mode takes an input folder, an *Include subfolders* toggle (default on), and the
  **same** sensitivity, minimum, maximum, Remove Audio and output-folder controls — one value per
  execution setting, shared between the modes rather than duplicated.
- **Start Batch** enumerates the supported videos, freezes that list in a deterministic order, and
  processes them one at a time: probe → scene detection → quality-first plan → export → next.
- **The V1 planner and detector are reused unchanged.** No new detection of any kind.
- **One broken or empty source never ends the batch.** Each source is classified — completed, no
  usable shots, unreadable, detection failed, planning refused, partial, export failed, cancelled or
  not attempted — and the next one starts. A partially exported source is never reported as
  complete.
- **One batch output folder** (`RawCutter_Batch_<timestamp>`) containing a separate folder per
  source, mirroring the input structure, so identical filenames in different input subfolders can
  never collide. An output root inside the source tree is refused, resolved through junctions, so a
  batch cannot re-ingest its own clips.
- **A `batch_manifest.json`** with one record per discovered source, written even after a failure or
  a cancellation.
- **Cancel Batch** stops at the next safe boundary, starts no further source, and keeps every
  finished clip.
- **One mutex acquisition for the whole batch**, sharing the existing process-global render lock, so
  a batch and a music-video render can never run at once.

#### R4-B — Live progress and active controls · **DONE — merged in PR #71, deployed to production**

A truthful progress surface for both Raw Cutter modes. Reviewed, remediated, **merged in PR #71**
(`main` at `2044f360`) and **deployed to production at `2044f360`**.

- **Analyze** names each real phase — reading the source, detecting scene changes, planning — before
  the blocking call it describes, with an **indeterminate** bar. One FFmpeg scene pass reports
  nothing measurable, so no percentage is shown rather than one being invented.
- **Cut Video** shows a determinate bar that advances **only when a clip is actually published**,
  with `done / total` counts and the name of the clip currently encoding. A long clip holds the
  percentage still and says so.
- **There is no ETA, and none is derivable** — segment lengths vary and the encoder is not linear in
  wall-clock.
- **Cancel** is inactive at idle, red and live only while a job it can really stop is running, and
  reports *cancellation requested* rather than claiming completion.
- **Folder Batch** gets the equivalent surface: source *N of M*, the current phase, and a bar that
  counts sources whose outcome has been recorded.
- Button states are a courtesy only: `plan_signature` and the shared render mutex remain the sole
  authorities and still refuse a caller that never saw the UI.

Measured before the merge: full suite **6 953 passed, 8 skipped, 0 failed**; a real-browser run
observed the bar advancing 0 → 11 → 22 → 33 → 44 → 56 → 67 → 78 → 89 → 100 % with counters
0/9 → 9/9 and each clip named as it encoded. Two independent reviews (P19-R3, P19-R5) and the
remediation of both their MAJOR findings are recorded in the pull request.

#### R4-C — Synthetic output naming · **DONE — merged in PR #72, deployed at `0aed35d6`**

Implemented on `feat/raw-cutter-synthetic-output-naming`, independently reviewed twice, merged as
PR #72 and deployed to production at `0aed35d6`.

Both reviews returned **QUALIFIED**, not an unconditional pass, and the owner accepted three
MINOR findings as non-blocking when merging. They remain open and are recorded here rather than
quietly dropped: the historical P1 report diffstat was wrong (`+905/-83`; the real figure is
`+2051/-83`); the *private* GUI handlers `_on_cut_run` and `_on_cut_batch_run` gained a required
positional argument, so no compatibility is claimed for unsupported out-of-tree callers; and
`OutputNaming.source_total` defaults to 1 and is not validated, so a malformed out-of-tree
construction can mix widths — unreachable from the GUI, which freezes the true discovered count.

An **opt-in** checkbox, **Rename output clips**, default **OFF**, shared by both modes. With it off,
naming is exactly what it is today — this adds a mode, it does not change one.

With it on, exported clips get synthetic, source-independent names:

```
RawCutter_Single_20261018_153000\        RawCutter_Batch_20261018_153000\
  File001_18102026_001.mp4                 File001\File001_18102026_001.mp4
  File001_18102026_002.mp4                 File001\File001_18102026_002.mp4
  File001_18102026_003.mp4                 File002\File002_18102026_001.mp4
                                           batch_manifest.json
```

- `File<SSS>` is the source's position in the **frozen** input list (always `001` for Single
  Video), `DDMMYYYY` the job-start date, `<CCC>` the clip's index within that source.
- The date is the **local job-start date**, captured **once** per job from the same clock reading
  the output folder is named after, so a batch crossing midnight does not change naming halfway
  through. Neither pure module reads a clock at all, which is what makes that structural.
- Indexes are **never renumbered** when an earlier source is skipped, unreadable or fails.
- Counters are at least three digits and **widen** rather than wrap. Both are padded to the
  **total** they count towards: the clip counter to that source's real clip count, and the source
  counter to the job's **complete frozen source count**, decided once at submission. So a
  1 200-source batch is `File0001`…`File1200` throughout rather than three digits up to 999 and four
  after it, and an ordinary filename sort is the discovery order **at every scale**. A batch of 999
  or fewer sources is unaffected — still exactly `File001`…`File999`.
  <br>This corrects the one ordering limitation the independent review of PR #72 recorded, by
  product-owner decision. The superseded spelling padded the source field from each index on its
  own, so `File1000` sorted before `File101`; identity, uniqueness and index retention were never
  affected, only the browsing order. The width is held in the one frozen naming record the batch
  layout and the exporter both read, so the per-source folders and the clips inside them cannot be
  padded differently.
- **No exported name and no automatically created output subfolder leaks the original basename**
  while this mode is on — including the **staging** file, which would otherwise leak for the whole
  length of an encode. In Folder Batch the mirrored input structure is **dropped** rather than
  sanitised, because sanitising a folder name still reproduces it.
- The operator-selected **output root is never renamed**; nor is `RawCutter_Batch_<timestamp>`,
  which is the run's identity rather than the source's.
- **Provenance is retained but kept separate**: `batch_manifest.json` keeps the real
  source → `File<SSS>` mapping and the clips each source produced, and the exported filenames do
  not. The schema name is unchanged and the only addition is
  `settings.rename_output_clips`.
- Applies to **both** Single Video and Folder Batch.
- No-overwrite publication, atomic staging, source preservation, cancellation, the 0.20 s scene
  guard, the shared render mutex, CFR/VFR frame timing and manifest completeness are **unchanged**.
  Measured against the real binary: the mode changes the output path and nothing else about the
  FFmpeg command, and adds no transcode and no extra pass.

Measured locally at the merged commit: full suite **7 182 passed, 8 skipped, 0 failed** (PR #72
as first published was 7 094 / 8 / 0, and the pre-R4-C `main` baseline 6 953 / 8 / 0), including a
new `tests/test_raw_cutter_naming.py`. The 88 added tests cover the job-wide source width at totals of
1, 2, 9, 10, 99, 100, 999, 1 000, 1 001, 1 200 and 10 000 — including an end-to-end 1 000-source
batch through the real handler and an executable negative control in which the superseded
per-index rule fails the ordering requirement.

#### Remaining R4 ideas · **PLANNED, NOT IMPLEMENTED**

- **Cancellation during an active FFmpeg segment**, rather than only between clips.
- **A native local file/folder picker**, without copying large videos into Gradio's upload folder.
- **Optional fast stream-copy mode**, with measured keyframe-drift reporting — only worth shipping
  if the report can honestly say which clips moved and by how much.

---

### R5 — Clips Preview Selector · **P1 IMPLEMENTED (read-only review) — transfer NOT implemented**

A **fifth top-level tab** for reviewing clips *after* the Raw Cutter has produced them. This is
post-cut media triage, deliberately **not** a per-cut confirmation step inside the Raw Cutter —
Folder Batch exists precisely so that cutting needs no interaction.

**What P1 does and does not do.** Discovery, authorized preview playback, poster frames,
server-authoritative classification, durable decisions and the gallery are implemented and
measured. **Moving the reviewed clips is not implemented at all** — not disabled, not
feature-flagged, not written. Confirm reports readiness and moves nothing. The transfer contract
below remains a design that needs its own authorization and its own review.

- A gallery of up to **100 clips per page**, 3–4 per desktop row, each starting **PENDING**.
- **Left click selects** (green ✓), **right click rejects** (red ✗), with an explicit way back to
  PENDING and a keyboard-accessible alternative for every action.
- **Hover plays muted inline**, leaving pauses, with optional bounded wheel scrubbing.
- **Confirm stays disabled until every clip in the batch is decided.** Selected clips move to a
  Media Library folder, rejected ones to a **quarantine** folder — rejection is *not* deletion, and
  V1 has no expiry and no permanent delete.
- Colour is never the decision record: server-side clip IDs and durable per-file outcomes are.
- Transfers are verified — atomic no-replace moves on one volume, staged verified copies across
  volumes — with per-file intent recorded so a partial failure is recoverable rather than silent.

Full specification: [`docs/claude/clips-preview-selector-v1.md`](docs/claude/clips-preview-selector-v1.md).

**The 100-clip page size has been benchmarked** at 25 / 50 / 100 items on the live tab in the
pinned Windows + Gradio 6.19.0 environment, cold and warm, with zero video elements mounted at
rest and at most one playing. The figures are in the R5-P1 task report; the specification carries
the method. If a page size ever fails to hold, the page size gives — not the safety rules.

#### R5-UX — Clips Preview usability and local sorting · **MERGED AND DEPLOYED**

> **Shipped.** Merged in **PR #76** and **deployed** to the owner's production installation at
> `25c4db3b` on 2026-10-27, after a hands-on owner test and a real-browser smoke of every
> feature below. It has still had **no independent review**, and PR #75 remains a separate,
> untouched branch.

Four increments, built and browser-verified in order:

| | What it does | Verified |
|---|---|---|
| **V1** | Confirm enables/disables itself the moment the page becomes fully decided, by asking the server to re-derive the state. Stale-response guard on rapid clicking. | Chrome: enable → disable → re-enable, all automatic |
| **V2** | Real clip length in every tile (`Clip 0001 (00:14)`) from the portable FFprobe, cached on clip identity; adaptive Shift+wheel seeking — 1 s under 20 s, 2 s to 90 s, 5 s beyond | Chrome: six clips at 10/19/20/45/90/120 s, correct step at both boundaries |
| **V3** | **Confirm actually sorts**: SELECTED → Media Library, REJECTED → Discarded, same-volume `os.rename` only. No overwrite, no collision renaming, no deletion; cross-volume refused in words rather than copied. Partial results preserved; next batch loads automatically | Chrome 12/12, 6 clips sorted across two batches, every checksum identical |
| **V4** | Native Windows folder picker on six folder fields, and saved default folders in `state/preferences.json` | Chrome: pick, cancel, save, restart, restore |

**What V3 deliberately does not do.** It is a **V1 sorter**, not the reviewed transfer engine. It
has no content fingerprint, no journal, no staging and no recovery report — it is `os.rename`
inside one volume, chosen precisely because that call refuses an existing destination and *raises*
across volumes instead of copying. The separately reviewed transfer engine
(`clips_review_transfer.py`) and the accepted Win32 primitive remain **unwired**, and the owner
decisions A/B/C/F that govern them remain **open**. Nothing in R5-UX closes any of them.

**Next for R5-UX:** independent review, then a decision about whether this simple sorter is the
shipping answer or whether the reviewed engine should replace it.

---

### R6 — Raw Cutter output video profiles · **MERGED AND DEPLOYED**

Today every exported clip inherits its source's dimensions and frame rate, so a folder of mixed
recordings produces a library that is mixed in exactly the same way. The next Raw Cutter feature
is one **Output Video Profile** selector, shared by Single Video and Folder Batch.

| Preset | Note |
|---|---|
| **Same as source** | **DEFAULT.** Byte-for-byte the current behaviour — the existing export path, unchanged |
| 1080p / 25 fps · 1080p / 30 fps | |
| 720p / 25 fps · 720p / 30 fps | |
| 1080p / 60 fps · 720p / 60 fps | later, once the first four are proven |

> **Built and measured** in the `R6-OUTPUT-PROFILES` playtest, on top of the R5-UX V4 candidate.
> Four profiles ship: **1080p/30, 1080p/25, 720p/30, 720p/25**, plus **Same as source** as the
> default. 60 fps is still deliberately absent.
>
> **Same as source is byte-identical to the pre-R6 command**, proven by extracting
> `build_segment_command` from both trees and comparing the argv for both encoder branches. The
> profile is applied as an explicit substitution over the reviewed list rather than by branching
> around it, so the P8 measurements still describe the code that runs.
>
> **`-enc_time_base:v demux` is NOT carried into the fixed branch**, and that is a measurement
> rather than a preference: with it, a 4-second 25 fps export came out as **61,440 frames at
> 15360/1**; with `-fps_mode:v cfr` alone it is **100 frames at 25/1**, uniformly spaced.
>
> Verified with FFprobe across 1080p30, 720p25, 4:3, portrait 9:16, anamorphic (SAR 16:15), VFR
> and an audio fixture, on **both** NVENC and libx264: exact dimensions, `r_frame_rate` equal to
> `avg_frame_rate`, exact frame counts, DAR preserved by padding (never stretching), audio
> duration matching video exactly at an unchanged sample rate, and no frames leaking past the
> planned boundary.

Requirements for whoever built it:

- Re-encode to consistent dimensions and frame rate.
- **Preserve aspect ratio by padding, never by stretching.**
- Scene-cut start/end positions stay conceptually unchanged.
- **Say plainly that fixed-fps conversion duplicates or drops frames.** A 24 fps source at 30 fps
  is not the same footage; the UI must not imply it is.
- NVENC when available, CPU fallback when not.
- No overwrite, and **Remove audio** keeps working exactly as it does now.
- One profile for an entire Folder Batch, recorded in `batch_manifest.json`.
- **Same as source must leave the current code path untouched**, so the default cannot regress.

### R7 — Raw Audio Cutter V1 · **LOCAL EXPERIMENTAL CANDIDATE, NOT MERGED, NOT DEPLOYED**

> **Read this status literally.** R7 exists as a **local experimental candidate** on branch
> `feat/fun-raw-audio-cutter-r7`, built and browser-verified in the `R7-RAW-AUDIO-CUTTER`
> playtest on port 7863. It is **not** on `main`, **not** in any PR, **not** running in the
> owner's production installation, and has had **no independent review**.

A sixth top-level **🎙️ Audio Cutter** tab, between Advanced Tools and Clips Preview, for building
a reusable library of dialogue, phrases, vocal expressions and miscellaneous sounds pulled out of
one recording at a time. Folder Batch is deliberately not in V1.

**V1 is mechanical. No AI, no transcription, no classifier.**

| | What shipped |
|---|---|
| **Input** | One file. WAV/MP3/FLAC/M4A and any video whose audio track survives (MP4, MKV, MOV…). Accepted on whether **FFprobe finds an audio stream**, not on the extension. Read in place — never uploaded or copied. Native Windows **Browse file…** dialog, and a folder dialog for the output. |
| **Detection** | FFmpeg `silencedetect`. Threshold **−35 dB**, minimum silence **0.5 s**, padding **0.10 s**, minimum clip **0.30 s** — all four exposed in the GUI. |
| **Analyze** | Source duration, silence count, planned-clip count, and a table of every planned clip with its source start/end. **Writes nothing.** |
| **Cut** | Re-analyzes, then writes each clip into a fresh timestamped run folder. **WAV 16-bit PCM @ 48 kHz** by default, **FLAC** optional. `audio_manifest.json` records source, settings, per-segment timings and output names. |
| **Review** | Pick a clip, hear it in the page, edit its name, then **Keep → voice library** or **Discard → quarantine**. One same-volume `os.rename`: no overwrite, no collision renaming, **no deletion**. |

**The two arithmetic properties worth stating.** Padding can never make two clips overlap — each
side takes at most *half the gap it reaches into*, so neighbours may meet but never cross. And
leading or trailing silence cannot produce an empty clip, because the clips are the *complement*
of the silences rather than a list built by splitting.

**What it must not claim, and does not.** It does not identify individual words, does not separate
speech from music, and cannot remove music from an already-mixed track — a pause detector finds
quiet, not vocals. A continuous passage stays one clip however long it is. The tab says so on
screen.

**Verified against Audio Layers:** an exported WAV was loaded into the Audio Layers voice input in
a real browser. Audio Layers itself is unchanged by R7; the tab carries a short usage tip only.

**Known limitations.** Single file per run (no Folder Batch); same-volume filing only; fixed
export rate of 48 kHz.

#### R7.1 — hover gallery and Comfort Mix · **MERGED AND DEPLOYED (PR #77, main `d98fb55a`)**

Two owner-requested improvements on top of R7, after a hands-on test that accepted the silence
cutting as-is. **The cutter itself is unchanged.**

**The review surface is now a gallery, not a dropdown.** Tiles carry the clip number, its length,
its source window, a Keep/Discard/Pending badge and an editable filename. **Hovering plays the
clip with sound and loops it; leaving stops and resets it; only one ever sounds at a time.**
Left-click keeps, right-click discards, middle-click or **P** returns a clip to undecided, **S/R/P**
and the arrow keys do the same from the keyboard, and **Shift+wheel** scrubs using the same
1 s / 2 s / 5 s convention the Clips Preview uses.

**Nothing moves until Confirm and Sort**, which refuses while any clip is still undecided. The
page releases every audio element on `pointerdown` of that button — on Windows a file a player
still holds open simply will not move, and the clip the operator just listened to is exactly the
one that would refuse.

Audio is embedded but **bounded** by a byte budget; past it a tile says so and remains playable
from the output folder. Widening Gradio's `allowed_paths` was refused again, for the reason it was
refused before. Chrome blocks audible autoplay before a gesture, so a blocked `play()` is caught
and an **Enable hover audio** button offers the one click that fixes it — deliberately *not*
falling back to muted playback, which would look like it worked while nothing was heard.

**Audio Layers gained an optional Comfort Mix**, described in R9's section below.

**Known R7.1 limitations.** Decisions live in the page until Confirm, so a reload loses them
(the Clips Preview's durable journal is deliberately not duplicated here); the gallery is one page
per cut; Comfort Mix measures the **longest** voice clip as the representative of the set rather
than every clip; and the Variant Lab batch render keeps the legacy mix, because Comfort Mix is
wired to the single-render click only.

> **Three of those limitations were closed by R7.2 below** — the byte budget, the one-page gallery
> and the Cutter-session-only review. The two Comfort Mix ones are still open.

#### R7.2 — lazy audio preview, a review tab, keep-only sorting, MP3 · **MERGED AND DEPLOYED (PR #77, main `d98fb55a`)**

Five owner-requested improvements after a hands-on R7.1 test on real footage and a rendered music
video. **The silence planner is unchanged, and so is every WAV/FLAC behaviour.** What the owner
confirmed good and R7.2 preserves: silence-based cuts (0.5–0.6 s ideal), no missing word endings,
hover playback and classification, Comfort Mix, and voice clips audible in the finished video.

**1 — Audio is no longer in the page, so clip size cannot make a clip unplayable.** R7.1 embedded
each clip as a base64 data URI under a cumulative ~40 MiB budget, and the budget failed exactly as
a budget must: a perfectly valid clip became unplayable because *earlier* clips had spent the
page's allowance, and the tile said "not loaded for preview on this page" — a sentence about a page
limit rather than about that clip. A tile now carries an **opaque URL** and no bytes, and one
`<audio>` element is created on hover and re-pointed as the pointer moves. Page weight is now
independent of clip size.

The bytes come from a **new authorized endpoint** that reuses the R5 Clips Preview's registry, its
six authorization gates and its descriptor-identity revalidation unmodified — the browser still
never receives a filesystem path, and `allowed_paths` was refused again for the reason it was
refused before. The route is byte-range capable, which is what makes Shift+wheel seeking work
without fetching a whole clip. **The loading limit is gone rather than raised**, so the only
remaining failures are genuine ones: a refused fetch and an undecodable clip are now reported
distinctly, and an unplayable clip stays classifiable.

**2 — A standalone 🎧 Audio Preview tab**, because the owner often cuts recordings and classifies
them later. It reads **any folder** of audio clips, needs no Audio Cutter run in the browser
session, and works after restarting BeatSync. WAV, FLAC and MP3 plus M4A/AAC/OGG/Opus; a native
Browse Folder button, optional subfolders, saved default paths, duration, filename, an editable
destination name and a Keep/Discard badge per tile. Large folders are **paged**, and decisions are
keyed by **file identity** rather than by page position, so paging loses none of them and a
re-recorded clip does not inherit an old decision. **Nothing scans as a side effect** — loading
saved preferences fills the folder box and reads no disk. A convenience **Open results in Audio
Preview** button on the Cutter fills that box and selects the tab, and still scans nothing.

The gallery, the sort policy and the destination rules are **one implementation** in
`beatsync_fork/audio_review.py`, used by both surfaces. The two surfaces hold **separate
registries**, because `ClipRegistry.new_session` revokes an owner's previous sessions and a shared
one would let an Audio Preview scan silently kill the Cutter's just-cut gallery.

**3 — Confirm needs one KEPT clip, not a fully decided page.** R7.1 refused Confirm while anything
was undecided, which made the operator click Reject on every clip they did not want. Marking the
good ones is now enough: explicit rejections still go to Discarded, and **unmarked clips are swept
there too — but only after an explicit confirmation showing the counts first**, e.g. *6 selected to
KEEP · 2 explicitly rejected · 12 unmarked will ALSO be moved to Discarded · nothing will be
permanently deleted*, with Proceed and Cancel. That confirmation is bound to **one batch** by a
signature over every decision and both destinations, so changing anything asks again. The report
afterwards keeps all four counts distinct — kept, explicitly discarded, automatically discarded,
failed — because "14 discarded" cannot tell an operator whether they rejected 14 clips or forgot 12.
Discarding is still a **move into quarantine**, never a deletion; nothing is ever overwritten; the
same-volume rule still holds; and a partial failure leaves the clips that did not move on screen
for another attempt.

**4 — Identical or nested destination folders are refused, visibly and immediately.** The owner
found this one in use: both destination fields were set to the same folder
(`…\VOICE OVER\Discart`) and sorting proceeded, so kept and discarded clips would have been mixed
together. Both audio surfaces now reuse the Clips Preview's **resolved-real-path** validation —
`commonpath` rather than string prefixes, plus the Windows `(st_dev, st_ino)` cross-check, so a
junction, a `subst` drive, a hard link, a trailing separator and a difference of letter case are
all recognised as the same folder. Refused: identical destinations, either nested in the other,
either inside the review folder, the review folder inside either, and the protected runtime
directories. It is checked **as the operator types** and **again at Confirm** — the folder that was
checked and the folder that is written must be the same one. The gallery stays fully usable while
the folders are being corrected; only sorting is withheld.

**5 — MP3 output** (libmp3lame, 192 kbps CBR), beside the unchanged WAV PCM-16/48 kHz default and
FLAC. `libmp3lame` is verified in the installed portable FFmpeg **before** anything is written, so
a missing encoder refuses in words instead of producing a folder of unplayable files. Correct
`.mp3` naming, mono/stereo preserved by omission, codec/format/bitrate recorded in the manifest,
and MP3 already accepted by Audio Layers' voice input. The UI is **blunt about the trade**: MP3 is
a lossy re-encode *including from an MP3 source*, because the silences are found by decoding the
audio and there is no stream-copy path across an arbitrary cut point. The advantage is smaller
files, not better audio.

*Measured on the portable build* (five clips from a 48 kHz mono source, all three input cases —
WAV in, MP3 in and a video file in — each writing 5 of 5 clips): `libmp3lame` present, output
`mp3 / 48000 Hz / 1ch / 192 kbps`, **mono preserved**, and per-clip duration drift of
**+0.000 s** at millisecond resolution against the planned segment. MP3 came to **26.2 %** of the
WAV size (653,148 → 171,297 bytes), FLAC to 18.8 %. The "about a quarter" figure in the UI is that
mono measurement; a stereo source is nearer an eighth, and an earlier draft of this note said
"an eighth" without qualifying it, which was wrong for the mono case the cutter is normally used
for.

**Known R7.2 limitations.** Decisions still live in the page until Confirm, so a reload loses them
— the Clips Preview's durable journal is still deliberately not duplicated for audio. Paging reuses
the file list captured at Scan, so a file added to the folder mid-review appears only after the
next Scan (a sort re-enumerates automatically). The sweep signature is a **change detector, not a
credential**: it prevents a stale confirmation from applying to a different batch, and the server
re-derives the whole plan and re-validates both folders before moving a byte regardless. MP3 clip
boundaries are frame-quantised to 1152 samples with the delay and padding recorded in the
Xing/LAME header — close, and **not** sample-exact, which is why WAV remains the default. The two
Comfort Mix limitations above are untouched.

### R7.3 — Voice clips that exceed the music duration · **OWNER-ACCEPTED, NOT YET DEPLOYED**

Branch `feat/fun-audio-overflow-r7-3`, cut from `main` at `d98fb55a`. **Modes 1–3 are implemented**;
mode 4 is still blocked on R10 and is deliberately **not offered**, rather than offered and refused.

**The owner has personally tested Strict / Skip / Trim and accepted them.** That moves this item
past "local candidate" on the *validation* axis only — it is still on a branch, still in a draft
PR, still not merged and still not running in production, and it has had no independent review.
Do not read acceptance as deployment.

Raised while testing R7.2: Audio Layers **failed the whole render** when the voice clips it was
given could not all be placed before the music ended — the owner hit
`Voice 24 (17.9s) has no legal placement after 187.6s` on real material and lost an otherwise
finished music video to clip 24 of 30. That refusal is the safe default and remains available; it
is no longer the only answer.

The Audio Layers setting **"When voice clips do not fit"** (`gr.Radio`, Audio Mix → Audio Layers),
with four modes:

1. **Strict / Error** — the historical behaviour. Refuse the render and name the clip, its duration
   and the cursor position, exactly as today. **This stays available and stays the thing an
   operator can choose.**
2. **Skip clips that cannot fit** — place what fits, skip the rest, report which.
3. **Trim the final usable clip at the music end, then skip the rest** — the only mode that
   deliberately shortens a clip, so it must say so per clip rather than in aggregate.
4. **Repeat Music to Fit** — extend the music instead of dropping speech. **Activate only after
   R10 exists**; until then the mode is not offered at all rather than offered and refused.

**The decision is a placement question, not an arithmetic one, and that is the part worth getting
right.** Whether a clip fits is not `sum(durations) <= music_duration`: placement already walks the
beat grid, honours **Minimum Gap**, respects **Avoid Drops** over the whole clip interval (not just
its start), and looks ahead at most `PLACEMENT_LOOKAHEAD_SECONDS` for a preferred section start. A
clip can therefore fail to fit with minutes of music left, and a naive duration sum would report
the opposite. The mode must be evaluated against the real planner, by the planner.

Acceptance goals, and what the implementation actually did about each:

- every mode **always reports clips used, trimmed and skipped**, by name — **met**.
  `AudioMixPlan.overflow_summary_line()` is unconditional (`N supplied, N used in full, N trimmed,
  N skipped. Music length: MM:SS.`) and `report_lines()` lists every skipped and trimmed **filename
  with its reason**. One formatter, in the pure module; `gui.py` reformats nothing, and a test
  asserts the GUI does not contain the phrases itself;
- **no silent truncation and no silent dropping**, in any mode — **met**. A trimmed row is marked
  `[TRIMMED: 23.0s of 25.0s played]`, and `VoicePlacement` carries `source_duration` beside the
  played `duration`;
- Strict remains selectable and remains the **default** — **met**. `DEFAULT_OVERFLOW_POLICY =
  "strict"`, appended last on `AudioMixConfig` so every pre-existing positional and keyword
  construction is unchanged, and any unrecognised value **resolves to strict** rather than to
  something permissive;
- the placement rules above are unchanged — **met structurally**, not merely by test: the
  whole-clip search and the bounded anchor preference are the untouched V1 code, so all three modes
  place an everything-fits selection identically, with identical duck envelopes;
- deterministic and seedless — **met**. No RNG was added and the planner still reads nothing but
  its config.

Three implementation facts worth recording, because each one is a place a future change goes wrong
quietly:

1. **A skip does not advance the cursor.** The cursor is the end of the last *placed* clip plus the
   gap; a clip that was never placed has no end. Advancing it would mean a 17.9 s clip that did not
   fit also pushed the 3 s clip behind it out of the song — the opposite of the point. This is why
   mode 2 can place a later, shorter clip at the exact instant the long one was refused.
2. **The planned number is only half of Trim.** `atrim=end=<played>` is emitted on the trimmed
   voice *input*, before its `volume`/`adelay`, and on no other voice input — so Strict still
   produces the byte-identical legacy filtergraph. Shortening only the plan would have left the
   duck envelope correct while the untrimmed file talked over it to the end of the song.
3. **A trim is only *observable* when `Avoid drops` bounds the room.** When the **music end** is the
   bound, the master's own `apad` + `atrim=end=<music>` already cut an overrunning voice, so a
   trimmed mix and an untrimmed one are acoustically equivalent. The decisive real-FFmpeg test
   therefore bounds the room with a `drop` span and asserts voice-level silence after the planned
   end, with a control proving the same file *would* have kept talking. A test bounded by the music
   end would have passed whether or not the `atrim` was ever emitted.

**One behaviour not in the original plan, added deliberately: zero placed clips stays fatal.**
"Some clips did not fit" is now survivable; "**none** of them fit" is not. A plan with no
placements out of a non-empty selection would mix a master byte-identical to the music and then
report "Audio Layers" as though something had happened, so `build_mixed_master` raises an
`AudioMixPlanError` naming the count, the music length and what the operator can change. The
classification is unchanged — `SHARED_FATAL` — and `voice_overflow` joins the exact set of
batch-frozen fields the placement-feasibility guard re-derives from source on every run.

#### R7.3.1 — voice clips now really do play in filename order

The R7.3 browser playtest surfaced a **pre-existing** defect in the feature's own input: a selection
named `01_a 02_b 03_long 04_c 05_long` actually played **`04_c 05_long 01_a 03_long 02_b`**.

`order_voice_paths` delegated to `input_manager.order_key`, which sorts the **whole** normalised
path. That is correct for a folder scan — every file shares a parent, so path order *is* filename
order — and wrong for the browser picker the help text is attached to, because Gradio stores each
uploaded file under its own randomly-named temporary directory and that random component dominated
the sort.

It mattered more once R7.3 existed than it had before: **the play order decides which clip
overflows**, and therefore which clip `skip` drops and which one `trim` shortens. An operator
numbering their files could neither predict nor control what they lost.

Voice now sorts with its own `audio_mix.voice_order_key` — `(filename case-folded, path case-folded,
path)`. `input_manager.order_key` is **unchanged**: it still orders video sources and the Smart Mix
SFX pools, where whole-path ordering is the documented and wanted behaviour. Two rules because the
two inputs genuinely differ, and a test asserts the two keys **disagree** on shuffled parents so the
fix cannot be proven by a vacuous assertion. Separators are normalised to `/` before the basename is
taken (Windows, forward-slash and mixed paths all split correctly on any host), the original string
is what gets returned, duplicate basenames keep both entries ordered by full path, and no selected
file can be lost in the sort — `prepare_voice_inputs` still re-checks the count.

**`voice_overflow` is threaded into the Variant Lab batch path as well as the single render**, which
R7.1's two Comfort Mix controls were not. It is placement configuration, so the batch freezes it
from its own submitted arguments exactly as it freezes `voice_avoid_drops`; no `AudioRecipe` field
resolves it and Variant Lab must never vary it (it is a sixth zero-writer Audio Layers widget).

### Stage-5 Cancel — Cancel actually stops video analysis · **LOCAL CANDIDATE, IN DRAFT PR, NOT DEPLOYED**

Branch `feat/fun-stage5-cancel-shared-cache`, stacked on `feat/fun-audio-overflow-r7-3`.

Raised by a real owner incident, not by code review: an entire video library was selected by
accident in a fresh playtest with no warm cache, Stage 5 began, Cancel was pressed — and Stage 5
kept working for **about an hour**. That was the documented design. C3-R1A's `BOUNDARY_ONLY_CANCEL`
deliberately kept the cancellation token out of `video_analysis.py`, on the argument that one
Stage-5 call can hold GPU minutes and killing it mid-inference abandons a model process and
discards paid-for work. The incident showed that argument points at *stopping it properly*, not at
not stopping it.

Cancellation is now observed at every boundary that costs real time:

| where | what it costs to reach the next one |
|---|---|
| before the cache scan | nothing — an already-landed Stop never starts the work |
| the cache-identity phase | one source's bounded fingerprint read (H2/D0 measured ~100–116 s cold over 1,815 sources, so this is not free) |
| the record scan, and between sources | one record lookup / one source |
| the FFmpeg scene pass | it decodes the **whole** file, and is stopped mid-child |
| frame decode | 32 target frames |
| window scoring | 16 windows |
| the Qwen worker wait | one 0.2 s poll — and the worker's own `llama-server` grandchild goes with it |

**Measured on real media in an isolated playtest** (48 cold synthetic sources, one app root):
`0.0160 s` when the Stop landed inside frame decode, `0.0370 s` between sources on the serial path,
`0.4511 s` on the 7-worker parallel path (the time for the in-flight workers to reach their next
boundary and the pool to drain), and `0.2040 s` during a deliberately slow stand-in Qwen worker.

Three properties make it safe rather than merely responsive, and all three are tested:

- **Completed work survives and is reused.** A cancelled cold run kept 16 of 48 records; the next
  run reported exactly 16 cache hits, analysed the other 32, and the survivors were byte-identical.
  Cancellation deletes nothing — there is no delete path in it at all.
- **Nothing incomplete becomes durable.** `_cache_entry_is_complete` is unchanged and is still the
  only completion rule; a source interrupted mid-analysis is simply never written.
- **No render begins.** `RenderCancelled` is an ordinary `Exception`, so Stage 5's own broad
  handler would have caught it, reported "video analysis failed" and continued into Stage 6 on
  **fallback sampling** — a video built from random clips for someone who had just pressed Cancel.
  A typed clause ahead of every such handler stops that; a pipeline-level test asserts Stage 6 is
  never entered and that no fallback warning is emitted.

The Qwen worker **process** is still completely cancellation-free, and that is the surviving half
of `BOUNDARY_ONLY_CANCEL`: it writes persisted semantic records, so nothing session-scoped may
enter it, and it needs no token because the parent owns and stops the child.

**Honest limits.** A native decoder call cannot be interrupted once entered, so the claim is
"cancellation takes effect at the next frame, window or source boundary" — never "immediately".
Stages 1–3 stay uncancellable mid-stage on purpose (~15.7 s measured, process-locally cached).
Timeout behaviour is unchanged, orphan included.

### Shared Analysis Cache — one Stage-5 cache for every BeatSync version · **LOCAL CANDIDATE, IN DRAFT PR, NOT DEPLOYED**

Same branch, same owner session. The cache directory was derived from the checkout's own root, so
every playtest tree started cold while production held thousands of records.

**It was never a migration problem.** Stage-5 identity is
`contract | version | abspath | size | mtime_ns | fingerprint | backend | qwen-config`, and the
directory is not one of those components — so a record written by one installation was always
lookupable by another under the identical filename. Only its *location* was installation-specific.
`CACHE_CONTRACT_VERSION` and `ANALYSIS_VERSION` are untouched and **no record was invalidated**.

Settings → **Video analysis cache**: a folder with the existing native Browse, a switch, a Check
button reporting the resolved location and what is in it, Save, the reuse limitations stated
plainly, and a one-time **Import existing cache**.

The setting lives at `%LOCALAPPDATA%\BeatSync\shared-config.json` — **outside every checkout**,
which is the whole feature. A path in `state/preferences.json` would leave the next playtest blind,
which is the problem rather than the fix.

**Measured across two app roots sharing one folder:** the first analysed 8 sources cold in
`3.243 s` and wrote 8 records to the shared folder; a *different* app root, in a fresh process that
discovered the setting on its own, reported **8/8 cache hits and 0 sources analysed in `0.053 s`**.
Flipping the AI identity correctly produced 8 misses. With sharing off, each root used its own local
cache exactly as before. Clearing the three unrelated cache categories left the Stage-5 records
untouched.

**Import**, run against the real production cache: 2,086 current-generation records copied
(27.8 MB), 3,985 older-generation records correctly **not** copied (their filenames could never be
looked up again), 920 retained Qwen worker files skipped, 0 failures — and production came out
**byte-identical**, verified by a directory digest taken before and after. A second import copied 0
and reported 2,086 already present, byte-for-byte unchanged. A fresh app root then classified 41
real sources against the imported records as **41 prepared, 0 needing analysis**.

**Honest limits.** A moved or renamed video re-keys and is analysed again — path is part of
identity, by the existing contract. Most of the production cache's records point at source folders
that no longer exist on disk, so importing it helps only for material still at its original paths.
A configured-but-unreachable folder (an offline drive) falls back to the local cache and **says
so**, loudly, rather than starting a silent cold re-analysis. Clearing a shared cache costs every
installation on the machine, and the Cache Management panel now says that before it is done.

### R10 — Quick Render music repeat · **PLANNED, NOT IMPLEMENTED**

A control beside the main music selector: **Repeat Music: 1×–30×**.

- **1× preserves the existing render path exactly** — same analysis, same planner, same output.
  Not "equivalent": the same path, so a non-repeating render cannot regress.
- **2×–30× repeats the selected song sequentially** and the **video duration extends with the
  audio**.
- **Cuts and source choices must cover the whole extended timeline.** Re-showing the
  already-rendered visual sequence is explicitly *not* acceptable — the point is a longer video,
  not a looped one.
- **Original speed and pitch preserved**; no time-stretch, no resampling.
- **Audio/video frame synchronisation maintained** across every repeat boundary, which is where
  this will actually be hard: the frame-lock invariant quantises absolute cut positions once over
  the whole timeline, so the repeat has to be part of that timeline rather than a concatenation of
  separately locked renders.
- **Reuse the analysis rather than repeating it.** Beat grid, sections and energy for repeat *n*
  are the first pass shifted by `n × duration`; re-running Stages 1–3 thirty times would be the
  obvious implementation and the wrong one.
- **Progress and an estimated output duration** before the render starts, and **cancellable**,
  through the existing `RenderLifecycle` rather than a second mechanism.
- Later, **Auto Repeat to Fit Voice Layers**: choose the smallest repeat count up to 30 that makes
  the voice selection placeable, decided by **actual placement feasibility** rather than by summing
  durations — the same correction R7.3 records above.

**Not in PR #77 and not started.** R7.3's mode 4 is blocked on this.

### R8 — Smart Audio Cut · **EXPERIMENTAL, NOT PLANNED FOR IMPLEMENTATION**

Only once R7 has proven useful in practice:

- Transcribe spoken material where suitable ASR is available.
- Classify segments as Voice, Dialogue, Impact, Mechanical, Nature, Ambience, Other.
- Suggest descriptive names and tags, with the operator free to confirm or change every one.
- Reuse the existing Qwen **text** model for classifying transcripts where that is enough.
- **Do not assume Qwen3-VL can understand raw audio.** Audio-capable models get investigated only
  after the mechanical cutter has earned it.

No new model, dependency or inference pipeline is to be added for this now.

### R9 — Audio Layers usability · **PARTLY IMPLEMENTED (R7.1 Comfort Mix), REST STILL PLANNED**

#### Comfort Mix — optional, OFF by default · **MERGED AND DEPLOYED (PR #77, main `d98fb55a`)**

The owner made a music video with voice layers and reported a real problem: the music is far
louder than the speech, so heavy ducking is the only way to hear the voice — and then the music
comes back abruptly. Two optional controls address the cause rather than the symptom.

* **Auto Balance Voice & Music** (checkbox, **OFF by default**). On, BeatSync measures the
  integrated loudness of the music and of the voice with FFmpeg's own EBU R128 `loudnorm` analysis
  and applies two **bounded** gains *before* mixing, towards −18 LUFS for music and −16 LUFS for
  voice. Corrections are capped at ±12 dB, a positive gain is reduced so it cannot push the
  measured true peak above −1 dBTP, and a clip measuring below −60 LUFS is **left alone rather
  than amplified** — lifting something that is mostly room tone is the worst thing an automatic
  balancer can do. If either side cannot be measured, **nothing is applied and the report says
  so**; it never claims a normalisation that did not happen.
* **Music return / fade back** (seconds). This is the duck *release*. It stays at the historical
  **0.4 s** with Comfort Mix off; with it on the suggested starting value is **2.0 s**, adjustable
  0.5–5 s. The duck *attack* is deliberately unchanged — the complaint was about the music coming
  back, and a longer attack would simply bury the lead-in.

**With Auto Balance off the FFmpeg command is byte-identical to what it was**, which is asserted
against the committed historical builder rather than merely claimed. No measurement pass runs at
all, so an ordinary render costs exactly what it cost before.

Measured A/B on a loud compressed bed and a deliberately quiet voice, with identical placements in
both: music −2.1 dB, voice +12.0 dB (bounded, and reported as bounded). Played back through the
browser's own audio pipeline, the music's recovery after a voice clip went from **+8.6 dB in
0.4 s** to **+2.1 dB in 0.4 s** — roughly 21 dB/s down to 5 dB/s. Neither mix clipped
(−10.0 and −12.1 dBTP).

This is **not** a compressor or a mastering suite, and it does not normalise the finished master:
the limiter settings and the duck depth are untouched.

#### Still planned

The documentation half of R9 is unchanged and unstarted. This is explanation, **not a redesign**:

- What each input actually does.
- How voices are positioned relative to the music.
- What **Music under Voice** means in practice.
- How to use a library built by the Raw Audio Cutter.
- One simple reproducible example, start to finish.

---

### Housekeeping note — `input/gradio_uploads` loses newer files on restart · **MERGED AND DEPLOYED (UX Refresh Phase 8, PR #78, main `d98fb55a`)**

Observed during the 2026-10-27 production deploy: starting the app ran `cleanup_on_startup()`,
which deleted three files from `input/gradio_uploads` that existed nowhere else. The function was
behaving as designed — it protects `audio`, `video` and `video_analysis_cache` and clears
everything else under `input/` — and it did this on **every** launch, so the loss was not specific
to deployment.

**The smallest change that would prevent silent loss was: move rather than delete.** That is now
built, as described, with one difference worth recording: the dated folder is
`input/retired_uploads/<YYYY-MM-DD_HHMMSS>/` rather than the `input/_cleared/…` this note
originally proposed, because the folder is also a **first-class Cache Management category** in the
new Settings tab and needed a name an operator would recognise in that list.

What shipped (local candidate, not deployed):

- one `os.rename` of `input/gradio_uploads` into the dated folder, no-replace, so two starts in
  the same second cannot merge or overwrite;
- `retired_uploads` added to the protected set, so the **next** start does not delete it and
  reproduce the old behaviour one run later;
- an empty uploads folder is skipped, so no empty directories accumulate;
- a failure leaves the old folder exactly where it is — nothing on this path deletes;
- the folder's size and file count are visible in **Settings → Cache Management**, which is how
  it is bounded: the operator clears it deliberately, having first seen what is in it.

It remains deliberately **one change to one function**. It is still **not** a backup or retention
project: there is no index, no manifest and no restore command — the files are simply still there.

R7 **avoids depending on the directory at all**: the Audio Cutter reads its source in place by
absolute path and writes to a folder the operator chooses, so nothing it produces or consumes ever
lives in `input/gradio_uploads`.

---

### UX Refresh — one coherent application rather than a pile of tools · **MERGED AND DEPLOYED (PR #78, main `d98fb55a`)**

> **Naming, stated plainly to prevent a real confusion.** The branch and task identifier for this
> work are `feat/fun-ux-refresh-r8` and `R8-UX-REFRESH`, and its source comments say "R8 UX
> Refresh". That is **not** roadmap item **R8 — Smart Audio Cut**, which is unrelated, still
> experimental and still unimplemented. The two identifiers collided by accident. Nothing in this
> section starts, closes or reinterprets Smart Audio Cut.

The owner's complaint was not about any one feature — audio cutting quality and Comfort Mix were
explicitly accepted — but that BeatSync read as *a collection of independently added tools*. Seven
top-level tabs had grown one at a time, each justified on its own, and the result was a drawer
called "Advanced Tools" holding three unrelated tenants, two separate review tabs, and a settings
panel buried at the foot of a third.

Built on top of the R7.2 candidate, in five separately testable commits:

- **Navigation.** Seven workflow-named tabs in the operator's own order: Render · Creative
  Settings · Raw Cutter · Audio Cutter · Audio Mix · **Media Library** · **Settings**. Clips
  Preview, Audio Preview and Library Preparation became the three sections of Media Library;
  preferences got a tab of their own. Proved to be a pure re-parenting by comparing the complete
  set of widget assignments before and after.
- **Central paths and a Settings tab.** A configurable **Render output** folder (the one
  destination that used to be fixed), every destination in one place, and full backward
  compatibility with existing `state/preferences.json`.
- **Filename collisions.** Opt-in automatic `_01`, `_02` suffixing for both review workflows.
  Default unchanged, and an existing file is never overwritten under either setting.
- **Cache management.** Inspect and clear four recognised application caches, with the expensive
  Stage-5 analysis cache separated and labelled; nothing else is reachable.
- **One truthful Creative Profile.** The preset selector could read `Balanced` while Freestyle, a
  Variation Seed and SFX were armed. It now reads e.g. `Balanced + Freestyle (1 section rule)`.
- **Use Media Library** as a render video source, reusing the existing scan and confirmation gate.
- **Output Video Profiles** (Auto / 1080p / 720p at 25 or 30 fps), which also fix a real defect:
  the render took its resolution from the first source and **stretched** every mismatched clip.
  Fixed profiles pad instead. Auto is the untouched historical path.
- **Audio Cutter soft edges**, bounded by each clip's actual padding so a fade can never reach
  into speech. Off by default.

Full record, including the measurements and the two deliberate deviations from the brief:
[`docs/claude/r8-ux-refresh.md`](docs/claude/r8-ux-refresh.md).

**One thing the brief asked for that is deliberately NOT built**, because an existing contract
forbids it and the contract is right:

- **`Custom — based on Balanced`** as a profile label. It needs a remembered "last selected
  preset", which `.claude/rules/creative-presets.md` forbids and a test pins — six integers
  describe a render completely, and a remembered base would be a second source of truth able to
  disagree with the sliders. A base is named only when the live values prove it.

**A correction to an earlier entry here.** This section previously also listed a reset action as
not-built, on the grounds that "selecting the `Balanced` preset already does it". **That was
false.** A preset writes the six base sliders and nothing else — it does not clear the Variation
Seed, does not disable Freestyle and does not touch Audio Mix. The standing prohibition is also
narrower than that entry implied: it forbids a reset *for the six global controls*, which an
action that deliberately leaves the sliders alone is not.

R8 Final Polish therefore **builds it**, as `🧹 Clear creative variations`. The two actions are
disjoint by construction — `Balanced` → the six sliders only; Clear → the seed and Freestyle only
— so neither is "reset everything" and neither becomes a second source of truth.

#### Final Polish — the finishing pass after the owner reviewed the screenshots

A sixth commit on the same branch, scoped to practical issues rather than another audit:

- **Video Review keep-only classification**, matching Audio Review. Confirm enables at **one clip
  kept**; anything still unmarked gets a second step with exact counts, both destinations and
  Proceed / Cancel. Proceed files the unmarked clips as Discarded **for this batch only** and
  writes nothing to the durable record; Cancel changes nothing. No second transfer engine was
  written — Audio Review's `SortPlan` / `plan_sort` / `plan_preview_lines` moved into the shared
  `clips_review` core and are re-exported, so the two tabs now run the *same objects*.
- **Creative Settings 3516 px → 1375 px**, by ordering rather than deletion: preset, seed and the
  six sliders first; AI Director, Freestyle and Variant Lab collapsed below them.
- **Settings laid out by purpose** — one path per row with its own Browse beside it (all nine
  verified on-page), grouped into render/video, audio/voice and review destinations, with Cache
  Management as the single collapsed block. Preference keys, values and handlers unchanged;
  defaults verified to survive a restart. **Cache deletion logic untouched** — no defect found.
- **Render input compacted** — drop zone bounded to 113 px, two headings that named the single
  control beneath them removed, profile help cut to one line. `Create Music Video` moved from
  y=903 to **y=826** at 1440 CSS px, i.e. from below a 900 px fold to above it.

**One brief item measured and deliberately NOT shipped:** a native Browse button for the music
track. It was built and reverted — a `gr.File` raises `InvalidPathError` for any path outside
Gradio's allowed roots, and the three workarounds each cost more than the button is worth
(widening `allowed_paths` exposes a tree to the browser; copying the file is the duplication the
brief ruled out; a path textbox rewrites the render source pipeline and loses drag-and-drop).
The measurement and the exact exception are pinned in `tests/test_render_music_input.py`.

End to end after all four: 720p · 30 fps chosen in the UI over four 320×240 sources rendered
`width=1280 height=720 r_frame_rate=30/1`.

#### Owner playtest fixes — what real use found

A seventh commit, from the owner's own hands-on session rather than from a review:

- **P0, Video Review Confirm died after the first batch.** The highest-priority defect, and it
  stopped work entirely: batch one sorted 100/100, batch two showed a correct `Kept 18 ·
  Discarded 82 · Undecided 0` and the button stayed greyed out. **Root cause was a client-side
  readiness cache with no notion of which page it described** — `syncConfirm` returns early when
  the answer equals the last one it saw, and the `true` left over from batch one swallowed batch
  two's first genuine transition, so the server was never asked. Pressing Re-read by hand enabled
  it instantly, which is what proved the record was right. Readiness is now keyed to
  `session/page`, every reply is tagged with the page it was requested for, and the repaint
  observer also watches the nonce attribute. **Verified across three real page transitions with
  205 clips: 31 kept + 174 discarded = 205, all original names, nothing lost or duplicated.**
- **Freestyle states its mode** and shows the ten section rules only when on — *hidden, never
  cleared*, so a rule set survives being switched off and back on.
- **Randomize and Clear are two equal buttons on one row**, still behaviourally disjoint, and the
  Creative Profile reads `Base preset: …` / `Active variations: …` from the same computation as
  before.
- **The four audio destinations stay four settings**, but Audio Review now falls back to the
  Voice pair when its own override is blank. The fallback is read-side only and a set override
  always wins, so nothing is ever consolidated or overwritten. A work folder that is also a
  finished library is now reported — and only reported.
- **Stale text corrected**: "where KEPT clips would go" (files move), "sort the decided clips"
  (keep-only also files the unmarked ones), and the Render tab's permanent "stays disabled until
  you confirm" that printed while confirmed and rendering.
- **Variant Lab**: long guidance behind a help panel, result boxes small when empty. Presentation
  only.

Encoding and the output profiles were not touched; 1080p · 25 fps still renders
`1920×1080 @ 25/1`.

---

## Status summary

Four different things are distinguished deliberately below, because conflating them is how a
roadmap starts lying: **deployed** (running in production), **merged** (on `main`, not necessarily
deployed), **local candidate** (exists on a branch or a playtest copy, validated, but neither
merged nor shipped), and **planned** (agreed, not written).

| Item | Status |
|---|---|
| **R1** — Raw Cutter V1, mechanical quality-first selection | **DONE** — merged in PR #68, **deployed** |
| **R4-A** — Automatic folder batch mode | **DONE** — merged in PR #69, **deployed** |
| **Scene guards + VFR-safe exports** — conservative 0.20 s guard at detected cuts, `-fps_mode:v passthrough -enc_time_base:v demux` | **DONE** — merged in PR #70, **deployed** |
| **R4-B** — Live progress and active controls | **DONE** — merged in PR #71, **deployed** at `2044f360` |
| **R4-C** — Synthetic output naming (opt-in, default off) | **DONE** — merged in PR #72, **deployed** at `0aed35d6`. Two QUALIFIED independent reviews; three MINOR findings accepted as non-blocking and still open (see the section above) |
| **R5** — Clips Preview Selector (post-cut triage tab) | **P1 IMPLEMENTED** — the fifth tab reviews and classifies clips, read-only and measured; **moving files is NOT implemented**. Specification: `docs/claude/clips-preview-selector-v1.md`; contract: `.claude/rules/clips-preview.md` |
| **R5-UX** — Clips Preview usability V1–V4 (auto-Confirm, durations, adaptive seek, same-volume sorting, native folder picker, saved defaults) | **DONE** — merged in PR #76, **deployed** at `25c4db3b`. Owner-accepted after a hands-on test; whole suite 7,912 passed / 9 skipped at the merge. Not independently reviewed |
| **R6** — Raw Cutter output video profiles | **DONE** — merged in PR #76, **deployed** at `25c4db3b`. Four profiles + Same as source; Same as source proven byte-identical; FFprobe-verified on both encoders in production |
| **R7** — Raw Audio Cutter V1 (mechanical, no AI) | **MERGED AND DEPLOYED** (PR #77, main `d98fb55a`) — silence-based cutting with the four controls exposed, WAV/FLAC export, a manifest, and in-page review. Owner-tested on real audio and **accepted**: cuts good, 0.5–0.6 s silence ideal, no truncated words. **Not independently reviewed** |
| **R7.1** — hover audio gallery + Audio Layers Comfort Mix | **MERGED AND DEPLOYED** (PR #77, main `d98fb55a`) — Clips-Preview-style gallery with hover playback and Confirm-and-Sort; optional Auto Balance (EBU R128) and an adjustable music return. Legacy mix proven byte-identical with Auto Balance off. Browser-verified on port 7864. Owner-tested and **accepted** |
| **R7.2** — lazy audio preview, 🎧 Audio Preview tab, keep-only sorting, folder validation, MP3 | **MERGED AND DEPLOYED** (PR #77, main `d98fb55a`) — the base64 page budget is **removed** (not raised) in favour of an authorized range-capable endpoint reusing R5's registry; a standalone review tab that works after a restart; Confirm admitted by one kept clip with a counted, batch-bound confirmation before unmarked clips are swept; identical/nested destinations refused as typed and again at Confirm; MP3 at 192 kbps with the lossy trade stated plainly. Owner-tested and **accepted**. **Not independently reviewed** |
| **R7.3** — "When voice clips do not fit" modes | **OWNER-ACCEPTED, IN DRAFT PR, NOT DEPLOYED** — branch `feat/fun-audio-overflow-r7-3` off `d98fb55a`. Modes 1–3 shipped: Strict (default, the historical refusal, byte-identical filtergraph) / Skip (cursor deliberately not advanced, so a later shorter clip still lands) / Trim-then-skip (a real `atrim` on the voice input, not only a shortened plan). Mode 4 (Repeat-music) still blocked on R10 and **not offered**. Decided by the real planner, never by summing durations; zero placed clips stays fatal. **The owner has personally tested Strict / Skip / Trim and accepted them** — which is a validation milestone, not a deployment one: **still not merged, still not deployed, still not independently reviewed** |
| **Stage-5 Cancel** — Cancel actually interrupts video analysis | **LOCAL CANDIDATE, IN DRAFT PR** — branch `feat/fun-stage5-cancel-shared-cache` stacked on `feat/fun-audio-overflow-r7-3`. Retires C3-R1A's `BOUNDARY_ONLY_CANCEL` for Stage 5 after a real owner incident (a whole library selected against a cold cache; Cancel pressed; Stage 5 ran on for about an hour). Cancellable during the cache-identity phase, the record scan, between sources, inside the FFmpeg scene pass, every 32 decoded frames, every 16 scored windows, and during the Qwen worker wait — which also terminates the worker's own `llama-server` grandchild. Measured latencies on real media: **0.016–0.45 s** deterministic, **0.20 s** Qwen. Completed records survive and are reused; nothing incomplete is written; Stage 6 never starts. The Qwen worker **process** is still cancellation-free by contract. **Not merged, not deployed, not independently reviewed** |
| **Shared Analysis Cache** — one Stage-5 cache across BeatSync versions | **LOCAL CANDIDATE, IN DRAFT PR** — same branch. A per-user setting at `%LOCALAPPDATA%\BeatSync\shared-config.json`, deliberately **outside every checkout**, so a new playtest tree finds the same cache with no setup. A location change, never a migration: the directory was never part of Stage-5 identity, so `CACHE_CONTRACT_VERSION` and `ANALYSIS_VERSION` are untouched and **no record was invalidated**. Measured across two app roots: 8 sources analysed cold in 3.24 s by the first, **8/8 cache hits in 0.053 s** by the second. Includes an additive, never-overwriting **Import existing cache** (2,086 current-generation records copied out of the real production cache, which came out byte-identical) and a shared-folder warning on the Cache Management panel. **Not merged, not deployed, not independently reviewed** |
| **R10** — Quick Render **Repeat Music 1×–30×** | **PLANNED, NOT IMPLEMENTED** — 1× must be the existing path unchanged; cuts must cover the whole extended timeline rather than repeating the rendered sequence; analysis reused rather than re-run; cancellable with an estimated duration |
| **UX Refresh** — seven workflow-named tabs, Media Library, Settings, truthful Creative Profile, Output Video Profiles, soft audio edges | **MERGED AND DEPLOYED** (PR #78, main `d98fb55a`) — branch `feat/fun-ux-refresh-r8`. Whole suite green at every commit; FFprobe-verified across 4 profiles × 5 source shapes; cache clearing, collision suffixing, preference persistence and the Media Library source all exercised in the running GUI. Owner-tested and **accepted**. **Not independently reviewed.** Its `R8-UX-REFRESH` identifier is unrelated to roadmap item R8 below |
| **R8** — Smart Audio Cut (ASR + classification) | **EXPERIMENTAL, DEFERRED** — still not planned for implementation. R7 adds no transcription and no classifier, so nothing here has started. **Unrelated to the `R8-UX-REFRESH` task identifier** used by the UX Refresh row above; the two collided by accident |
| **R9** — Audio Layers usability and tutorial | **PARTLY IMPLEMENTED** — R7.1's optional Comfort Mix (Auto Balance + music return) is merged and deployed at `d98fb55a`, and R7.3's overflow policy is a local candidate; the documentation half is unstarted. **R10** is the next Audio Layers-adjacent item |
| **R2** — Transition Detection + Edge Cleaner | **DEFERRED** — measured as unreliable on real footage; no detector exists, and it does not block R4 |
| **R3** — Smart Passages / AI Director Cutter (Qwen-guided) | **PLANNED** for future consideration — **opt-in by design**; no Qwen in the Raw Cutter today |
| **R4** — remaining usability and scale ideas | **PLANNED** |

### Current priority order (product-owner decision)

Re-set by the owner after the V3 playtest: **practical usability first, one feature at a time.**

1. ~~**Finish R4-B** live progress and get it reviewed.~~ **Done** — merged in PR #71 and deployed.
2. ~~**R4-C** synthetic output naming.~~ **Done** — merged in PR #72 and deployed at `0aed35d6`,
   with three accepted MINOR findings still open.
3. **R5 / R5-UX — Clips Preview, finish the usability work.** The gallery (P1) is merged and
   deployed; the usability and sorting work is a **local candidate only**. Next: owner playtest,
   then a decision on whether the simple same-volume sorter ships as-is or is replaced by the
   separately reviewed transfer engine. The A/B/C/F owner decisions stay open either way.
4. **R6 — Raw Cutter output video profiles.** The recommended next build: it is bounded, it has a
   default that cannot regress anything (*Same as source*), and it makes the clip library usable
   as a whole rather than as a pile of mismatched formats.
5. **R7 — Raw Audio Cutter V1**, mechanical only, no AI. **DONE** — R7 / R7.1 / R7.2 are
   **merged (PR #77) and deployed at main `d98fb55a`**, owner-tested and accepted.
5a. **UX Refresh** — **DONE**, merged as PR #78 and deployed at the same `d98fb55a`, owner-tested
   and accepted. It changed no R7 engine: the Audio Cutter, the review galleries and Comfort Mix
   are the same code in different containers, plus one opt-in fade.
6. **R7.3 — "When voice clips do not fit"** — **OWNER-ACCEPTED, NOT DEPLOYED**, on
   `feat/fun-audio-overflow-r7-3`. Modes 1–3 ship (Strict / Skip / Trim-then-skip); mode 4 waits
   on R10. The owner test is **done and passed**; what remains is merge and deploy, which are
   separate authorizations.
6a. **Stage-5 Cancel + Shared Analysis Cache** — **LOCAL CANDIDATES** on
   `feat/fun-stage5-cancel-shared-cache`, stacked on R7.3's branch. Two usability fixes that come
   from the same owner session: Cancel now actually interrupts video analysis (measured
   0.016–0.45 s, against an incident where it took about an hour), and one shared cache folder
   serves every BeatSync version under the same Windows user (measured 8/8 reuse across two app
   roots). Awaiting the owner test; neither merged nor deployed.
7. **R10 — Repeat Music 1×–30× — THE NEXT PRIORITY.** It unblocks R7.3's fourth mode
   (Repeat Music to Fit) and the later Auto-Repeat-to-Fit, and it is the one remaining answer to
   "my voice material is longer than my song" that drops and shortens nothing at all.
8. **R9 — Audio Layers explanation**, which costs no engine work and unblocks the existing feature.
9. **R3 / Smart Passages** and **R8 / Smart Audio Cut** stay behind all of the above. Both are
   **opt-in by design**, and neither may be mixed with Stage-5 cache identity or the existing
   Qwen worker.

> **The security research programme is separate and is not re-prioritised here.** PR #75, the
> Win32 primitive and the A/B/C/F owner decisions are recorded in their own evidence packages and
> in `.claude/rules/clips-preview.md`. Nothing in this product roadmap closes, reopens or
> reinterprets any of them.

---

## Related documents

| Document | What it is |
|---|---|
| [`CHANGELOG-FORK.md`](CHANGELOG-FORK.md) | What changed and when — the AGPL-3.0 §5(a) modification record |
| [`docs/claude/raw-cutter-edge-cleaner.md`](docs/claude/raw-cutter-edge-cleaner.md) | The R2 feasibility measurement, and why the obvious signal does not work |
| [`docs/claude/clips-preview-selector-v1.md`](docs/claude/clips-preview-selector-v1.md) | The R5 Clips Preview Selector specification — gallery, classification and safe file movement |
| [`docs/claude/r8-ux-refresh.md`](docs/claude/r8-ux-refresh.md) | The UX Refresh audit and record — what was observed in the running UI, the seven-tab restructure, and the measurements behind the output profiles and audio fades |
| [`docs/claude/README.md`](docs/claude/README.md) | Map of the engineering rules and reference material |
| [`README.md`](README.md) | What the application is and how to run it |

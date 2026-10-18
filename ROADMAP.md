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
the active work is **naming the output** (R4-C — open draft PR #72, not merged) and
**triaging the result** (R5, the next feature to build). The ordering is in the status summary at the
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
**No Playtest package contains R4-C**, which is still an open pull request.

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

#### R4-C — Synthetic output naming · **OPEN DRAFT PR #72 — NOT merged, NOT deployed**

Implemented on `feat/raw-cutter-synthetic-output-naming`, independently reviewed, and published as
**open draft pull request #72**. It is **not on `main`** and **production does not have it** —
production remains at `2044f360`. The independent review returned a QUALIFIED verdict with two
non-blocking MINOR findings; the source-index ordering one has since been corrected by product-owner
decision (see the counter bullet below), which is a further local change on top of the published
commit and is itself awaiting independent delta review.

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

Measured locally: full suite **7 182 passed, 8 skipped, 0 failed** (PR #72 as published is
7 094 / 8 / 0, and the accepted `main` baseline is 6 953 / 8 / 0), including a new
`tests/test_raw_cutter_naming.py`. The 88 added tests cover the job-wide source width at totals of
1, 2, 9, 10, 99, 100, 999, 1 000, 1 001, 1 200 and 10 000 — including an end-to-end 1 000-source
batch through the real handler and an executable negative control in which the superseded
per-index rule fails the ordering requirement.

#### Remaining R4 ideas · **PLANNED, NOT IMPLEMENTED**

- **Cancellation during an active FFmpeg segment**, rather than only between clips.
- **A native local file/folder picker**, without copying large videos into Gradio's upload folder.
- **Optional fast stream-copy mode**, with measured keyframe-drift reporting — only worth shipping
  if the report can honestly say which clips moved and by how much.

---

### R5 — Clips Preview Selector · **PLANNED, NOT IMPLEMENTED — next high-priority workflow**

A **fifth top-level tab** for reviewing clips *after* the Raw Cutter has produced them. This is
post-cut media triage, deliberately **not** a per-cut confirmation step inside the Raw Cutter —
Folder Batch exists precisely so that cutting needs no interaction.

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

**The 100-clip page size is a product target, not a measured performance claim.** It needs
benchmarking at 25 / 50 / 100 items in the pinned Windows + Gradio environment before anyone
promises it.

---

## Status summary

Three different things are distinguished deliberately below, because conflating them is how a
roadmap starts lying: **deployed** (running in production), **merged** (on `main`, not necessarily
deployed), and **local candidate** (exists on a branch, validated, but neither merged nor shipped).

| Item | Status |
|---|---|
| **R1** — Raw Cutter V1, mechanical quality-first selection | **DONE** — merged in PR #68, **deployed** |
| **R4-A** — Automatic folder batch mode | **DONE** — merged in PR #69, **deployed** |
| **Scene guards + VFR-safe exports** — conservative 0.20 s guard at detected cuts, `-fps_mode:v passthrough -enc_time_base:v demux` | **DONE** — merged in PR #70, **deployed** |
| **R4-B** — Live progress and active controls | **DONE** — merged in PR #71, **deployed** at `2044f360` |
| **R4-C** — Synthetic output naming (opt-in, default off) | **OPEN DRAFT PR #72** on `feat/raw-cutter-synthetic-output-naming`; **not merged, not deployed**. One reviewed MINOR (source-index sort order above 999) has been corrected locally on top of the published commit and awaits independent delta review |
| **R5** — Clips Preview Selector (post-cut triage tab) | **PLANNED** — specified in `docs/claude/clips-preview-selector-v1.md`, not implemented |
| **R2** — Transition Detection + Edge Cleaner | **DEFERRED** — measured as unreliable on real footage; no detector exists, and it does not block R4 |
| **R3** — Smart Passages / AI Director Cutter (Qwen-guided) | **PLANNED** for future consideration — **opt-in by design**; no Qwen in the Raw Cutter today |
| **R4** — remaining usability and scale ideas | **PLANNED** |

### Current priority order (product-owner decision)

1. ~~**Finish R4-B** live progress and get it reviewed.~~ **Done** — merged in PR #71 and deployed.
2. **R4-C** synthetic output naming — **open draft PR #72**. Independently reviewed (QUALIFIED); the
   source-index ordering MINOR has been corrected locally since, so the next step is an independent
   delta review of that correction. Nothing about it is merged or deployed.
3. **R5** Clips Preview Selector — the next high-priority feature to build, and the reserved fifth
   top-level tab. The interactive gallery first, safe file movement second. The accepted
   specification in `docs/claude/clips-preview-selector-v1.md` is unchanged.
4. **R3 / Smart Passages** only once the post-cut review workflow exists. It stays **opt-in** by
   design, and it must not be mixed with Stage-5 cache identity or the existing Qwen worker.

---

## Related documents

| Document | What it is |
|---|---|
| [`CHANGELOG-FORK.md`](CHANGELOG-FORK.md) | What changed and when — the AGPL-3.0 §5(a) modification record |
| [`docs/claude/raw-cutter-edge-cleaner.md`](docs/claude/raw-cutter-edge-cleaner.md) | The R2 feasibility measurement, and why the obvious signal does not work |
| [`docs/claude/clips-preview-selector-v1.md`](docs/claude/clips-preview-selector-v1.md) | The R5 Clips Preview Selector specification — gallery, classification and safe file movement |
| [`docs/claude/README.md`](docs/claude/README.md) | Map of the engineering rules and reference material |
| [`README.md`](README.md) | What the application is and how to run it |

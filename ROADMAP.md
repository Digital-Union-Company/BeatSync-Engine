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

### R1 — Raw Cutter V1 · **IMPLEMENTED IN PR #68**

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

**Not in the frozen Playtest.** `D:\BeatSync-Playtest-dd059fb1\` was built from `dd059fb1`, which
predates this work. The Raw Cutter is not in that package and will not be until a later Playtest
build is made from a commit that contains it.

---

### R2 — Transition Detection + Edge Cleaner · **PLANNED, NOT IMPLEMENTED**

The next priority, and the one that most affects real footage.

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

### R3 — AI Director Cutter · **PLANNED, NOT IMPLEMENTED**

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

### R4 — Raw Cutter usability and scale · **PLANNED, NOT IMPLEMENTED**

Secondary improvements, kept deliberately separate from R2 (transition quality) and R3 (AI-assisted
selection). These are worth doing when Playtest feedback justifies them, not before.

- **Real per-clip progress updates** during a cut run. Progress is currently captured but not
  streamed, so a long job shows one line until it finishes.
- **Cancellation during an active FFmpeg segment**, rather than only between clips.
- **A native local file/folder picker**, without copying large videos into Gradio's upload folder.
- **Optional fast stream-copy mode**, with measured keyframe-drift reporting — only worth shipping
  if the report can honestly say which clips moved and by how much.
- **Batch processing** of multiple source files in one run.

---

## Status summary

| Item | Status |
|---|---|
| **R1** — Raw Cutter V1, mechanical quality-first selection | **IMPLEMENTED IN PR #68** |
| **R2** — Transition Detection + Edge Cleaner | **PLANNED** — the measurement is done, the detector is not built |
| **R3** — AI Director Cutter (Qwen-guided selection) | **PLANNED** — no Qwen in the Raw Cutter today |
| **R4** — Raw Cutter usability and scale | **PLANNED** |

---

## Related documents

| Document | What it is |
|---|---|
| [`CHANGELOG-FORK.md`](CHANGELOG-FORK.md) | What changed and when — the AGPL-3.0 §5(a) modification record |
| [`docs/claude/raw-cutter-edge-cleaner.md`](docs/claude/raw-cutter-edge-cleaner.md) | The R2 feasibility measurement, and why the obvious signal does not work |
| [`docs/claude/README.md`](docs/claude/README.md) | Map of the engineering rules and reference material |
| [`README.md`](README.md) | What the application is and how to run it |

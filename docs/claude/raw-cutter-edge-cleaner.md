# Raw Cutter — Edge Cleaner feasibility: MEASURED AND DEFERRED

**Date:** 2026-10-16 · **Verdict:** deferred to a focused V2 task · **No detector was shipped.**

The product owner asked for a lightweight pass that trims transition frames off a clip's edges —
so a cross-dissolve does not leave half of the *next* shot ghosted onto the end of this one — and
explicitly preferred an honest deferral over a speculative detector. This records the measurement
that decided it.

## What was proposed

An edge-only pass after shot planning and before the final encode: inspect ~2–3 seconds at each
end of a candidate clip, at low resolution and modest sampling FPS, and trim boundary frames that
look like a gradual transition. No Qwen, no full-resolution optical flow.

The obvious cheap signal is the one the Raw Cutter already uses: FFmpeg's `scene` score, read per
frame rather than thresholded.

## The measurement

Four synthetic fixtures at 320×180 / 15 fps, analysed at **8 fps / 192 px wide** — the budget an
edge pass could actually afford. Per-frame `lavfi.scene_score` via
`select='gte(scene,0)',metadata=print`.

| Fixture | Max score | Frames ≥ 0.05 | Shape |
|---|---|---|---|
| Hard cut at 5 s | **1.000** | 1 | one isolated spike, everything else exactly `0.000` |
| 1 s cross-dissolve at 4–5 s | **0.290** | 2 | sparse spikes (`0.290`, `0.140`) separated by `0.000` |
| 2 s cross-dissolve at 4–6 s | **0.140** | 4 | `0.140`, `0.070`, `0.080`, `0.060` scattered among `0.010` |
| Continuous motion, no edit | **0.061** | 1 | a *regular* ~`0.034–0.045` pulse about twice a second |

## Why this is a refusal, not a tuning problem

**The ranges overlap, and not narrowly.** The frames that identify the 2-second dissolve sit at
`0.060–0.140`. Ordinary motion with no edit at all routinely reaches `0.045` and peaks at `0.061`.
Any threshold low enough to catch the slow dissolve also fires on a camera pan — which is exactly
the failure the requirement forbids ("never trim because of ordinary camera movement without
supporting evidence").

**The signal is structurally wrong for the job, which is the deeper point.** `scene` measures the
difference between *adjacent* frames. A dissolve is defined by the opposite property: each frame
is nearly identical to the one before it, sustained over a second or more. So the better the
dissolve, the *smaller* its scene score — the 2-second dissolve scored lower (`0.140`) than the
1-second one (`0.290`), and both scored far below the hard cut (`1.000`). Lengthening the
transition makes it *harder* to see with this signal, which is precisely backwards.

The hypothesis going in was "a dissolve shows up as a sustained run of moderate scores". The data
says it does not: the elevated frames are **sparse**, with `0.000` and `0.010` in between.

## What would actually work (the V2 proposal)

The smallest implementation that stands a chance uses a **long-baseline** comparison instead of an
adjacent-frame one:

1. Decode the edge window only (first/last ~3 s), at ~4 fps and ~160 px wide, to raw greyscale —
   one `ffmpeg -vf fps=4,scale=160:-2,format=gray -f rawvideo -` per edge.
2. Compare frame *N* with frame *N+k* where `k ≈ 1 second`, not `N+1`. A dissolve produces a large
   **long-baseline** difference while the adjacent-frame difference stays near zero; a pan produces
   both; a static shot produces neither. That ratio — long-baseline change divided by accumulated
   adjacent change — is the discriminator, and it is cheap.
3. Optionally confirm with a histogram/variance check: a cross-dissolve is a linear blend, so mid-
   transition frames have *lower* contrast than either source, which a pan does not.
4. Trim only when the evidence is clear, and if trimming takes the clip under the minimum, skip the
   clip — the same quality-first rule the planner already follows.

Budget: two short decodes per clip edge, no model, no full-resolution work. The reason it is not in
this change is that it needs its own calibration against real footage and its own regression
fixtures, which is a focused task rather than a rider on a policy correction.

## The limitation to state plainly either way

**An edge-only pass cannot find a dissolve in the middle of an otherwise undivided interval.** If
the first detector misses a transition entirely — and a slow dissolve is exactly what it misses,
per the table above — the planner sees one long continuous shot, and an edge pass that only
inspects the first and last few seconds of that shot will never look at the middle where the
transition actually is. Edge cleaning improves the *boundaries the planner already found*. It is
not a second chance at detection.

Catching mid-interval dissolves requires a different pass over the whole source, with the
long-baseline signal above. That is a larger piece of work and should not be confused with edge
cleaning.

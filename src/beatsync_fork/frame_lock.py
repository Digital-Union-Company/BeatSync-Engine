"""FUN R10 Final Frame Fix: the frame-exact segment contract.

**A segment that requests N output frames must produce N output frames, or fail clearly.**

Nothing in this module runs FFmpeg, probes a file or touches a disk. It owns three pure
decisions, so each is testable on a bare interpreter exactly as CLAUDE.md's hard rule intends:

* :func:`frames_from_progress` -- what FFmpeg's own ``-progress`` stream said it wrote;
* :func:`recovery_seek` -- where to seek instead, when a window flush against the end of a
  source came back short;
* :func:`describe_shortfall` / :func:`describe_truncated_plan` -- the bounded wording a short
  segment is reported with.

## The defect this exists to correct, measured

`src_b_1080p.mp4` is 30.000000 s at 30 fps (900 frames, PTS 0 .. 29.966667). A 720p25 render
planned clip 22 as ``start=28.840000`` for ``1.160000`` -- exactly the source's remaining
content -- and asked for 29 output frames at 25 fps. FFmpeg wrote **28**:

```
-ss 28.84 -t 1.16 -i src_b_1080p.mp4                     ->  34 source frames  (not 35)
  ... -vf trim,setpts                                    ->  34
  ... -vf trim,setpts,fps=25                             ->  28   SHORT
-ss 27.84 -t 1.16 -i src_b_1080p.mp4 (1.0 s earlier)     ->  35 source frames
  ... -vf trim,setpts,fps=25                             ->  29   OK
```

So the ``fps`` filter is **innocent**: given 35 input frames it produces 29 every time. The loss
is in the input window. Accurate ``-ss`` snaps the window start **forward** to the first frame
whose PTS is at or after the seek point -- 28.840000 becomes 28.866667, frame 866 -- which costs
up to one *source* frame of content. Anywhere else in the file that is invisible, because the
window simply reaches 1/30 s further into the source and the 35th frame is there. At the end of
the source there is nothing past EOF to make it up with, so the shortfall survives into the
output: 34 source frames spanning 1.133 s fill only 28 slots of a 25 fps grid.

A second, smaller contributor rides along at 30 fps. The planner clamps its start to
``video_duration - source_duration`` using the **unquantised** duration, while the extractor
extends the window to the quantised ``frame_count / fps``; at 1.16 s and 30 fps that is
1.166667 s, so the window already overhangs the source by 0.006667 s before the seek rounds.
Both are the same mistake -- a window anchored at its start when only its end is pinned -- and
both are corrected by the same arithmetic.

## Why the correction is a re-seek and not a padded frame

:func:`recovery_seek` anchors the window to the **end** of the source and then steps back one
whole source frame, so accurate ``-ss`` still has the full requested span of real content in
front of it after it rounds forward. Every frame in the result is a decoded source frame; the
clip's content shifts at most one source frame earlier, and ``-vframes N`` still bounds the
output, so the segment can never come back long. Holding or duplicating the last frame to reach
N was rejected: a real frame *can* be decoded here, and a fabricated one would make a
frame-exact timeline out of content the source never contained.

When no earlier position exists -- the source is genuinely shorter than the segment, or the
window already starts at zero -- this module answers ``None``. That is deliberately not a
recovery: a caller that cannot get N real frames must report the shortfall, never absorb it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

from beatsync_fork.media_sample import parse_progress_output

#: Seconds of slack allowed when comparing two timeline positions. One microsecond is far below
#: a frame at any rate this application renders and far above float noise in ``n / fps``.
FRAME_LOCK_TOLERANCE_SECONDS = 1e-6

#: How the produced frame count was established. ``UNMEASURED`` is a genuine third answer and is
#: never silently read as zero -- "FFmpeg did not tell us" and "FFmpeg wrote nothing" lead a
#: caller somewhere different.
MEASURED_BY_PROGRESS = "progress"
MEASURED_BY_CONTAINER = "container"
UNMEASURED = "unmeasured"

#: The total number of extraction attempts one segment may cost, re-seek included. Two: the
#: planned window, then at most one corrected window. A third would be an unbounded retry loop
#: wearing a constant, and nothing measured here needs one.
MAX_EXTRACTION_ATTEMPTS = 2


@dataclass(frozen=True)
class FrameCountVerdict:
    """How many output frames a segment asked for, and how many it actually got.

    ``produced is None`` means **unmeasured**, which is neither a pass nor a failure: it is the
    absence of evidence, and a caller must not convert it into either.
    """

    requested: int
    produced: Optional[int] = None
    measured_by: str = UNMEASURED

    @property
    def measured(self) -> bool:
        return self.produced is not None

    @property
    def exact(self) -> bool:
        """True only on affirmative evidence that the counts match. Never true when unmeasured."""
        return self.produced is not None and self.produced == self.requested

    @property
    def short_by(self) -> Optional[int]:
        """Frames missing, or ``None`` when unmeasured. Zero when the segment is long, not negative."""
        if self.produced is None:
            return None
        return max(0, self.requested - self.produced)

    @property
    def over_by(self) -> Optional[int]:
        """Frames in excess of the request. ``-vframes`` makes this impossible, so a non-zero
        value is evidence the output is not the segment that was asked for rather than a
        cosmetic oddity -- and it is reported, not clamped away."""
        if self.produced is None:
            return None
        return max(0, self.produced - self.requested)


def frames_from_progress(requested: Any, raw: Any) -> FrameCountVerdict:
    """Read the written output-frame count out of an FFmpeg ``-progress`` stream.

    ``frame=`` is documented as a count of frames **written to the output**, which is exactly the
    quantity ``-vframes`` bounds, so it answers this contract's question directly and costs
    nothing -- the stream is already captured alongside stderr.

    Delegates the parse to :func:`beatsync_fork.media_sample.parse_progress_output`, whose
    block-terminator semantics are the hardened ones: evidence comes from the **last completed
    block alone**, so ``frame=29 / progress=continue / progress=end`` reports no count rather
    than inheriting an earlier one. A stream whose final block is not ``progress=end`` is a
    truncated record, not a smaller valid one, so it yields **UNMEASURED** and the caller falls
    back to reading the file it just wrote.
    """
    requested_frames = _non_negative_int(requested)
    if requested_frames is None:
        return FrameCountVerdict(requested=0)
    evidence = parse_progress_output(raw)
    if not evidence.saw_end or evidence.frames is None:
        return FrameCountVerdict(requested=requested_frames)
    return FrameCountVerdict(requested=requested_frames, produced=evidence.frames,
                             measured_by=MEASURED_BY_PROGRESS)


def from_container_count(requested: Any, counted: Any) -> FrameCountVerdict:
    """The same verdict, from a metadata-only read of the produced file.

    Measured on the bundled FFmpeg build: for the CFR segments this application writes, the
    container's declared frame count agrees exactly with a full ``-count_frames`` decode, on
    every case in `tests/test_frame_lock.py`'s reported-defect matrix. So the cheap read is the
    reliable one, and no full-frame decode belongs on a render path.
    """
    requested_frames = _non_negative_int(requested)
    if requested_frames is None:
        return FrameCountVerdict(requested=0)
    produced = _non_negative_int(counted)
    if produced is None:
        return FrameCountVerdict(requested=requested_frames)
    return FrameCountVerdict(requested=requested_frames, produced=produced,
                             measured_by=MEASURED_BY_CONTAINER)


def recovery_seek(start_time: Any, needed_seconds: Any,
                  source_duration: Any, source_fps: Any) -> Optional[float]:
    """Where to seek instead, so a short window gets its missing frames from the source.

    Returns a strictly earlier seek position, or ``None`` when there is no honest one -- which a
    caller must treat as "report the shortfall", never as "accept the segment".

    The arithmetic is two steps and both are load-bearing:

    1. **Anchor on the end of the source, not the start of the window.** ``min(start_time,
       source_duration - needed)`` undoes the planner's overhang: it never moves a window that
       already fits, and it pulls one that does not back to the latest position where the full
       requested span still lies inside the file.
    2. **Then step back one whole source frame.** Accurate ``-ss`` rounds the start *forward* to
       the next source frame boundary, so a window anchored exactly at ``duration - needed``
       still loses up to one frame. One source frame of lead-in is the exact amount that rounding
       can consume, which is why the margin is ``1 / source_fps`` and not ``1 / output_fps``: a
       24 fps source feeding a 30 fps render needs 1/24 s of room, and 1/30 s would not be
       enough.

    ``None`` is returned when the inputs are not finite positive measurements, when the source is
    genuinely shorter than the requested segment (no seek can conjure content that is not there),
    or when the computed position is not actually earlier than the one that already failed --
    retrying the same window is not a recovery.
    """
    start = _finite(start_time)
    needed = _finite(needed_seconds)
    duration = _finite(source_duration)
    fps = _finite(source_fps)
    if start is None or needed is None or duration is None or fps is None:
        return None
    if needed <= 0.0 or duration <= 0.0 or fps <= 0.0 or start < 0.0:
        return None
    # The source cannot supply the requested span from anywhere. Not a recovery -- a shortfall.
    if duration + FRAME_LOCK_TOLERANCE_SECONDS < needed:
        return None

    anchored = min(start, duration - needed)
    candidate = max(0.0, anchored - (1.0 / fps))
    if candidate >= start - FRAME_LOCK_TOLERANCE_SECONDS:
        return None
    return candidate


def describe_shortfall(verdict: FrameCountVerdict, attempts: Any = 1,
                       budget: int = 240) -> str:
    """One bounded sentence explaining why a segment was refused.

    States the measurement and where it came from, because "28 of 29" read off a container header
    and the same figure read off FFmpeg's own progress stream are different strengths of
    evidence. Never claims the file is damaged: a short segment is a short segment, and this
    module has not established anything about the source's bytes.
    """
    produced = verdict.produced
    counted = "none reported" if produced is None else str(produced)
    tries = _non_negative_int(attempts)
    attempt_text = "" if tries is None or tries <= 1 else f" after {tries} attempts"
    if verdict.over_by:
        text = (f"Frame-locked segment is LONG: {counted} output frames written where "
                f"{verdict.requested} were requested{attempt_text} "
                f"(measured from the {verdict.measured_by}); -vframes should make that impossible, "
                f"so the segment is not the one that was planned.")
    else:
        text = (f"Frame-locked segment is SHORT: {counted} of {verdict.requested} requested "
                f"output frames written{attempt_text} (measured from the {verdict.measured_by}); "
                f"the planned timeline needs all {verdict.requested}, and a short segment "
                f"desynchronises every later cut.")
    return _bounded(text, budget)


def describe_truncated_plan(planned_frames: Any, available_frames: Any,
                            source_duration: Any, budget: int = 240) -> str:
    """The sentence for a source that is simply too short for the segment it was planned into.

    This one is not a frame-rounding defect and no re-seek can address it: the file does not
    contain the requested span. It is reported rather than silently shortened, because the
    frame-locked timeline has already allocated the slot and a clip that quietly under-fills it
    shifts every cut after it.
    """
    planned = _non_negative_int(planned_frames)
    available = _non_negative_int(available_frames)
    duration = _finite(source_duration)
    planned_text = "unknown" if planned is None else str(planned)
    available_text = "unknown" if available is None else str(available)
    duration_text = "unavailable" if duration is None else f"{duration:.3f}s"
    text = (f"Source is too short for its planned segment: it can supply {available_text} of the "
            f"{planned_text} output frames the frame-locked timeline allocated "
            f"(source duration {duration_text}). A short clip desynchronises every later cut, "
            f"so it is refused rather than truncated.")
    return _bounded(text, budget)


# ---------------------------------------------------------------------------
# small total helpers -- local on purpose, so this module imports no runtime
# ---------------------------------------------------------------------------

def _finite(value: Any) -> Optional[float]:
    """A finite float, or ``None``. Never raises, never substitutes a plausible default."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _non_negative_int(value: Any) -> Optional[int]:
    """A non-negative integer, or ``None``.

    Rejects ``bool`` deliberately: ``True`` is an ``int`` to Python and a requested frame count of
    ``True`` is a caller mistake, not a request for one frame. Floats are accepted only when they
    are exactly integral, so ``28.5`` is junk rather than silently 28.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    number = _finite(value)
    if number is None or number < 0.0 or number != int(number):
        return None
    return int(number)


def _bounded(text: str, limit: int = 240) -> str:
    """Truncate visibly. A diagnostic that silently loses its own tail is worse than a short one."""
    try:
        ceiling = int(limit)
    except (TypeError, ValueError):
        ceiling = 240
    if ceiling < 8:
        ceiling = 8
    flat = " ".join(str(text).split())
    if len(flat) <= ceiling:
        return flat
    return flat[: ceiling - 3] + "..."
